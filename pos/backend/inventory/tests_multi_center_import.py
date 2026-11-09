"""
Importing a master list across branches.

The owner's design: upload staff, vendors, products and services ONCE, have them
appear at every branch, then change a price or a name at one branch without
touching the others. Product, Vendor and StaffMember each carry a `center` FK, so
one row per branch is exactly that - the rows are independent afterwards.

The vendor importer got the branch resolution badly wrong:

    center_id_param = request.data.get('center_id') or coerce_center_id(...)
    if center_id_param and str(center_id_param).lower() != 'null':
        try:
            all_centers = [Center.objects.get(id=int(center_id_param))]
        except Center.DoesNotExist:
            all_centers = list(Center.objects.all())
    else:
        all_centers = list(Center.objects.all())

Three problems:
  * `Center.objects.all()` is EVERY branch in the business with no permission
    check, so a manager of one salon importing a vendor list wrote rows into
    branches they cannot see;
  * an unknown `center_id` fell back to every branch - the opposite of failing
    closed;
  * `int(...)` inside `except Center.DoesNotExist` meant a junk center_id raised
    ValueError, which the outer handler turned into a generic failure.

`pos_backend.bulk_upload.resolve_target_centers` is now the one resolver, and the
response says which branches were written to - a silent all-branches import is
otherwise indistinguishable from a one-branch one.
"""

import io

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Vendor
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
    return SimpleUploadedFile('vendors.xlsx', buffer.getvalue())


ROWS = [
    ['Vendor Name', 'Vendor Code', 'Phone'],
    ['Acme Supplies', 'V-001', '9876500001'],
    ['Best Beauty', 'V-002', '9876500002'],
]


class VendorImportScopeTests(TestCase):
    URL = '/inventory/api/vendors/bulk_upload/'

    def setUp(self):
        self.branch_a = Center.objects.create(center_name='Branch A', display_name='A')
        self.branch_b = Center.objects.create(center_name='Branch B', display_name='B')
        self.branch_c = Center.objects.create(center_name='Branch C', display_name='C')

        self.owner_role = Role.objects.create(name='Owner',
                                             permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.branch_a)

        # A manager of A and B only. C is none of their business.
        self.manager_role = Role.objects.create(name='Manager', permissions={
            'inventory': {'vendors': {'read': True, 'create': True,
                                      'update': True, 'delete': False}},
        })
        self.manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=self.manager_role, center=self.branch_a)
        self.manager.centers.set([self.branch_a, self.branch_b])

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def upload(self, client=None, **extra):
        return (client or self.api).post(
            self.URL, {'file': workbook(ROWS), **extra}, format='multipart')

    def as_manager(self):
        client = APIClient()
        client.force_authenticate(self.manager)
        return client

    # ── the intended behaviour ───────────────────────────────────────────

    def test_an_owner_import_reaches_every_branch(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200, response.data)

        for branch in (self.branch_a, self.branch_b, self.branch_c):
            self.assertEqual(
                Vendor.objects.filter(center=branch).count(), 2,
                f'{branch.center_name} did not get the vendors')

    def test_the_response_says_which_branches_were_written_to(self):
        """A silent all-branches import cannot be told from a one-branch one."""
        response = self.upload()
        self.assertEqual(response.data['branch_count'], 3)
        self.assertEqual(set(response.data['applied_to']), {'A', 'B', 'C'})
        self.assertEqual(response.data['scope'], 'all')
        self.assertIn('own copy', response.data['note'])

    def test_each_branch_gets_its_own_row_so_edits_do_not_leak(self):
        """This is the whole point of the design."""
        self.upload()

        a_vendor = Vendor.objects.get(center=self.branch_a, vendor_code='V-001')
        b_vendor = Vendor.objects.get(center=self.branch_b, vendor_code='V-001')
        self.assertNotEqual(a_vendor.pk, b_vendor.pk)

        a_vendor.name = 'Acme Supplies (Bandra)'
        a_vendor.save()

        b_vendor.refresh_from_db()
        self.assertEqual(b_vendor.name, 'Acme Supplies')

    def test_a_named_branch_gets_only_that_branch(self):
        response = self.upload(center_id=self.branch_a.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['scope'], 'center')
        self.assertEqual(response.data['applied_to'], ['A'])

        self.assertEqual(Vendor.objects.filter(center=self.branch_a).count(), 2)
        self.assertEqual(Vendor.objects.filter(center=self.branch_b).count(), 0)
        self.assertEqual(Vendor.objects.filter(center=self.branch_c).count(), 0)

    # ── the permission hole ──────────────────────────────────────────────

    def test_a_manager_import_reaches_only_their_own_branches(self):
        """It used to write into every branch in the business."""
        response = self.upload(client=self.as_manager())
        self.assertEqual(response.status_code, 200, response.data)

        self.assertEqual(Vendor.objects.filter(center=self.branch_a).count(), 2)
        self.assertEqual(Vendor.objects.filter(center=self.branch_b).count(), 2)
        self.assertEqual(
            Vendor.objects.filter(center=self.branch_c).count(), 0,
            'the import reached a branch the manager cannot see')

        self.assertEqual(set(response.data['applied_to']), {'A', 'B'})

    def test_a_manager_cannot_target_a_branch_they_do_not_have(self):
        response = self.upload(client=self.as_manager(), center_id=self.branch_c.id)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Vendor.objects.filter(center=self.branch_c).count(), 0)

    def test_an_unknown_branch_does_not_fall_back_to_every_branch(self):
        """The fallback was `Center.objects.all()`."""
        response = self.upload(center_id=99999999)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Vendor.objects.count(), 0)

    def test_a_junk_branch_id_is_a_clean_error(self):
        for value in ('abc', 'null', 'undefined', ''):
            with self.subTest(value):
                response = self.upload(center_id=value)
                self.assertLess(response.status_code, 500, value)

    # ── repeat imports ───────────────────────────────────────────────────

    def test_the_same_file_twice_updates_rather_than_duplicating(self):
        first = self.upload()
        second = self.upload()
        self.assertEqual(first.data['created'], 6)   # 2 vendors x 3 branches
        self.assertEqual(second.data['created'], 0)
        self.assertEqual(Vendor.objects.count(), 6)

    def test_a_later_single_branch_import_does_not_touch_the_others(self):
        self.upload()
        Vendor.objects.filter(center=self.branch_b, vendor_code='V-001').update(
            name='Renamed at B')

        self.upload(center_id=self.branch_a.id)

        self.assertEqual(
            Vendor.objects.get(center=self.branch_b, vendor_code='V-001').name,
            'Renamed at B')

    def test_an_anonymous_caller_cannot_import(self):
        response = APIClient().post(self.URL, {'file': workbook(ROWS)},
                                    format='multipart')
        self.assertIn(response.status_code, (401, 403))

    def test_a_user_with_no_branch_is_told_so(self):
        role = Role.objects.create(name='Floating', permissions={
            'inventory': {'vendors': {'read': True, 'create': True}},
        })
        floating = CustomUser.objects.create_user(
            email='f@test.com', password='StrongPass!234', full_name='F',
            role=role, center=None)
        client = APIClient()
        client.force_authenticate(floating)

        response = self.upload(client=client)
        self.assertEqual(response.status_code, 403)
        self.assertIn('branch', str(response.data).lower())
