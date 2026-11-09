"""SAP's item master is read straight from HANA, in the shape the Service Layer gives it, and only read.

HANA is never touched here: its answers are mocked. The point is that the three background jobs (stock,
unit, master) get the same item dicts from either source, ask HANA first, fall back to the Service Layer
when HANA cannot answer, and that nothing but a SELECT can ever be sent.
"""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from inventory import sap_items
from inventory.models import Product, StockTransaction
from inventory.sap_stock_sync import sync_stock_from_sap
from inventory.sap_unit_sync import sync_units_from_sap
from pos_backend import hana_reads
from salon_admin.models import Center


class OnlySelect(SimpleTestCase):
    def test_anything_but_a_select_is_refused_before_a_connection_is_made(self):
        with mock.patch.object(hana_reads, '_connect', side_effect=AssertionError('connected')):
            for sql in ('UPDATE "OITM" SET "ItemName" = \'x\'', 'DELETE FROM "OITW"', 'DROP TABLE "OITM"',
                        'INSERT INTO "OITM" VALUES (1)', 'CALL x()'):
                with self.assertRaises(ValueError):
                    hana_reads.rows(sql)
                with self.assertRaises(ValueError):
                    hana_reads.many([('SELECT 1 FROM DUMMY', ()), (sql, ())])

    def test_it_is_unavailable_while_sap_is_off(self):
        self.assertFalse(hana_reads.available())
        with self.assertRaises(hana_reads.HanaUnavailable):
            hana_reads.rows('SELECT 1 FROM DUMMY')

    @override_settings(SAP_ENABLED=True, HANA_ADDRESS='h', HANA_USER='u')
    def test_a_driver_or_host_problem_becomes_unavailable_not_a_crash(self):
        with mock.patch.dict('sys.modules', {'hdbcli': None}):
            with self.assertRaises(hana_reads.HanaUnavailable):
                hana_reads.rows('SELECT 1 FROM DUMMY')


class ItemShapes(SimpleTestCase):
    """The dicts carry the keys the sync code reads from the Service Layer's items."""

    def test_unit_items(self):
        with mock.patch.object(hana_reads, 'rows', return_value=[('A1', 'Y', 'PCS'), ('S1', 'N', None)]):
            self.assertEqual(sap_items.unit_items(), [
                {'ItemCode': 'A1', 'InventoryItem': 'tYES', 'InventoryUOM': 'PCS'},
                {'ItemCode': 'S1', 'InventoryItem': 'tNO', 'InventoryUOM': None}])

    def test_stock_items_groups_the_warehouses_of_each_item(self):
        rows = [('A1', 'W1', Decimal('5')), ('A1', 'W2', Decimal('0')), ('B2', 'W1', Decimal('2.5'))]
        with mock.patch.object(hana_reads, 'many', return_value=[rows]) as many:
            items = sap_items.stock_items(['W1', 'W2', ' '])
        self.assertEqual(items, [
            {'ItemCode': 'A1', 'InventoryItem': 'tYES', 'ItemWarehouseInfoCollection': [
                {'WarehouseCode': 'W1', 'InStock': Decimal('5')}, {'WarehouseCode': 'W2', 'InStock': Decimal('0')}]},
            {'ItemCode': 'B2', 'InventoryItem': 'tYES', 'ItemWarehouseInfoCollection': [
                {'WarehouseCode': 'W1', 'InStock': Decimal('2.5')}]}])
        (sql, params), = many.call_args[0][0]
        self.assertEqual(params, ['W1', 'W2'])
        self.assertTrue(sql.lstrip().upper().startswith('SELECT'))

    def test_many_warehouses_go_in_chunks(self):
        codes = [f'W{n}' for n in range(1200)]
        with mock.patch.object(hana_reads, 'many', return_value=[[], [], []]) as many:
            sap_items.stock_items(codes)
        queries = many.call_args[0][0]
        self.assertEqual([len(params) for _, params in queries], [500, 500, 200])

    def test_no_warehouses_means_no_query(self):
        with mock.patch.object(hana_reads, 'many', side_effect=AssertionError('queried')):
            self.assertEqual(sap_items.stock_items([]), [])

    def test_master_items_carry_what_pull_from_sap_reads(self):
        items = [('A1', 'Acai', 'Y', 'Y', None, 121, 'BRAND', 'SUB', '18', 8, -1, 'PCS')]
        prices = [('A1', 1, Decimal('99.5')), ('A1', 2, None)]
        stock = [('A1', 'W1', Decimal('3'))]
        with mock.patch.object(hana_reads, 'many', side_effect=[[items, prices], [stock]]):
            (item,) = sap_items.master_items(['W1'], price_list=1)
        self.assertEqual(item, {
            'ItemCode': 'A1', 'ItemName': 'Acai', 'InventoryItem': 'tYES', 'SalesItem': 'tYES', 'BarCode': None,
            'ItemsGroupCode': 121, 'U_BRAND': 'BRAND', 'U_SUB_GR': 'SUB', 'U_TAXRT': '18', 'ChapterID': 8,
            'SACEntry': -1, 'InventoryUOM': 'PCS',
            'ItemPrices': [{'PriceList': 1, 'Price': Decimal('99.5')}, {'PriceList': 2, 'Price': None}],
            'ItemWarehouseInfoCollection': [{'WarehouseCode': 'W1', 'InStock': Decimal('3')}]})

    def test_item_groups(self):
        with mock.patch.object(hana_reads, 'rows', return_value=[(121, 'RETAIL'), (122, 'TECHNICAL')]):
            self.assertEqual(sap_items.item_groups(), {121: 'RETAIL', 122: 'TECHNICAL'})


