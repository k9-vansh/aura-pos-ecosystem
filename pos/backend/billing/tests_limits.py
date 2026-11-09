"""A bill, an order and a card stay inside what the database can hold."""
import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from inventory.models import Product, PurchaseOrder, Vendor
from marketing.models import Membership
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster


class LimitTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', role=role, center=self.center, is_superuser=True)
        self.api = APIClient()
        self.api.force_authenticate(owner)
        self.service = ServiceMaster.objects.create(name='Cut', default_price=Decimal('500'), tax_percentage=Decimal('18'))
        self.product = Product.objects.create(name='Serum', price=Decimal('300'), mrp=Decimal('500'), gst_percent=Decimal('18'),
                                              current_stock=50, center=self.center)

    def bill(self, qty, status='draft', item='service'):
        obj = self.service if item == 'service' else self.product
        ct = 'services.servicemaster' if item == 'service' else 'inventory.product'
        return self.api.post('/billing/invoices/', {
            'center': self.center.id, 'status': status, 'discount': '0',
            'items': [{'content_type': ct, 'object_id': obj.id, 'description': 'x', 'unit_price': str(obj.default_price if item == 'service' else obj.mrp),
                       'quantity': qty, 'discount': '0'}]}, format='json')

    def test_a_silly_quantity_is_refused_not_saved(self):
        for qty in (99999999999, 100001, 10 ** 12):
            r = self.bill(qty)
            self.assertEqual(r.status_code, 400, qty)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_a_normal_quantity_is_fine(self):
        self.assertEqual(self.bill(3).status_code, 201)

    def test_a_bill_too_big_for_its_columns_is_refused(self):
        self.service.default_price = Decimal('99999999.00')
        self.service.save()
        r = self.bill(5)                                  # 5 x 99,999,999 is far over the limit
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_an_order_line_with_a_silly_quantity_is_refused(self):
        vendor = Vendor.objects.create(name='V', center=self.center)
        r = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': vendor.id, 'center': self.center.id, 'invoice_number': 'X1',
            'items': [{'product': self.product.id, 'quantity': '99999999999', 'rate': '100',
                       'discount_percent': '0', 'tax_percent': '18'}]}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertEqual(PurchaseOrder.objects.count(), 0)

    def test_an_order_line_whose_total_overflows_is_refused(self):
        vendor = Vendor.objects.create(name='V', center=self.center)
        r = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': vendor.id, 'center': self.center.id, 'invoice_number': 'X2',
            'items': [{'product': self.product.id, 'quantity': '999999', 'rate': '99999999',
                       'discount_percent': '0', 'tax_percent': '18'}]}, format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_a_stock_request_quantity_must_be_a_positive_sensible_number(self):
        for bad in ('-5', '0', '99999999999'):
            r = self.api.post('/inventory/api/stock_requests/', {
                'requesting_center': self.center.id, 'stock_type': 'Retail',
                'items': [{'product': self.product.id, 'quantity': bad}]}, format='json')
            self.assertEqual(r.status_code, 400, bad)

    def test_a_card_or_membership_cannot_last_beyond_a_hundred_years(self):
        r = self.api.post('/marketing/api/memberships/', {
            'name': 'Forever', 'expiry_days': 99999999999, 'post_tax_price': '1000', 'pre_tax_price': '847.46',
            'gst_percent': '18', 'discount_percent': '10'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('expiry_days', r.data)
        self.assertFalse(Membership.objects.exists())
