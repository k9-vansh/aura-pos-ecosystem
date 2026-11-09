from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.LoginView.as_view(), name='staff-login'),
    path('logout/', views.LogoutView.as_view(), name='staff-logout'),
    path('me/', views.MeView.as_view(), name='staff-me'),
    path('jobcard/', views.JobCardView.as_view(), name='staff-jobcard'),
    path('month/', views.MonthView.as_view(), name='staff-month'),
    path('appointments/', views.AppointmentsView.as_view(), name='staff-appointments'),
    path('profile/', views.ProfileView.as_view(), name='staff-profile'),
]
