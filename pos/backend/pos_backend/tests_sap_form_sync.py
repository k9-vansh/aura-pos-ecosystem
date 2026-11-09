"""What is made or changed on a POS form reaches SAP (2026-10-31): a product switched off or on, a product's opening
stock, a vendor's edit, stock a staff perk uses up. SAP is FakeSap: no test reaches the network.
"""
import datetime
from decimal import Decimal
from unittest import mock

import requests
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockTransaction, Vendor
from pos_backend import sap_jobs, sap_utils
from pos_backend.models import BackgroundJob
from pos_backend.tests_sap_integrity import FakeResponse, SapTestBase
from salon_admin.models import Center
from staff.models import StaffMember


class FormSyncTests(SapTestBase):
    def setUp(self):
        super().setUp()
        self.other = Center.objects.create(center_name='Noida', sap_warehouse_code='WH-NOIDA')
        admin = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        self.api = APIClient()
        self.api.force_authenticate(admin)

    def keys(self):
        return set(BackgroundJob.objects.values_list('dedupe_key', flat=True))

    def patch_body(self, code):
        (call,) = [c for c in self.sap.calls if c[0] == 'PATCH' and f"Items('{code}')" in c[1]]
        return call[2]

    # a product switched off / on
    def test_editing_a_product_queues_an_item_update_not_a_create(self):
        product = Product.objects.create(name='Serum', product_code='SER1', center=self.center, price=Decimal('100'))
        res = self.api.patch(f'/inventory/api/products/{product.pk}/', {'is_active': False}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn(f'sap:product_update:{product.pk}', self.keys())
        self.assertEqual(sap_jobs._loader('product_update')[1], sap_utils.update_sap_item)

    def test_switching_a_product_off_everywhere_freezes_the_sap_item(self):
        a = Product.objects.create(name='Serum', product_code='SER1', center=self.center, is_active=False)
        Product.objects.create(name='Serum', product_code='SER1', center=self.other, is_active=False)
        self.assertTrue(sap_utils.update_sap_item(a))
        self.assertEqual(self.patch_body('SER1')['Frozen'], 'tYES')

    def test_one_branch_switching_it_off_does_not_freeze_the_item_for_the_others(self):
        a = Product.objects.create(name='Serum', product_code='SER1', center=self.center, is_active=False)
        Product.objects.create(name='Serum', product_code='SER1', center=self.other, is_active=True)
        self.assertTrue(sap_utils.update_sap_item(a))
        self.assertEqual(self.patch_body('SER1')['Frozen'], 'tNO')

    def test_switching_a_product_back_on_unfreezes_it(self):
        a = Product.objects.create(name='Serum', product_code='SER1', center=self.center, is_active=True)
        self.assertTrue(sap_utils.update_sap_item(a))
        self.assertEqual(self.patch_body('SER1')['Frozen'], 'tNO')

    def test_an_item_that_never_reached_sap_is_created_instead_of_failing_the_edit(self):
        product = Product.objects.create(name='Serum', product_code='SER2', center=self.center, price=Decimal('50'))

        def send(method, url, timeout=15, **kwargs):
            if method == 'PATCH':
                raise requests.exceptions.HTTPError(response=FakeResponse(404, {'error': {'message': {'value': 'no'}}}))
            return self.sap(method, url, timeout, **kwargs)
        with mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=send):
            self.assertTrue(sap_utils.update_sap_item(product))
        (post,) = self.sap.posts('/Items')
        self.assertEqual(post[2]['ItemCode'], 'SER2')

    # opening stock
    def test_opening_stock_typed_on_the_product_form_is_queued_as_a_goods_receipt(self):
        res = self.api.post('/inventory/api/products/', {
            'name': 'Mask', 'center': self.center.id, 'price': '200', 'gst_percent': '18', 'current_stock': '5'},
            format='json')
        self.assertEqual(res.status_code, 201, res.content)
        row = StockTransaction.objects.get(product_id=res.data['id'])
        self.assertIn(f'sap:stock_transaction:{row.pk}', self.keys())
        self.assertIn(f"sap:product:{res.data['id']}", self.keys())

    def test_a_product_made_with_no_opening_stock_queues_no_receipt(self):
        res = self.api.post('/inventory/api/products/', {
            'name': 'Mask', 'center': self.center.id, 'price': '200', 'gst_percent': '18'}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        self.assertFalse(any(k.startswith('sap:stock_transaction:') for k in self.keys()))

    # a vendor
    def test_editing_a_vendor_queues_it_and_updates_the_sap_partner(self):
        vendor = Vendor.objects.create(name='Loreal', center=self.center, vendor_code='V9', phone='')
        res = self.api.patch(f'/inventory/api/vendors/{vendor.pk}/', {'name': 'Loreal India', 'phone': '9811100000'},
                             format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertIn(f'sap:vendor:{vendor.pk}', self.keys())
        vendor.refresh_from_db()
        self.assertTrue(sap_utils.create_sap_vendor(vendor))          # the partner exists (FakeSap answers 200)
        (call,) = [c for c in self.sap.calls if c[0] == 'PATCH' and "BusinessPartners('V9')" in c[1]]
        self.assertEqual(call[2], {'CardName': 'Loreal India', 'Phone1': '9811100000'})   # blank fields are left alone
        self.assertEqual(self.sap.posts('BusinessPartners'), [])

    # staff perk
    def test_stock_a_staff_perk_uses_up_is_queued_as_a_goods_issue(self):
        product = Product.objects.create(name='Hair Spa', product_code='HS1', center=self.center, current_stock=5)
        staff = StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist',
                                           salary=Decimal('20000'), allocated_points=100)
        res = self.api.post('/staff/api/consumptions/', {
            'staff': staff.pk, 'center': self.center.pk, 'service_name': 'Hair Spa', 'amount': '10',
            'payment_method': 'Points', 'date': str(datetime.date.today()), 'time': '10:00:00'}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        row = StockTransaction.objects.get(product=product, transaction_type='CHECKOUT')
        self.assertIn(f'sap:stock_transaction:{row.pk}', self.keys())
