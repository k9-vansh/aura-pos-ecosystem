from rest_framework import routers
from .views import InvoiceViewSet, AdvancePaymentViewSet, BillChangeLogViewSet
from django.urls import path, include

router = routers.DefaultRouter()
router.register(r'invoices', InvoiceViewSet, basename='invoices')
router.register(r'advances', AdvancePaymentViewSet, basename='advances')
router.register(r'change-logs', BillChangeLogViewSet, basename='change-logs')

urlpatterns = [
    path('', include(router.urls)),
]
