from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate
from salon_admin.models import Center
from salon_admin.views import CenterViewSet
from django.contrib.auth import get_user_model

User = get_user_model()

class CenterSoftDeleteTest(TestCase):
    def setUp(self):
        self.factory = APIRequestFactory()
        self.user = User.objects.create_superuser(email='admin@test.com', password='password')
        self.center = Center.objects.create(center_name='Test Center', address='Test Location', is_active=True)

    def test_center_soft_delete(self):
        """
        Verify that a DELETE request to a center sets is_active=False
        and correctly filters it out from get_queryset.
        """
        view = CenterViewSet.as_view({'delete': 'destroy', 'get': 'list'})
        
        # Soft delete the center
        request = self.factory.delete(f'/api/centers/{self.center.id}/')
        force_authenticate(request, user=self.user)
        response = view(request, pk=self.center.id)
        
        self.assertEqual(response.status_code, 204)
        
        # Verify it still exists in the database but is_active=False
        self.center.refresh_from_db()
        self.assertFalse(self.center.is_active)
        
        # Verify it is filtered out from get_queryset
        request_get = self.factory.get('/api/centers/')
        force_authenticate(request_get, user=self.user)
        response_get = view(request_get)
        
        self.assertEqual(response_get.status_code, 200)
        self.assertEqual(len(response_get.data), 0)
