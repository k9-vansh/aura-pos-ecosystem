"""
A report opened with no filter must not scan the whole history.

`_get_filtered_invoices` applied a date filter only when one was supplied, so a
request with no dates returned every invoice ever raised - and every InvoiceItem
joined to it. That is harmless on day one and ruinous in year three, on a single
worker, while the tills are trying to bill.

The default is now the current month, which is what these screens open on anyway.
An explicit date always wins, so nothing an operator asked for is narrowed, and
`?all_time=true` is there for the rare genuine case.
"""

import datetime
from pos_backend.testing import bill_and_pay
from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class ReportWindowTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('1000.00'),
            tax_percentage=Decimal('5.00'))

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.today = datetime.date.today()
        self.first_of_month = self.today.replace(day=1)

    def bill(self, when=None):
        response = bill_and_pay(self.api, {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'paid', 'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id, 'quantity': 1,
                'unit_price': '1000.00', 'discount': '0',
                'tax_percentage': '5.00', 'description': 'Haircut',
                'staff': self.staff.id,
            }],
            'payments': [{'payment_method': 'Cash', 'amount': '1050.00'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data['id']
        if when:
            Invoice.objects.filter(pk=invoice_id).update(
                created_at=datetime.datetime.combine(when, datetime.time(12, 0)))
        return invoice_id

    def register_summary(self, **params):
        response = self.api.get('/finance/api/register_summary/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def services_revenue(self, **params):
        """Taxable service revenue in scope, which is what the window changes."""
        data = self.register_summary(**params)
        revenues = data.get('revenues', {})
        entry = revenues.get('services', 0)
        value = entry.get('amount', 0) if isinstance(entry, dict) else entry
        return Decimal(str(value or 0))

    # ── the default ───────────────────────────────────────────────────────

    def test_with_no_dates_only_this_month_is_counted(self):
        """The whole point: an unfiltered report used to scan everything."""
        self.bill()                                              # this month
        self.bill(when=self.first_of_month - datetime.timedelta(days=90))
        self.bill(when=self.first_of_month - datetime.timedelta(days=400))

        # Only the current month's bill is in scope.
        self.assertEqual(self.services_revenue(), Decimal('1000.00'))

    def test_an_explicit_range_always_wins(self):
        """An operator asking for last year must get last year."""
        old_date = self.first_of_month - datetime.timedelta(days=400)
        self.bill(when=old_date)

        self.assertEqual(
            self.services_revenue(
                start_date=(old_date - datetime.timedelta(days=1)).isoformat(),
                end_date=(old_date + datetime.timedelta(days=1)).isoformat()),
            Decimal('1000.00'))

    def test_a_start_date_alone_is_respected(self):
        old_date = self.first_of_month - datetime.timedelta(days=90)
        self.bill(when=old_date)
        self.bill()

        # Both bills, because the caller set the lower bound themselves.
        self.assertEqual(
            self.services_revenue(
                start_date=(old_date - datetime.timedelta(days=1)).isoformat()),
            Decimal('2000.00'))

    def test_all_time_is_available_for_the_rare_genuine_case(self):
        self.bill(when=self.first_of_month - datetime.timedelta(days=400))
        self.bill()

        self.assertEqual(self.services_revenue(all_time='true'), Decimal('2000.00'))

    def test_a_junk_date_does_not_silently_become_all_time(self):
        """An unparseable date is ignored - it must fall back to the default
        window, not to scanning everything."""
        self.bill(when=self.first_of_month - datetime.timedelta(days=400))
        self.bill()

        self.assertEqual(self.services_revenue(start_date='not-a-date'),
                         Decimal('1000.00'))

    # ── the cost ─────────────────────────────────────────────────────────

    def test_an_unfiltered_report_does_not_grow_with_history(self):
        for offset in (30, 60, 90, 120, 400, 500):
            self.bill(when=self.first_of_month - datetime.timedelta(days=offset))
        self.bill()

        with CaptureQueriesContext(connection) as captured:
            self.register_summary()

        # The count is what matters less than the SCOPE: assert the report saw
        # one month, so the query cannot be walking six months of history.
        self.assertEqual(self.services_revenue(), Decimal('1000.00'))
        self.assertLess(len(captured), 60, f'{len(captured)} queries for one report')

    def test_branch_scoping_is_unaffected(self):
        other = Center.objects.create(center_name='Other')
        other_customer = Client.objects.create(
            first_name='Bela', phone='9990000002', center=other)
        response = bill_and_pay(self.api, {
            'client': other_customer.id, 'center': other.id,
            'status': 'paid', 'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id, 'quantity': 1,
                'unit_price': '1000.00', 'discount': '0',
                'tax_percentage': '5.00', 'description': 'Haircut',
            }],
            'payments': [{'payment_method': 'Cash', 'amount': '1050.00'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.bill()

        self.assertEqual(self.services_revenue(center_id=self.center.id),
                         Decimal('1000.00'))
