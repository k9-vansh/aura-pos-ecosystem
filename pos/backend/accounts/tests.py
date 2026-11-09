"""
Authentication and access-control tests.

Pins H29 (module permissions were browser-only), H30 (center scoping failed
open), H33 (token expiry was dead code), H35 (audit-log redaction) and H36
(media served publicly).
"""

import datetime
from decimal import Decimal

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from pos_backend.permissions import IsOwner, has_module_permission
from pos_backend.scoping import (
    can_use_center, coerce_center_id, scope_to_centers, user_center_ids,
)
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class AccessTestBase(TestCase):
    def setUp(self):
        self.center_a = Center.objects.create(center_name='A', display_name='A')
        self.center_b = Center.objects.create(center_name='B', display_name='B')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center_a,
        )

        self.single_role = Role.objects.create(name='Manager A', permissions={
            'billing': {'invoices': {'read': True, 'create': True,
                                     'update': False, 'delete': False}},
            'finance': {'register_summary': {'read': True}},
        })
        self.manager_a = CustomUser.objects.create_user(
            email='a@test.com', password='StrongPass!234', full_name='Manager A',
            role=self.single_role, center=self.center_a,
        )

        # The dangerous shape: a role, but no center at all.
        self.orphan_role = Role.objects.create(name='Orphan', permissions={
            'billing': {'invoices': {'read': True}},
        })
        self.orphan = CustomUser.objects.create_user(
            email='orphan@test.com', password='StrongPass!234', full_name='Orphan',
            role=self.orphan_role,
        )

        Invoice.objects.create(center=self.center_a, status='paid',
                               subtotal=Decimal('100'), total_amount=Decimal('100'))
        Invoice.objects.create(center=self.center_b, status='paid',
                               subtotal=Decimal('200'), total_amount=Decimal('200'))

        self.api = APIClient()


class CenterScopingTests(AccessTestBase):
    """H30 - the pattern had no else branch, so no filter was applied."""

    def test_owner_sees_every_center(self):
        self.assertIsNone(user_center_ids(self.owner))
        scoped = scope_to_centers(Invoice.objects.all(), self.owner)
        self.assertEqual(scoped.count(), 2)

    def test_single_center_user_sees_only_their_center(self):
        scoped = scope_to_centers(Invoice.objects.all(), self.manager_a)
        self.assertEqual(scoped.count(), 1)
        self.assertEqual(scoped.first().center_id, self.center_a.id)

    def test_user_with_no_center_sees_nothing(self):
        """The whole point: this used to return every center's data."""
        self.assertEqual(user_center_ids(self.orphan), set())
        scoped = scope_to_centers(Invoice.objects.all(), self.orphan)
        self.assertEqual(scoped.count(), 0)

    def test_requesting_a_forbidden_center_yields_nothing(self):
        scoped = scope_to_centers(
            Invoice.objects.all(), self.manager_a, requested_center=self.center_b.id
        )
        self.assertEqual(scoped.count(), 0)

    def test_multi_center_user_sees_their_set(self):
        self.manager_a.centers.set([self.center_a, self.center_b])
        self.assertEqual(
            user_center_ids(self.manager_a), {self.center_a.id, self.center_b.id}
        )
        self.assertEqual(scope_to_centers(Invoice.objects.all(), self.manager_a).count(), 2)

    def test_all_centers_permission_lifts_the_restriction(self):
        role = Role.objects.create(name='Regional', permissions={'all_centers': True})
        regional = CustomUser.objects.create_user(
            email='regional@test.com', password='pw', full_name='Regional', role=role,
        )
        self.assertIsNone(user_center_ids(regional))
        self.assertEqual(scope_to_centers(Invoice.objects.all(), regional).count(), 2)

    def test_coerce_center_id_tolerates_junk(self):
        """M19: `int('null')` was a live 500; the frontend does send 'null'."""
        for junk in ['null', 'undefined', '', 'abc', None, 'None']:
            self.assertIsNone(coerce_center_id(junk))
        self.assertEqual(coerce_center_id('7'), 7)
        self.assertEqual(coerce_center_id(7), 7)

    def test_can_use_center(self):
        self.assertTrue(can_use_center(self.owner, self.center_b.id))
        self.assertTrue(can_use_center(self.manager_a, self.center_a.id))
        self.assertFalse(can_use_center(self.manager_a, self.center_b.id))
        self.assertFalse(can_use_center(self.orphan, self.center_a.id))
        self.assertFalse(can_use_center(self.manager_a, None))

    def test_scoped_invoice_endpoint_honours_the_same_rules(self):
        self.api.force_authenticate(self.orphan)
        response = self.api.get('/billing/invoices/')
        self.assertEqual(response.status_code, 200)
        rows = response.data if isinstance(response.data, list) else response.data['results']
        self.assertEqual(len(rows), 0)


