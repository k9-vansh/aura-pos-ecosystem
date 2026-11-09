"""
Customer import tests.

The old handler's worst property was that it always succeeded. Headings were
matched exactly and case-sensitively, so a file with 'Phone' instead of 'phone'
imported nobody and still returned HTTP 200 with
"Successfully imported 0 clients". Every other skip - duplicate phone, missing
name - was equally silent.

Also pinned:
  * `len(bulk_create(..., ignore_conflicts=True))` counted rows that were never
    inserted, so the reported total was wrong;
  * the duplicate-phone set was loaded for every center, so a customer of
    another branch blocked creating them at this one;
  * a row with a phone and no name created a nameless customer.
"""

import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client
from salon_admin.models import Center, Role


def workbook(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile('clients.xlsx', buffer.getvalue())


def csv_file(text):
    return SimpleUploadedFile('clients.csv', text.encode('utf-8'),
                              content_type='text/csv')


class ClientImportTestBase(TestCase):
    URL = '/clients/api/clients/bulk_upload/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main Center',
                                            display_name='Main')
        self.other = Center.objects.create(center_name='Second Center')

        # Importing customers is Admin > Clients. Permissions are enforced
        # server-side now, so the role has to actually grant it.
        self.manager_role = Role.objects.create(name='Manager', permissions={
            'admin': {'clients': {'read': True, 'create': True, 'update': True, 'delete': True,
                                  'actions': {'bulk_upload': True}}},
        })
        self.manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='Manager',
            role=self.manager_role, center=self.center)

        self.client = APIClient()
        self.client.force_authenticate(self.manager)

    def upload(self, rows, **extra):
        return self.client.post(
            self.URL, {'file': workbook(rows), **extra}, format='multipart')


