from django.urls import path

from notifications import views

urlpatterns = [
    path('api/bell/', views.BellView.as_view(), name='bell'),
    path('api/bell/read/', views.BellReadView.as_view(), name='bell-read'),
    path('api/rules/', views.RulesView.as_view(), name='notification-rules'),
    path('api/integrations/', views.IntegrationsView.as_view(), name='integrations'),
    path('api/integrations/<str:key>/', views.IntegrationDetailView.as_view(), name='integration'),
    path('api/integrations/<str:key>/test/', views.IntegrationTestView.as_view(), name='integration-test'),
    path('api/deliveries/', views.DeliveriesView.as_view(), name='deliveries'),
]
