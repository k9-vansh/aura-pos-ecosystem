"""SAP posting integrity: nothing posted twice, nothing silently lost.

SAP is replaced by FakeSap, which records every request; no test reaches the
network (settings_test also forces SAP_ENABLED off and points every address at
a dead local port). Each test names the defect it pins.
"""

import json
import sys
from decimal import Decimal
from unittest import mock

import requests
from django.contrib.contenttypes.models import ContentType
from django.core.checks import run_checks
from django.test import TestCase, override_settings
from django.utils import timezone

from billing.models import AdvancePayment, Invoice, InvoiceItem, Payment
from clients.models import Client
from inventory.models import Product, PurchaseOrder, PurchaseOrderItem, StockTransaction, StockTransfer, StockTransferItem, Vendor
from pos_backend import sap_jobs, sap_ledger, sap_utils
from pos_backend.models import SapPosting, SapSyncQueue
from salon_admin.models import Center
from services.models import ServiceMaster


class FakeResponse:
    def __init__(self, status=201, data=None):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = json.dumps(self._data)
        self.content = self.text.encode()

    def json(self):
        return self._data


class FakeSap:
    """Stands in for _send_sap_request. Records calls; can lose replies."""

    def __init__(self):
        self.calls = []
        self.entry = 1000
        self.lose_reply = set()    # URL fragments whose POST reaches SAP but the reply is lost
        self.existing = {}         # URL fragment -> rows returned by GET lookups
        self.entities = {}         # URL fragment -> a single entity returned by GET

    def __call__(self, method, url, timeout=15, **kwargs):
        self.calls.append((method, url, kwargs.get('json')))
        if kwargs.get('json') is not None:
            json.dumps(kwargs['json'])   # what requests does: a Decimal here fails for real
        if method == 'GET':
            if 'BusinessPlaces' in url:
                return FakeResponse(200, {'value': []})
            if "BusinessPartners('" in url:
                return FakeResponse(200, {})
            for fragment, entity in self.entities.items():
                if fragment in url:
                    return FakeResponse(200, entity)
            for fragment, rows in self.existing.items():
                if fragment in url:
                    return FakeResponse(200, {'value': rows})
            return FakeResponse(200, {'value': []})
        self.entry += 1
        for fragment in self.lose_reply:
            if fragment in url:
                raise requests.exceptions.ReadTimeout('reply lost')
        return FakeResponse(201, {'DocEntry': self.entry, 'JdtNum': self.entry})

    def posts(self, fragment):
        return [c for c in self.calls if c[0] == 'POST' and fragment in c[1]]


