"""
Staff import: one person, one branch.

A decided policy, recorded here so it cannot drift.

Products, vendors and services are catalogue data - the same shampoo and the same
haircut exist at every branch, so importing them creates a row per branch and
each branch can then price or rename its own copy.

Staff are NOT catalogue data. A stylist works at one salon. Importing a staff
list to "all branches" would put the same person on every branch's roster, and
they would then appear in every branch's stylist dropdown, every branch's
commission report, and every branch's payroll.

So each row goes to the branch its own `center` column names. The only fallback
is when the importer has exactly ONE accessible branch, where there is nothing to
be ambiguous about. An unmatched branch name fails the whole file rather than
guessing - a staff list silently loaded into the wrong salon is tedious to unpick
and quietly corrupts commission.
"""

import io
from decimal import Decimal

import openpyxl
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from staff.models import StaffMember


def workbook(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return SimpleUploadedFile('staff.xlsx', buffer.getvalue())


class StaffImportBranchPolicyTests(TestCase):
    URL = '/staff/api/members/bulk_upload/'

    def setUp(self):
        self.bandra = Center.objects.create(center_name='Bandra', display_name='Bandra West')
        self.andheri = Center.objects.create(center_name='Andheri', display_name='Andheri')
        self.powai = Center.objects.create(center_name='Powai', display_name='Powai')

        self.owner_role = Role.objects.create(name='Owner',
                                             permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.bandra)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def upload(self, rows, client=None, **extra):
        return (client or self.api).post(
            self.URL, {'file': workbook(rows), **extra}, format='multipart')

    # ── the policy ────────────────────────────────────────────────────────

    def test_each_person_lands_only_at_the_branch_their_row_names(self):
        response = self.upload([
            ['firstName', 'lastName', 'center', 'salary'],
            ['Asha', 'Rao', 'Bandra', '25000'],
            ['Bela', 'Nair', 'Andheri', '22000'],
            ['Chitra', 'Iyer', 'Powai', '28000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['created'], 3)

        # Three people, three branches, one row each.
        self.assertEqual(StaffMember.objects.count(), 3)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.bandra)
        self.assertEqual(
            StaffMember.objects.get(first_name='Bela').center, self.andheri)
        self.assertEqual(
            StaffMember.objects.get(first_name='Chitra').center, self.powai)

    def test_a_person_is_never_duplicated_across_branches(self):
        """The distinction from the product and vendor importers."""
        self.upload([
            ['firstName', 'lastName', 'center'],
            ['Asha', 'Rao', 'Bandra'],
        ])
        self.assertEqual(StaffMember.objects.filter(first_name='Asha').count(), 1)
        self.assertEqual(StaffMember.objects.filter(center=self.andheri).count(), 0)
        self.assertEqual(StaffMember.objects.filter(center=self.powai).count(), 0)

    def test_apply_to_all_branches_does_not_duplicate_staff_either(self):
        """Even asked explicitly, a person must not appear on three rosters."""
        response = self.upload([
            ['firstName', 'lastName', 'center'],
            ['Asha', 'Rao', 'Bandra'],
        ], apply_to_all_centers='true')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(StaffMember.objects.filter(first_name='Asha').count(), 1)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.bandra)

    def test_the_display_name_also_matches(self):
        """Operators type whichever name they see on screen."""
        response = self.upload([
            ['firstName', 'center'],
            ['Asha', 'Bandra West'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.bandra)

    def test_branch_matching_ignores_case_and_spacing(self):
        response = self.upload([
            ['firstName', 'center'],
            ['Asha', '  bandra  '],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.bandra)

    # ── ambiguity is refused, not guessed ────────────────────────────────

    def test_an_unmatched_branch_imports_nobody(self):
        """All-or-nothing: a half-loaded roster is worse than none.

        Commission and payroll follow the branch, so one person filed at the
        wrong salon quietly corrupts both.
        """
        response = self.upload([
            ['firstName', 'center'],
            ['Asha', 'Bandra'],
            ['Bela', 'Nowhere Branch'],
        ])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(StaffMember.objects.count(), 0)
        self.assertIn('accessible_centers', response.data)

    def test_a_blank_branch_is_refused_when_several_are_possible(self):
        response = self.upload([
            ['firstName', 'salary'],
            ['Asha', '25000'],
        ])
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(StaffMember.objects.count(), 0)

    def test_a_blank_branch_is_fine_with_exactly_one_branch(self):
        """Nothing to be ambiguous about."""
        self.andheri.delete()
        self.powai.delete()

        response = self.upload([
            ['firstName', 'salary'],
            ['Asha', '25000'],
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            StaffMember.objects.get(first_name='Asha').center, self.bandra)

    # ── permission still applies ─────────────────────────────────────────

    def test_a_manager_cannot_import_into_a_branch_they_do_not_have(self):
        # `directory` is the submodule StaffMemberViewSet declares; granting
        # it gets us PAST the module check so the BRANCH check is what this
        # test is actually exercising.
        role = Role.objects.create(name='Manager', permissions={
            'staff': {'directory': {'read': True, 'create': True, 'update': True}},
        })
        manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=role, center=self.bandra)
        manager.centers.set([self.bandra])
        client = APIClient()
        client.force_authenticate(manager)

        response = self.upload([
            ['firstName', 'center'],
            ['Asha', 'Powai'],
        ], client=client)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(StaffMember.objects.count(), 0)
        # Powai is not among the branches offered in the error.
        self.assertNotIn('Powai', str(response.data.get('accessible_centers', [])))

    # ── re-import updates the same person ────────────────────────────────

    def test_the_same_file_twice_updates_rather_than_duplicating(self):
        rows = [
            ['staffCode', 'firstName', 'center', 'salary'],
            ['STF-001', 'Asha', 'Bandra', '25000'],
        ]
        first = self.upload(rows)
        second = self.upload(rows)

        self.assertEqual(first.data['created'], 1)
        self.assertEqual(second.data['updated'], 1)
        self.assertEqual(StaffMember.objects.count(), 1)

    def test_moving_a_person_between_branches_needs_a_transfer_not_an_import(self):
        """An import must not silently relocate someone.

        Staff matching is scoped to the branch in the row, so the same code at a
        different branch creates a NEW person there rather than moving them.
        Moving someone is a StaffTransfer, which keeps the history that
        commission and payroll depend on.
        """
        self.upload([
            ['staffCode', 'firstName', 'center', 'salary'],
            ['STF-001', 'Asha', 'Bandra', '25000'],
        ])
        self.upload([
            ['staffCode', 'firstName', 'center', 'salary'],
            ['STF-001', 'Asha', 'Andheri', '25000'],
        ])

        # The Bandra record is untouched.
        self.assertTrue(
            StaffMember.objects.filter(staff_code='STF-001', center=self.bandra).exists())
        self.assertEqual(
            StaffMember.objects.get(staff_code='STF-001', center=self.bandra).salary,
            25000)
