"""
Staff import tests.

This is the endpoint the production log shows returning 400 eight times in a
row, with no reason recorded anywhere - the handler returned its error to the
browser and this module had no logging at all.

Pinned here:
  * the downloaded template used to carry its own guidance in the data sheet
    ("Required", "Optional", "Male/Female") plus a worked example row, and the
    importer read both as people, so the template failed on row 2;
  * `center_id=abc` reached `int()` inside `except Center.DoesNotExist` - a 500;
  * one bad row poisoned the enclosing atomic block instead of being skipped;
  * `existing.gender = gender` with `gender` defaulting to 'Female' rewrote
    every male member on a re-import;
  * `if salary:` could never set a salary of 0;
  * a partial import was committed while an error was reported.
"""

import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from staff.models import StaffMember


def workbook(rows):
    """An .xlsx upload from a list of row lists (first row = headings)."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile(
        'staff.xlsx', buffer.getvalue(),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def csv_file(text, name='staff.csv'):
    return SimpleUploadedFile(name, text.encode('utf-8'), content_type='text/csv')


class StaffImportTestBase(TestCase):
    URL = '/staff/api/members/bulk_upload/'
    TEMPLATE_URL = '/staff/api/members/bulk_upload_template/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main Center',
                                           display_name='Main')
        self.owner_role = Role.objects.create(name='Owner',
                                              permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center)

        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def upload(self, rows, **extra):
        return self.client.post(
            self.URL, {'file': workbook(rows), **extra}, format='multipart')


class TemplateRoundTripTests(StaffImportTestBase):
    def test_the_template_data_sheet_holds_only_headings(self):
        """The guidance and example rows used to be data rows."""
        response = self.client.get(self.TEMPLATE_URL)
        self.assertEqual(response.status_code, 200)

        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        sheet = wb['Staff Import']
        rows = [
            row for row in sheet.iter_rows(values_only=True)
            if any(c is not None and str(c).strip() for c in row)
        ]
        self.assertEqual(len(rows), 1, rows)
        self.assertIn('firstName', rows[0])

    def test_the_guidance_is_on_its_own_sheet(self):
        response = self.client.get(self.TEMPLATE_URL)
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        self.assertIn('Instructions', wb.sheetnames)
        blob = str(list(wb['Instructions'].iter_rows(values_only=True)))
        self.assertIn('Required', blob)

    def test_the_downloaded_template_imports_after_one_row_is_filled_in(self):
        """The end-to-end path an operator actually takes."""
        response = self.client.get(self.TEMPLATE_URL)
        wb = openpyxl.load_workbook(io.BytesIO(response.content))
        sheet = wb['Staff Import']
        headings = [c.value for c in sheet[1]]

        row = {'firstName': 'Asha', 'lastName': 'Rao', 'center': 'Main Center',
               'salary': '20000'}
        sheet.append([row.get(h, '') for h in headings])

        buffer = io.BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        upload = SimpleUploadedFile('filled.xlsx', buffer.getvalue())

        result = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(result.status_code, 200, result.data)
        self.assertEqual(result.data['created'], 1, result.data)
        self.assertTrue(StaffMember.objects.filter(first_name='Asha').exists())

    def test_an_old_template_guidance_row_is_skipped_not_imported(self):
        """Files saved from the previous template still carry the guide rows."""
        response = self.upload([
            ['firstName', 'lastName', 'center'],
            ['Required', 'Optional', 'Required'],
            ['John', 'Doe', 'Main Center'],
            ['Asha', 'Rao', 'Main Center'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(StaffMember.objects.filter(first_name='Required').exists())
        self.assertEqual(response.data['created'], 2)
        self.assertTrue(
            any('guidance row' in w for w in response.data.get('warnings', [])),
            response.data)


class CenterResolutionTests(StaffImportTestBase):
    def test_a_junk_center_id_is_not_a_500(self):
        for value in ('abc', 'null', 'undefined', ''):
            response = self.upload(
                [['firstName', 'center'], ['Asha', 'Main Center']],
                center_id=value)
            self.assertLess(response.status_code, 500, value)

    def test_a_blank_center_column_uses_the_only_center(self):
        response = self.upload([['firstName'], ['Asha']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)

    def test_an_unmatched_center_imports_nothing_at_all(self):
        """A partial import used to be committed while an error was reported."""
        Center.objects.create(center_name='Second Center')
        response = self.upload([
            ['firstName', 'center'],
            ['Asha', 'Main Center'],
            ['Bela', 'Nowhere Branch'],
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(StaffMember.objects.count(), 0)
        self.assertIn('accessible_centers', response.data)

    def test_a_display_name_matches(self):
        response = self.upload([['firstName', 'center'], ['Asha', 'Main']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.center)

    def test_a_center_the_user_cannot_use_is_refused(self):
        other = Center.objects.create(center_name='Other')
        limited_role = Role.objects.create(name='Manager', permissions={})
        manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=limited_role, center=self.center)
        client = APIClient()
        client.force_authenticate(manager)

        response = client.post(
            self.URL,
            {'file': workbook([['firstName'], ['Asha']]), 'center_id': other.id},
            format='multipart')
        self.assertEqual(response.status_code, 403)


class GenderTests(StaffImportTestBase):
    def test_a_reimport_without_a_gender_column_does_not_change_gender(self):
        """This flipped every male member to Female."""
        member = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='K', gender='Male')

        response = self.upload([
            ['firstName', 'lastName', 'salary'],
            ['Ravi', 'K', '30000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['updated'], 1)

        member.refresh_from_db()
        self.assertEqual(member.gender, 'Male')
        self.assertEqual(member.salary, 30000)

    def test_an_unrecognised_gender_leaves_the_record_alone_and_warns(self):
        member = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='K', gender='Male')

        response = self.upload([
            ['firstName', 'lastName', 'gender'],
            ['Ravi', 'K', 'M'],
        ])
        member.refresh_from_db()
        self.assertEqual(member.gender, 'Male')
        self.assertTrue(
            any('not recognised' in w for w in response.data.get('warnings', [])),
            response.data)

    def test_a_stated_gender_is_applied(self):
        member = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='K', gender='Male')
        response = self.upload([
            ['firstName', 'lastName', 'gender'],
            ['Ravi', 'K', 'female'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        member.refresh_from_db()
        self.assertEqual(member.gender, 'Female')
        self.assertEqual(response.data['gender_changed'], 1)


class NumericFieldTests(StaffImportTestBase):
    def test_a_salary_of_zero_can_be_set(self):
        """`if salary:` skipped it, so a salary could never be corrected to 0."""
        member = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='K', salary=50000)
        response = self.upload([
            ['firstName', 'lastName', 'salary'],
            ['Ravi', 'K', '0'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        member.refresh_from_db()
        self.assertEqual(member.salary, 0)

    def test_a_negative_salary_is_skipped_with_a_reason(self):
        response = self.upload([
            ['firstName', 'salary'],
            ['Asha', '-500'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 0)
        self.assertEqual(response.data['skipped'], 1)
        self.assertIn('negative', str(response.data['warnings']))

    def test_a_salary_with_a_currency_symbol_and_commas_is_read(self):
        response = self.upload([
            ['firstName', 'salary'],
            ['Asha', '₹25,000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(StaffMember.objects.get(first_name='Asha').salary, 25000)

    def test_a_commission_of_zero_can_be_set(self):
        member = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='K',
            commission_percentage=10)
        self.upload([
            ['firstName', 'lastName', 'commission_percentage'],
            ['Ravi', 'K', '0'],
        ])
        member.refresh_from_db()
        self.assertEqual(member.commission_percentage, 0)

    def test_a_phone_read_as_a_float_loses_its_decimal(self):
        response = self.upload([
            ['firstName', 'phone'],
            ['Asha', '9876543210.0'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(StaffMember.objects.get(first_name='Asha').phone,
                         '9876543210')


class MalformedFileTests(StaffImportTestBase):
    def test_no_file_is_a_400_with_an_actionable_message(self):
        response = self.client.post(self.URL, {}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('error', response.data)

    def test_unrecognised_headings_list_what_was_found(self):
        response = self.upload([['alpha', 'beta'], ['1', '2']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('columns_found', response.data)
        self.assertIn('alpha', response.data['columns_found'])

    def test_a_file_with_headings_but_no_rows_says_so(self):
        response = self.upload([['firstName', 'center']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('no data rows', str(response.data))

    def test_a_missing_name_column_is_named_in_the_error(self):
        response = self.upload([['center', 'salary'], ['Main Center', '1']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('need_one_of', response.data)

    def test_a_corrupt_workbook_is_a_400_not_a_traceback(self):
        upload = SimpleUploadedFile('staff.xlsx', b'this is not a workbook')
        response = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback', str(response.data))
        self.assertNotIn('File "', str(response.data))

    def test_an_unsupported_extension_names_what_is_accepted(self):
        upload = SimpleUploadedFile('staff.pdf', b'%PDF-1.4')
        response = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertIn('supported', response.data)

    @override_settings()
    def test_a_row_over_the_limit_is_refused_before_any_write(self):
        from pos_backend.bulk_upload import MAX_ROWS

        rows = [['firstName']] + [[f'Person{i}'] for i in range(MAX_ROWS + 1)]
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertIn('limit', str(response.data))
        self.assertEqual(StaffMember.objects.count(), 0)

    def test_a_csv_import_works_the_same_way(self):
        response = self.client.post(
            self.URL,
            {'file': csv_file('firstName,lastName,salary\nAsha,Rao,15000\n')},
            format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(StaffMember.objects.get(first_name='Asha').salary, 15000)

    def test_an_anonymous_caller_cannot_import(self):
        client = APIClient()
        response = client.post(
            self.URL, {'file': workbook([['firstName'], ['Asha']])},
            format='multipart')
        self.assertIn(response.status_code, (401, 403))


class MatchingTests(StaffImportTestBase):
    def test_a_staff_code_matches_an_existing_member(self):
        member = StaffMember.objects.create(
            center=self.center, first_name='Old', last_name='Name',
            staff_code='STF-001')
        response = self.upload([
            ['staffCode', 'firstName', 'lastName'],
            ['stf-001', 'New', 'Name'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['updated'], 1)
        member.refresh_from_db()
        self.assertEqual(member.first_name, 'New')

    def test_the_same_file_twice_updates_rather_than_duplicating(self):
        rows = [['firstName', 'lastName', 'salary'], ['Asha', 'Rao', '15000']]
        first = self.upload(rows)
        second = self.upload(rows)
        self.assertEqual(first.data['created'], 1)
        self.assertEqual(second.data['created'], 0)
        self.assertEqual(second.data['updated'], 1)
        self.assertEqual(StaffMember.objects.filter(first_name='Asha').count(), 1)

    def test_a_single_name_column_is_split(self):
        response = self.upload([['name'], ['Asha Rao']])
        self.assertEqual(response.status_code, 200, response.data)
        member = StaffMember.objects.get(first_name='Asha')
        self.assertEqual(member.last_name, 'Rao')

    def test_a_joining_date_in_indian_format_is_read(self):
        response = self.upload([
            ['firstName', 'joiningDate'],
            ['Asha', '15-01-2023'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        member = StaffMember.objects.get(first_name='Asha')
        self.assertIsNotNone(member.joining_date)
        self.assertEqual(member.joining_date.year, 2023)
        self.assertEqual(member.joining_date.month, 1)
        self.assertEqual(member.joining_date.day, 15)

    def test_an_unparseable_date_leaves_the_field_empty(self):
        response = self.upload([
            ['firstName', 'joiningDate'],
            ['Asha', 'sometime last year'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNone(StaffMember.objects.get(first_name='Asha').joining_date)
