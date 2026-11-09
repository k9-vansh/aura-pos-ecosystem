"""Inventory documents for a batch-managed SAP item.

Found in the 2026-10-21 check of the SAP test company: 1,924 of its 1,927
inventory items are managed by batch, SAP refuses a receipt or an issue of
such an item that names no batches, and the POS sent the wrong thing in
several places. SAP is FakeSap; no test reaches the network.
"""
import datetime
from decimal import Decimal
from unittest import mock

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from inventory.models import (Product, PurchaseOrder, PurchaseOrderItem, StockBatch, StockTransaction,
                              StockTransfer, StockTransferItem, Vendor)
from pos_backend import sap_utils
from pos_backend.management.commands.pull_from_sap import MARKETING_ITEM
from pos_backend.tests_sap_integrity import FakeResponse, SapTestBase
from salon_admin.models import Center

# SapTestBase replaces this name while a test runs; keep the real one.
REAL_GET_HANA_BATCHES = sap_utils.get_hana_batches


def at(year, month, day):
    moment = datetime.datetime(year, month, day, 12, 0)
    return timezone.make_aware(moment) if settings.USE_TZ else moment


class BatchManagedBase(SapTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.product = Product.objects.create(name='Gel', price=Decimal('200'), center=self.center, product_code='P2')
        self.vendor = Vendor.objects.create(name='Loreal', center=self.center, vendor_code='V000001')

    def batch_managed(self, flag=True):
        self.sap.entities["Items('P2')"] = {'ItemCode': 'P2', 'ManageBatchNumbers': 'tYES' if flag else 'tNO'}

    def receipt(self, batch=None, received=at(2026, 3, 10), ordered=at(2026, 1, 5)):
        po = PurchaseOrder.objects.create(vendor=self.vendor, center=self.center, status='Delivered')
        PurchaseOrder.objects.filter(pk=po.pk).update(created_at=ordered)
        PurchaseOrderItem.objects.create(purchase_order=po, product=self.product, quantity=10,
                                         received_quantity=10, rate=Decimal('80'), total_price=Decimal('800'))
        txn = StockTransaction.objects.create(product=self.product, center=self.center, transaction_type='PO_RECEIPT',
                                              quantity_change=Decimal('10'), batch=batch,
                                              notes=f'Receipt from PO-{po.id}')
        StockTransaction.objects.filter(pk=txn.pk).update(created_at=received)
        return PurchaseOrder.objects.get(pk=po.pk)

    def posted(self, fragment):
        return self.sap.posts(fragment)[0][2]


class GrpoBatchTests(BatchManagedBase):
    def test_a_batch_item_names_the_batch_the_pos_received(self):
        """New receipts carry a StockBatch; the GRPO read only the old lot, so it named none."""
        self.batch_managed()
        batch = StockBatch.objects.create(product=self.product, batch_number='LOT-7',
                                          expiry_date=datetime.date(2027, 1, 31), quantity=10)
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(self.receipt(batch)))
        line = self.posted('PurchaseDeliveryNotes')['DocumentLines'][0]
        self.assertEqual(line['BatchNumbers'], [{'BatchNumber': 'LOT-7', 'Quantity': '10', 'ExpiryDate': '2027-01-31'}])

    def test_goods_without_a_batch_number_go_in_as_a_pos_batch_dated_the_day_received(self):
        self.batch_managed()
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(self.receipt()))
        line = self.posted('PurchaseDeliveryNotes')['DocumentLines'][0]
        self.assertEqual(line['BatchNumbers'], [{'BatchNumber': 'POS-20260310', 'Quantity': '10'}])

    def test_a_plain_item_is_sent_no_batches(self):
        self.batch_managed(False)
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(self.receipt()))
        self.assertNotIn('BatchNumbers', self.posted('PurchaseDeliveryNotes')['DocumentLines'][0])

    def test_the_grpo_is_dated_the_day_it_was_received_not_the_day_the_po_was_written(self):
        self.batch_managed(False)
        self.assertTrue(sap_utils.create_sap_purchase_delivery_note(self.receipt()))
        self.assertEqual(self.posted('PurchaseDeliveryNotes')['DocDate'], '2026-03-10')
        self.assertEqual(self.posted('PurchaseOrders')['DocDate'], '2026-01-05')