@override_settings(SAP_ENABLED=True)
class SapTestBase(TestCase):
    def setUp(self):
        self.sap = FakeSap()
        patches = [
            mock.patch('pos_backend.sap_utils._send_sap_request', self.sap),
            mock.patch('pos_backend.sap_utils.get_hana_batches', return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.center = Center.objects.create(center_name='Delhi Main', gst_number='07AAAAA0000A1Z5',
                                            sap_warehouse_code='WH-DELHI')
        self.client_b2c = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)

    def invoice(self, **kw):
        defaults = dict(client=self.client_b2c, center=self.center, status='paid',
                        subtotal=Decimal('1000'), total_amount=Decimal('1000'),
                        paid_amount=Decimal('1000'), sap_doc_entry=500, invoice_number=None)
        defaults.update(kw)
        return Invoice.objects.create(**defaults)


# ─── Building blocks ──────────────────────────────────────────────────────

class GstSplitTests(TestCase):
    def test_halves_add_back_to_the_total(self):
        """Rs 5.05 used to become 2.53 + 2.53 = 5.06, an unbalanced journal."""
        for gst in ['5.05', '0.01', '18.37', '0', '1000.00', '2.25']:
            cgst, sgst = sap_utils._split_gst(Decimal(gst))
            self.assertAlmostEqual(cgst + sgst, float(gst), places=2)
            self.assertLessEqual(abs(cgst - sgst), 0.011)


class PostOnceTests(TestCase):
    def test_a_recorded_posting_is_never_sent_again(self):
        post = mock.Mock(return_value=42)
        self.assertEqual(sap_ledger.post_once('k:1', post), '42')
        self.assertEqual(sap_ledger.post_once('k:1', post), '42')
        post.assert_called_once()

    def test_a_retry_asks_sap_before_posting_again(self):
        """A lost reply must be recovered, not posted a second time."""
        def lost():
            raise requests.exceptions.ReadTimeout('reply lost')
        with self.assertRaises(requests.exceptions.ReadTimeout):
            sap_ledger.post_once('k:2', lost)
        post, find = mock.Mock(return_value=99), mock.Mock(return_value=7)
        self.assertEqual(sap_ledger.post_once('k:2', post, find), '7')
        post.assert_not_called()

    def test_first_attempt_does_not_spend_a_lookup(self):
        find = mock.Mock(return_value=None)
        sap_ledger.post_once('k:3', lambda: 5, find)
        find.assert_not_called()

    def test_a_posting_in_progress_is_not_started_twice(self):
        SapPosting.objects.create(key='k:4', locked_until=timezone.now() + timezone.timedelta(minutes=2))
        with self.assertRaises(sap_ledger.PostingBusy):
            sap_ledger.post_once('k:4', lambda: 1)

    def test_only_one_process_holds_the_worker_lease(self):
        self.assertTrue(sap_ledger.hold_worker_lease('a'))
        self.assertFalse(sap_ledger.hold_worker_lease('b'))
        self.assertTrue(sap_ledger.hold_worker_lease('a'))  # renewal


# ─── The retry queue ──────────────────────────────────────────────────────

class QueueTests(SapTestBase):
    def test_journal_functions_get_real_queue_types(self):
        """They were queued as 'unknown' and the worker deleted them unposted."""
        inv = self.invoice()
        item = InvoiceItem.objects.create(invoice=inv, description='Card', total_price=Decimal('1000'))
        adv = AdvancePayment.objects.create(client=self.client_b2c, amount=Decimal('100'))
        self.assertEqual(sap_jobs.queue_object(sap_utils.create_sap_valuecard_je, (inv, item)), ('marketing_je', item.id))
        self.assertEqual(sap_jobs.queue_object(sap_utils.create_sap_package_je, (inv, item)), ('marketing_je', item.id))
        self.assertEqual(sap_jobs.queue_object(sap_utils.create_sap_advance_je, (adv,)), ('advance_je', adv.id))
        self.assertEqual(sap_jobs.queue_object(sap_utils.create_sap_invoice, (inv,)), ('invoice', inv.id))

    @override_settings(SAP_ENABLED=False)
    def test_with_sap_off_a_sync_is_queued_not_lost(self):
        from pos_backend.models import BackgroundJob
        inv = self.invoice()
        with self.captureOnCommitCallbacks(execute=True):
            sap_utils.run_sap_sync_async(sap_utils.create_sap_invoice, inv)
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:invoice:{inv.id}').exists())
        self.assertEqual(self.sap.calls, [])

    def test_a_row_is_worked_once_even_if_a_second_worker_runs(self):
        row = SapSyncQueue.objects.create(object_type='invoice', object_id=1)
        runs = []

        def run_job(object_type, object_id):
            runs.append(object_id)
            # A second worker starting while this one holds the row:
            sap_jobs.process_due()
            return 'failed', 'still failing'

        with mock.patch('pos_backend.sap_jobs.run_job', side_effect=run_job):
            sap_jobs.process_due()
        self.assertEqual(runs, [1])
        row.refresh_from_db()
        self.assertEqual(row.retry_count, 1)
        self.assertGreater(row.next_retry_at, timezone.now())

    def test_a_queued_journal_is_retried_not_dropped(self):
        inv = self.invoice()
        ct = ContentType.objects.get(app_label='marketing', model='valuecard')
        item = InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=1, description='Gold card',
                                          total_price=Decimal('1000'), tax_amount=Decimal('50'), tax_percentage=Decimal('5'))
        SapSyncQueue.objects.create(object_type='marketing_je', object_id=item.id)
        counts = sap_jobs.process_due()
        self.assertEqual(counts['done'], 1)
        self.assertEqual(len(self.sap.posts('JournalEntries')), 1)
        self.assertFalse(SapSyncQueue.objects.exists())


# ─── Documents ────────────────────────────────────────────────────────────

