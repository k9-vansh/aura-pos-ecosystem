import json

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer

from pos_backend import realtime
from notifications.service import user_group


class UserConsumer(AsyncWebsocketConsumer):
    """A signed-in user's private channel: bell notifications and approval updates.

    Needs a valid token (?token=...). Joins only that user's own group, so
    nobody can listen to anyone else's messages.
    """

    group = None

    async def connect(self):
        user = await database_sync_to_async(realtime.user_from_token)(
            realtime.token_from_scope(self.scope))
        if user is None:
            await self.close(code=4001)
            return
        self.group = user_group(user.pk)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()

    async def disconnect(self, close_code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def user_event(self, event):
        await self.send(text_data=json.dumps(event['payload']))
