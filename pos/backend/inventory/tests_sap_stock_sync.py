"""Stock changed in SAP reaches the POS; the POS's own unposted movements do not look like SAP changes."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from inventory.models import Product, StockTransaction
from inventory.sap_stock_sync import sync_stock_from_sap
from salon_admin.models import Center


def sap_rows(*rows):
    """What SAP's Items list returns for [(item, warehouse, stock)]."""
    return [{'ItemCode': code, 'InventoryItem': 'tYES',
             'ItemWarehouseInfoCollection': [{'WarehouseCode': wh, 'InStock': stock}]}
            for code, wh, stock in rows]


class SapStockSync(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='Delhi', display_name='Delhi', sap_warehouse_code='WH-D')
        self.product = Product.objects.create(name='Gel', product_code='GEL', price=Decimal('10'), mrp=Decimal('10'),
                                              gst_percent=Decimal('18'), current_stock=100, center=self.centre)
        self.later = timezone.now() + timedelta(minutes=10)

    def run_sync(self, *rows):
        with mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate',
                        return_value=iter(sap_rows(*rows))):
            return sync_stock_from_sap(now=self.later)

    def stock(self):
        self.product.refresh_from_db()
        return self.product.current_stock

    def test_stock_added_in_sap_reaches_the_pos(self):
        self.assertEqual(self.run_sync(('GEL', 'WH-D', 130)), 1)
        self.assertEqual(self.stock(), 130)
        row = StockTransaction.objects.get(product=self.product, transaction_type='SAP_SYNC')
        self.assertEqual(row.quantity_change, 30)
        self.assertTrue(row.sap_sync_status)        # never posted back to SAP

    def test_stock_removed_in_sap_reaches_the_pos(self):
        self.assertEqual(self.run_sync(('GEL', 'WH-D', 60)), 1)
        self.assertEqual(self.stock(), 60)

    def test_no_difference_changes_nothing(self):
        self.assertEqual(self.run_sync(('GEL', 'WH-D', 100)), 0)
        self.assertFalse(StockTransaction.objects.filter(transaction_type='SAP_SYNC').exists())

    def test_a_sale_not_yet_in_sap_is_not_undone(self):
        # The POS sold 5 (stock 100) and SAP has not been told yet: it still says 105.
        self.product.current_stock = 100
        self.product.save()
        StockTransaction.objects.create(product=self.product, center=self.centre, transaction_type='SALE',
                                        quantity_change=-5, sap_sync_status=False)
        self.assertEqual(self.run_sync(('GEL', 'WH-D', 105)), 0)
        self.assertEqual(self.stock(), 100)

    def test_a_movement_from_the_last_minutes_is_left_alone(self):
        StockTransaction.objects.create(product=self.product, center=self.centre, transaction_type='SALE',
                                        quantity_change=-5, sap_sync_status=True)
        self.later = timezone.now()                 # the sale is seconds old
        self.assertEqual(self.run_sync(('GEL', 'WH-D', 130)), 0)
        self.assertEqual(self.stock(), 100)

    def test_unknown_warehouse_and_item_are_ignored(self):
        self.assertEqual(self.run_sync(('GEL', 'WH-X', 5), ('NOPE', 'WH-D', 5)), 0)
        self.assertEqual(self.stock(), 100)
