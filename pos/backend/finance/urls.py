from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    MultiSalonBalancesView,
    MultiSalonSalesExportView,
    MultiSalonCategoriesView,
    MultiSalonServiceDrilldownView,
    MultiSalonProductDrilldownView,
    MultiSalonClientsView,
    MultiSalonStaffView,
    PettyCashEntryViewSet, PettyCashCategoryViewSet, DailyClosingViewSet, IncentiveConfigViewSet, ShiftViewSet,
    IncentiveRuleViewSet, ExportFinanceView,
    RegisterSummaryView, MonthlySalesView, DetailedRevenuesView,
    RefundsView, ProcurementReportView,
    TaxReportView, SapReconciliationView, ServiceDrilldownView, StaffPerformanceReportView, ManagerDiscountsAuditView,
    StaffIncentiveCalculationView, MultiSalonExportView,
)

router = DefaultRouter()
router.register(r'petty-cash', PettyCashEntryViewSet, basename='petty-cash')
router.register(r'petty-cash-categories', PettyCashCategoryViewSet, basename='petty-cash-category')
router.register(r'daily-closing', DailyClosingViewSet, basename='daily-closing')
router.register(r'incentives', IncentiveConfigViewSet, basename='incentives')
router.register(r'rules', IncentiveRuleViewSet, basename='rules')
router.register(r'shifts', ShiftViewSet, basename='shifts')

urlpatterns = [
    path('api/reports/tax/', TaxReportView.as_view()),
    path('api/reports/sap-reconciliation/', SapReconciliationView.as_view(), name='sap-reconciliation'),
    path('api/reports/services/', ServiceDrilldownView.as_view()),
    path('api/reports/staff-performance/', StaffPerformanceReportView.as_view()),
    path('api/reports/discounts/', ManagerDiscountsAuditView.as_view()),
    path('api/reports/incentive-calculation/', StaffIncentiveCalculationView.as_view(), name='incentive-calculation'),
    path('api/', include(router.urls)),
    path('api/export/', ExportFinanceView.as_view(), name='finance-export'),
    path('api/export_multi_salon/', MultiSalonExportView.as_view(), name='export_multi_salon'),
    path('api/reports/multi_salon/balances/', MultiSalonBalancesView.as_view(), name='multi_salon_balances'),
    path('api/reports/multi_salon/sales_export/', MultiSalonSalesExportView.as_view(), name='multi_salon_sales_export'),
    path('api/reports/multi_salon/categories/', MultiSalonCategoriesView.as_view(), name='multi_salon_categories'),
    path('api/reports/multi_salon/drilldown/services/', MultiSalonServiceDrilldownView.as_view(), name='multi_salon_drilldown_services'),
    path('api/reports/multi_salon/drilldown/products/', MultiSalonProductDrilldownView.as_view(), name='multi_salon_drilldown_products'),
    path('api/reports/multi_salon/clients/', MultiSalonClientsView.as_view(), name='multi_salon_clients'),
    path('api/reports/multi_salon/staff/', MultiSalonStaffView.as_view(), name='multi_salon_staff'),
    path('api/register_summary/', RegisterSummaryView.as_view(), name='register_summary'),
    path('api/monthly_sales/', MonthlySalesView.as_view(), name='monthly_sales'),
    path('api/detailed_revenues/', DetailedRevenuesView.as_view(), name='detailed_revenues'),
    path('api/refunds/', RefundsView.as_view(), name='refunds'),
    path('api/procurement/', ProcurementReportView.as_view(), name='procurement'),
]
