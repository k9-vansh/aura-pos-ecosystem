"""
Tests for the previously-unread model fields and the dashboard aggregates.

Covers M16 (Center.is_active / gst_enabled declared but never read),
M22 (service revenue grouped by description), M23 (package liability priced at
the original purchase price) and M24 (revenue projection).
"""

import datetime
from pos_backend.testing import bill_and_pay
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem
from clients.models import Client, ClientPackage
from marketing.models import Package
from pos_backend.money import money
from salon_admin.dashboard_endpoints import _outstanding_package_liability
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class CenterSoftDeleteTests(TestCase):
    """M16 - Center.is_active was set by delete() and read by nothing."""

    def setUp(self):
        self.active = Center.objects.create(center_name='Open', display_name='Open')
        self.closed = Center.objects.create(center_name='Closed', display_name='Closed')

    def test_default_manager_hides_a_disabled_center(self):
        self.closed.delete()
        names = set(Center.objects.values_list('center_name', flat=True))
        self.assertIn('Open', names)
        self.assertNotIn('Closed', names)

    def test_delete_does_not_remove_the_row(self):
        self.closed.delete()
        self.assertTrue(Center.all_objects.filter(pk=self.closed.pk).exists())
        self.closed.refresh_from_db()
        self.assertFalse(self.closed.is_active)

    def test_all_objects_still_sees_it(self):
        self.closed.delete()
        self.assertEqual(Center.all_objects.count(), 2)

    def test_reactivate_brings_it_back(self):
        self.closed.delete()
        self.closed.reactivate()
        self.assertIn('Closed', set(Center.objects.values_list('center_name', flat=True)))

    def test_a_historical_invoice_can_still_resolve_its_closed_center(self):
        """base_manager_name keeps related traversal working."""
        invoice = Invoice.objects.create(
            center=self.closed, status='paid',
            subtotal=Decimal('100'), total_amount=Decimal('100'),
        )
        self.closed.delete()
        invoice.refresh_from_db()
        self.assertIsNotNone(invoice.center)
        self.assertEqual(invoice.center.center_name, 'Closed')


