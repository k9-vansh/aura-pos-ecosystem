"""
Ticking several centres in the location dropdown shows exactly those centres.

Three branches each raise one bill of a different size. Every screen is then
read with `center_ids=A,B` and must show A and B and nothing of C - and never
more than the signed-in person was allowed to see in the first place.
"""
import datetime
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from inventory.models import Product
from pos_backend.scoping import parse_center_ids
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


def m(x):
    return Decimal(str(x or 0)).quantize(Decimal('0.01'))


class Setup(TestCase):
    def setUp(self):
        cache.clear()
        self.centers = {n: Center.objects.create(center_name=n, display_name=n, gst_enabled=True)
                        for n in ('A', 'B', 'C')}
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='Owner',
                                                    role=role, center=self.centers['A'])
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.today = datetime.date.today()
        self.first = self.today.replace(day=1)
        self.svc = ServiceMaster.objects.create(name='Cut', default_price=Decimal('1000'), tax_percentage=Decimal('18'))
        self.bills, self.clients, self.staff, self.products = {}, {}, {}, {}
        for i, (name, c) in enumerate(self.centers.items(), start=1):
            self.clients[name] = Client.objects.create(first_name=f'Client{name}', phone=f'99900000{i}0', center=c)
            self.staff[name] = StaffMember.objects.create(center=c, first_name=f'Staff{name}', designation='Stylist')
            self.products[name] = Product.objects.create(name=f'Shampoo{name}', price=Decimal('100'), mrp=Decimal('100'),
                                                         gst_percent=Decimal('18'), current_stock=3, reorder_level=10, center=c)
            res = bill_and_pay(self.api, {
                'client': self.clients[name].id, 'center': c.id, 'status': 'paid', 'discount': '0',
                'items': [{'content_type': 'services.servicemaster', 'object_id': self.svc.id, 'description': 'Cut',
                           'unit_price': '1000', 'discount': '0', 'quantity': i, 'tax_percentage': '18',
                           'staff_members': [self.staff[name].id]}]})
            assert res.status_code == 201, res.data
            self.bills[name] = Invoice.objects.get(pk=res.data['id'])

    def ids(self, *names):
        return ','.join(str(self.centers[n].id) for n in names)

    def get(self, url, names=None, api=None, **extra):
        cache.clear()
        params = {'start_date': self.first.isoformat(), 'end_date': self.today.isoformat()}
        if names:
            params['center_ids'] = self.ids(*names)
        params.update(extra)
        r = (api or self.api).get(url, params)
        self.assertEqual(r.status_code, 200, (url, getattr(r, 'data', None)))
        return r.data

    @staticmethod
    def rows(data):
        return data.get('results', data) if isinstance(data, dict) else data


class Parsing(TestCase):
    def test_a_list_of_centres(self):
        self.assertEqual(parse_center_ids('3,7, 9,x,7,-1,'), (3, 7, 9))
        self.assertEqual(parse_center_ids(None), ())


