from django.urls import path

from . import views

urlpatterns = [
    path('login/', views.LoginView.as_view(), name='client-login'),
    path('forgot/', views.ForgotView.as_view(), name='client-forgot'),
    path('logout/', views.LogoutView.as_view(), name='client-logout'),
    path('me/', views.MeView.as_view(), name='client-me'),
    path('home/', views.HomeView.as_view(), name='client-home'),
    path('bills/', views.BillsView.as_view(), name='client-bills'),
    path('bills/<int:pk>/', views.BillView.as_view(), name='client-bill'),
    path('appointments/', views.AppointmentsView.as_view(), name='client-appointments'),
    path('appointments/book/', views.BookView.as_view(), name='client-book'),
    path('appointments/<int:pk>/cancel/', views.CancelBookingView.as_view(), name='client-cancel'),
    path('book/options/', views.BookOptionsView.as_view(), name='client-book-options'),
    path('pin/', views.PinView.as_view(), name='client-pin'),
]
