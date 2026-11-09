"""An Inventory Transfer is posted to SAP only for Inventory Items (OITM.InvntItem = Y).

The SAP team's rule: items that are not Inventory Items have no stock in SAP, so a transfer for them is not
posted. The POS still moves their stock between branches; only their lines are left out of the SAP document.
SAP is FakeSap (Service Layer) or a mocked HANA; no test reaches the network.
"""
from decimal import Decimal
from unittest import mock

from inventory.models import Product, StockTransfer, StockTransferItem
from pos_backend import hana_reads, sap_utils
from pos_backend.tests_sap_integrity import SapTestBase
from salon_admin.models import Center



def hana_flags(*pairs):
    """A HANA that answers the Inventory Item question with these (code, Y/N) pairs, and knows nothing else
    (its other question, which items are managed by batch, then falls to the Service Layer as before)."""
    def rows(sql, params=()):
        return list(pairs) if 'InvntItem' in sql else []
    return mock.patch.object(hana_reads, 'rows', side_effect=rows)


FLAGS_URL = '$select=ItemCode,InventoryItem'


class TransferStockItemTests(SapTestBase):
    def setUp(self):
        super().setUp()
        self.other = Center.objects.create(center_name='Gurgaon', sap_warehouse_code='WH-GGN')
        self.yes = Product.objects.create(name='Gel', product_code='P1', price=Decimal('10'), center=self.center)
        self.no = Product.objects.create(name='Appointment card', product_code='P2', price=Decimal('1'), center=self.center)

    def transfer(self, *products):
        tr = StockTransfer.objects.create(from_center=self.center, to_center=self.other, status='Completed')
        for product in products:
            StockTransferItem.objects.create(transfer=tr, product=product, quantity=3)
        return tr

    def lines(self):
        (post,) = self.sap.posts('StockTransfers')
        return [line['ItemCode'] for line in post[2]['StockTransferLines']]

    def test_only_inventory_items_are_sent_when_hana_answers(self):
        tr = self.transfer(self.yes, self.no)
        with hana_flags(('P1', 'Y'), ('P2', 'N')):
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(self.lines(), ['P1'])
        self.assertFalse([c for c in self.sap.calls if FLAGS_URL in c[1]])        # HANA was enough
        tr.refresh_from_db()
        self.assertTrue(tr.sap_sync_status)
        self.assertIn('P2', tr.sap_sync_message)
        self.assertTrue(tr.sap_sync_message.startswith('Success'))

    def test_the_service_layer_answers_when_hana_cannot(self):
        self.sap.existing[FLAGS_URL] = [{'ItemCode': 'P1', 'InventoryItem': 'tYES'},
                                        {'ItemCode': 'P2', 'InventoryItem': 'tNO'}]
        tr = self.transfer(self.yes, self.no)
        with mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')):
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(self.lines(), ['P1'])

    def test_a_transfer_of_only_inventory_items_goes_whole_and_says_plain_success(self):
        tr = self.transfer(self.yes)
        with hana_flags(('P1', 'Y')):
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(self.lines(), ['P1'])
        tr.refresh_from_db()
        self.assertEqual(tr.sap_sync_message, 'Success')

    def test_a_transfer_of_no_inventory_item_posts_nothing_and_is_not_left_unsent(self):
        tr = self.transfer(self.no)
        with hana_flags(('P2', 'N')):
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(self.sap.posts('StockTransfers'), [])
        tr.refresh_from_db()
        self.assertTrue(tr.sap_sync_status)                    # the retry sweep must not pick it up again
        self.assertIn('P2', tr.sap_sync_message)

    def test_an_item_sap_does_not_know_stays_in_and_sap_answers_for_it(self):
        tr = self.transfer(self.yes, self.no)
        with hana_flags(('P2', 'N')):          # P1 not found
            self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(self.lines(), ['P1'])

    def test_a_sap_that_cannot_be_asked_holds_the_transfer_back_instead_of_guessing(self):
        tr = self.transfer(self.yes, self.no)
        with mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=ConnectionError('SAP is down')):
            self.assertFalse(sap_utils.create_sap_stock_transfer(tr))
        tr.refresh_from_db()
        self.assertFalse(tr.sap_sync_status)
        self.assertIn('Inventory Items'.lower(), tr.sap_sync_message.lower())
        self.assertEqual(self.sap.posts('StockTransfers'), [])

    # The same rule for a checkout / stock count (a Goods Issue or Goods Receipt moves SAP stock too).
    def movement(self, product, change='-2'):
        from inventory.models import StockTransaction
        return StockTransaction.objects.create(product=product, center=self.center, transaction_type='CHECKOUT',
                                               quantity_change=Decimal(change), notes='Checked out for salon use')

    def test_a_checkout_of_an_item_that_is_not_an_inventory_item_posts_nothing(self):
        txn = self.movement(self.no)
        with hana_flags(('P2', 'N')):
            self.assertTrue(sap_utils.create_sap_stock_transaction(txn))
        self.assertEqual(self.sap.posts('InventoryGenExits'), [])
        txn.refresh_from_db()
        self.assertTrue(txn.sap_sync_status)                 # and it is not queued again
        self.assertIn('Inventory Item', txn.sap_sync_message)

    def test_a_checkout_of_an_inventory_item_is_posted_as_before(self):
        txn = self.movement(self.yes)
        with hana_flags(('P1', 'Y')):
            self.assertTrue(sap_utils.create_sap_stock_transaction(txn))
        (post,) = self.sap.posts('InventoryGenExits')
        self.assertEqual([line['ItemCode'] for line in post[2]['DocumentLines']], ['P1'])

    def test_a_goods_receipt_for_a_purchase_order_is_not_filtered_by_this_rule(self):
        """A purchase of a non-stock item is a real purchase: the rule is for moving stock only."""
        from inventory.models import PurchaseOrder, PurchaseOrderItem, Vendor
        vendor = Vendor.objects.create(name='V', center=self.center, vendor_code='V000001')
        po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Delivered')
        PurchaseOrderItem.objects.create(purchase_order=po, product=self.no, quantity=2, received_quantity=2,
                                         rate=Decimal('5'), total_price=Decimal('10'))
        def rows(sql, params=()):
            if 'InvntItem' in sql:                       # the Inventory Item question: must not be asked for a GRPO
                raise AssertionError('the rule was applied to a GRPO')
            return []
        with mock.patch.object(hana_reads, 'rows', side_effect=rows):
            sap_utils.create_sap_purchase_delivery_note(po)
        self.assertEqual([line['ItemCode'] for line in self.sap.posts('PurchaseDeliveryNotes')[0][2]['DocumentLines']],
                         ['P2'])

    def test_a_retry_of_a_posted_transfer_asks_nothing(self):
        tr = self.transfer(self.yes)
        StockTransfer.objects.filter(pk=tr.pk).update(sap_doc_entry=77)
        with mock.patch.object(hana_reads, 'rows', side_effect=AssertionError('asked')):
            self.assertTrue(sap_utils.create_sap_stock_transfer(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(self.sap.posts('StockTransfers'), [])
