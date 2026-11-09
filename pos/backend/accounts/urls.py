from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    ChangePasswordView, ChatUserListView, CustomAuthToken, MessageReactionViewSet,
    MessageViewSet, ResetStaffUserPasswordView, UnreadMessageCountView, UserViewSet,
)

router = DefaultRouter()
router.register(r'users', UserViewSet, basename='users')
router.register(r'chat/messages', MessageViewSet, basename='chat-messages')
router.register(r'chat/reactions', MessageReactionViewSet, basename='chat-reactions')

urlpatterns = [
    path('api/login/', CustomAuthToken.as_view(), name='api_login'),
    path('api/change-password/', ChangePasswordView.as_view(), name='change-password'),
    path('api/users/<int:user_id>/reset-password/', ResetStaffUserPasswordView.as_view(),
         name='reset-user-password'),
    path('api/chat/users/', ChatUserListView.as_view(), name='chat-users'),
    path('api/chat/unread/', UnreadMessageCountView.as_view(), name='chat-unread'),
    path('api/', include(router.urls)),
]
