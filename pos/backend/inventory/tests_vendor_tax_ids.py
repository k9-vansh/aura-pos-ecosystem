"""A vendor's PAN and GSTIN are checked when typed; an old short CST number still goes in."""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.identity import gstin_checksum
from inventory.models import Vendor
from salon_admin.models import Center


class VendorTaxIdTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        self.owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def post(self, **extra):
        body = {'name': 'Loreal', 'center': self.center.id, 'phone': '9810000001'}
        body.update(extra)
        return self.api.post('/inventory/api/vendors/', body, format='json')

    def test_a_bad_pan_is_refused(self):
        for bad in ('xx', 'ABCDE12345', '1234567890'):
            r = self.post(pan_number=bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn('pan_number', r.data)

    def test_a_good_pan_is_kept_in_capitals(self):
        r = self.post(pan_number='abcde1234f')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Vendor.objects.get(pk=r.data['id']).pan_number, 'ABCDE1234F')

    def test_a_fifteen_character_gstin_must_be_a_real_one(self):
        r = self.post(cst_number='07AAAAA0000A1Z0')           # right shape, wrong check character
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('cst_number', r.data)

    def test_a_valid_gstin_is_accepted(self):
        first14 = '07AAAAA0000A1Z'
        r = self.post(cst_number=first14 + gstin_checksum(first14))
        self.assertEqual(r.status_code, 201, r.data)

    def test_an_old_short_cst_number_is_still_accepted(self):
        self.assertEqual(self.post(cst_number='CST/123/04').status_code, 201)

    def test_blank_tax_ids_are_fine(self):
        self.assertEqual(self.post(cst_number='', pan_number='').status_code, 201)