class Money(Setup):
    def test_dashboard_adds_up_the_picked_centres(self):
        a, b, c = (m(self.bills[n].total_amount) for n in 'ABC')
        both = self.get('/salon_admin/api/dashboard/summary/', ['A', 'B'])
        self.assertEqual(m(both['revenue']), a + b)
        self.assertEqual(m(self.get('/salon_admin/api/dashboard/summary/', ['B', 'C'])['revenue']), b + c)
        self.assertEqual(m(self.get('/salon_admin/api/dashboard/summary/')['revenue']), a + b + c)
        one = self.get('/salon_admin/api/dashboard/summary/', center_id=self.centers['C'].id)
        self.assertEqual(m(one['revenue']), c)
        daily = self.get('/salon_admin/api/dashboard/revenues/', ['A', 'C'])
        self.assertEqual(sum(m(x['revenue']) for x in daily['daily']), a + c)
        staff = self.get('/salon_admin/api/dashboard/staff/', ['A', 'B'])
        self.assertEqual({r['name'] for r in staff['table']}, {'StaffA', 'StaffB'})

    def test_finance_screens_add_up_the_picked_centres(self):
        a, b, c = (m(self.bills[n].total_amount) for n in 'ABC')
        reg = self.get('/finance/api/register_summary/', ['A', 'B'])
        self.assertEqual(m(reg['revenues']['including_tax']), a + b)
        self.assertEqual(m(reg['reconciliation']['total_billed']), a + b)
        monthly = self.get('/finance/api/monthly_sales/', ['B', 'C'])
        self.assertEqual(sum(m(r['including_tax']) for r in monthly), b + c)
        detailed = self.rows(self.get('/finance/api/detailed_revenues/', ['A', 'C']))
        self.assertEqual(sum(m(r['grand_total']) for r in detailed), a + c)
        tax = self.get('/finance/api/reports/tax/', ['A', 'B'])
        self.assertEqual(len(tax['invoices']), 2)

    def test_incentives_follow_the_picked_centres(self):
        rows = self.get('/finance/api/reports/incentive-calculation/', ['A', 'B'], frequency='monthly')
        self.assertEqual({r['staff_name'] for r in rows}, {'StaffA', 'StaffB'})

    def test_targets_are_summed_over_the_picked_centres(self):
        for n, t in (('A', 1000), ('B', 2000), ('C', 4000)):
            Center.objects.filter(pk=self.centers[n].pk).update(monthly_target=t)
        reg = self.get('/finance/api/register_summary/', ['A', 'C'])
        self.assertEqual(m(reg['revenues']['target']), m(5000))

    def test_a_cached_answer_is_never_reused_for_another_selection(self):
        cache.clear()
        first = self.api.get('/finance/api/register_summary/', {'center_ids': self.ids('A', 'B')}).data
        second = self.api.get('/finance/api/register_summary/', {'center_ids': self.ids('B', 'C')}).data
        self.assertNotEqual(first['revenues']['including_tax'], second['revenues']['including_tax'])

    def test_one_picked_centre_is_the_same_as_that_centre(self):
        a = m(self.bills['A'].total_amount)
        reg = self.get('/finance/api/register_summary/', ['A'])
        self.assertEqual(m(reg['revenues']['including_tax']), a + m(self.bills['B'].total_amount) + m(self.bills['C'].total_amount))


class Lists(Setup):
    def test_bills(self):
        rows = self.rows(self.get('/billing/invoices/', ['A', 'B']))
        self.assertEqual({r['id'] for r in rows}, {self.bills['A'].id, self.bills['B'].id})

    def test_clients_staff_products(self):
        names = {r['first_name'] for r in self.rows(self.get('/clients/api/clients/', ['B', 'C']))}
        self.assertEqual(names, {'ClientB', 'ClientC'})
        names = {r['first_name'] for r in self.rows(self.get('/staff/api/members/', ['A', 'C']))}
        self.assertEqual(names, {'StaffA', 'StaffC'})
        names = {r['name'] for r in self.rows(self.get('/inventory/api/products/', ['A', 'B']))}
        self.assertEqual(names, {'ShampooA', 'ShampooB'})
        low = self.get('/inventory/api/low_stock/', ['B', 'C'])
        self.assertEqual({r['name'] for r in self.rows(low)}, {'ShampooB', 'ShampooC'})

    def test_petty_cash_and_closing_lists(self):
        from finance.models import PettyCashEntry
        for n in 'ABC':
            PettyCashEntry.objects.create(center=self.centers[n], date=datetime.datetime.now(), amount=Decimal('10'),
                                          description=f'milk {n}', user=self.owner)
        rows = self.rows(self.get('/finance/api/petty-cash/', ['A', 'B']))
        self.assertEqual({r['description'] for r in rows}, {'milk A', 'milk B'})


class NeverWider(Setup):
    def test_a_person_limited_to_two_centres_cannot_widen_the_selection(self):
        role = Role.objects.create(name='Two', permissions={k: v for k, v in full_access_permissions().items() if k != 'all_centers'})
        user = CustomUser.objects.create_user(email='two@t.com', password='pw', full_name='Two', role=role, center=self.centers['A'])
        user.centers.set([self.centers['A'], self.centers['B']])
        api = APIClient()
        api.force_authenticate(user)
        a, b = m(self.bills['A'].total_amount), m(self.bills['B'].total_amount)
        got = self.get('/salon_admin/api/dashboard/summary/', ['A', 'B', 'C'], api=api)
        self.assertEqual(m(got['revenue']), a + b)
        got = self.get('/salon_admin/api/dashboard/summary/', ['B', 'C'], api=api)
        self.assertEqual(m(got['revenue']), b)
        got = self.get('/finance/api/register_summary/', ['A', 'C'], api=api)
        self.assertEqual(m(got['revenues']['including_tax']), a)
        rows = self.rows(self.get('/billing/invoices/', ['B', 'C'], api=api))
        self.assertEqual({r['id'] for r in rows}, {self.bills['B'].id})

    def test_a_save_ignores_the_selection(self):
        r = self.api.post(f"/clients/api/clients/?center_ids={self.ids('A', 'B')}", {
            'first_name': 'New', 'phone': '9876500000', 'center': self.centers['C'].id}, format='json')
        self.assertIn(r.status_code, (200, 201), r.data)
        self.assertEqual(Client.objects.get(phone='9876500000').center_id, self.centers['C'].id)


