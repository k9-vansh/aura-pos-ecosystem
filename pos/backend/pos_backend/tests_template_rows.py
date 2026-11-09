"""Uploading a downloaded template untouched must not create the template's own sample rows."""
import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, Vendor
from pos_backend.bulk_upload import is_template_row
from salon_admin.models import Center


class TemplateRowTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        owner = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='Owner',
                                               is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)

    def upload(self, url, content, name='t.xlsx'):
        return self.api.post(url, {'file': SimpleUploadedFile(name, content), 'center_id': self.center.id},
                             format='multipart')

    def test_the_help_row_and_the_worked_example_are_recognised(self):
        self.assertTrue(is_template_row({'vendorcode': 'Optional (for update)', 'vendorname': 'Required', 'phone': 'Optional'}))
        self.assertTrue(is_template_row({'vendorcode': 'VND-001', 'vendorname': 'Acme Supplies', 'phone': '9876543210'}))
        self.assertTrue(is_template_row({'productname': 'Loreal Shampoo', 'productcode': 'PRD-001'}))

    def test_real_rows_are_not_mistaken_for_them(self):
        self.assertFalse(is_template_row({'vendorname': 'Required Supplies Pvt Ltd', 'phone': '9810000001'}))
        self.assertFalse(is_template_row({'vendorcode': 'VND-002', 'vendorname': 'Acme Supplies'}))
        self.assertFalse(is_template_row({}))

    def test_the_untouched_vendor_template_creates_nothing(self):
        tpl = self.api.get('/inventory/api/vendors/bulk_upload_template/')
        self.assertEqual(tpl.status_code, 200)
        r = self.upload('/inventory/api/vendors/bulk_upload/', tpl.content)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Vendor.objects.count(), 0)

    def test_the_untouched_product_template_creates_nothing(self):
        tpl = self.api.get('/inventory/api/products/bulk_upload_template/')
        r = self.upload('/inventory/api/products/bulk_upload/', tpl.content)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['created'], 0, r.data)
        self.assertTrue(any('guidance row' in w for w in r.data.get('warnings', [])), r.data)
        self.assertEqual(Product.objects.count(), 0)

    def test_the_untouched_centre_template_creates_nothing(self):
        tpl = self.api.get('/salon_admin/api/centers/bulk-import-template/')
        r = self.upload('/salon_admin/api/centers/bulk-import/', tpl.content)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Center.objects.count(), 1)

    def test_a_real_vendor_row_after_the_template_rows_is_still_imported(self):
        tpl = self.api.get('/inventory/api/vendors/bulk_upload_template/')
        wb = openpyxl.load_workbook(io.BytesIO(tpl.content))
        wb.active.append(['', 'Real Supplies', '', '9810000001', '', '', '', '', '', '', ''])
        out = io.BytesIO()
        wb.save(out)
        r = self.upload('/inventory/api/vendors/bulk_upload/', out.getvalue())
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(list(Vendor.objects.values_list('name', flat=True)), ['Real Supplies'])
