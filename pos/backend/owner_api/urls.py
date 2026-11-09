from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.OwnerLoginView.as_view(), name='owner-login'),
    path('logout/', views.LogoutView.as_view(), name='owner-logout'),
    path('me/', views.MeView.as_view(), name='owner-me'),
    path('today/', views.TodayView.as_view(), name='owner-today'),
    path('month/', views.MonthView.as_view(), name='owner-month'),
    path('invoices/', views.InvoiceListView.as_view(), name='owner-invoices'),
    path('invoices/<int:pk>/', views.InvoiceDetailView.as_view(), name='owner-invoice'),
    path('staff/', views.StaffView.as_view(), name='owner-staff'),
    path('reports/', views.ReportListView.as_view(), name='owner-reports'),
    path('reports/create/', views.ReportCreateView.as_view(), name='owner-report-create'),
    path('reports/<int:pk>/', views.ReportDetailView.as_view(), name='owner-report'),
    path('ai/', views.AiKeysView.as_view(), name='owner-ai-keys'),
    path('master-pins/', views.MasterPinsView.as_view(), name='owner-master-pins'),
    path('settings/', views.SettingsView.as_view(), name='owner-settings'),
]
