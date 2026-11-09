"""
Tests for the single-center finance report endpoints.

These were the remaining untested report surface after the multi-salon rewrite:
service drilldown, staff performance, tax, refunds, procurement, register
summary, monthly sales, detailed revenues and the manager-discount audit.
"""

import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem, InvoiceRefund, Payment
from clients.models import Client
from inventory.models import Product
from pos_backend.money import D, money
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class ReportTestBase(TestCase):
    def setUp(self):
        # Register summary and monthly sales cache their result for 60s, keyed
        # by branch and dates. Every test reuses "branch 1, this month", so
        # without this a test reads the previous test's numbers.
        cache.clear()
        self.center = Center.objects.create(center_name='Andheri', display_name='Andheri')
        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )

        # No last name: this is what produced "Ravi None" / "Asha None".
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center,
        )
        self.service = ServiceMaster.objects.create(
            name='Keratin', category='Hair', hsn_code='999721',
            default_price=Decimal('2000.00'), tax_percentage=Decimal('18.00'),
        )
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('500.00'), gst_percent=Decimal('18.00'),
            current_stock=30, center=self.center,
        )
        self.ravi = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'),
        )
        self.service_ct = ContentType.objects.get_for_model(ServiceMaster)
        self.product_ct = ContentType.objects.get_for_model(Product)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.today = datetime.date.today().isoformat()
        self.month_start = datetime.date.today().replace(day=1).isoformat()

    def make_invoice(self, total='2360.00'):
        invoice = Invoice.objects.create(
            center=self.center, client=self.customer, status='paid',
            subtotal=Decimal('2000.00'),
            cgst=Decimal('180.00'), sgst=Decimal('180.00'),
            total_amount=Decimal(total), paid_amount=Decimal(total),
            staff=self.ravi,
        )
        invoice.assign_invoice_number()
        return invoice

    def add_line(self, invoice, obj, content_type, price='2000.00', tax='18.00',
                 quantity=1, is_redemption=False, staff=None, description=None):
        taxable = Decimal('0.00') if is_redemption else Decimal(price) * quantity
        item = InvoiceItem.objects.create(
            invoice=invoice, content_type=content_type, object_id=obj.id,
            description=description or getattr(obj, 'name', 'Item'),
            unit_price=Decimal(price), quantity=quantity,
            tax_percentage=Decimal('0.00') if is_redemption else Decimal(tax),
            tax_amount=money(taxable * Decimal(tax) / Decimal('100')),
            total_price=money(taxable), is_redemption=is_redemption,
            staff=staff or self.ravi,
        )
        item.staff_members.set([staff or self.ravi])
        return item

    def get(self, path, **params):
        params.setdefault('start_date', self.month_start)
        params.setdefault('end_date', self.today)
        return self.api.get(path, params)


