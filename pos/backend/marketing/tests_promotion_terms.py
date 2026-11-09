"""A discount promotion must discount something."""
import datetime

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from marketing.models import Promotion
from salon_admin.models import Center


class PromotionTermsTests(TestCase):
    URL = '/marketing/api/promotions/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        owner = CustomUser.objects.create_user(
            email='o@test.com', password='pw', full_name='Owner', is_superuser=True, is_staff=True, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(owner)
        today = datetime.date.today()
        self.base = {'name': 'Promo', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Percentage',
                     'start_date': str(today), 'end_date': str(today + datetime.timedelta(days=30)), 'config': {}}

    def post(self, **extra):
        return self.api.post(self.URL, dict(self.base, **extra), format='json')

    def test_no_value_or_zero_is_refused(self):
        for extra in ({}, {'discount_value': None}, {'discount_value': '0'}):
            r = self.post(**extra)
            self.assertEqual(r.status_code, 400, extra)
            self.assertIn('discount_value', r.data)
        self.assertEqual(Promotion.objects.count(), 0)

    def test_a_value_in_the_config_counts(self):
        self.assertEqual(self.post(config={'discount_value': 15}).status_code, 201)

    def test_a_real_discount_is_saved(self):
        self.assertEqual(self.post(discount_value='10').status_code, 201)

    def test_switching_a_promotion_off_does_not_ask_for_the_terms_again(self):
        promo = Promotion.objects.create(
            name='Old', promo_type='Discount', discount_type='Percentage', discount_value=None,
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=5))
        r = self.api.patch(f'{self.URL}{promo.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_other_promotion_kinds_are_not_held_to_the_discount_rule(self):
        r = self.post(promo_type='FlatPrice', config={'flat_price': 99})
        self.assertEqual(r.status_code, 201, r.data)
