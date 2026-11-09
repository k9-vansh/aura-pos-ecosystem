from django.db import models
from rest_framework.routers import DefaultRouter
from django.urls import path, include
from .views import WhatsAppMessageViewSet, PromotionViewSet, ValueCardViewSet, MembershipViewSet, PackageViewSet

router = DefaultRouter()
router.register(r'whatsapp', WhatsAppMessageViewSet, basename='whatsapp')
router.register(r'promotions', PromotionViewSet, basename='promotion')
router.register(r'cards', ValueCardViewSet, basename='card')
router.register(r'memberships', MembershipViewSet, basename='membership')
router.register(r'packages', PackageViewSet, basename='package')

urlpatterns = [
    path('api/', include(router.urls)),
]
