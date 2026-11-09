from django.urls import path

from approvals import views

urlpatterns = [
    path('api/policies/', views.PoliciesView.as_view(), name='approval-policies'),
    path('api/policies/<str:kind>/', views.PolicyDetailView.as_view(), name='approval-policy'),
    path('api/inbox/', views.InboxView.as_view(), name='approval-inbox'),
    path('api/history/', views.HistoryView.as_view(), name='approval-history'),
    path('api/requests/<int:pk>/', views.RequestDetailView.as_view(), name='approval-request'),
    path('api/requests/<int:pk>/decide/', views.DecideView.as_view(), name='approval-decide'),
    path('api/requests/<int:pk>/cancel/', views.CancelView.as_view(), name='approval-cancel'),
    path('api/link/<str:token>/', views.LinkView.as_view(), name='approval-link'),
]
