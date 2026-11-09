"""
Service price-list import tests.

The two findings that change what customers are charged:

  * `existing.tax_percentage = tax_percentage` ran on every row, and the
    fallback when the file has no Tax column is 5%. Re-importing a price list
    without a Tax column reset every matched service's GST rate to 5%.
  * a blank Price became 0.0, so a new service was created at zero and then
    billed at zero - the same shape as the zero-rupee invoice lines already
    found in billing.

Also pinned: no permission check on `center_id` (one branch could rewrite
another branch's prices), and `int('null')` failing the whole upload.
"""

import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import CenterService, ServiceMaster


def workbook(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile('services.xlsx', buffer.getvalue())


class ServiceImportTestBase(TestCase):
    URL = '/services/api/master/bulk_upload/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main Center',
                                            display_name='Main')
        self.other = Center.objects.create(center_name='Second Center')

        self.owner_role = Role.objects.create(name='Owner',
                                              permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center)

        # The service catalogue is Admin > Services.
        self.manager_role = Role.objects.create(name='Manager', permissions={
            'admin': {'services': {'read': True, 'create': True, 'update': True, 'delete': True}},
        })
        self.manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='Manager',
            role=self.manager_role, center=self.center)

        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def upload(self, rows, client=None, **extra):
        return (client or self.client).post(
            self.URL, {'file': workbook(rows), **extra}, format='multipart')