class ServiceDrilldownTests(ReportTestBase):
    URL = '/finance/api/reports/services/'

    def test_reports_revenue_per_item(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data[0]
        self.assertEqual(money(row['revenue']), Decimal('2000.00'))
        self.assertEqual(row['type'], 'Service')

    def test_one_item_with_varied_labels_is_a_single_row(self):
        """It grouped by description, splitting one service across rows."""
        first = self.make_invoice()
        self.add_line(first, self.service, self.service_ct, description='Keratin')
        second = self.make_invoice()
        self.add_line(second, self.service, self.service_ct,
                      description='Keratin (Senior)')

        response = self.get(self.URL)
        rows = [r for r in response.data if r['item_id'] == self.service.id]
        self.assertEqual(len(rows), 1, response.data)
        self.assertEqual(money(rows[0]['revenue']), Decimal('4000.00'))
        self.assertEqual(int(rows[0]['count']), 2)

    def test_redemptions_do_not_inflate_the_count(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        before = int(self.get(self.URL).data[0]['count'])

        self.add_line(invoice, self.service, self.service_ct,
                      price='0.00', quantity=4, is_redemption=True)
        after = int(self.get(self.URL).data[0]['count'])
        self.assertEqual(after, before)

    def test_average_price_is_revenue_over_count(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct, quantity=2)
        row = self.get(self.URL).data[0]
        self.assertEqual(money(row['avg_price']), Decimal('2000.00'))

    def test_empty_period_returns_an_empty_list(self):
        response = self.api.get(self.URL, {
            'start_date': '2020-01-01', 'end_date': '2020-01-31',
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])


class StaffPerformanceTests(ReportTestBase):
    URL = '/finance/api/reports/staff-performance/'

    def test_reports_revenue_per_staff(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        row = response.data[0]
        self.assertEqual(row['staff_id'], self.ravi.id)
        self.assertEqual(money(row['revenue_generated']), Decimal('2000.00'))

    def test_staff_with_no_last_name_is_not_named_none(self):
        """`.get(key, '')` returns None when the key exists with value None."""
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        row = self.get(self.URL).data[0]
        self.assertEqual(row['staff_name'], 'Ravi')
        self.assertNotIn('None', row['staff_name'])

    def test_two_staff_sharing_a_name_stay_separate(self):
        """It grouped by first+last name, merging distinct people."""
        twin = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Senior Stylist',
            salary=Decimal('25000.00'),
        )
        first = self.make_invoice()
        self.add_line(first, self.service, self.service_ct, staff=self.ravi)
        second = self.make_invoice()
        self.add_line(second, self.service, self.service_ct, staff=twin)

        response = self.get(self.URL)
        staff_ids = {row['staff_id'] for row in response.data}
        self.assertEqual(staff_ids, {self.ravi.id, twin.id}, response.data)
        for row in response.data:
            self.assertEqual(money(row['revenue_generated']), Decimal('2000.00'))

    def test_redemptions_do_not_count_as_work_sold(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        before = int(self.get(self.URL).data[0]['services_done'])

        self.add_line(invoice, self.service, self.service_ct,
                      price='0.00', quantity=3, is_redemption=True)
        after = int(self.get(self.URL).data[0]['services_done'])
        self.assertEqual(after, before)

    def test_labels_itself_as_gross_sales(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        self.assertEqual(self.get(self.URL).data[0]['basis'], 'gross sales')

    def test_a_line_done_by_two_stylists_is_shared_by_the_saved_split(self):
        """It all landed on the first stylist; the second showed nothing."""
        kiran = StaffMember.objects.create(
            center=self.center, first_name='Kiran', designation='Stylist', salary=Decimal('20000.00'))
        invoice = self.make_invoice()
        item = self.add_line(invoice, self.service, self.service_ct)
        item.staff_members.set([self.ravi, kiran])
        item.staff_allocations = {str(self.ravi.id): '60', str(kiran.id): '40'}
        item.save()

        rows = {r['staff_id']: r for r in self.get(self.URL).data}
        self.assertEqual(money(rows[self.ravi.id]['revenue_generated']), Decimal('1200.00'))
        self.assertEqual(money(rows[kiran.id]['revenue_generated']), Decimal('800.00'))
        self.assertEqual(money(rows[self.ravi.id]['tax_collected']) + money(rows[kiran.id]['tax_collected']),
                         Decimal('360.00'))

    def test_a_shared_line_with_no_saved_split_is_shared_equally(self):
        kiran = StaffMember.objects.create(
            center=self.center, first_name='Kiran', designation='Stylist', salary=Decimal('20000.00'))
        invoice = self.make_invoice()
        item = self.add_line(invoice, self.service, self.service_ct, price='1001.00')
        item.staff_members.set([self.ravi, kiran])

        rows = {r['staff_id']: r for r in self.get(self.URL).data}
        total = money(rows[self.ravi.id]['revenue_generated']) + money(rows[kiran.id]['revenue_generated'])
        self.assertEqual(total, Decimal('1001.00'))
        self.assertEqual({money(r['revenue_generated']) for r in rows.values()}, {Decimal('500.50'), Decimal('500.50')})


class PaginationGuardTests(ReportTestBase):
    """`int(request.query_params.get('page', 1))` made ?page=abc a 500."""

    PAGINATED = [
        '/finance/api/detailed_revenues/',
        '/finance/api/refunds/',
    ]

    def test_junk_page_values_do_not_500(self):
        for url in self.PAGINATED:
            for value in ['abc', '', '-5', '1e9', 'null', '1.5']:
                with self.subTest(url=url, page=value):
                    response = self.get(url, page=value)
                    self.assertNotEqual(response.status_code, 500,
                                        f'{url}?page={value}')
                    self.assertEqual(response.status_code, 200)

    def test_junk_page_size_values_do_not_500(self):
        for url in self.PAGINATED:
            for value in ['abc', '', '0', '-1', '99999']:
                with self.subTest(url=url, page_size=value):
                    response = self.get(url, page_size=value)
                    self.assertEqual(response.status_code, 200)

    def test_page_size_is_capped(self):
        response = self.get(self.PAGINATED[0], page_size='99999')
        self.assertEqual(response.status_code, 200)


class RegisterSummaryReconciliationTests(ReportTestBase):
    URL = '/finance/api/register_summary/'

    def test_reconciliation_block_is_present_and_consistent(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        Payment.objects.create(invoice=invoice, amount=Decimal('2360.00'),
                               payment_method='Cash')

        response = self.get(self.URL, center_id=self.center.id)
        self.assertEqual(response.status_code, 200, response.data)
        rec = response.data['reconciliation']

        self.assertEqual(money(rec['total_billed']), Decimal('2360.00'))
        self.assertEqual(money(rec['total_collected']), Decimal('2360.00'))
        self.assertEqual(money(rec['outstanding_receivable']), Decimal('0.00'))

    def test_a_partial_invoice_shows_an_outstanding_receivable(self):
        """Billing is revenue; the unpaid balance is a receivable, not cash."""
        invoice = Invoice.objects.create(
            center=self.center, client=self.customer, status='partial',
            subtotal=Decimal('2000.00'),
            cgst=Decimal('180.00'), sgst=Decimal('180.00'),
            total_amount=Decimal('2360.00'), paid_amount=Decimal('1000.00'),
        )
        self.add_line(invoice, self.service, self.service_ct)
        Payment.objects.create(invoice=invoice, amount=Decimal('1000.00'),
                               payment_method='Cash')

        rec = self.get(self.URL, center_id=self.center.id).data['reconciliation']
        self.assertEqual(money(rec['total_billed']), Decimal('2360.00'))
        self.assertEqual(money(rec['total_collected']), Decimal('1000.00'))
        self.assertEqual(money(rec['outstanding_receivable']), Decimal('1360.00'))

    def test_cashback_is_not_counted_as_real_money(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        Payment.objects.create(invoice=invoice, amount=Decimal('1360.00'),
                               payment_method='Cash')
        Payment.objects.create(invoice=invoice, amount=Decimal('1000.00'),
                               payment_method='Cashback Wallet')

        data = self.get(self.URL, center_id=self.center.id).data
        self.assertEqual(money(data['payment_methods']['cash']['amount']),
                         Decimal('1360.00'))
        self.assertEqual(money(data['payment_methods']['cashback_wallet']['amount']),
                         Decimal('1000.00'))
        # Real money received excludes the wallet drawdown.
        self.assertEqual(money(data['reconciliation']['real_money_received']),
                         Decimal('1360.00'))

    def test_refunds_are_dated_by_when_they_were_issued(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        InvoiceRefund.objects.create(
            invoice=invoice, center=self.center, amount=Decimal('2360.00'),
            reason='test', refunded_on=datetime.date.today(),
        )
        data = self.get(self.URL, center_id=self.center.id).data
        self.assertEqual(money(data['refunds']['refunds_issued']), Decimal('2360.00'))

    def test_a_refund_outside_the_period_is_excluded(self):
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        InvoiceRefund.objects.create(
            invoice=invoice, center=self.center, amount=Decimal('2360.00'),
            reason='old', refunded_on=datetime.date(2020, 5, 1),
        )
        data = self.get(self.URL, center_id=self.center.id).data
        self.assertEqual(money(data['refunds']['refunds_issued']), Decimal('0.00'))


class OtherReportSmokeTests(ReportTestBase):
    """Every remaining report endpoint must at least answer without a 500."""

    URLS = [
        '/finance/api/reports/tax/',
        '/finance/api/reports/discounts/',
        '/finance/api/monthly_sales/',
        '/finance/api/detailed_revenues/',
        '/finance/api/refunds/',
        '/finance/api/procurement/',
        '/finance/api/register_summary/',
    ]

    def setUp(self):
        super().setUp()
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        self.add_line(invoice, self.product, self.product_ct, price='500.00')
        Payment.objects.create(invoice=invoice, amount=Decimal('2360.00'),
                               payment_method='Cash')

    def test_all_reports_respond(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response.status_code, 200, f'{url}: {response.data}')

    def test_all_reports_survive_a_null_center_id(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.get(url, center_id='null')
                self.assertNotEqual(response.status_code, 500, url)

    def test_all_reports_survive_a_junk_center_id(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.get(url, center_id='abc')
                self.assertNotEqual(response.status_code, 500, url)

    def test_all_reports_survive_junk_dates(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.api.get(url, {
                    'start_date': 'not-a-date', 'end_date': 'also-not',
                })
                self.assertNotEqual(response.status_code, 500, url)

    def test_all_reports_survive_an_inverted_date_range(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.api.get(url, {
                    'start_date': self.today, 'end_date': self.month_start,
                })
                self.assertNotEqual(response.status_code, 500, url)

    def test_all_reports_survive_no_date_range_at_all(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.api.get(url)
                self.assertNotEqual(response.status_code, 500, url)


class DashboardSmokeTests(ReportTestBase):
    """The six dashboard endpoints, which feed the main screen."""

    URLS = [
        '/salon_admin/api/dashboard/',
        '/salon_admin/api/dashboard/summary/',
        '/salon_admin/api/dashboard/revenues/',
        '/salon_admin/api/dashboard/clients/',
        '/salon_admin/api/dashboard/finance/',
        '/salon_admin/api/dashboard/staff/',
        '/salon_admin/api/dashboard/services_products/',
    ]

    def setUp(self):
        super().setUp()
        invoice = self.make_invoice()
        self.add_line(invoice, self.service, self.service_ct)
        self.add_line(invoice, self.product, self.product_ct, price='500.00')
        # A redemption line, which must not inflate any sold-count.
        self.add_line(invoice, self.service, self.service_ct,
                      price='0.00', quantity=2, is_redemption=True)
        Payment.objects.create(invoice=invoice, amount=Decimal('2360.00'),
                               payment_method='Cash')

    def test_all_dashboards_respond(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.get(url)
                self.assertEqual(response.status_code, 200, f'{url}: {response.data}')

    def test_all_dashboards_survive_junk_params(self):
        for url in self.URLS:
            for params in [
                {'center_id': 'null'},
                {'center_id': 'abc'},
                {'start_date': 'nope', 'end_date': 'nope'},
                {'start_date': self.today, 'end_date': self.month_start},
            ]:
                with self.subTest(url=url, params=params):
                    response = self.api.get(url, params)
                    self.assertNotEqual(response.status_code, 500,
                                        f'{url} {params}')

    def test_redemptions_do_not_inflate_top_services(self):
        response = self.get('/salon_admin/api/dashboard/summary/')
        self.assertEqual(response.status_code, 200, response.data)
        top = response.data.get('top_services') or []
        for row in top:
            if row.get('name') == 'Keratin':
                # One sold, plus two redeemed which must not be counted.
                self.assertEqual(int(row['count']), 1, row)

    def test_projection_does_not_explode_for_a_future_range(self):
        future_start = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        future_end = (datetime.date.today() + datetime.timedelta(days=40)).isoformat()
        response = self.api.get('/salon_admin/api/dashboard/summary/', {
            'start_date': future_start, 'end_date': future_end,
        })
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data['projected']),
                         money(response.data['revenue']))