class GstEnabledTests(TestCase):
    """M16 - Center.gst_enabled was declared and read by nothing."""

    def setUp(self):
        self.gst_center = Center.objects.create(center_name='GST', gst_enabled=True)
        self.no_gst_center = Center.objects.create(center_name='No GST', gst_enabled=False)

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner', role=self.owner_role,
        )
        self.service = ServiceMaster.objects.create(
            name='Haircut', category='Cuts',
            default_price=Decimal('1000.00'), tax_percentage=Decimal('5.00'),
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def payload(self, center):
        return {
            'client': None,
            'center': center.id,
            'status': 'paid',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id,
                'description': 'Haircut',
                'unit_price': '1000.00',
                'quantity': 1,
            }],
            'payments': [],
        }

    def test_gst_center_charges_tax(self):
        response = bill_and_pay(self.api, self.payload(self.gst_center),
                                 format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.cgst) + money(invoice.sgst), Decimal('50.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('1050.00'))

    def test_non_gst_center_charges_no_tax(self):
        response = bill_and_pay(self.api, self.payload(self.no_gst_center),
                                 format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.cgst) + money(invoice.sgst), Decimal('0.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('1000.00'))
        # And the line agrees with the header.
        item = invoice.items.first()
        self.assertEqual(money(item.tax_amount), Decimal('0.00'))
        self.assertEqual(money(item.total_price), Decimal('1000.00'))


class PackageLiabilityTests(TestCase):
    """M23 - liability was Sum(package__price), ignoring consumption."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main')
        self.client_obj = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center,
        )
        self.service_a = ServiceMaster.objects.create(
            name='Facial', category='Skin', default_price=Decimal('1000.00'),
        )
        self.service_b = ServiceMaster.objects.create(
            name='Haircut', category='Cuts', default_price=Decimal('500.00'),
        )
        self.template = Package.objects.create(
            name='Bundle', price=Decimal('12000.00'), validity_days=180,
        )

    def make_package(self, remaining, package=None):
        return ClientPackage.objects.create(
            client=self.client_obj, package=package,
            services_remaining=remaining,
            expiry_date=datetime.date.today() + datetime.timedelta(days=90),
        )

    def test_liability_reflects_what_is_left(self):
        # 2 facials (Rs 1000) + 3 haircuts (Rs 500) left = Rs 3500,
        # not the Rs 12,000 the package originally cost.
        self.make_package({str(self.service_a.id): 2, str(self.service_b.id): 3},
                          package=self.template)
        result = _outstanding_package_liability(ClientPackage.objects.all())
        self.assertEqual(result['total'], Decimal('3500.00'))
        self.assertEqual(result['count'], 1)

    def test_a_fully_consumed_package_carries_no_liability(self):
        self.make_package({str(self.service_a.id): 0}, package=self.template)
        result = _outstanding_package_liability(ClientPackage.objects.all())
        self.assertEqual(result['total'], Decimal('0.00'))

    def test_custom_packages_are_included(self):
        """A custom bundle has package=None and was excluded entirely."""
        self.make_package({str(self.service_a.id): 1}, package=None)
        result = _outstanding_package_liability(ClientPackage.objects.all())
        self.assertEqual(result['total'], Decimal('1000.00'))

    def test_string_quantities_in_the_json_do_not_break_it(self):
        self.make_package({str(self.service_a.id): '2'}, package=self.template)
        result = _outstanding_package_liability(ClientPackage.objects.all())
        self.assertEqual(result['total'], Decimal('2000.00'))

    def test_unknown_service_ids_are_skipped(self):
        self.make_package({'999999': 5}, package=self.template)
        result = _outstanding_package_liability(ClientPackage.objects.all())
        self.assertEqual(result['total'], Decimal('0.00'))

    def test_no_packages_returns_zero(self):
        result = _outstanding_package_liability(ClientPackage.objects.none())
        self.assertEqual(result['total'], Decimal('0.00'))
        self.assertEqual(result['count'], 0)


class ServiceRevenueGroupingTests(TestCase):
    """M22 - grouping by description split one service across several rows."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )
        self.service = ServiceMaster.objects.create(
            name='Haircut', category='Cuts', default_price=Decimal('1000.00'),
        )
        self.service_ct = ContentType.objects.get_for_model(ServiceMaster)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_varied_descriptions_collapse_into_one_row(self):
        invoice = Invoice.objects.create(
            center=self.center, status='paid',
            subtotal=Decimal('3000.00'), total_amount=Decimal('3000.00'),
            paid_amount=Decimal('3000.00'),
        )
        # The same service, three different labels - exactly what produced
        # duplicate rows in the old report.
        for label in ['Haircut', 'Haircut ', 'Haircut (Senior)']:
            InvoiceItem.objects.create(
                invoice=invoice, content_type=self.service_ct,
                object_id=self.service.id, description=label,
                unit_price=Decimal('1000.00'), quantity=1,
                total_price=Decimal('1000.00'),
            )

        response = self.api.get('/salon_admin/api/dashboard/',
                                {'center_id': self.center.id})
        self.assertEqual(response.status_code, 200, response.data)

        breakdown = response.data.get('service_revenue_breakdown', [])
        haircut_rows = [r for r in breakdown if r.get('service_id') == self.service.id]
        self.assertEqual(len(haircut_rows), 1, breakdown)
        self.assertEqual(money(haircut_rows[0]['revenue']), Decimal('3000.00'))
        self.assertEqual(haircut_rows[0]['name'], 'Haircut')


class RevenueProjectionTests(TestCase):
    """M24 - a future start date multiplied revenue by the month length."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        Invoice.objects.create(
            center=self.center, status='paid',
            subtotal=Decimal('1000.00'), total_amount=Decimal('1000.00'),
            paid_amount=Decimal('1000.00'),
        )

    def summary(self, start, end):
        response = self.api.get('/salon_admin/api/dashboard/summary/',
                                {'start_date': start, 'end_date': end})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_a_completed_period_projects_to_its_actual(self):
        past_start = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
        past_end = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        data = self.summary(past_start, past_end)
        self.assertEqual(money(data['projected']), money(data['revenue']))

    def test_a_future_period_does_not_invent_a_projection(self):
        """The old formula turned days_elapsed into 1 and multiplied by ~30."""
        future_start = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        future_end = (datetime.date.today() + datetime.timedelta(days=40)).isoformat()
        data = self.summary(future_start, future_end)
        self.assertEqual(money(data['projected']), money(data['revenue']))

    def test_an_inverted_range_does_not_project(self):
        later = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        earlier = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        data = self.summary(later, earlier)
        self.assertEqual(money(data['projected']), money(data['revenue']))

    def test_a_partial_period_projects_within_the_selected_range(self):
        start = (datetime.date.today() - datetime.timedelta(days=4)).isoformat()
        end = (datetime.date.today() + datetime.timedelta(days=5)).isoformat()
        data = self.summary(start, end)
        revenue = money(data['revenue'])
        projected = money(data['projected'])
        # 5 of 10 days elapsed: roughly double, and never more than the whole
        # range's worth of the observed daily rate.
        self.assertGreaterEqual(projected, revenue)
        self.assertLessEqual(projected, revenue * 10)