class TaxRateTests(ServiceImportTestBase):
    def test_a_file_without_a_tax_column_leaves_gst_alone(self):
        """This reset every matched service to 5% GST."""
        service = ServiceMaster.objects.create(
            name='Haircut', default_price=500, tax_percentage=18)

        response = self.upload([
            ['Service Name', 'Price'],
            ['Haircut', '600'],
        ])
        self.assertEqual(response.status_code, 200, response.data)

        service.refresh_from_db()
        self.assertEqual(service.tax_percentage, 18)
        self.assertIn('tax_note', response.data)

    def test_a_stated_tax_rate_is_applied(self):
        service = ServiceMaster.objects.create(
            name='Haircut', default_price=500, tax_percentage=18)
        response = self.upload([
            ['Service Name', 'Tax'],
            ['Haircut', '12'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        service.refresh_from_db()
        self.assertEqual(service.tax_percentage, 12)

    def test_a_percent_sign_is_tolerated(self):
        service = ServiceMaster.objects.create(name='Haircut', tax_percentage=5)
        self.upload([['Service Name', 'Tax'], ['Haircut', '18%']])
        service.refresh_from_db()
        self.assertEqual(service.tax_percentage, 18)

    def test_a_tax_rate_of_one_stays_one_percent(self):
        """`if 0 < gst <= 1: gst *= 100` turned 1% into 100% in the sibling
        product importer; this asserts the shared helper does not."""
        service = ServiceMaster.objects.create(name='Haircut', tax_percentage=18)
        self.upload([['Service Name', 'Tax'], ['Haircut', '1']])
        service.refresh_from_db()
        self.assertEqual(service.tax_percentage, 1)

    def test_an_unreadable_tax_rate_warns_and_changes_nothing(self):
        service = ServiceMaster.objects.create(name='Haircut', tax_percentage=18)
        response = self.upload([['Service Name', 'Tax'], ['Haircut', 'eighteen']])
        service.refresh_from_db()
        self.assertEqual(service.tax_percentage, 18)
        self.assertTrue(
            any('tax rate' in w for w in response.data.get('warnings', [])),
            response.data)

    def test_a_new_service_without_a_tax_column_keeps_the_model_default(self):
        response = self.upload([['Service Name', 'Price'], ['Facial', '900']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(ServiceMaster.objects.get(name='Facial').tax_percentage, 5)


class PriceTests(ServiceImportTestBase):
    def test_a_new_service_with_no_price_is_not_created_at_zero(self):
        """A zero-rupee service is billed at zero."""
        response = self.upload([['Service Name', 'Category'], ['Facial', 'Skin']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 0)
        self.assertEqual(response.data['skipped'], 1)
        self.assertIn('no price', str(response.data['warnings']))
        self.assertFalse(ServiceMaster.objects.filter(name='Facial').exists())

    def test_a_negative_price_is_skipped(self):
        response = self.upload([['Service Name', 'Price'], ['Facial', '-100']])
        self.assertEqual(response.data['skipped'], 1)
        self.assertFalse(ServiceMaster.objects.filter(name='Facial').exists())

    def test_a_center_upload_writes_the_override_not_the_org_price(self):
        service = ServiceMaster.objects.create(name='Haircut', default_price=500)
        response = self.upload(
            [['Service Name', 'Price'], ['Haircut', '700']],
            center_id=self.center.id)
        self.assertEqual(response.status_code, 200, response.data)

        service.refresh_from_db()
        self.assertEqual(service.default_price, 500)
        self.assertEqual(
            CenterService.objects.get(center=self.center, service=service).price, 700)

    def test_an_ambiguous_branch_updates_the_org_price_and_says_so(self):
        """The owner can see two branches and named neither.

        This used to take `.first()` of their accessible centers and write a
        center-specific override for that one branch, leaving the other on the
        old price while reporting success.
        """
        service = ServiceMaster.objects.create(name='Haircut', default_price=500)

        response = self.upload([['Service Name', 'Price'], ['Haircut', '700']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(response.data['center'])
        self.assertIn('organisation-wide', response.data['note'])

        service.refresh_from_db()
        self.assertEqual(service.default_price, 700)
        self.assertFalse(CenterService.objects.exists())

    def test_a_single_branch_user_needs_no_center_id(self):
        """One accessible branch is unambiguous, so it is still assumed."""
        service = ServiceMaster.objects.create(name='Haircut', default_price=500)
        self.other.delete()

        client = APIClient()
        client.force_authenticate(self.manager)
        response = self.upload([['Service Name', 'Price'], ['Haircut', '700']],
                               client=client)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            CenterService.objects.get(center=self.center, service=service).price, 700)
        service.refresh_from_db()
        self.assertEqual(service.default_price, 500)

    def test_a_center_row_with_no_price_inherits_the_org_price(self):
        service = ServiceMaster.objects.create(name='Haircut', default_price=450)
        self.upload([['Service Name', 'Category'], ['Haircut', 'Hair']],
                    center_id=self.center.id)
        self.assertEqual(
            CenterService.objects.get(center=self.center, service=service).price, 450)

    def test_an_existing_center_override_is_not_reset_by_a_priceless_row(self):
        service = ServiceMaster.objects.create(name='Haircut', default_price=450)
        CenterService.objects.create(center=self.center, service=service, price=800)
        self.upload([['Service Name', 'Category'], ['Haircut', 'Hair']],
                    center_id=self.center.id)
        self.assertEqual(
            CenterService.objects.get(center=self.center, service=service).price, 800)


class PermissionTests(ServiceImportTestBase):
    def test_a_manager_cannot_import_into_another_branch(self):
        """There was no permission check on the resolved center at all."""
        response = self.upload(
            [['Service Name', 'Price'], ['Haircut', '700']],
            client=self._as_manager(), center_id=self.other.id)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(CenterService.objects.filter(center=self.other).exists())

    def test_a_manager_can_import_into_their_own_branch(self):
        response = self.upload(
            [['Service Name', 'Price'], ['Haircut', '700']],
            client=self._as_manager(), center_id=self.center.id)
        self.assertEqual(response.status_code, 200, response.data)

    def _as_manager(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        return client


class MalformedFileTests(ServiceImportTestBase):
    def test_a_junk_center_id_is_not_a_500(self):
        for value in ('abc', 'null', 'undefined', ''):
            response = self.upload(
                [['Service Name', 'Price'], ['Haircut', '700']], center_id=value)
            self.assertLess(response.status_code, 500, value)

    def test_unrecognised_headings_are_reported(self):
        response = self.upload([['alpha', 'beta'], ['1', '2']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('columns_found', response.data)

    def test_a_missing_name_column_is_reported(self):
        response = self.upload([['Price', 'Tax'], ['100', '18']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('need_one_of', response.data)

    def test_no_file_is_a_400(self):
        response = self.client.post(self.URL, {}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_a_corrupt_file_does_not_leak_a_traceback(self):
        upload = SimpleUploadedFile('services.xlsx', b'not a workbook')
        response = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback', str(response.data))

    def test_a_blank_service_name_is_skipped_with_a_reason(self):
        response = self.upload([
            ['Service Name', 'Price'],
            ['', '500'],
            ['Haircut', '500'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)
        self.assertEqual(response.data['skipped'], 1)

    def test_the_same_file_twice_updates_rather_than_duplicating(self):
        rows = [['Service Name', 'Price', 'Tax'], ['Haircut', '500', '18']]
        first = self.upload(rows)
        second = self.upload(rows)
        self.assertEqual(first.data['created'], 1)
        self.assertEqual(second.data['updated'], 1)
        self.assertEqual(ServiceMaster.objects.filter(name='Haircut').count(), 1)
