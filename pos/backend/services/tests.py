from decimal import Decimal
from rest_framework.test import APIRequestFactory
from django.test import TestCase

from salon_admin.models import Center
from .models import CenterService, ServiceMaster
from .serializers import ServiceMasterSerializer


class ServiceMasterSerializerTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Test Center', display_name='Test Center')
        self.service = ServiceMaster.objects.create(
            name='Haircut',
            category='Cuts',
            default_price=Decimal('120.00'),
            level='Organisation',
        )

    def test_price_field_uses_center_override_when_available(self):
        CenterService.objects.create(center=self.center, service=self.service, price=Decimal('150.00'), is_active=True)
        request = APIRequestFactory().get('/services/api/master/', {'center_id': str(self.center.id)})

        serializer = ServiceMasterSerializer(self.service, context={'request': request})

        self.assertEqual(serializer.data['price'], Decimal('150.00'))

    def test_price_field_falls_back_to_default_price(self):
        request = APIRequestFactory().get('/services/api/master/', {'center_id': str(self.center.id)})

        serializer = ServiceMasterSerializer(self.service, context={'request': request})

        self.assertEqual(serializer.data['price'], Decimal('120.00'))