class GoodsMovementBatchTests(BatchManagedBase):
    def txn(self, qty, kind='AUDIT', batch=None):
        return StockTransaction.objects.create(product=self.product, center=self.center, transaction_type=kind,
                                               quantity_change=Decimal(qty), batch=batch, notes='count')

    def test_a_goods_receipt_goes_in_as_a_batch_with_its_expiry(self):
        self.batch_managed()
        batch = StockBatch.objects.create(product=self.product, batch_number='B-9',
                                          expiry_date=datetime.date(2027, 6, 30), quantity=4)
        self.assertTrue(sap_utils.create_sap_stock_transaction(self.txn('4', batch=batch)))
        line = self.posted('InventoryGenEntries')['DocumentLines'][0]
        self.assertEqual(line['BatchNumbers'], [{'BatchNumber': 'B-9', 'Quantity': '4', 'ExpiryDate': '2027-06-30'}])

    def test_a_goods_receipt_for_a_plain_item_names_no_batch(self):
        """It used to send POS-<today> for every item and hope SAP ignored it."""
        self.batch_managed(False)
        self.assertTrue(sap_utils.create_sap_stock_transaction(self.txn('4')))
        self.assertNotIn('BatchNumbers', self.posted('InventoryGenEntries')['DocumentLines'][0])

    def test_the_quantity_can_be_written_as_json(self):
        """A Decimal quantity raised TypeError inside requests; FakeSap now serialises like requests."""
        self.batch_managed(False)
        self.assertTrue(sap_utils.create_sap_stock_transaction(self.txn('-2.5', kind='CHECKOUT')))
        self.assertEqual(self.posted('InventoryGenExits')['DocumentLines'][0]['Quantity'], '2.5')

    def test_a_goods_issue_takes_the_batches_sap_holds(self):
        self.batch_managed()
        held = [{'BatchNumber': 'B1', 'Quantity': 1.0}, {'BatchNumber': 'B2', 'Quantity': 1.0}]
        with mock.patch('pos_backend.sap_utils.get_hana_batches', return_value=held) as hana:
            self.assertTrue(sap_utils.create_sap_stock_transaction(self.txn('-2', kind='CHECKOUT')))
        self.assertEqual(self.posted('InventoryGenExits')['DocumentLines'][0]['BatchNumbers'], held)
        self.assertTrue(hana.call_args.kwargs['strict'])

    def test_a_shortfall_in_sap_says_so_instead_of_posting_part_of_it(self):
        self.batch_managed()
        txn = self.txn('-3', kind='CHECKOUT')
        with mock.patch('pos_backend.sap_utils.get_hana_batches', return_value=[{'BatchNumber': 'B1', 'Quantity': 1.0}]):
            self.assertFalse(sap_utils.create_sap_stock_transaction(txn))
        txn.refresh_from_db()
        self.assertIn('holds only 1', txn.sap_sync_message)
        self.assertEqual(self.sap.posts('InventoryGenExits'), [])

    def test_unreadable_batches_fail_with_the_reason(self):
        self.batch_managed()
        txn = self.txn('-1', kind='CHECKOUT')
        with mock.patch('pos_backend.sap_utils.get_hana_batches', side_effect=RuntimeError('hdbcli is not installed')):
            self.assertFalse(sap_utils.create_sap_stock_transaction(txn))
        txn.refresh_from_db()
        self.assertIn('hdbcli', txn.sap_sync_message)


class TransferTests(BatchManagedBase):
    def test_a_transfer_names_batches_and_is_dated_the_day_it_arrived(self):
        self.batch_managed()
        other = Center.objects.create(center_name='Gurgaon', sap_warehouse_code='WH-GGN')
        tr = StockTransfer.objects.create(from_center=self.center, to_center=other, status='Completed',
                                          received_at=at(2026, 4, 2))
        StockTransferItem.objects.create(transfer=tr, product=self.product, quantity=3)
        held = [{'BatchNumber': 'B1', 'Quantity': 3.0}]
        with mock.patch('pos_backend.sap_utils.get_hana_batches', return_value=held):
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        body = self.posted('StockTransfers')
        self.assertEqual(body['DocDate'], '2026-04-02')
        self.assertEqual(body['StockTransferLines'][0]['BatchNumbers'], held)


