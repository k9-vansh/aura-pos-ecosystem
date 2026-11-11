"""
URL configuration for pos_backend project.
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.views.static import serve
from django.conf import settings
from django.conf.urls.static import static

from pos_backend.app_settings_api import AppSettingsView
from pos_backend.sap_monitor_api import SapSyncListView, SapSyncResolveView, SapSyncRetryView
from pos_backend.views import root_portal_view
from pos_backend.health_api import health_check_view

urlpatterns = [
    path('healthz/', health_check_view, name='health-check'),
    path('', root_portal_view, name='root-portal'),
    path('admin/', admin.site.urls),
    path('accounts/', include('accounts.urls')),
    path('salon_admin/', include('salon_admin.urls')),
    path('inventory/', include('inventory.urls')),
    path('marketing/', include('marketing.urls')),
    path('staff/', include('staff.urls')),
    path('appointments/', include('appointments.urls')),
    path('clients/', include('clients.urls')),
    path('services/', include('services.urls')),
    path('billing/', include('billing.urls')),
    path('finance/', include('finance.urls')),
    path('audit_logs/', include('audit_logs.urls')),
    path('approvals/', include('approvals.urls')),
    path('notifications/', include('notifications.urls')),
    path('owner/api/', include('owner_api.urls')),
    path('client/api/', include('client_api.urls')),
    path('team/api/', include('staff_api.urls')),
    path('app-settings/api/', AppSettingsView.as_view(), name='app-settings'),
    path('sap-sync/api/', SapSyncListView.as_view(), name='sap-sync'),
    path('sap-sync/api/<int:pk>/retry/', SapSyncRetryView.as_view(), name='sap-sync-retry'),
    path('sap-sync/api/<int:pk>/resolve/', SapSyncResolveView.as_view(), name='sap-sync-resolve'),
]

# Serve uploaded media through Django only when explicitly enabled.
#
# This route used to be registered unconditionally, despite the comment saying
# "development only" - so every chat image and staff photograph was publicly
# retrievable by anyone who knew or guessed the path, and /media/ is in the
# audit middleware's excluded prefixes so the access was not even logged.
#
# In production serve /media/ from nginx behind authentication, or move uploads
# to object storage with signed URLs.
if getattr(settings, 'SERVE_MEDIA_FILES', settings.DEBUG):
    urlpatterns += [
        re_path(r"^media/(?P<path>.*)$", serve, {"document_root": settings.MEDIA_ROOT}),
    ]
