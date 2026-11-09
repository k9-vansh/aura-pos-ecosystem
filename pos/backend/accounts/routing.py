from django.urls import re_path
from . import consumers as account_consumers
from pos_backend import consumers as dashboard_consumers
from notifications import consumers as notification_consumers

websocket_urlpatterns = [
    re_path(r'^ws/chat/(?P<room_id>\d+)/$', account_consumers.ChatConsumer.as_asgi()),
    re_path(r'^ws/user/$', notification_consumers.UserConsumer.as_asgi()),
    re_path(r'^ws/dashboard/$', dashboard_consumers.DashboardConsumer.as_asgi()),
]
