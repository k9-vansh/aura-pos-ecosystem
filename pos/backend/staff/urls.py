from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    StaffMemberViewSet, ServiceLogViewSet, StaffConsumptionLogViewSet,
    StaffTransferViewSet, StaffToolTrackerViewSet, PayrollRecordViewSet, DesignationViewSet,
    revenue_report, usage_report, consumption_report,
)

router = DefaultRouter()
router.register(r'designations', DesignationViewSet, basename='designation')
router.register(r'members', StaffMemberViewSet, basename='staffmember')
router.register(r'logs', ServiceLogViewSet, basename='servicelog')
router.register(r'consumptions', StaffConsumptionLogViewSet, basename='staffconsumption')
router.register(r'transfers', StaffTransferViewSet, basename='stafftransfer')
router.register(r'tools', StaffToolTrackerViewSet, basename='stafftooltracker')
router.register(r'payrolls', PayrollRecordViewSet, basename='payroll')

urlpatterns = [

    path('api/', include(router.urls)),
    path('api/reports/revenue/', revenue_report, name='revenue-report'),
    path('api/reports/usage/', usage_report, name='usage-report'),
    path('api/reports/consumption/', consumption_report, name='consumption-report'),
]

