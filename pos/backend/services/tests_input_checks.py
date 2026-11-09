"""Service prices and branch overrides are checked before they are saved.

A branch price of "abc" used to be written to the database as it was; every bill for that service
at that branch then crashed while reading it."""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from salon_admin.models import Center
from services.models import CenterService, ServiceMaster


class ServiceInputTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)
        self.service = ServiceMaster.objects.create(name='Cut', default_price=Decimal('500'), tax_percentage=Decimal('18'))

    def override(self, **extra):
        body = {'center_id': self.center.id, 'service_id': self.service.id, 'price': '450'}
        body.update(extra)
        return self.api.post('/services/api/center/override/', body, format='json')

    def test_a_branch_price_is_saved(self):
        r = self.override(price='450')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(CenterService.objects.get().price, Decimal('450.00'))

    def test_a_price_that_is_not_a_number_is_refused_and_nothing_is_stored(self):
        for bad in ('abc', '99999999999', '-5', 'NaN', 'Infinity'):
            r = self.override(price=bad)
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(CenterService.objects.count(), 0)

    def test_an_unknown_centre_or_service_is_refused(self):
        self.assertEqual(self.override(center_id=9999).status_code, 400)
        self.assertEqual(self.override(service_id=9999).status_code, 400)
        self.assertEqual(self.override(service_id='x').status_code, 400)

    def test_a_blank_price_means_use_the_catalogue_price(self):
        r = self.override(price='')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNone(CenterService.objects.get().price)

    def test_the_active_switch_is_read_as_a_switch(self):
        self.assertEqual(self.override(is_active='false').status_code, 200)
        self.assertFalse(CenterService.objects.get().is_active)

    def test_a_service_cannot_have_a_negative_price_or_gst_over_100(self):
        body = {'name': 'Bad', 'category': 'Hair', 'default_price': '-5', 'tax_percentage': '18', 'duration_mins': 30}
        self.assertEqual(self.api.post('/services/api/master/', body, format='json').status_code, 400)
        body.update(default_price='100', tax_percentage='150')
        self.assertEqual(self.api.post('/services/api/master/', body, format='json').status_code, 400)

    def test_a_service_length_must_be_a_sensible_number_of_minutes(self):
        for bad in (-5, 0, 99999):
            body = {'name': f'L{bad}', 'category': 'Hair', 'default_price': '100', 'tax_percentage': '18', 'duration_mins': bad}
            self.assertEqual(self.api.post('/services/api/master/', body, format='json').status_code, 400, bad)
        ok = {'name': 'Fine', 'category': 'Hair', 'default_price': '100', 'tax_percentage': '18', 'duration_mins': 45}
        self.assertEqual(self.api.post('/services/api/master/', ok, format='json').status_code, 201)

    def test_a_service_loaded_from_sap_can_be_edited_whole(self):
        """SAP gives no length and may repeat a name; the edit form sends every field back."""
        a = ServiceMaster.objects.create(name='Facial', category='Skin', default_price='0', tax_percentage='5', duration_mins=0)
        ServiceMaster.objects.create(name='Facial', category='Skin', default_price='0', tax_percentage='5', duration_mins=0)
        body = {'name': 'Facial', 'category': 'Skin', 'default_price': '900', 'tax_percentage': '5', 'duration_mins': 0}
        r = self.api.put(f'/services/api/master/{a.pk}/', body, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        a.refresh_from_db()
        self.assertEqual(str(a.default_price), '900.00')
        # but a real edit still cannot set a nonsense length
        self.assertEqual(self.api.put(f'/services/api/master/{a.pk}/', dict(body, duration_mins=-3), format='json').status_code, 400)

    def test_two_services_cannot_share_a_name_within_a_category(self):
        body = {'name': 'Cut', 'category': 'Hair', 'default_price': '100', 'tax_percentage': '18', 'duration_mins': 30}
        ServiceMaster.objects.filter(pk=self.service.pk).update(category='Hair')
        r = self.api.post('/services/api/master/', dict(body, name=' cut '), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('name', r.data)
        # the same name in another category is a different service
        self.assertEqual(self.api.post('/services/api/master/', dict(body, category='Skin'), format='json').status_code, 201)

    def test_saving_a_service_under_its_own_name_is_fine(self):
        ServiceMaster.objects.filter(pk=self.service.pk).update(category='Hair')
        r = self.api.patch(f'/services/api/master/{self.service.pk}/', {'name': 'Cut', 'default_price': '550'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
