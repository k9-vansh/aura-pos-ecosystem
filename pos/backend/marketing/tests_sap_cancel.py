"""What SAP is told when a bill that sold or used Marketing things is cancelled.

SAP is never called: the recorder from the journey test stands in for it.
"""
import requests
from decimal import Decimal
from unittest import mock

from billing.models import Invoice
from clients.models import ClientValueCard
from marketing.tests_journey_sap import MarketingToSapJourney, drain, m
from pos_backend import sap_utils
from pos_backend.models import BackgroundJob, SapPosting


class CancelReversalTests(MarketingToSapJourney):
    def test_the_whole_month(self):          # the base class's journey is run on its own
        self.skipTest('see tests_journey_sap')

    test_the_whole_month.__test__ = False

    def setUp(self):
        super().setUp()
        self.gold = self.post('/marketing/api/memberships/', {
            'name': 'Gold', 'level': 'Organisation', 'pre_tax_price': '1000', 'post_tax_price': '1180',
            'gst_percent': '18', 'discount_percent': '10', 'expiry_days': 365})
        self.card = self.post('/marketing/api/cards/', {
            'title': 'Silver', 'level': 'Organisation', 'pre_tax_price': '2000', 'post_tax_price': '2360',
            'gst_percent': '18', 'value': '2500', 'expiry_days': 365})
        self.asha = self.customer('Asha', '9990000001')

    def cancels(self):
        return [url.rsplit('/b1s/v1/', 1)[-1] for (method, url, _) in self.sap.calls
                if method == 'POST' and url.endswith('/Cancel')]

    def posted_kinds(self):
        return [url.rsplit('/', 1)[-1] for (method, url, _) in self.sap.calls
                if method == 'POST' and not url.endswith('/Cancel')]

    def run_posting(self):
        """Work the queue until it is empty, letting the jobs that said "retry in 2 minutes" go again."""
        from django.utils import timezone
        for _ in range(8):
            drain(200)
            waiting = BackgroundJob.objects.filter(status__in=['queued', 'failed'], run_after__gt=timezone.now())
            if not waiting.exists():
                break
            waiting.update(run_after=timezone.now())

    def test_cancelling_a_membership_sale_cancels_its_journal(self):
        bill = self.sell(self.asha, [self.perk_line('membership', self.gold, 1000)], 'Cash')
        self.run_posting()
        self.assertEqual(self.cancels(), [])
        self.cancel(bill)
        self.run_posting()
        sale = SapPosting.objects.get(key__startswith='je:sale:', status=SapPosting.DONE)
        self.assertEqual(self.cancels(), [f'JournalEntries({sale.doc_entry})/Cancel'])

    def test_it_is_done_once_however_often_it_runs(self):
        bill = self.sell(self.asha, [self.perk_line('membership', self.gold, 1000)], 'Cash')
        self.run_posting()
        self.cancel(bill)
        self.run_posting()
        bill.refresh_from_db()
        for _ in range(3):
            self.assertTrue(sap_utils.create_sap_cancel_reversals(bill))
        self.assertEqual(len(self.cancels()), 1)

    def test_a_cancel_that_sap_says_is_already_done_counts_as_done(self):
        bill = self.sell(self.asha, [self.perk_line('membership', self.gold, 1000)], 'Cash')
        self.run_posting()
        self.cancel(bill)
        original = self.sap.__call__

        def already(method, url, timeout=15, **kw):
            if url.endswith('/Cancel'):
                response = mock.Mock(status_code=400, text='{"error":{"code":-5002,"message":{"value":"Journal entry is already canceled"}}}')
                raise requests.exceptions.HTTPError(response=response)
            return original(method, url, timeout, **kw)

        with mock.patch.object(sap_utils, '_send_sap_request', already):
            bill.refresh_from_db()
            self.assertTrue(sap_utils.create_sap_cancel_reversals(bill))

    def test_an_sap_outage_is_retried_not_lost(self):
        bill = self.sell(self.asha, [self.perk_line('membership', self.gold, 1000)], 'Cash')
        self.run_posting()
        self.cancel(bill)
        bill.refresh_from_db()
        down = mock.Mock(side_effect=requests.exceptions.ConnectionError('SAP is down'))
        with mock.patch.object(sap_utils, '_send_sap_request', down):
            self.assertFalse(sap_utils.create_sap_cancel_reversals(bill))
        self.assertEqual(self.cancels(), [])
        self.assertTrue(sap_utils.create_sap_cancel_reversals(bill))       # back up: now it goes
        self.assertEqual(len(self.cancels()), 1)

    def test_a_service_bill_has_its_payment_cancelled_before_the_credit_memo(self):
        bill = self.sell(self.asha, [self.line('cut')], 'Cash')
        self.run_posting()
        self.cancel(bill)
        self.run_posting()
        calls = [(method, url.rsplit('/b1s/v1/', 1)[-1]) for (method, url, _) in self.sap.calls if method == 'POST']
        names = [u for _, u in calls]
        pay = next(i for i, u in enumerate(names) if u.startswith('IncomingPayments(') and u.endswith('/Cancel'))
        memo = next(i for i, u in enumerate(names) if u == 'CreditNotes')
        self.assertLess(pay, memo, names)

    def test_cancelling_before_anything_reached_sap_still_ends_clean(self):
        """The bill is cancelled while its postings are still queued: they post, then are taken back."""
        bill = self.sell(self.asha, [self.line('cut')], 'Cash')
        self.cancel(bill)                                  # nothing has been posted yet
        self.run_posting()
        names = self.posted_kinds()
        self.assertIn('Invoices', names)
        self.assertEqual(len(self.cancels()), 1)
        self.assertIn('CreditNotes', names)
        self.assertFalse(BackgroundJob.objects.filter(
            kind='sap.sync', status__in=['queued', 'failed', 'dead']).exists())

    def test_a_card_spend_bill_gives_the_balance_back_in_pos_and_sap(self):
        self.sell(self.asha, [self.perk_line('valuecard', self.card, 2000)], 'UPI')
        cv = ClientValueCard.objects.get(client_id=self.asha['id'])
        quote = self.api.post('/billing/invoices/quote/', dict(
            self.head(self.asha), items=[self.line('cut')], value_card_id=cv.id), format='json')
        due = Decimal(quote.data['payable'])
        bill = self.pay_with(self.asha, [self.line('cut')],
                             lambda t: ([{'payment_method': 'UPI', 'amount': str(due)}] if due else []),
                             value_card_id=cv.id)
        self.run_posting()
        before = len(self.cancels())
        self.cancel(bill)
        self.run_posting()
        cv.refresh_from_db()
        self.assertEqual(m(cv.balance), Decimal('2500.00'))                 # POS gives it back...
        self.assertGreater(len(self.cancels()), before)                    # ...and SAP's spend journal is cancelled

    def test_a_draft_that_is_deleted_sends_nothing(self):
        held = self.api.post('/billing/invoices/', dict(self.head(self.asha, 'draft'), items=[self.line('cut')]), format='json')
        self.assertEqual(held.status_code, 201)
        self.run_posting()
        self.assertEqual(self.sap.posts('Invoices') + self.sap.posts('JournalEntries'), [])
        self.assertEqual(self.cancels(), [])
