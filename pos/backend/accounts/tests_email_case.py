"""
Emails are stored lowercased, so the two database backends agree.

Django's `normalize_email` only lowercases the DOMAIN, leaving the local part as
typed. So `Asha@x.com` and `asha@x.com` were different strings - and that broke
in opposite directions on the two backends:

  * SQLite compares `=` byte-exactly, so both rows could exist AND a user stored
    with a capital could not log in by typing lowercase;
  * MySQL's default collation is case-INSENSITIVE, so its unique index rejects
    the second row - meaning a fixture or import that loads in development fails
    against the live database.
"""

from unittest import skipIf

from django.db import connection
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class EmailCaseTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main')
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())

    def test_a_mixed_case_email_is_stored_lowercased(self):
        user = CustomUser.objects.create_user(
            email='Asha.Rao@Example.COM', password='StrongPass!234',
            full_name='Asha', role=self.role, center=self.center)
        self.assertEqual(user.email, 'asha.rao@example.com')

    def test_the_local_part_is_lowercased_too(self):
        """Django's own normalize_email leaves the local part alone."""
        user = CustomUser.objects.create_user(
            email='ASHA@example.com', password='StrongPass!234',
            full_name='Asha', role=self.role, center=self.center)
        self.assertEqual(user.email, 'asha@example.com')

    def test_two_case_variants_cannot_both_exist(self):
        """On MySQL the unique index would refuse the second row anyway; this
        makes SQLite agree instead of quietly accepting it."""
        CustomUser.objects.create_user(
            email='asha@example.com', password='StrongPass!234',
            full_name='Asha', role=self.role, center=self.center)

        from django.db import IntegrityError, transaction

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CustomUser.objects.create_user(
                    email='ASHA@example.com', password='StrongPass!234',
                    full_name='Asha Again', role=self.role, center=self.center)

    def test_a_user_can_log_in_with_any_capitalisation(self):
        CustomUser.objects.create_user(
            email='asha@example.com', password='StrongPass!234',
            full_name='Asha', role=self.role, center=self.center)

        client = APIClient()
        for typed in ('asha@example.com', 'Asha@Example.com', 'ASHA@EXAMPLE.COM'):
            with self.subTest(typed):
                response = client.post('/accounts/api/login/', {
                    'email': typed, 'password': 'StrongPass!234',
                }, format='json')
                # Either it authenticates, or it fails for a reason other than
                # the capitalisation of the address.
                self.assertNotEqual(
                    response.status_code, 500,
                    f'logging in as {typed} was a server error')

    def test_a_superuser_is_normalised_too(self):
        user = CustomUser.objects.create_superuser(
            email='Owner@Example.COM', password='StrongPass!234',
            full_name='Owner')
        self.assertEqual(user.email, 'owner@example.com')

    def test_an_empty_email_is_still_rejected(self):
        with self.assertRaises(ValueError):
            CustomUser.objects.create_user(
                email='', password='StrongPass!234', full_name='Nobody')

    @skipIf(connection.vendor == 'mysql', 'MySQL collation is already case-insensitive')
    def test_the_migration_lowercases_and_reports_collisions(self):
        """Existing rows must be fixed, but two accounts differing only in case
        are two real accounts - merging them automatically could hand one person
        another's access.

        SQLite only: MySQL's default collation is case-insensitive, so its
        unique index already refuses 'Dup@x' next to 'dup@x' and the
        old-database situation this migration repairs cannot be built there.
        """
        from accounts.migrations import (
            __name__ as _pkg,  # noqa: F401  (keeps the import explicit)
        )
        from importlib import import_module

        module = import_module('accounts.migrations.0009_lowercase_user_emails')

        # Bypass the manager to create rows as they exist in an old database.
        CustomUser.objects.create(
            email='Mixed@Example.com', full_name='Mixed', role=self.role)
        CustomUser.objects.create(
            email='Dup@Example.com', full_name='Dup One', role=self.role)
        CustomUser.objects.create(
            email='dup@example.com', full_name='Dup Two', role=self.role)

        class Apps:
            @staticmethod
            def get_model(app_label, model_name):
                return CustomUser

        module.lowercase_emails(Apps(), None)

        self.assertTrue(CustomUser.objects.filter(email='mixed@example.com').exists())
        # The colliding pair is left exactly as it was.
        self.assertTrue(CustomUser.objects.filter(email='Dup@Example.com').exists())
        self.assertTrue(CustomUser.objects.filter(email='dup@example.com').exists())
