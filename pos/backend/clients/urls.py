from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ClientViewSet

from .pin_views import MasterPinsView, PinNeededView, PinSendView, PinVerifyView

router = DefaultRouter()
router.register(r'clients', ClientViewSet, basename='clients')

urlpatterns = [
    path('api/pin/verify/', PinVerifyView.as_view(), name='client-pin-verify'),
    path('api/pin/needed/', PinNeededView.as_view(), name='client-pin-needed'),
    path('api/pin/send/', PinSendView.as_view(), name='client-pin-send'),
    path('api/master-pins/', MasterPinsView.as_view(), name='master-pins'),
    path('api/', include(router.urls)),
]