class GrpoTests(SapTestBase):
    def setUp(self):
        super().setUp()
        self.vendor = Vendor.objects.create(name='Loreal', center=self.center, vendor_code='V000001')
        self.product = Product.objects.create(name='Shampoo', price=Decimal('500'), center=self.center, product_code='P1')
        self.po = PurchaseOrder.objects.create(vendor=self.vendor, center=self.center, status='Delivered')
        PurchaseOrderItem.objects.create(purchase_order=self.po, product=self.product, quantity=10,
                                         received_quantity=10, rate=Decimal('100'), total_price=Decimal('1000'))
        StockTransaction.objects.create(product=self.product, center=self.center, transaction_type='PO_RECEIPT',
                                        quantity_change=10, notes=f'Receipt from PO-{self.po.id}')

    def test_a_grpo_is_posted_once_and_reported_as_success(self):
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(self.po))
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(PurchaseOrder.objects.get(pk=self.po.pk)))
        self.assertEqual(len(self.sap.posts('PurchaseDeliveryNotes')), 1)
        self.assertEqual(len(self.sap.posts('PurchaseOrders')), 1)
        self.assertFalse(StockTransaction.objects.filter(sap_sync_status=False).exists())

    def test_a_po_that_has_its_grpo_is_not_posted_again_by_the_fallback(self):
        """The fallback re-posted the whole PO on every retry: double stock."""
        StockTransaction.objects.update(sap_sync_status=True)
        PurchaseOrder.objects.filter(pk=self.po.pk).update(sap_doc_entry=321, sap_po_doc_entry=320)
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(PurchaseOrder.objects.get(pk=self.po.pk)))
        self.assertEqual(self.sap.posts('PurchaseDeliveryNotes'), [])

    def test_a_lost_reply_is_recovered_from_sap(self):
        self.sap.lose_reply.add('PurchaseDeliveryNotes')
        self.assertFalse(sap_utils.create_sap_purchase_delivery_note(self.po))
        self.sap.lose_reply.clear()
        self.sap.existing['PurchaseDeliveryNotes'] = [{'DocEntry': 777}]
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(PurchaseOrder.objects.get(pk=self.po.pk)))
        self.assertEqual(len(self.sap.posts('PurchaseDeliveryNotes')), 1)  # only the lost one
        self.assertEqual(PurchaseOrder.objects.get(pk=self.po.pk).sap_doc_entry, 777)


class StockMovementTests(SapTestBase):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.create(name='Gel', price=Decimal('200'), center=self.center, product_code='P2')

    def test_sales_and_refunds_are_not_posted_as_goods_movements(self):
        """The credit memo returns refunded stock; a Goods Receipt returned it again."""
        for kind, qty in (('SALE', -1), ('REFUND', 1)):
            txn = StockTransaction.objects.create(product=self.product, center=self.center,
                                                  transaction_type=kind, quantity_change=qty, notes='x')
            self.assertTrue(sap_utils.create_sap_stock_transaction(txn))
            txn.refresh_from_db()
            self.assertTrue(txn.sap_sync_status)
        self.assertEqual([c for c in self.sap.calls if c[0] == 'POST'], [])

    def test_a_checkout_is_posted_once(self):
        txn = StockTransaction.objects.create(product=self.product, center=self.center,
                                              transaction_type='CHECKOUT', quantity_change=-2, notes='salon use')
        self.assertTrue(sap_utils.create_sap_stock_transaction(txn))
        self.assertTrue(sap_utils.create_sap_stock_transaction(StockTransaction.objects.get(pk=txn.pk)))
        self.assertEqual(len(self.sap.posts('InventoryGenExits')), 1)

    def test_a_transfer_is_posted_once(self):
        other = Center.objects.create(center_name='Gurgaon', sap_warehouse_code='WH-GGN')
        tr =StockTransfer.objects.create(from_center=self.center, to_center=other, status='Completed')
        StockTransferItem.objects.create(transfer=tr, product=self.product, quantity=3)
        self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertTrue(sap_utils.create_sap_stock_transfer(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(len(self.sap.posts('StockTransfers')), 1)


class CreditNoteTests(SapTestBase):
    def test_credit_note_uses_the_invoice_customer_and_tax_codes(self):
        """C<id> for a B2C client and codes like CGST@2.5 were rejected by SAP."""
        svc = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('1000'), tax_percentage=Decimal('18'))
        inv = self.invoice(status='refunded')
        InvoiceItem.objects.create(invoice=inv, content_type=ContentType.objects.get_for_model(ServiceMaster),
                                   object_id=svc.id, description='Haircut', quantity=1,
                                   total_price=Decimal('1000'), tax_percentage=Decimal('18'))
        self.assertTrue(sap_utils.create_sap_credit_note(inv))
        self.assertTrue(sap_utils.create_sap_credit_note(Invoice.objects.get(pk=inv.pk)))
        posts = self.sap.posts('CreditNotes')
        self.assertEqual(len(posts), 1)
        payload = posts[0][2]
        self.assertEqual(payload['CardCode'], 'B2C')
        self.assertEqual(payload['DocumentLines'][0]['TaxCode'], 'CG+SG@18')
        self.assertNotIn('VatGroup', payload['DocumentLines'][0])


class JournalTests(SapTestBase):
    def _balanced(self, payload):
        lines = payload['JournalEntryLines']
        return round(sum(l['Debit'] for l in lines), 2) == round(sum(l['Credit'] for l in lines), 2)

    def test_advance_journal_balances_with_odd_paise_and_posts_once(self):
        adv = AdvancePayment.objects.create(client=self.client_b2c, amount=Decimal('101.00'),
                                            gst_amount=Decimal('5.05'), center=self.center)
        self.assertTrue(sap_utils.create_sap_advance_je(adv))
        self.assertTrue(sap_utils.create_sap_advance_je(AdvancePayment.objects.get(pk=adv.pk)))
        posts = self.sap.posts('JournalEntries')
        self.assertEqual(len(posts), 1)
        self.assertTrue(self._balanced(posts[0][2]))

    def test_sale_journal_balances_and_posts_once_per_line(self):
        inv = self.invoice()
        ct = ContentType.objects.get(app_label='marketing', model='membership')
        a = InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=1, description='Gold',
                                       total_price=Decimal('1000'), tax_amount=Decimal('180.05'), tax_percentage=Decimal('18'))
        b = InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=1, description='Gold',
                                       total_price=Decimal('1000'), tax_amount=Decimal('180.05'), tax_percentage=Decimal('18'))
        for item in (a, b, a):
            self.assertTrue(sap_utils.create_sap_membership_je(inv, item))
        posts = self.sap.posts('JournalEntries')
        self.assertEqual(len(posts), 2)  # one per sold line, the repeat of `a` skipped
        self.assertTrue(all(self._balanced(p[2]) for p in posts))


