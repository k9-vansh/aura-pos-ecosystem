from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CenterViewSet, RoleViewSet, dashboard_view, bulk_import_centers, bulk_import_template, sap_branches_view
from .usage_report import usage_report
from .analytics_endpoints import (
    analytics_revenues, analytics_clients, analytics_services, analytics_products,
    analytics_memberships, analytics_value_cards,
)
from .dashboard_endpoints import (
    dashboard_summary, dashboard_revenues, dashboard_clients,
    dashboard_finance, dashboard_staff, dashboard_services_products
)

router = DefaultRouter()
router.register(r'centers', CenterViewSet, basename='centers')
router.register(r'roles', RoleViewSet)

urlpatterns = [
    path('api/dashboard/', dashboard_view, name='admin-dashboard'),
    path('api/dashboard/summary/', dashboard_summary, name='dashboard-summary'),
    path('api/dashboard/revenues/', dashboard_revenues, name='dashboard-revenues'),
    path('api/dashboard/clients/', dashboard_clients, name='dashboard-clients'),
    path('api/dashboard/finance/', dashboard_finance, name='dashboard-finance'),
    path('api/analytics/revenues/', analytics_revenues, name='analytics-revenues'),
    path('api/analytics/clients/', analytics_clients, name='analytics-clients'),
    path('api/analytics/services/', analytics_services, name='analytics-services'),
    path('api/analytics/products/', analytics_products, name='analytics-products'),
    path('api/analytics/memberships/', analytics_memberships, name='analytics-memberships'),
    path('api/analytics/value-cards/', analytics_value_cards, name='analytics-value-cards'),
    path('api/usage-report/<str:section>/', usage_report, name='usage-report'),
    path('api/dashboard/staff/', dashboard_staff, name='dashboard-staff'),
    path('api/dashboard/services_products/', dashboard_services_products, name='dashboard-services-products'),
    path('api/centers/bulk-import/', bulk_import_centers, name='centers-bulk-import'),
    path('api/centers/bulk-import-template/', bulk_import_template, name='centers-bulk-import-template'),
    path('api/sap-branches/', sap_branches_view, name='sap-branches'),
    path('api/', include(router.urls)),
]
