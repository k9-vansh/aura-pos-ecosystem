"""The inventory screens' read paths: no query per row, filters on the server, and SAP never waited on.

Each test below pins one thing the screens depend on at a large size (many branches, many products and a
long ledger): a list costs the same few queries whether it holds 3 rows or 30, the ledger can be asked for
one day's checkouts, the stock history answers from the ledger alone, "All locations" adds stock up on the
server, and a transfer list never waits for SAP.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.fast_lists import aggregate_rows, product_rows
from inventory.models import (
    Product, PurchaseOrder, PurchaseOrderItem, StockRequest, StockRequestItem, StockTransaction, StockTransfer,
    StockTransferItem, Vendor,
)
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

D = Decimal


class ReadPathBase(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Center.objects.create(center_name='A', display_name='A', gst_number='07AAAAA0000A1Z5')
        self.b = Center.objects.create(center_name='B', display_name='B', gst_number='09CCCCC0000C1Z5')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='Owner', role=role,
                                                   center=self.a)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def queries(self, url):
        with CaptureQueriesContext(connection) as ctx:
            res = self.api.get(url)
        self.assertEqual(res.status_code, 200, getattr(res, 'data', res.content))
        return len(ctx), res.json()

    def product(self, name, center=None, **kw):
        kw.setdefault('product_code', name.upper())
        return Product.objects.create(name=name, center=center or self.a, **kw)


class NoQueryPerRow(ReadPathBase):
    """The same number of queries for 2 rows and for 12."""

    def grow(self, make, url):
        make(2)
        few, rows = self.queries(url)
        self.assertEqual(len(rows), 2)
        make(10)
        many, rows = self.queries(url)
        self.assertEqual(len(rows), 12)
        self.assertEqual(many, few, f'{url}: {few} queries for 2 rows but {many} for 12')

    def test_purchase_orders(self):
        vendor = Vendor.objects.create(name='V', center=self.a)
        product = self.product('Gel')
        vendor.mapped_products.add(product)

        def make(count):
            for _ in range(count):
                vendor_n = Vendor.objects.create(name='V2', center=self.a)
                vendor_n.mapped_products.add(product)
                po = PurchaseOrder.objects.create(vendor=vendor_n, center=self.a)
                PurchaseOrderItem.objects.create(purchase_order=po, product=product, quantity=1, rate=5, total_price=5)
        self.grow(make, '/inventory/api/purchase-orders/')

    def test_stock_transfers(self):
        product = self.product('Gel')

        def make(count):
            for _ in range(count):
                t = StockTransfer.objects.create(from_center=self.a, to_center=self.b, created_by=self.user,
                                                 dispatched_by=self.user, received_by=self.user)
                StockTransferItem.objects.create(transfer=t, product=product, quantity=1)
        self.grow(make, '/inventory/api/stock_transfers/')

    def test_stock_requests(self):
        product = self.product('Gel', category='Retail')

        def make(count):
            for _ in range(count):
                r = StockRequest.objects.create(requesting_center=self.a, stock_type='Retail', created_by=self.user)
                StockRequestItem.objects.create(stock_request=r, product=product, quantity=1)
        self.grow(make, '/inventory/api/stock_requests/')

    def test_stock_transactions(self):
        product = self.product('Gel')

        def make(count):
            for _ in range(count):
                StockTransaction.objects.create(product=product, center=self.a, transaction_type='SALE',
                                                quantity_change=-1, created_by=self.user)
        self.grow(make, '/inventory/api/stock-transactions/')


class StockTransactionFilters(ReadPathBase):
    def setUp(self):
        super().setUp()
        self.gel = self.product('Gel')
        self.oil = self.product('Oil')
        self.today = datetime.datetime.now().replace(hour=10, minute=0, second=0, microsecond=0)
        rows = [
            ('CHECKOUT', self.gel, self.today), ('CHECKOUT', self.oil, self.today + datetime.timedelta(hours=5)),
            ('SALE', self.gel, self.today), ('CHECKOUT', self.gel, self.today - datetime.timedelta(days=1)),
            ('CHECKOUT', self.gel, self.today + datetime.timedelta(days=1)),
        ]
        for ttype, product, when in rows:
            row = StockTransaction.objects.create(product=product, center=self.a, transaction_type=ttype, quantity_change=-1)
            StockTransaction.objects.filter(pk=row.pk).update(created_at=when)
        self.day = self.today.date().isoformat()

    def ids(self, query):
        res = self.api.get('/inventory/api/stock-transactions/' + query)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_no_filter_is_the_whole_ledger_newest_first(self):
        rows = self.ids('')
        self.assertEqual(len(rows), 5)
        self.assertEqual([r['created_at'] for r in rows], sorted((r['created_at'] for r in rows), reverse=True))

    def test_one_type_on_one_day(self):
        rows = self.ids(f'?transaction_type=CHECKOUT&date={self.day}')
        self.assertEqual(sorted(r['product_name'] for r in rows), ['Gel', 'Oil'])
        self.assertEqual({r['transaction_type'] for r in rows}, {'CHECKOUT'})

    def test_a_range_and_a_product(self):
        yesterday = (self.today - datetime.timedelta(days=1)).date().isoformat()
        self.assertEqual(len(self.ids(f'?date_from={yesterday}&date_to={self.day}')), 4)
        self.assertEqual(len(self.ids(f'?date_from={self.day}')), 4)
        self.assertEqual(len(self.ids(f'?product={self.oil.id}')), 1)

    def test_a_bad_date_or_product_is_a_400_not_a_500(self):
        for query in ('?date=tomorrow', '?date_from=1/2/3', '?date_to=x', '?product=abc'):
            self.assertEqual(self.api.get('/inventory/api/stock-transactions/' + query).status_code, 400, query)

    def test_the_branch_scoping_still_applies(self):
        other = self.product('Other', center=self.b)
        StockTransaction.objects.create(product=other, center=self.b, transaction_type='CHECKOUT', quantity_change=-1)
        role = Role.objects.create(name='Clerk', permissions={'inventory': {'stock_history': {'read': True}}})
        clerk = CustomUser.objects.create_user(email='c@t.com', password='pw', full_name='C', role=role, center=self.a)
        api = APIClient()
        api.force_authenticate(clerk)
        names = {r['product_name'] for r in api.get('/inventory/api/stock-transactions/?transaction_type=CHECKOUT').json()}
        self.assertNotIn('Other', names)


class StockHistory(ReadPathBase):
    def test_it_is_current_stock_minus_what_moved_afterwards(self):
        gel = self.product('Gel', current_stock=D('10'))
        oil = self.product('Oil', current_stock=D('4'))
        self.product('Elsewhere', center=self.b, current_stock=D('9'))
        cutoff = datetime.datetime(2026, 10, 10, 12, 0)

        def move(product, qty, when):
            row = StockTransaction.objects.create(product=product, center=self.a, transaction_type='SALE', quantity_change=qty)
            StockTransaction.objects.filter(pk=row.pk).update(created_at=when)

        move(gel, D('-3'), cutoff + datetime.timedelta(days=2))       # after the day: counted back in
        move(gel, D('5'), cutoff + datetime.timedelta(days=3))        # after the day: taken back out
        move(gel, D('-7'), cutoff - datetime.timedelta(days=1))       # before the day: already in the figure
        res = self.api.get(f'/inventory/api/products/stock_history/?date=2026-10-10&center_id={self.a.id}')
        self.assertEqual(res.status_code, 200, res.content)
        by_name = {r['name']: Decimal(str(r['historical_stock'])) for r in res.json()}
        self.assertEqual(by_name, {'Gel': D('10') - (D('-3') + D('5')), 'Oil': D('4')})
        self.assertEqual(sorted(r['name'] for r in res.json()), ['Gel', 'Oil'])

    def test_it_does_not_cost_a_query_per_product(self):
        for n in range(3):
            self.product(f'P{n}')
        few, _ = self.queries('/inventory/api/products/stock_history/?date=2026-10-10')
        for n in range(3, 20):
            self.product(f'P{n}')
        many, _ = self.queries('/inventory/api/products/stock_history/?date=2026-10-10')
        self.assertEqual(few, many)


def fold_in_the_browser(products):
    """What the Inventory screen did itself for 'All locations': first row of each code, stock added up."""
    seen = {}
    for p in products:
        key = (p['product_code'] or p['product_id_str'] or p['name'] or '').lower()
        if key in seen:
            seen[key]['current_stock'] += p['current_stock'] or 0
        else:
            seen[key] = dict(p, current_stock=p['current_stock'] or 0)
    return list(seen.values())


class AllLocationsTotals(ReadPathBase):
    def setUp(self):
        super().setUp()
        self.c = Center.objects.create(center_name='C', display_name='C')
        for center, stock in ((self.a, '4.5'), (self.b, '2'), (self.c, '1.25')):
            self.product('Gel', center, current_stock=D(stock), reorder_level=10)
            self.product('Oil', center, current_stock=D('1'), product_code='OIL-1')
        self.product('Loose', self.a, product_code='', product_id_str='X9', current_stock=D('3'))
        self.product('Loose', self.b, product_code=None, product_id_str='x9', current_stock=D('2'))
        self.product('No code', self.a, product_code=None, current_stock=D('7'))
        self.product('No code', self.b, product_code=None, current_stock=D('1'))

    def test_it_equals_what_the_screen_computed_itself(self):
        everything, _ = product_rows(Product.objects.order_by('name', 'id'), 1000)
        expected = fold_in_the_browser(everything)
        got, truncated = aggregate_rows(Product.objects.all(), 1000)
        self.assertFalse(truncated)
        self.assertEqual({(r['name'], r['current_stock']) for r in got},
                         {(r['name'], r['current_stock']) for r in expected})
        self.assertEqual([r['name'] for r in got], [r['name'] for r in expected])

    def test_the_total_is_one_row_per_product_with_the_sum(self):
        res = self.api.get('/inventory/api/products/?group=code')
        self.assertEqual(res.status_code, 200)
        rows = {r['name']: r for r in res.json()}
        self.assertEqual(len(res.json()), 4)                                        # Gel, Loose, No code, Oil
        self.assertEqual(rows['Gel']['current_stock'], 7.75)
        self.assertTrue(rows['Gel']['is_low_stock'])                                # 7.75 <= reorder level 10
        self.assertEqual(rows['Loose']['current_stock'], 5)
        self.assertEqual(rows['Oil']['current_stock'], 3)
        self.assertEqual(rows['No code']['current_stock'], 8)

    def test_it_follows_the_branch_selection_and_scoping(self):
        res = self.api.get(f'/inventory/api/products/?group=code&center_ids={self.a.id},{self.b.id}')
        self.assertEqual({r['name']: r['current_stock'] for r in res.json()}['Gel'], 6.5)
        role = Role.objects.create(name='Clerk', permissions={'inventory': {'products': {'read': True}}})
        clerk = CustomUser.objects.create_user(email='c@t.com', password='pw', full_name='C', role=role, center=self.b)
        api = APIClient()
        api.force_authenticate(clerk)
        self.assertEqual({r['name']: r['current_stock'] for r in api.get('/inventory/api/products/?group=code').json()}['Gel'], 2)

    def test_without_group_the_list_is_unchanged(self):
        self.assertEqual(len(self.api.get('/inventory/api/products/').json()), 10)

    def test_the_cost_does_not_grow_with_the_branches(self):
        few, _ = self.queries('/inventory/api/products/?group=code')
        for n in range(5):
            branch = Center.objects.create(center_name=f'N{n}', display_name=f'N{n}')
            self.product('Gel', branch, current_stock=D('1'))
        many, rows = self.queries('/inventory/api/products/?group=code')
        self.assertEqual(few, many)
        self.assertEqual({r['name']: r['current_stock'] for r in rows}['Gel'], 12.75)


class VendorsAllLocations(ReadPathBase):
    def setUp(self):
        super().setUp()
        self.c = Center.objects.create(center_name='C', display_name='C')
        for center in (self.a, self.b, self.c):                       # every branch holds its own copy of each vendor
            Vendor.objects.create(name='Loreal', vendor_code='V1', center=center)
            Vendor.objects.create(name='Wella', vendor_code='', center=center)
        Vendor.objects.create(name='Only at B', vendor_code='V9', center=self.b)

    def names(self, query=''):
        res = self.api.get('/inventory/api/vendors/' + query)
        self.assertEqual(res.status_code, 200)
        return sorted(v['name'] for v in res.json())

    def test_the_plain_list_is_unchanged(self):
        self.assertEqual(len(self.names()), 7)

    def test_grouped_it_is_one_row_per_vendor(self):
        self.assertEqual(self.names('?group=code'), ['Loreal', 'Only at B', 'Wella'])

    def test_a_vendor_with_no_code_is_told_apart_by_name_not_merged_with_others(self):
        Vendor.objects.create(name='wella', vendor_code='', center=self.a)            # same name, other case
        Vendor.objects.create(name='Keune', vendor_code='', center=self.a)
        self.assertEqual(self.names('?group=code'), ['Keune', 'Loreal', 'Only at B', 'Wella'])

    def test_it_follows_the_branch_selection_and_the_users_branches(self):
        self.assertEqual(self.names(f'?group=code&center_ids={self.a.id},{self.c.id}'), ['Loreal', 'Wella'])
        role = Role.objects.create(name='Clerk', permissions={'inventory': {'vendors': {'read': True}}})
        clerk = CustomUser.objects.create_user(email='c@t.com', password='pw', full_name='C', role=role, center=self.a)
        api = APIClient()
        api.force_authenticate(clerk)
        self.assertEqual(sorted(v['name'] for v in api.get('/inventory/api/vendors/?group=code').json()),
                         ['Loreal', 'Wella'])

    def test_the_cost_does_not_grow_with_the_branches(self):
        few, _ = self.queries('/inventory/api/vendors/?group=code')
        for n in range(5):
            Vendor.objects.create(name='Loreal', vendor_code='V1', center=Center.objects.create(center_name=f'N{n}'))
        many, rows = self.queries('/inventory/api/vendors/?group=code')
        self.assertEqual((few, len(rows)), (many, 3))


class LowStockBadge(ReadPathBase):
    """The shell polls the low-stock count from every till: one scan per set of branches, not per person."""

    def clerk(self, email, center):
        role = Role.objects.get_or_create(name='Clerk', defaults={'permissions': {'inventory': {'products': {'read': True}}}})[0]
        user = CustomUser.objects.create_user(email=email, password='pw', full_name=email, role=role, center=center)
        api = APIClient()
        api.force_authenticate(user)
        return api

    def count(self, api, query='?count_only=1'):
        res = api.get('/inventory/api/low_stock/' + query)
        self.assertEqual(res.status_code, 200)
        return res.json()['count']

    def test_people_with_the_same_branches_share_one_scan(self):
        Product.objects.create(name='Low', center=self.a, current_stock=0, reorder_level=5, product_code='L')
        Product.objects.create(name='Fine', center=self.a, current_stock=50, reorder_level=5, product_code='F')
        Product.objects.create(name='Low B', center=self.b, current_stock=0, reorder_level=5, product_code='LB')
        one, two, other = self.clerk('1@t.com', self.a), self.clerk('2@t.com', self.a), self.clerk('3@t.com', self.b)
        self.assertEqual(self.count(one), 1)
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.count(two), 1)                       # answered from the shared count
        self.assertFalse([q for q in ctx.captured_queries if 'inventory_product' in q['sql']])
        self.assertEqual(self.count(other), 1)                          # a different branch has its own count
        self.assertEqual(self.count(self.api), 2)                       # the owner sees both branches
        self.assertEqual(self.count(self.api, f'?count_only=1&center_id={self.b.id}'), 1)

    def test_nobody_gets_a_branch_they_may_not_see_through_the_shared_count(self):
        Product.objects.create(name='Low B', center=self.b, current_stock=0, reorder_level=5, product_code='LB')
        self.assertEqual(self.count(self.api), 1)                       # fills the cache for "all branches"
        clerk = self.clerk('1@t.com', self.a)
        self.assertEqual(self.count(clerk, f'?count_only=1&center_id={self.b.id}'), 0)
        self.assertEqual(self.count(clerk), 0)


class SapIsNeverWaitedOn(ReadPathBase):
    """A person's request does not wait on SAP: HANA answers in a fraction of a second, and when it is down
    that is remembered instead of being asked again for every row."""

    def setUp(self):
        super().setUp()
        self.nowhere = Center.objects.create(center_name='Mystery', display_name='Mystery', sap_branch_name='Mystery BPL')

    def test_a_down_sap_is_asked_once_not_once_per_row(self):
        from inventory import places
        from pos_backend import hana_reads, sap_utils
        asked = []

        def down(*args, **kwargs):
            asked.append(1)
            raise ConnectionError('SAP is down')

        with mock.patch.object(sap_utils, 'sap_enabled', return_value=True), \
                mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch.object(sap_utils, 'bpl_states', side_effect=down):
            for _ in range(25):
                self.assertEqual(places.center_state(self.nowhere), '')
        self.assertEqual(len(asked), 1)

    def test_the_states_come_from_hana_when_it_answers(self):
        from inventory import places
        from pos_backend import hana_reads, sap_utils
        with mock.patch.object(sap_utils, 'sap_enabled', return_value=True), \
                mock.patch.object(hana_reads, 'rows', return_value=[('Mystery BPL', 'UP'), ('Main', None)]) as rows, \
                mock.patch.object(sap_utils, 'bpl_states', side_effect=AssertionError('the Service Layer was used')):
            self.assertEqual(places.center_state(self.nowhere), 'UP')
            self.assertEqual(places.center_state(self.nowhere), 'UP')
        self.assertEqual(rows.call_count, 1)                                    # then it is remembered

    def test_nothing_is_asked_while_sap_is_off(self):
        from inventory import places
        from pos_backend import hana_reads
        with mock.patch.object(hana_reads, 'rows', side_effect=AssertionError('HANA was asked')):
            self.assertEqual(places.center_state(self.nowhere), '')

    def test_the_transfer_list_does_not_ask_sap_per_row(self):
        from pos_backend import hana_reads, sap_utils
        product = self.product('Gel')
        for _ in range(6):
            t = StockTransfer.objects.create(from_center=self.nowhere, to_center=self.a, created_by=self.user)
            StockTransferItem.objects.create(transfer=t, product=product, quantity=1)
        with mock.patch.object(sap_utils, 'sap_enabled', return_value=True), \
                mock.patch.object(hana_reads, 'rows', return_value=[('Mystery BPL', 'DL')]) as rows:
            res = self.api.get('/inventory/api/stock_transfers/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual({r['transfer_kind'] for r in res.json()}, {'same_state'})
        self.assertEqual(rows.call_count, 1)

    def test_the_branch_drop_down_reads_hana_and_remembers(self):
        from pos_backend import hana_reads
        su = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        api = APIClient()
        api.force_authenticate(su)
        with mock.patch.object(hana_reads, 'rows', return_value=[(1, 'New Delhi'), (2, 'Main')]) as rows:
            first = api.get('/salon_admin/api/sap-branches/').json()
            second = api.get('/salon_admin/api/sap-branches/').json()
        self.assertEqual(first, [{'BPLID': 1, 'BPLName': 'New Delhi'}, {'BPLID': 2, 'BPLName': 'Main'}])
        self.assertEqual(second, first)
        self.assertEqual(rows.call_count, 1)

    def test_the_branch_drop_down_falls_back_to_the_service_layer(self):
        from pos_backend import hana_reads
        su = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        api = APIClient()
        api.force_authenticate(su)
        with mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')), \
                mock.patch('pos_backend.sap_utils._get_all', return_value=[{'BPLID': 3, 'BPLName': 'Punjab'}]):
            self.assertEqual(api.get('/salon_admin/api/sap-branches/').json(), [{'BPLID': 3, 'BPLName': 'Punjab'}])