class IncomingPaymentTests(SapTestBase):
    def _new_invoice(self, **kw):
        inv = self.invoice(**kw)
        sap_ledger.mark_done(f'invoice:{inv.id}', inv.sap_doc_entry)  # posted under the new rules
        return inv

    def test_a_later_part_payment_reaches_sap_and_nothing_twice(self):
        """Payments added after checkout never reached SAP."""
        inv = self._new_invoice(status='partial', paid_amount=Decimal('400'))
        Payment.objects.create(invoice=inv, amount=Decimal('400'), payment_method='Cash')
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        Payment.objects.create(invoice=inv, amount=Decimal('600'), payment_method='UPI')
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        posts = self.sap.posts('IncomingPayments')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2].get('CashSum'), 400.0)
        self.assertNotIn('TransferSum', posts[0][2])
        self.assertEqual(posts[1][2].get('TransferSum'), 600.0)
        self.assertNotIn('CashSum', posts[1][2])

    def test_first_posting_amounts_are_unchanged(self):
        inv = self._new_invoice()
        Payment.objects.create(invoice=inv, amount=Decimal('700'), payment_method='Cash')
        Payment.objects.create(invoice=inv, amount=Decimal('300'), payment_method='UPI')
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        (post,) = self.sap.posts('IncomingPayments')
        body = post[2]
        self.assertEqual((body['CashSum'], body['TransferSum']), (700.0, 300.0))
        self.assertEqual(body['PaymentInvoices'][0]['SumApplied'], '1000.0')

    def test_cashback_settlement_is_posted_once_and_failure_is_retried(self):
        inv = self._new_invoice()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cashback Wallet')
        self.sap.lose_reply.add('IncomingPayments')
        self.assertFalse(sap_utils.create_sap_incoming_payment(inv))  # used to report success
        self.sap.lose_reply.clear()
        self.sap.existing['IncomingPayments'] = [{'DocEntry': 55}]
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(len(self.sap.posts('IncomingPayments')), 1)

    def test_an_advance_is_spent_by_journal_posted_once_and_a_failure_is_retried(self):
        """The SAP sheet's JE (Advance Adjustment): no incoming payment for an advance."""
        inv = self._new_invoice()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Advance')
        with mock.patch.object(sap_utils, '_post_je_once', return_value=None):
            self.assertFalse(sap_utils.create_sap_incoming_payment(inv))  # SAP refused: retried later
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(self.sap.posts('IncomingPayments'), [])
        self.assertEqual(len(self.sap.posts('JournalEntries')), 1)

    def test_an_advance_already_settled_the_old_way_is_not_journalled_again(self):
        from pos_backend import sap_ledger
        inv = self._new_invoice()
        p = Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Advance')
        sap_ledger.mark_done(f"inpay:{inv.id}:virt:{p.id}", '55')
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(self.sap.posts('JournalEntries'), [])

    def test_an_invoice_synced_before_this_change_is_not_paid_again(self):
        inv = self.invoice()  # no posting record: synced by the old code
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        self.sap.entities['Invoices(500)'] = {'PaidToDate': 1000.0}
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(self.sap.posts('IncomingPayments'), [])

    def test_an_old_invoice_with_nothing_paid_in_sap_is_posted(self):
        """A payment that really failed under the old code must not be dropped."""
        inv = self.invoice()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        self.sap.entities['Invoices(500)'] = {'PaidToDate': 0}
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(len(self.sap.posts('IncomingPayments')), 1)

    def test_an_old_card_only_invoice_does_not_repeat_its_journal(self):
        inv = self.invoice()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Value Card')
        self.sap.entities['Invoices(500)'] = {'PaidToDate': 0}
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        self.assertEqual(self.sap.posts('JournalEntries'), [])


