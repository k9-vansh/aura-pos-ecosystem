"""What SAP is told when a bill is paid with a brand's coupon (Gift Card).

The A/R invoice is the whole bill. The coupon's part is an incoming payment to
the Gift Card account (what the brand owes the business), carrying the code so
the brand's statement can be matched; the rest goes where it always went. SAP is
never called: the recorder from the journey test stands in for it.
"""
from decimal import Decimal

from django.utils import timezone

from billing.models import Invoice
from marketing import coupons
from marketing.models import CouponCode
from marketing.tests_journey_sap import MarketingToSapJourney, day, drain
from pos_backend import app_settings
from pos_backend.models import AppSetting, BackgroundJob


class CouponToSapTests(MarketingToSapJourney):
    def test_the_whole_month(self):          # the base class's journey is run on its own
        self.skipTest('see tests_journey_sap')

    test_the_whole_month.__test__ = False

    def setUp(self):
        super().setUp()
        self.asha = self.customer('Asha', '9990000001')
        self.promo = self.post('/marketing/api/promotions/', {
            'name': 'Blinkit 500', 'promo_type': 'Coupon', 'level': 'Organisation',
            'start_date': day(), 'end_date': day(30),
            'config': {'brand': 'Blinkit', 'coupon_value_type': 'Flat', 'coupon_value': 500},
            'code_source': 'list', 'codes_text': 'BLK-SAP-0001\nBLK-SAP-0002'})

    def map_account(self, account):
        AppSetting.objects.update_or_create(
            key=app_settings.payment_account_key('Gift Card'), scope='global', defaults={'value': account})
        app_settings.invalidate()

    def run_posting(self):
        for _ in range(8):
            drain(200)
            waiting = BackgroundJob.objects.filter(status__in=['queued', 'failed'], run_after__gt=timezone.now())
            if not waiting.exists():
                break
            waiting.update(run_after=timezone.now())

    def coupon_bill(self, code='BLK-SAP-0001', gift='500'):
        return self.pay_with(
            self.asha, [self.line('cut')],
            lambda total: [{'payment_method': 'Gift Card', 'amount': gift, 'reference': code},
                           {'payment_method': 'UPI', 'amount': str(total - Decimal(gift)), 'reference': 'UTR1'}])

    def payments(self):
        return [payload for _, _, payload in self.sap.posts('IncomingPayments')
                if payload and 'PaymentInvoices' in payload]

    def test_the_invoice_is_the_whole_bill_and_the_coupon_goes_to_its_own_account(self):
        self.map_account('BRANDREC01')
        bill = self.coupon_bill()
        self.assertEqual(bill.total_amount, Decimal('1180.00'))
        self.run_posting()
        invoice = self.sap.posts('Invoices')[0][2]
        net = sum(float(l['UnitPrice']) * float(l['Quantity']) for l in invoice['DocumentLines'])
        self.assertAlmostEqual(net, 1000.0, places=2)           # the whole bill, not 1000 less the coupon
        by_account = {p['TransferAccount']: p for p in self.payments()}
        self.assertIn('BRANDREC01', by_account, list(by_account))
        gift = by_account['BRANDREC01']
        self.assertEqual(gift['TransferSum'], 500.0)
        self.assertIn('BLK-SAP-0001', gift['TransferReference'])
        self.assertEqual(gift['PaymentInvoices'][0]['SumApplied'], '500.0')
        upi = next(p for account, p in by_account.items() if account != 'BRANDREC01')
        self.assertEqual(upi['TransferSum'], 680.0)

    def test_without_a_mapped_account_it_goes_where_other_online_money_goes(self):
        self.coupon_bill()
        self.run_posting()
        self.assertTrue(self.payments())
        self.assertEqual(sum(p['TransferSum'] for p in self.payments()), 1180.0)

    def test_a_coupon_is_never_posted_as_an_advance_or_card_adjustment(self):
        self.map_account('BRANDREC01')
        self.coupon_bill()
        self.run_posting()
        for _, _, payload in self.sap.posts('JournalEntries'):
            self.assertNotIn('Advance Adjustment', (payload or {}).get('Memo', ''))

    def test_cancelling_takes_the_coupon_payment_back_in_sap_and_frees_the_code(self):
        self.map_account('BRANDREC01')
        bill = self.coupon_bill()
        self.run_posting()
        self.cancel(bill)
        self.run_posting()
        cancels = [url for (method, url, _) in self.sap.calls if method == 'POST' and url.endswith('/Cancel')]
        self.assertEqual(len([u for u in cancels if 'IncomingPayments(' in u]), 2, cancels)
        self.assertTrue([1 for (m_, url, _) in self.sap.calls if m_ == 'POST' and url.endswith('CreditNotes')])
        code = CouponCode.objects.get(code_key='BLKSAP0001')
        self.assertFalse(code.is_used)
        self.assertEqual(code.times_released, 1)

    def test_the_second_bill_with_a_used_code_posts_nothing(self):
        self.map_account('BRANDREC01')
        self.coupon_bill()
        self.run_posting()
        before = len(self.sap.posts('Invoices'))
        r = self.api.post('/billing/invoices/', dict(
            self.head(self.asha), items=[self.line('cut')],
            payments=[{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'BLK-SAP-0001'},
                      {'payment_method': 'UPI', 'amount': '680'}]), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.run_posting()
        self.assertEqual(len(self.sap.posts('Invoices')), before)
        self.assertEqual(Invoice.objects.filter(status='paid').count(), 1)

    def test_a_coupon_that_covers_the_bill_posts_one_payment(self):
        self.map_account('BRANDREC01')
        coupons.import_codes(CouponCode.objects.get(code_key='BLKSAP0001').promotion, 'BIG-0001,5000')
        bill = self.pay_with(self.asha, [self.line('cut')],
                             lambda total: [{'payment_method': 'Gift Card', 'amount': str(total), 'reference': 'BIG-0001'}])
        self.assertEqual(bill.paid_amount, Decimal('1180.00'))
        self.run_posting()
        pays = self.payments()
        self.assertEqual(len(pays), 1)
        self.assertEqual((pays[0]['TransferAccount'], pays[0]['TransferSum']), ('BRANDREC01', 1180.0))