class BatchFlagsFromHana(TestCase):
    """Posting asks 'is this item managed by batch?' for every line: one HANA query answers for all items."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.addCleanup(cache.clear)

    def test_one_query_answers_for_every_item_and_no_sap_call_is_made(self):
        from pos_backend import sap_utils
        with mock.patch.object(hana_reads, 'rows', return_value=[('A1', 'Y'), ('B2', 'N'), ('C3', 'Y')]) as rows, \
                mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=AssertionError('SAP was asked')):
            self.assertTrue(sap_utils.batch_managed('A1'))
            self.assertFalse(sap_utils.batch_managed('B2'))
            self.assertTrue(sap_utils.batch_managed('C3'))
        self.assertEqual(rows.call_count, 1)

    def test_the_service_layer_answers_when_hana_cannot_and_hana_is_not_retried_every_time(self):
        from pos_backend import sap_utils
        answer = mock.Mock()
        answer.json.return_value = {'ItemCode': 'A1', 'ManageBatchNumbers': 'tYES'}
        with mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')) as rows, \
                mock.patch('pos_backend.sap_utils._send_sap_request', return_value=answer) as sap:
            self.assertTrue(sap_utils.batch_managed('A1'))
            self.assertTrue(sap_utils.batch_managed('A2'))
        self.assertEqual(rows.call_count, 1)             # remembered as down for a minute
        self.assertEqual(sap.call_count, 2)


class StockSyncSources(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='Delhi', display_name='Delhi', sap_warehouse_code='WH-D')
        self.product = Product.objects.create(name='Gel', product_code='GEL', price=Decimal('10'), mrp=Decimal('10'),
                                              gst_percent=Decimal('18'), current_stock=100, center=self.centre)
        self.later = timezone.now() + timedelta(minutes=10)

    def stock(self):
        self.product.refresh_from_db()
        return self.product.current_stock

    def test_the_stock_comes_from_hana_when_it_can_answer(self):
        hana = [{'ItemCode': 'GEL', 'InventoryItem': 'tYES',
                 'ItemWarehouseInfoCollection': [{'WarehouseCode': 'WH-D', 'InStock': Decimal('130')}]}]
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'stock_items', return_value=hana) as stock_items, \
                mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate',
                           side_effect=AssertionError('the Service Layer was used')):
            self.assertEqual(sync_stock_from_sap(now=self.later), 1)
        self.assertEqual(self.stock(), 130)
        self.assertEqual(stock_items.call_args[0][0], ['WH-D'])
        row = StockTransaction.objects.get(product=self.product, transaction_type='SAP_SYNC')
        self.assertTrue(row.sap_sync_status)                # never posted back

    def test_the_service_layer_is_the_fallback_when_hana_is_down(self):
        service_layer = [{'ItemCode': 'GEL', 'InventoryItem': 'tYES',
                          'ItemWarehouseInfoCollection': [{'WarehouseCode': 'WH-D', 'InStock': 60}]}]
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'stock_items', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate',
                           return_value=iter(service_layer)):
            self.assertEqual(sync_stock_from_sap(now=self.later), 1)
        self.assertEqual(self.stock(), 60)

    def test_a_page_of_products_larger_than_one_query_can_hold_is_synced(self):
        # 100 items x 60 branches = 6000 products in one page: far more than one "IN (...)" carries on SQLite.
        centres = [Center.objects.create(center_name=f'C{n}', display_name=f'C{n}', sap_warehouse_code=f'W{n}')
                   for n in range(60)]
        codes = [f'ITEM{n:03d}' for n in range(100)]
        Product.objects.bulk_create([Product(name=c, product_code=c, center=centre, current_stock=1)
                                     for centre in centres for c in codes])
        page = [{'ItemCode': c, 'InventoryItem': 'tYES',
                 'ItemWarehouseInfoCollection': [{'WarehouseCode': f'W{n}', 'InStock': 5} for n in range(60)]}
                for c in codes]
        with mock.patch.object(hana_reads, 'available', return_value=False), \
                mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate', return_value=iter(page)):
            self.assertEqual(sync_stock_from_sap(now=self.later), 6000)
        self.assertEqual(Product.objects.filter(product_code__in=codes, current_stock=5).count(), 6000)


class UnitSyncSources(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='A', display_name='A')
        self.foam = Product.objects.create(name='FOAM', product_code='F1', unit='pcs', center=self.centre)

    def test_units_come_from_hana(self):
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'unit_items',
                                  return_value=[{'ItemCode': 'F1', 'InventoryItem': 'tYES', 'InventoryUOM': 'ML'}]), \
                mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate',
                           side_effect=AssertionError('the Service Layer was used')):
            self.assertEqual(sync_units_from_sap(), 1)
        self.foam.refresh_from_db()
        self.assertEqual(self.foam.unit, 'ml')

    def test_the_service_layer_is_the_fallback(self):
        rows = iter([{'ItemCode': 'F1', 'InventoryItem': 'tYES', 'InventoryUOM': 'ML'}])
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'unit_items', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate', return_value=rows):
            self.assertEqual(sync_units_from_sap(), 1)


class MasterSyncSources(TestCase):
    def test_the_item_master_is_read_from_hana_then_the_service_layer(self):
        from pos_backend.management.commands.pull_from_sap import Command

        centre = Center.objects.create(center_name='A', display_name='A', sap_warehouse_code='W1')
        command = Command()
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'master_items', return_value=[{'ItemCode': 'A1'}]) as master:
            self.assertEqual(list(command._master_items('url', [centre], 1)), [{'ItemCode': 'A1'}])
        self.assertEqual(master.call_args[0], (['W1'], 1))
        with mock.patch.object(hana_reads, 'available', return_value=True), \
                mock.patch.object(sap_items, 'master_items', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch.object(Command, '_paginate', return_value=iter([{'ItemCode': 'B2'}])) as paginate:
            self.assertEqual(list(command._master_items('url', [centre], 1)), [{'ItemCode': 'B2'}])
        paginate.assert_called_once_with('url', 'ItemCode')
        with mock.patch.object(hana_reads, 'available', return_value=False), \
                mock.patch.object(Command, '_paginate', return_value=iter([{'ItemCode': 'C3'}])):
            self.assertEqual(list(command._master_items('url', [centre], 1)), [{'ItemCode': 'C3'}])