class InvoiceTests(SapTestBase):
    def test_an_invoice_is_not_posted_when_sap_cannot_be_asked_first(self):
        """The duplicate check failing used to fall through to posting."""
        inv = self.invoice(sap_doc_entry=None)

        def broken_get(method, url, timeout=15, **kw):
            if method == 'GET' and 'Invoices?' in url:
                raise requests.exceptions.ConnectionError('down')
            return self.sap(method, url, timeout, **kw)

        with mock.patch('pos_backend.sap_utils._send_sap_request', broken_get):
            self.assertFalse(sap_utils.create_sap_invoice(inv))
        self.assertEqual(self.sap.posts('Invoices'), [])


# ─── Switches and startup ─────────────────────────────────────────────────

class SwitchTests(TestCase):
    @override_settings(SAP_ENABLED=False)
    def test_sap_off_sends_nothing(self):
        with self.assertRaises(sap_utils.SapDisabled):
            sap_utils._send_sap_request('GET', 'http://127.0.0.1:9/b1s/v1/Items')
        self.assertEqual(sap_utils.get_hana_batches('P1', 'WH', 5), [])
        self.assertEqual(sap_utils.get_sap_session(), (None, None))

    @override_settings(SAP_CONFIGURED=False, SAP_ENABLED=False)
    def test_missing_sap_settings_are_reported(self):
        ids = {w.id for w in run_checks(tags=['deployment'])}
        self.assertIn('deployment.W009', ids)

    def test_the_worker_only_starts_in_a_server_process(self):
        from pos_backend import scheduler
        cases = {
            ('manage.py', 'test'): False,
            ('manage.py', 'migrate'): False,
            ('manage.py', 'runserver'): False,           # autoreloader parent
            ('manage.py', 'runserver', '--noreload'): True,
            ('daphne', 'pos_backend.asgi:application'): True,
        }
        for argv, expected in cases.items():
            with mock.patch.object(sys, 'argv', list(argv)), mock.patch.dict('os.environ', {'RUN_MAIN': ''}):
                self.assertEqual(scheduler._is_server_process(), expected, argv)

    def test_the_worker_does_not_start_in_a_management_command(self):
        """A test run, migrate or shell must never start posting."""
        from pos_backend import scheduler
        with mock.patch('threading.Thread') as thread, \
                mock.patch.object(sys, 'argv', ['manage.py', 'test']):
            scheduler.start_scheduler()
        thread.assert_not_called()

    @override_settings(SAP_SCHEDULER=False)
    def test_the_worker_does_not_start_when_it_runs_as_a_service(self):
        from pos_backend import scheduler
        with mock.patch('threading.Thread') as thread, \
                mock.patch.object(sys, 'argv', ['daphne', 'pos_backend.asgi:application']):
            scheduler.start_scheduler()
        thread.assert_not_called()