class HeadingTests(ClientImportTestBase):
    def test_capitalised_headings_are_recognised(self):
        """'Phone' and 'First Name' matched nothing and reported success."""
        response = self.upload([
            ['First Name', 'Last Name', 'Phone'],
            ['Asha', 'Rao', '9876500001'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)
        self.assertTrue(Client.objects.filter(phone='9876500001').exists())

    def test_unrecognised_headings_are_a_400_not_a_silent_success(self):
        response = self.upload([['alpha', 'beta'], ['1', '2']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('columns_found', response.data)
        self.assertEqual(Client.objects.count(), 0)

    def test_a_file_with_no_phone_column_is_refused(self):
        response = self.upload([['First Name'], ['Asha']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('need_one_of', response.data)

    def test_a_mobile_column_is_accepted_as_the_phone(self):
        response = self.upload([['name', 'Mobile'], ['Asha Rao', '9876500002']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)


class SkipReportingTests(ClientImportTestBase):
    def test_every_skip_is_reported_with_a_reason(self):
        Client.objects.create(center=self.center, first_name='Existing',
                              phone='9876500001')
        response = self.upload([
            ['name', 'phone'],
            ['Asha Rao', '9876500001'],     # already a customer here
            ['', '9876500002'],             # no name
            ['Bela Nair', ''],              # no phone
            ['Chitra Iyer', '9876500003'],  # fine
            ['Devi Menon', '9876500003'],   # duplicate within the file
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 1)
        self.assertEqual(response.data['skipped'], 4)

        warnings = ' '.join(response.data['warnings'])
        self.assertIn('already used by an existing client', warnings)
        self.assertIn('no name given', warnings)
        self.assertIn('phone number is empty', warnings)
        self.assertIn('more than once', warnings)

    def test_the_created_count_is_the_number_actually_inserted(self):
        """`len(bulk_create(ignore_conflicts=True))` overcounted."""
        response = self.upload([
            ['name', 'phone'],
            ['Asha Rao', '9876500001'],
            ['Bela Nair', '9876500002'],
        ])
        self.assertEqual(response.data['created'], 2)
        self.assertEqual(Client.objects.count(), 2)

    def test_a_customer_of_another_branch_blocks_the_same_number(self):
        """F2: a phone is unique across the business, not per branch."""
        Client.objects.create(center=self.other, first_name='Same',
                              phone='9876500001')
        response = self.upload([['name', 'phone'], ['Asha Rao', '9876500001']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 0)
        self.assertEqual(Client.objects.filter(phone='9876500001').count(), 1)

    def test_a_duplicate_email_is_reported(self):
        Client.objects.create(center=self.center, first_name='Existing',
                              phone='9876500009', email='asha@example.com')
        response = self.upload([
            ['name', 'phone', 'email'],
            ['Asha Rao', '9876500001', 'asha@example.com'],
        ])
        self.assertEqual(response.data['created'], 0)
        self.assertIn('already on file', str(response.data['warnings']))


class FieldParsingTests(ClientImportTestBase):
    def test_a_phone_read_as_a_float_loses_its_decimal(self):
        response = self.upload([['name', 'phone'], ['Asha Rao', '9876500001.0']])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Client.objects.filter(phone='9876500001').exists())

    def test_a_single_name_column_is_split(self):
        self.upload([['name', 'phone'], ['Asha Rao', '9876500001']])
        client = Client.objects.get(phone='9876500001')
        self.assertEqual(client.first_name, 'Asha')
        self.assertEqual(client.last_name, 'Rao')

    def test_a_one_word_name_leaves_the_last_name_empty(self):
        self.upload([['name', 'phone'], ['Asha', '9876500001']])
        client = Client.objects.get(phone='9876500001')
        self.assertEqual(client.first_name, 'Asha')
        self.assertEqual(client.last_name, '')

    def test_gender_is_normalised(self):
        self.upload([
            ['name', 'phone', 'gender'],
            ['Asha Rao', '9876500001', 'Female'],
            ['Ravi Kumar', '9876500002', 'M'],
        ])
        self.assertEqual(Client.objects.get(phone='9876500001').gender, 'female')
        self.assertEqual(Client.objects.get(phone='9876500002').gender, 'male')

    def test_an_unrecognised_gender_warns_rather_than_being_stored(self):
        response = self.upload([
            ['name', 'phone', 'gender'],
            ['Asha Rao', '9876500001', 'yes'],
        ])
        self.assertEqual(response.data['created'], 1)
        self.assertNotEqual(Client.objects.get(phone='9876500001').gender, 'yes')
        self.assertIn('not recognised', str(response.data['warnings']))

    def test_a_birthday_in_indian_format_is_read(self):
        self.upload([
            ['name', 'phone', 'birthday'],
            ['Asha Rao', '9876500001', '15-01-1990'],
        ])
        birthday = Client.objects.get(phone='9876500001').birthday
        self.assertIsNotNone(birthday)
        self.assertEqual((birthday.year, birthday.month, birthday.day),
                         (1990, 1, 15))

    def test_an_unreadable_birthday_is_left_empty(self):
        self.upload([
            ['name', 'phone', 'birthday'],
            ['Asha Rao', '9876500001', 'not a date'],
        ])
        self.assertIsNone(Client.objects.get(phone='9876500001').birthday)

    def test_imported_customers_are_assigned_to_the_callers_branch(self):
        self.upload([['name', 'phone'], ['Asha Rao', '9876500001']])
        self.assertEqual(Client.objects.get(phone='9876500001').center, self.center)


class PermissionTests(ClientImportTestBase):
    def test_a_manager_cannot_import_into_another_branch(self):
        response = self.upload([['name', 'phone'], ['Asha Rao', '9876500001']],
                               center_id=self.other.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Client.objects.count(), 0)

    def test_a_junk_center_id_is_not_a_500(self):
        for value in ('abc', 'null', 'undefined', ''):
            response = self.upload(
                [['name', 'phone'], ['Asha Rao', f'98765{value or "0"}0001']],
                center_id=value)
            self.assertLess(response.status_code, 500, value)

    def test_an_anonymous_caller_cannot_import(self):
        client = APIClient()
        response = client.post(
            self.URL, {'file': workbook([['name', 'phone'], ['A', '1']])},
            format='multipart')
        self.assertIn(response.status_code, (401, 403))


class MalformedFileTests(ClientImportTestBase):
    def test_no_file_is_a_400(self):
        response = self.client.post(self.URL, {}, format='multipart')
        self.assertEqual(response.status_code, 400)

    def test_a_corrupt_file_does_not_leak_a_traceback(self):
        upload = SimpleUploadedFile('clients.xlsx', b'not a workbook')
        response = self.client.post(self.URL, {'file': upload}, format='multipart')
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('Traceback', str(response.data))

    def test_a_file_with_headings_but_no_rows_says_so(self):
        response = self.upload([['name', 'phone']])
        self.assertEqual(response.status_code, 400)
        self.assertIn('no data rows', str(response.data))

    def test_a_row_over_the_limit_is_refused_before_any_write(self):
        from pos_backend.bulk_upload import MAX_ROWS

        rows = ([['name', 'phone']]
                + [[f'Person{i}', f'{9000000000 + i}'] for i in range(MAX_ROWS + 1)])
        response = self.upload(rows)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Client.objects.count(), 0)

    def test_a_csv_import_works_the_same_way(self):
        response = self.client.post(
            self.URL,
            {'file': csv_file('name,phone\nAsha Rao,9876500001\n')},
            format='multipart')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Client.objects.filter(phone='9876500001').exists())

    def test_warning_truncation_is_declared(self):
        """The old code cut the list at 20 without saying so."""
        # A phone with no name: skipped, and each skip is a reported warning.
        rows = ([['name', 'phone']]
                + [['', f'{9876500000 + i}'] for i in range(80)])
        response = self.upload(rows)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['skipped'], 80)
        self.assertIn('total_warnings', response.data)
        self.assertEqual(response.data['total_warnings'], 80)
