"""The Users screen's password box: the plain user update never changes a password, so the screen sends the
typed password to the reset-password call. Both halves are pinned here, and that a reset password can sign in."""
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from salon_admin.models import Center, Role

LOGIN = '/accounts/api/login/'


class PasswordResetScreenTests(TestCase):
    def setUp(self):
        # The sign-in is rate limited (5 a minute) through the cache, which the whole test run shares: the
        # sign-ins of earlier tests used the allowance up, so these tests passed alone and failed in the full run.
        cache.clear()
        self.addCleanup(cache.clear)
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        self.role = Role.objects.create(name='Clerk', permissions={})
        self.owner = CustomUser.objects.create_superuser(email='owner@t.com', password='Owner#Pass2026')
        self.user = CustomUser.objects.create_user(email='clerk@t.com', password='Old#Pass2026a', full_name='Clerk',
                                                   role=self.role, center=self.center)
        self.user.centers.add(self.center)
        self.api = APIClient(HTTP_HOST='localhost')
        self.api.force_authenticate(self.owner)

    def can_sign_in(self, password):
        return APIClient(HTTP_HOST='localhost').post(
            LOGIN, {'username': 'clerk@t.com', 'password': password}, format='json').status_code == 200

    def test_the_plain_user_update_leaves_the_password_alone(self):
        res = self.api.patch(f'/accounts/api/users/{self.user.id}/', {
            'full_name': 'Clerk Two', 'role': self.role.id, 'center': self.center.id, 'centers': [self.center.id],
            'password': 'New#Pass2026b'}, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(self.can_sign_in('Old#Pass2026a'))
        self.assertFalse(self.can_sign_in('New#Pass2026b'))

    def test_the_reset_call_changes_it_and_the_new_password_signs_in(self):
        res = self.api.post(f'/accounts/api/users/{self.user.id}/reset-password/', {'new_password': 'New#Pass2026b'},
                            format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(self.can_sign_in('New#Pass2026b'))
        self.assertFalse(self.can_sign_in('Old#Pass2026a'))

    def test_a_short_or_common_password_is_refused_with_a_reason(self):
        for weak in ('12345', 'password'):
            res = self.api.post(f'/accounts/api/users/{self.user.id}/reset-password/', {'new_password': weak},
                                format='json')
            self.assertEqual(res.status_code, 400, weak)
            self.assertTrue(res.data['new_password'])
        self.assertTrue(self.can_sign_in('Old#Pass2026a'))

    def test_only_someone_ticked_for_it_can_reset(self):
        clerk_api = APIClient(HTTP_HOST='localhost')
        clerk_api.force_authenticate(self.user)
        res = clerk_api.post(f'/accounts/api/users/{self.user.id}/reset-password/', {'new_password': 'New#Pass2026b'},
                             format='json')
        self.assertEqual(res.status_code, 403)
        self.assertTrue(self.can_sign_in('Old#Pass2026a'))
