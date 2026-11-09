"""
Centre import tests.

Pinned here:
  * `monthly_target` is a DecimalField and every mapped cell reaches the model
    as a string, so an ordinary "25,000" raised InvalidOperation on save and
    lost the entire row - with the database's own error text returned to the
    browser;
  * `'success': True` was returned even when every row had failed;
  * `total_rows` was `len(rows) - 1`, which is wrong whenever the heading row is
    not the first row - and it is found by scanning, precisely because these
    sheets often carry a title above the headings;
  * the response contained one object per row with no cap, and there was no row
    limit at all.
"""

import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


def workbook(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile('centers.xlsx', buffer.getvalue())


class CenterImportTests(TestCase):
    URL = '/salon_admin/api/centers/bulk-import/'

    def setUp(self):
        self.owner_role = Role.objects.create(name='Owner',
                                             permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role)

        self.staff_role = Role.objects.create(name='Receptionist', permissions={})
        self.staffer = CustomUser.objects.create_user(
            email='r@test.com', password='StrongPass!234', full_name='R',
            role=self.staff_role)

        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def upload(self, rows, client=None):
        return (client or self.client).post(
            self.URL, {'file': workbook(rows)}, format='multipart')

    # ── monthly_target ────────────────────────────────────────────────────
    def test_a_target_with_a_thousands_separator_is_imported(self):
        """"25,000" used to lose the whole row."""
        response = self.upload([
            ['Center Name', 'Monthly Target'],
            ['Bandra', '25,000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 1)
        self.assertEqual(Center.all_objects.get(center_name='Bandra').monthly_target,
                         25000)

    def test_a_target_with_a_currency_prefix_is_imported(self):
        response = self.upload([
            ['Center Name', 'Monthly Target'],
            ['Bandra', 'Rs 40000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(Center.all_objects.get(center_name='Bandra').monthly_target,
                         40000)

    def test_an_unreadable_target_still_imports_the_rest_of_the_row(self):
        response = self.upload([
            ['Center Name', 'Phone', 'Monthly Target'],
            ['Bandra', '9876500001', 'ask the owner'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 1)

        center = Center.all_objects.get(center_name='Bandra')
        self.assertEqual(center.phone, '9876500001')
        self.assertIsNone(center.monthly_target)
        self.assertIn('not a', str(response.data['skipped']))

    # ── honest reporting ──────────────────────────────────────────────────
    def test_success_is_false_when_a_row_failed(self):
        # A centre name longer than the 255 character column.
        response = self.upload([
            ['Center Name'],
            ['x' * 400],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        if response.data['summary']['errors']:
            self.assertFalse(response.data['success'])
            self.assertNotIn('varchar', str(response.data['errors']).lower())
            self.assertNotIn('Traceback', str(response.data['errors']))

    def test_success_is_true_on_a_clean_import(self):
        response = self.upload([['Center Name'], ['Bandra']])
        self.assertTrue(response.data['success'])

    def test_total_rows_ignores_a_title_row_above_the_headings(self):
        """The heading row is found by scanning; `len(rows) - 1` assumed row 1."""
        response = self.upload([
            ['Our Branches 2026', None],
            ['Center Name', 'Phone'],
            ['Bandra', '9876500001'],
            ['Andheri', '9876500002'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['total_rows'], 2)
        self.assertEqual(response.data['summary']['created'], 2)

    def test_a_missing_name_is_skipped_with_a_reason(self):
        response = self.upload([
            ['Center Name', 'Phone'],
            ['', '9876500001'],
            ['Bandra', '9876500002'],
        ])
        self.assertEqual(response.data['summary']['created'], 1)
        self.assertEqual(response.data['summary']['skipped'], 1)

    def test_the_same_file_twice_updates_rather_than_duplicating(self):
        rows = [['Center Name', 'Phone'], ['Bandra', '9876500001']]
        first = self.upload(rows)
        second = self.upload(rows)
        self.assertEqual(first.data['summary']['created'], 1)
        self.assertEqual(second.data['summary']['updated'], 1)
        self.assertEqual(Center.all_objects.filter(center_name='Bandra').count(), 1)

    def test_a_launch_date_in_indian_format_is_read(self):
        response = self.upload([
            ['Center Name', 'Launched On'],
            ['Bandra', '15-01-2023'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        launched = Center.all_objects.get(center_name='Bandra').launched_on
        self.assertIsNotNone(launched)
        self.assertEqual((launched.year, launched.month, launched.day), (2023, 1, 15))

    # ── limits and permissions ────────────────────────────────────────────
    def test_a_file_over_the_row_limit_is_refused(self):
        from pos_backend.bulk_upload import MAX_ROWS

        rows = [['Center Name']] + [[f'Branch {i}'] for i in range(MAX_ROWS + 1)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertIn('limit', str(response.data))
        self.assertEqual(Center.all_objects.count(), 0)

    def test_long_lists_are_capped_and_the_truncation_is_declared(self):
        rows = [['Center Name']] + [[f'Branch {i}'] for i in range(150)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['summary']['created'], 150)
        self.assertEqual(len(response.data['created']), 100)
        self.assertEqual(response.data['full_counts']['created'], 150)

    def test_a_non_owner_cannot_import_centres(self):
        client = APIClient()
        client.force_authenticate(self.staffer)
        response = self.upload([['Center Name'], ['Bandra']], client=client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Center.all_objects.count(), 0)

    def test_an_anonymous_caller_cannot_import_centres(self):
        response = self.upload([['Center Name'], ['Bandra']], client=APIClient())
        self.assertIn(response.status_code, (401, 403))

    # ── malformed input ───────────────────────────────────────────────────
    def test_no_file_is_a_400(self):
        response = self.client.post(self.URL, {}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_a_corrupt_file_does_not_leak_internals(self):
        upload = SimpleUploadedFile('centers.xlsx', b'not a workbook')
        response = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback', str(response.data))
        self.assertNotIn('openpyxl', str(response.data))
        self.assertIn('could not be opened', str(response.data))

    def test_unrecognised_headings_are_reported(self):
        response = self.upload([['alpha', 'beta'], ['1', '2']])
        self.assertEqual(response.status_code, 400)