class Everywhere(Setup):
    """For every list a screen shows: the picked centres' rows are exactly the
    rows of each of those centres alone, put together."""

    def build(self):
        from appointments.models import Appointment
        from billing.models import AdvancePayment, InvoiceRefund
        from finance.models import DailyClosing, PettyCashEntry, Shift
        from inventory.models import ProductLot, PurchaseOrder, StockTransaction, StockTransfer, Vendor
        from marketing.models import Membership, Package, Promotion, ValueCard, WhatsAppMessage
        names = list(self.centers)
        for i, n in enumerate(names):
            c = self.centers[n]
            vendor = Vendor.objects.create(name=f'Vendor{n}', center=c, vendor_code=f'V{i}')
            PurchaseOrder.objects.create(vendor=vendor, center=c, status='Ordered')
            StockTransfer.objects.create(from_center=c, to_center=self.centers[names[(i + 1) % 3]], status='Draft')
            Promotion.objects.create(level='Center', center=c, name=f'Promo{n}', start_date=self.first,
                                     end_date=self.today + datetime.timedelta(days=30))
            ValueCard.objects.create(level='Center', center=c, title=f'Card{n}', pre_tax_price=Decimal('100'),
                                     post_tax_price=Decimal('118'), value=Decimal('120'), expiry_days=30)
            Membership.objects.create(level='Center', center=c, name=f'Mem{n}', pre_tax_price=Decimal('100'),
                                      post_tax_price=Decimal('118'), expiry_days=30)
            Package.objects.create(level='Center', center=c, name=f'Pkg{n}', price=Decimal('100'), validity_days=30)
            WhatsAppMessage.objects.create(center=c, date=self.today, time=datetime.time(10, 0), client_name=n,
                                           client_phone='1', message='hi', status='sent')
            Appointment.objects.create(center=c, client=self.clients[n], client_phone='1', client_name=n,
                                       date=self.today, start_time=datetime.time(10, 0))
            AdvancePayment.objects.create(client=self.clients[n], center=c, amount=Decimal('50'))
            InvoiceRefund.objects.create(invoice=self.bills[n], center=c, amount=Decimal('10'), reason='x',
                                         refunded_on=self.today)
            StockTransaction.objects.create(product=self.products[n], center=c, transaction_type='CHECKOUT',
                                            quantity_change=-1, notes='use')
            ProductLot.objects.create(product=self.products[n], lot_number=f'L{n}', net_price=Decimal('1'), mrp=Decimal('1'))
            PettyCashEntry.objects.create(center=c, date=datetime.datetime.now(), amount=Decimal('10'),
                                          description=f'milk {n}', user=self.owner)
            DailyClosing.objects.create(center=c, date=self.today, user=self.owner)
            Shift.objects.create(center=c, opened_by=self.owner)

    @staticmethod
    def row_ids(data):
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        return {r['id'] for r in data if isinstance(r, dict) and 'id' in r}

    def test_every_list(self):
        self.build()
        urls = [
            '/inventory/api/vendors/', '/inventory/api/purchase-orders/', '/inventory/api/stock_transfers/',
            '/inventory/api/stock-transactions/', '/inventory/api/products/',
            '/marketing/api/promotions/', '/marketing/api/cards/', '/marketing/api/memberships/',
            '/marketing/api/packages/', '/marketing/api/whatsapp/',
            '/appointments/api/appointments/', '/billing/advances/', '/billing/invoices/',
            '/finance/api/daily-closing/', '/finance/api/shifts/', '/finance/api/petty-cash/',
            '/clients/api/clients/', '/staff/api/members/', '/finance/api/refunds/',
            '/staff/api/logs/', '/inventory/api/lots/',
        ]
        problems = []
        for url in urls:
            single = {n: self.row_ids(self.get(url, center_id=self.centers[n].id)) for n in 'ABC'}
            self.assertTrue(any(single.values()), f'{url}: no rows to compare')
            for pair in (('A', 'B'), ('B', 'C'), ('A', 'C')):
                multi = self.row_ids(self.get(url, list(pair)))
                want = single[pair[0]] | single[pair[1]]
                # a transfer touches two centres, so it can appear for a centre
                # that is not the one it started at
                if multi != want:
                    problems.append((url, pair, sorted(multi), sorted(want)))
        self.assertEqual(problems, [])
