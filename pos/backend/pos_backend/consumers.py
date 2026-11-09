import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from pos_backend import realtime


class DashboardConsumer(AsyncWebsocketConsumer):
    """Live dashboard events, per branch.

    Needs a valid token (?token=...) and the Dashboard permission. The socket
    joins only the channels of the branches the user can see; an all-branches
    role or the superuser joins the all-branches channel.
    """

    groups_joined = ()

    async def connect(self):
        user = await database_sync_to_async(realtime.user_from_token)(
            realtime.token_from_scope(self.scope))
        if user is None:
            await self.close(code=4001)
            return
        groups = await database_sync_to_async(realtime.dashboard_groups_for)(user)
        if not groups:
            await self.close(code=4003)
            return
        self.groups_joined = tuple(groups)
        for group in self.groups_joined:
            await self.channel_layer.group_add(group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        for group in self.groups_joined:
            await self.channel_layer.group_discard(group, self.channel_name)

    async def dashboard_update(self, event):
        # `type` is the envelope the frontend listens for; what happened is in
        # `update_type` (it used to be sent as `type`, so the page discarded it).
        await self.send(text_data=json.dumps({
            'type': 'dashboard_update',
            'update_type': event.get('update_type'),
            'data': event.get('data'),
        }))