class HanaTests(SapTestBase):
    def test_a_hana_that_cannot_be_read_raises_in_strict_mode_and_says_why(self):
        with mock.patch.dict('sys.modules', {'hdbcli': None}):
            with self.assertRaises(RuntimeError) as caught:
                REAL_GET_HANA_BATCHES('P1', 'WH', 5, strict=True)
            self.assertIn('hdbcli', str(caught.exception))
            self.assertEqual(REAL_GET_HANA_BATCHES('P1', 'WH', 5), [])    # the old behaviour is kept


class ItemTests(SapTestBase):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.create(name='Gel Pro', price=Decimal('200'), gst_percent=Decimal('18'),
                                              center=self.center, product_code='P2')

    def refusing(self, patch_fails=False):
        """POST /Items answers 'already exists'; the PATCH then works or is refused."""
        def respond(method, url, timeout=15, **kwargs):
            if method == 'POST' and url.endswith('/Items'):
                self.sap.calls.append((method, url, kwargs.get('json')))
                resp = FakeResponse(400, {})
                resp.text = '{ "error" : { "code" : -2035, "message" : { "value" : "already exists" } } }'
                raise requests.exceptions.HTTPError(response=resp)
            if method == 'PATCH' and patch_fails:
                self.sap.calls.append((method, url, kwargs.get('json')))
                resp = FakeResponse(400, {})
                resp.text = '{ "error" : { "code" : -5002, "message" : { "value" : "price list locked" } } }'
                raise requests.exceptions.HTTPError(response=resp)
            return self.sap(method, url, timeout, **kwargs)
        return respond

    def test_a_renamed_product_reaches_sap(self):
        """The queue runs create_sap_item for every product job; its 'already exists' path left the name alone."""
        with mock.patch('pos_backend.sap_utils._send_sap_request', self.refusing()):
            self.assertTrue(sap_utils.create_sap_item(self.product))
        patch = [c for c in self.sap.calls if c[0] == 'PATCH'][0]
        self.assertEqual(patch[2]['ItemName'], 'Gel Pro')

    def test_a_refused_update_is_not_reported_as_synced(self):
        with mock.patch('pos_backend.sap_utils._send_sap_request', self.refusing(patch_fails=True)):
            self.assertFalse(sap_utils.create_sap_item(self.product))
        self.product.refresh_from_db()
        self.assertFalse(self.product.sap_sync_status)
        self.assertIn('price list locked', self.product.sap_sync_message)

    def test_a_new_item_is_batch_managed_like_the_rest_of_the_item_master(self):
        self.assertTrue(sap_utils.create_sap_item(self.product))
        self.assertEqual(self.sap.posts('/Items')[0][2]['ManageBatchNumbers'], 'tYES')


class WarehouseTests(SapTestBase):
    def test_the_first_business_place_is_assigned_too(self):
        """Business place 1 was treated as 'none', so that warehouse stayed unassigned."""
        self.center.sap_branch_name = 'Head Office'
        self.center.save()
        with mock.patch('pos_backend.sap_utils._resolve_bpl_id', return_value=1):
            self.assertTrue(sap_utils.create_sap_warehouse(self.center))
        patch = [c for c in self.sap.calls if c[0] == 'PATCH'][0]
        self.assertEqual(patch[2]['BusinessPlaceID'], 1)

    def test_a_centre_with_no_branch_name_is_left_unassigned(self):
        with mock.patch('pos_backend.sap_utils._resolve_bpl_id', return_value=1):
            self.assertTrue(sap_utils.create_sap_warehouse(self.center))
        patch = [c for c in self.sap.calls if c[0] == 'PATCH'][0]
        self.assertNotIn('BusinessPlaceID', patch[2])


class PullTests(SapTestBase):
    def test_only_pos_marketing_items_are_skipped(self):
        for code in ('VC000012', 'MB000003', 'PK000104'):
            self.assertTrue(MARKETING_ITEM.match(code), code)
        for code in ('VCO-SERUM', 'MBR001', 'PK1', 'LP000088', 'VC0000123'):
            self.assertFalse(MARKETING_ITEM.match(code), code)
