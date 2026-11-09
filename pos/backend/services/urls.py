from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import ServiceMasterViewSet, CenterServiceViewSet

router = DefaultRouter()
router.register(r'master', ServiceMasterViewSet, basename='servicemaster')
router.register(r'center', CenterServiceViewSet, basename='centerservice')

urlpatterns = [
    path('api/', include(router.urls)),
]
