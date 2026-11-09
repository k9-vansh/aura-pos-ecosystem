"""A staff member has a real phone, a PIN code that exists, and is not typed in twice."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from salon_admin.models import Center
from staff.models import StaffMember


class StaffIdentityTests(TestCase):
    URL = '/staff/api/members/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida')
        owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)

    def post(self, **extra):
        body = {'first_name': 'Asha', 'designation': 'Stylist', 'center': self.center.id, 'salary': '20000'}
        body.update(extra)
        return self.api.post(self.URL, body, format='json')

    def test_a_short_or_garbled_phone_is_refused(self):
        for bad in ('12', '98765', 'abcdefghij', '98765432101234'):
            r = self.post(phone=bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn('phone', r.data)

    def test_a_phone_is_kept_as_ten_digits(self):
        r = self.post(phone='+91 98222-00001')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(StaffMember.objects.get(pk=r.data['id']).phone, '9822200001')

    def test_the_same_phone_cannot_be_two_staff_members(self):
        self.assertEqual(self.post(phone='9822200001').status_code, 201)
        r = self.post(first_name='Again', phone='9822200001')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('phone', r.data)
        self.assertEqual(StaffMember.objects.count(), 1)

    def test_a_staff_code_is_used_once_per_centre(self):
        self.assertEqual(self.post(staff_code='S-1').status_code, 201)
        self.assertEqual(self.post(first_name='B', staff_code='s-1').status_code, 400)
        self.assertEqual(self.post(first_name='C', staff_code='S-1', center=self.other.id).status_code, 201)

    def test_saving_a_member_again_with_the_same_phone_is_not_a_duplicate(self):
        r = self.post(phone='9822200001', staff_code='S-1')
        self.assertEqual(r.status_code, 201, r.data)
        same = self.api.patch(f"{self.URL}{r.data['id']}/", {'phone': '9822200001', 'staff_code': 'S-1', 'city': 'Delhi'},
                              format='json')
        self.assertEqual(same.status_code, 200, same.data)

    def test_a_bad_pin_code_is_refused(self):
        self.assertEqual(self.post(pin_code='12').status_code, 400)
        self.assertEqual(self.post(pin_code='011001').status_code, 400)
        self.assertEqual(self.post(pin_code='110001').status_code, 201)

    def test_blank_phone_and_code_are_fine(self):
        self.assertEqual(self.post(phone='', staff_code='').status_code, 201)
        self.assertEqual(self.post(first_name='B', phone='', staff_code='').status_code, 201)
