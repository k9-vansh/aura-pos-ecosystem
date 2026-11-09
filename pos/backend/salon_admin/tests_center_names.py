"""One name, one centre."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from salon_admin.models import Center


class CenterNameTests(TestCase):
    URL = '/salon_admin/api/centers/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        owner = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='Owner',
                                               is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)

    def post(self, name, **extra):
        body = {'center_name': name, 'display_name': name, 'region': 'NORTH'}
        body.update(extra)
        return self.api.post(self.URL, body, format='json')

    def test_a_second_centre_with_the_same_name_is_refused(self):
        for name in ('Saket', 'saket', ' SAKET '):
            r = self.post(name)
            self.assertEqual(r.status_code, 400, name)
            self.assertIn('center_name', r.data)
        self.assertEqual(Center.objects.count(), 1)

    def test_a_new_name_is_fine(self):
        self.assertEqual(self.post('Noida').status_code, 201)

    def test_saving_a_centre_again_under_its_own_name_is_fine(self):
        r = self.api.patch(f'{self.URL}{self.center.id}/', {'center_name': 'Saket', 'phone': '9811100001'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_renaming_onto_another_centres_name_is_refused(self):
        other = Center.objects.create(center_name='Noida', display_name='Noida')
        r = self.api.patch(f'{self.URL}{other.id}/', {'center_name': 'saket'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