class ModulePermissionTests(AccessTestBase):
    """H29 - permissions were enforced only by the Angular route guard."""

    def test_permission_lookup_matches_the_roles_screen_shape(self):
        self.assertTrue(has_module_permission(self.manager_a, 'billing', 'read'))
        self.assertTrue(has_module_permission(self.manager_a, 'billing', 'create'))
        self.assertFalse(has_module_permission(self.manager_a, 'billing', 'delete'))
        self.assertFalse(has_module_permission(self.manager_a, 'staff', 'read'))

    def test_submodule_lookup(self):
        self.assertTrue(
            has_module_permission(self.manager_a, 'billing', 'read', 'invoices')
        )
        self.assertFalse(
            has_module_permission(self.manager_a, 'billing', 'read', 'new_invoice')
        )

    def test_only_the_superuser_bypasses_every_check(self):
        """Decision #6: the superuser is the one built-in full-access identity."""
        boss = CustomUser.objects.create_superuser(
            email='super@test.com', password='StrongPass!234', full_name='Super')
        self.assertTrue(has_module_permission(boss, 'anything', 'delete', 'at_all'))

    def test_a_role_named_owner_gets_nothing_from_its_name(self):
        """A role called "owner" used to receive everything. It no longer does."""
        # setUp already holds an "Owner" role; MySQL's collation treats role
        # names case-insensitively, so reuse it with nothing granted.
        role, _ = Role.objects.update_or_create(
            name__iexact='owner', defaults={'name': 'owner', 'permissions': {}})
        user = CustomUser.objects.create_user(
            email='named-owner@test.com', password='pw', full_name='Named', role=role)
        self.assertFalse(has_module_permission(user, 'billing', 'read'))
        self.assertFalse(has_module_permission(user, 'anything', 'delete', 'at_all'))

    def test_role_with_no_permissions_is_denied(self):
        role = Role.objects.create(name='Empty', permissions={})
        user = CustomUser.objects.create_user(
            email='empty@test.com', password='pw', full_name='Empty', role=role,
        )
        self.assertFalse(has_module_permission(user, 'billing', 'read'))

    def test_user_with_no_role_is_denied(self):
        user = CustomUser.objects.create_user(
            email='norole@test.com', password='pw', full_name='No Role',
        )
        self.assertFalse(has_module_permission(user, 'billing', 'read'))

    def test_invoice_delete_is_blocked_server_side(self):
        """H29 + H32: DELETE used to cascade the whole financial record away."""
        invoice = Invoice.objects.filter(center=self.center_a).first()
        self.api.force_authenticate(self.manager_a)
        response = self.api.delete(f'/billing/invoices/{invoice.id}/')
        self.assertIn(response.status_code, (403, 405))
        self.assertTrue(Invoice.objects.filter(pk=invoice.id).exists())

    def test_module_without_read_permission_is_refused(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.get('/staff/api/payrolls/')
        # No staff permission at all: refused or empty, never another
        # center's payroll.
        if response.status_code == 200:
            rows = response.data if isinstance(response.data, list) else response.data['results']
            self.assertEqual(len(rows), 0)
        else:
            self.assertEqual(response.status_code, 403)


class TokenLifecycleTests(AccessTestBase):
    """H33 - expiry was dead code, and enabling it would have locked everyone out."""

    def test_login_issues_a_fresh_token_each_time(self):
        """H33: get_or_create returned the same stale token forever."""
        first = self.api.post('/accounts/api/login/', {
            'username': 'owner@test.com', 'password': 'StrongPass!234',
        }, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        token_one = first.data['token']

        second = self.api.post('/accounts/api/login/', {
            'username': 'owner@test.com', 'password': 'StrongPass!234',
        }, format='json')
        self.assertEqual(second.status_code, 200, second.data)
        token_two = second.data['token']

        self.assertNotEqual(token_one, token_two)
        # And only one token exists, so the old one is genuinely revoked.
        self.assertEqual(Token.objects.filter(user=self.owner).count(), 1)

    def test_login_reports_the_expiry_window(self):
        response = self.api.post('/accounts/api/login/', {
            'username': 'owner@test.com', 'password': 'StrongPass!234',
        }, format='json')
        self.assertIn('token_expires_in_days', response.data)

    @override_settings(API_TOKEN_MAX_AGE_DAYS=7)
    def test_an_expired_token_is_rejected(self):
        """H33: the expiring authentication class was never wired up."""
        token = Token.objects.create(user=self.owner)
        Token.objects.filter(pk=token.pk).update(
            created=datetime.datetime.now() - datetime.timedelta(days=8)
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = client.get('/billing/invoices/')
        self.assertEqual(response.status_code, 401)

    @override_settings(API_TOKEN_MAX_AGE_DAYS=7)
    def test_a_fresh_token_is_accepted(self):
        token = Token.objects.create(user=self.owner)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = client.get('/billing/invoices/')
        self.assertEqual(response.status_code, 200)

    def test_inactive_user_token_is_rejected(self):
        token = Token.objects.create(user=self.manager_a)
        self.manager_a.is_active = False
        self.manager_a.save(update_fields=['is_active'])
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f'Token {token.key}')
        response = client.get('/billing/invoices/')
        self.assertEqual(response.status_code, 401)


class PasswordChangeTests(AccessTestBase):
    """The endpoint UserSerializer's comment referred to but that did not exist."""

    def test_user_can_change_their_own_password(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.post('/accounts/api/change-password/', {
            'current_password': 'StrongPass!234',
            'new_password': 'EvenStronger!567',
            'confirm_password': 'EvenStronger!567',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.manager_a.refresh_from_db()
        self.assertTrue(self.manager_a.check_password('EvenStronger!567'))

    def test_wrong_current_password_is_rejected(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.post('/accounts/api/change-password/', {
            'current_password': 'wrong', 'new_password': 'EvenStronger!567',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_mismatched_confirmation_is_rejected(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.post('/accounts/api/change-password/', {
            'current_password': 'StrongPass!234',
            'new_password': 'EvenStronger!567',
            'confirm_password': 'Different!890',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_weak_password_is_rejected(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.post('/accounts/api/change-password/', {
            'current_password': 'StrongPass!234', 'new_password': '1234',
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_password_change_revokes_existing_tokens(self):
        old = Token.objects.create(user=self.manager_a)
        self.api.force_authenticate(self.manager_a)
        response = self.api.post('/accounts/api/change-password/', {
            'current_password': 'StrongPass!234', 'new_password': 'EvenStronger!567',
        }, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Token.objects.filter(key=old.key).exists())

    def test_only_an_owner_can_reset_someone_elses_password(self):
        self.api.force_authenticate(self.manager_a)
        response = self.api.post(
            f'/accounts/api/users/{self.orphan.id}/reset-password/',
            {'new_password': 'Whatever!123'}, format='json',
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_can_reset_a_password(self):
        self.api.force_authenticate(self.owner)
        response = self.api.post(
            f'/accounts/api/users/{self.orphan.id}/reset-password/',
            {'new_password': 'BrandNew!456'}, format='json',
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.orphan.refresh_from_db()
        self.assertTrue(self.orphan.check_password('BrandNew!456'))


class AuditRedactionTests(TestCase):
    """H35 - exact-key matching let app_password / app_pin through in cleartext."""

    def test_password_variants_are_redacted(self):
        from audit_logs.middleware import _sanitise
        import json

        payload = json.dumps({
            'email': 'a@b.com',
            'password': 'hunter2',
            'app_password': '4321',
            'app_pin': '9999',
            'new_pin': '1111',
            'aadhar_number': '1234-5678-9012',
            'auth_token': 'abc',
        }).encode()

        result = _sanitise(payload)
        self.assertEqual(result['email'], 'a@b.com')
        for key in ['password', 'app_password', 'app_pin', 'new_pin',
                    'aadhar_number', 'auth_token']:
            self.assertEqual(result[key], '***', f'{key} was not redacted')

    def test_nested_secrets_are_redacted(self):
        from audit_logs.middleware import _sanitise
        import json

        payload = json.dumps({
            'user': {'email': 'a@b.com', 'password': 'hunter2'},
            'items': [{'name': 'x', 'pin': '1234'}],
        }).encode()

        result = _sanitise(payload)
        self.assertEqual(result['user']['password'], '***')
        self.assertEqual(result['user']['email'], 'a@b.com')
        self.assertEqual(result['items'][0]['pin'], '***')

    def test_malformed_body_returns_an_empty_dict(self):
        from audit_logs.middleware import _sanitise
        self.assertEqual(_sanitise(b'not json'), {})
        self.assertEqual(_sanitise(b''), {})


class MediaExposureTests(TestCase):
    """H36 - the media route was registered outside any DEBUG guard."""

    @override_settings(SERVE_MEDIA_FILES=False)
    def test_media_is_not_served_when_disabled(self):
        from django.urls import clear_url_caches
        from importlib import reload
        import pos_backend.urls as project_urls

        reload(project_urls)
        clear_url_caches()
        try:
            client = APIClient()
            response = client.get('/media/chat_images/anything.png')
            self.assertEqual(response.status_code, 404)
        finally:
            reload(project_urls)
            clear_url_caches()
