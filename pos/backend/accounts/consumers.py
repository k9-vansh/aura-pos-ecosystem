import json
import logging
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone

logger = logging.getLogger(__name__)

# `Message.content` is a TextField, so before this cap an oversized body was
# accepted and then pushed into every participant's message list.
MAX_MESSAGE_CHARS = 4000
# `MessageReaction.emoji` is max_length=10.
MAX_EMOJI_CHARS = 10
# A read receipt used to accept an unbounded list, giving an unbounded IN query.
MAX_READ_RECEIPT_IDS = 500


def _as_id(value):
    """Coerce a client-supplied primary key to a positive int, or None.

    Everything arriving over the socket is caller-controlled JSON. Passing it
    straight into a queryset meant a string id raised ValueError deep inside a
    database helper, where a bare `except Exception` swallowed it and the action
    failed silently.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


class ChatConsumer(AsyncWebsocketConsumer):
    """
    WebSocket consumer for real-time chat.
    URL pattern: ws://host/ws/chat/<room_id>/?token=<auth_token>
    """

    async def connect(self):
        # The route is `(?P<room_id>\d+)`, so this is always numeric - but keep
        # it an int, because `int(self.room_id)` was being called ad hoc in one
        # broadcast payload and left as a string in the others, so the same room
        # arrived at the client as 7 in some events and "7" in the rest.
        self.room_id = int(self.scope['url_route']['kwargs']['room_id'])
        self.user = None

        # Authenticate via token in query string
        token_key = self._get_token_from_scope()
        if not token_key:
            await self.close(code=4001)
            return

        self.user = await self._get_user_from_token(token_key)
        if not self.user:
            await self.close(code=4001)
            return

        # The role must grant Chat (finding R13), exactly as the REST API checks.
        if not await self._may_chat(self.user):
            await self.close(code=4003)
            return

        # Check room membership
        is_participant = await self._is_participant(self.room_id, self.user)
        if not is_participant:
            await self.close(code=4003)
            return

        # Join room group. `room_group_name` is deliberately set only once the
        # connection has been authorised: it used to be assigned on the first
        # line, so a rejected connection still ran the full `disconnect` body -
        # discarding a group it had never joined and, worse, broadcasting to a
        # room the caller had just been refused entry to.
        self.room_group_name = f'chat_{self.room_id}'
        await self.channel_layer.group_add(self.room_group_name, self.channel_name)
        
        # Track online presence in user-specific group
        self.user_group = f'user_{self.user.id}'
        await self.channel_layer.group_add(self.user_group, self.channel_name)

        await self.accept()

        # Broadcast this user is online
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'user_online',
                'user_id': self.user.id,
                'full_name': self.user.full_name,
            }
        )

    def _get_token_from_scope(self):
        query_string = self.scope.get('query_string', b'').decode('utf-8')
        for part in query_string.split('&'):
            if part.startswith('token='):
                return part[6:]
        return None

    @database_sync_to_async
    def _get_user_from_token(self, token_key):
        """Resolve a token to a user, applying the SAME expiry rule as HTTP.

        This used to accept any token that existed, regardless of age, while
        `ExpiringTokenAuthentication` rejected tokens older than
        API_TOKEN_MAX_AGE_DAYS on every REST call. The websocket was therefore a
        way around token expiry: a stale token from a device that had been
        logged out for months still opened a live chat session.
        """
        import datetime

        from django.conf import settings

        try:
            from rest_framework.authtoken.models import Token
            token = Token.objects.select_related('user').get(key=token_key)
        except Exception:
            return None

        if not token.user.is_active:
            return None

        max_age_days = getattr(settings, 'API_TOKEN_MAX_AGE_DAYS', 7)
        if token.created < timezone.now() - datetime.timedelta(days=max_age_days):
            logger.info('[ChatConsumer] rejected expired token for user %s', token.user_id)
            return None

        return token.user

    @database_sync_to_async
    def _may_chat(self, user):
        from pos_backend.permissions import has_module_permission
        return has_module_permission(user, 'chat', 'read', 'messages')

    @database_sync_to_async
    def _is_participant(self, room_id, user):
        try:
            from .models import ChatRoom
            return ChatRoom.objects.filter(id=room_id, participants=user).exists()
        except Exception:
            return False

    async def disconnect(self, close_code):
        if hasattr(self, 'room_group_name'):
            # Broadcast offline status
            if self.user:
                await self.channel_layer.group_send(
                    self.room_group_name,
                    {
                        'type': 'user_offline',
                        'user_id': self.user.id,
                    }
                )
            await self.channel_layer.group_discard(self.room_group_name, self.channel_name)
        if hasattr(self, 'user_group'):
            await self.channel_layer.group_discard(self.user_group, self.channel_name)

    async def receive(self, text_data):
        """Handle incoming WebSocket messages from the client."""
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        msg_type = data.get('type')

        if msg_type == 'chat_message':
            await self._handle_chat_message(data)
        elif msg_type == 'typing':
            await self._handle_typing(data)
        elif msg_type == 'stop_typing':
            await self._handle_stop_typing()
        elif msg_type == 'read_receipt':
            await self._handle_read_receipt(data)
        elif msg_type == 'reaction':
            await self._handle_reaction(data)
        elif msg_type == 'delete_message':
            await self._handle_delete_message(data)
        elif msg_type == 'edit_message':
            await self._handle_edit_message(data)

    async def _send_error(self, message):
        """Tell the sender their action failed, instead of dropping it silently."""
        await self.send(text_data=json.dumps({'type': 'error', 'message': message}))

    async def _handle_chat_message(self, data):
        content = str(data.get('content') or '').strip()
        reply_to_id = _as_id(data.get('reply_to_id'))

        if not content:
            return
        if len(content) > MAX_MESSAGE_CHARS:
            # `content` is a TextField, so an oversized body was accepted and
            # then rendered into every participant's message list.
            await self._send_error(
                f'That message is too long ({len(content)} characters). '
                f'The limit is {MAX_MESSAGE_CHARS}.'
            )
            return

        # Save to database
        message = await self._save_message(content, reply_to_id)
        if not message:
            # The send used to fail in total silence: the handler returned, the
            # message never appeared, and the sender had no way to know it had
            # not been delivered.
            await self._send_error('Your message could not be sent. Please try again.')
            return

        # Update room's updated_at for sorting in room list
        await self._touch_room()

        # Broadcast to room
        reply_data = None
        if reply_to_id:
            reply_data = await self._get_reply_preview(reply_to_id)

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'chat_message',
                'id': message['id'],
                'content': message['content'],
                'sender_id': self.user.id,
                'sender_name': self.user.full_name,
                'timestamp': message['timestamp'],
                'status': 'sent',
                'reply_to': reply_data,
            }
        )

        # Delivery status is promoted when a recipient acknowledges the
        # message. This used to call a `database_sync_to_async` helper whose
        # entire body was `pass`, paying for a thread hop on every message sent.

    async def _handle_typing(self, data):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'typing_indicator',
                'user_id': self.user.id,
                'user_name': self.user.full_name,
                'is_typing': True,
            }
        )

    async def _handle_stop_typing(self):
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'typing_indicator',
                'user_id': self.user.id,
                'user_name': self.user.full_name,
                'is_typing': False,
            }
        )

    async def _handle_read_receipt(self, data):
        # Whatever the client sent, reduce it to a bounded list of integers. A
        # string arrived here as a list of characters, and an unbounded list
        # became an unbounded `IN (...)` query.
        raw = data.get('message_ids') or []
        if not isinstance(raw, (list, tuple)):
            raw = [raw]
        message_ids = [i for i in (_as_id(v) for v in raw[:MAX_READ_RECEIPT_IDS]) if i]
        if not message_ids:
            return

        updated_sender_ids = await self._mark_messages_read(message_ids)

        # Notify senders their messages were read
        for sender_id in updated_sender_ids:
            await self.channel_layer.group_send(
                f'user_{sender_id}',
                {
                    'type': 'messages_read',
                    'message_ids': message_ids,
                    'reader_id': self.user.id,
                    'room_id': self.room_id,
                }
            )

    async def _handle_reaction(self, data):
        message_id = _as_id(data.get('message_id'))
        emoji = str(data.get('emoji') or '').strip()
        if not message_id or not emoji:
            return
        if len(emoji) > MAX_EMOJI_CHARS:
            # `MessageReaction.emoji` is max_length=10; a longer value raised a
            # DataError on MySQL, which the helper swallowed into
            # {'action': 'error'} and then broadcast as a reaction anyway.
            return

        reaction = await self._save_reaction(message_id, emoji)
        if not reaction:
            # Either the write failed or the message is not in this room.
            return

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                'type': 'reaction_update',
                'message_id': message_id,
                'user_id': self.user.id,
                'user_name': self.user.full_name,
                'emoji': emoji,
                'action': reaction['action'],  # 'added' or 'removed'
            }
        )

    async def _handle_delete_message(self, data):
        message_id = _as_id(data.get('message_id'))
        if not message_id:
            return

        success = await self._soft_delete_message(message_id)
        if success:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'message_deleted',
                    'message_id': message_id,
                    'deleted_by': self.user.id,
                }
            )

    async def _handle_edit_message(self, data):
        message_id = _as_id(data.get('message_id'))
        new_content = str(data.get('content') or '').strip()
        if not message_id or not new_content:
            return
        if len(new_content) > MAX_MESSAGE_CHARS:
            await self._send_error(
                f'That message is too long ({len(new_content)} characters). '
                f'The limit is {MAX_MESSAGE_CHARS}.'
            )
            return

        success = await self._edit_message(message_id, new_content)
        if success:
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    'type': 'message_edited',
                    'message_id': message_id,
                    'content': new_content,
                    'edited_by': self.user.id,
                }
            )

    # ─── Group event handlers (called by channel layer) ─────────────────────

    async def _forward(self, event, client_type):
        """Send a channel-layer event to this client under `client_type`.

        Every handler here used to build its frame as `{'type': 'x', **event}`.
        `**event` is expanded last, so the channel-layer routing name in
        `event['type']` overwrote the literal. For most events the two names are
        the same string and nothing showed; `typing_indicator` is dispatched
        under that name while the frontend switches on `typing`, so the
        "... is typing" indicator never fired at all.
        """
        payload = {k: v for k, v in event.items() if k != 'type'}
        payload['type'] = client_type
        await self.send(text_data=json.dumps(payload))

    async def chat_message(self, event):
        await self._forward(event, 'chat_message')

    async def typing_indicator(self, event):
        # Don't send typing events back to the same user
        if event.get('user_id') != self.user.id:
            await self._forward(event, 'typing')

    async def user_online(self, event):
        if event.get('user_id') != self.user.id:
            await self._forward(event, 'user_online')

    async def user_offline(self, event):
        if event.get('user_id') != self.user.id:
            await self._forward(event, 'user_offline')

    async def messages_read(self, event):
        await self._forward(event, 'messages_read')

    async def reaction_update(self, event):
        await self._forward(event, 'reaction_update')

    async def message_deleted(self, event):
        await self._forward(event, 'message_deleted')

    async def message_edited(self, event):
        await self._forward(event, 'message_edited')

    # ─── Database helpers ────────────────────────────────────────────────────

    @database_sync_to_async
    def _save_message(self, content, reply_to_id=None):
        try:
            from .models import ChatRoom, Message
            room = ChatRoom.objects.get(id=self.room_id)

            # A reply may only point at a message in the same room. The id came
            # from the client and was written through unchecked, so a reply
            # could be threaded onto a message in a conversation the sender was
            # not part of. An id that does not exist at all raised an
            # IntegrityError here, which was caught below - and the sender's
            # message was discarded with no error shown.
            if reply_to_id and not Message.objects.filter(
                id=reply_to_id, room_id=self.room_id
            ).exists():
                reply_to_id = None

            msg = Message.objects.create(
                sender=self.user,
                room=room,
                content=content,
                reply_to_id=reply_to_id or None,
                status=Message.STATUS_SENT,
            )
            return {'id': msg.id, 'content': msg.content, 'timestamp': msg.timestamp.isoformat()}
        except Exception as e:
            logger.error(f"[ChatConsumer] Failed to save message: {e}", exc_info=True)
            return None

    @database_sync_to_async
    def _touch_room(self):
        try:
            from .models import ChatRoom
            ChatRoom.objects.filter(id=self.room_id).update(updated_at=timezone.now())
        except Exception:
            pass

    @database_sync_to_async
    def _get_reply_preview(self, reply_to_id):
        """Preview of the quoted message - restricted to this room.

        This was `Message.objects.get(id=reply_to_id)` with no room filter, and
        the first 100 characters of whatever it found were broadcast to everyone
        in the sender's room. Quoting a message id from a private conversation
        therefore leaked its text into a room of the sender's choosing. Message
        ids are sequential, so they did not need to be guessed.
        """
        try:
            from .models import Message
            msg = (Message.objects
                   .select_related('sender')
                   .filter(id=reply_to_id, room_id=self.room_id)
                   .first())
            if not msg:
                return None
            if msg.deleted_at:
                # Quoting used to resurrect the text of a deleted message.
                preview = '[Message deleted]'
            elif msg.content:
                preview = msg.content[:100]
            else:
                preview = '[Image]'
            return {
                'id': msg.id,
                'content': preview,
                'sender_name': msg.sender.full_name if msg.sender else '',
            }
        except Exception:
            logger.exception('[ChatConsumer] reply preview failed')
            return None

    @database_sync_to_async
    def _mark_messages_read(self, message_ids):
        """Mark messages as read, return list of sender IDs who need to be notified."""
        try:
            from .models import Message
            messages = Message.objects.filter(
                id__in=message_ids,
                room_id=self.room_id,
                status__in=[Message.STATUS_SENT, Message.STATUS_DELIVERED]
            ).exclude(sender=self.user).select_related('sender')
            
            sender_ids = set(m.sender_id for m in messages)
            messages.update(status=Message.STATUS_READ, is_read=True)
            return list(sender_ids)
        except Exception as e:
            logger.error(f"[ChatConsumer] read receipt failed: {e}")
            return []

    @database_sync_to_async
    def _save_reaction(self, message_id, emoji):
        """Toggle this user's reaction on a message in THIS room.

        The room check is the point: `get_or_create(message_id=...)` accepted any
        id in the table, so a reaction row could be attached to a message in a
        conversation the user had no access to - and it then appeared in that
        room's reaction list when its participants next loaded their history.
        """
        try:
            from .models import Message, MessageReaction

            if not Message.objects.filter(id=message_id, room_id=self.room_id).exists():
                logger.warning(
                    '[ChatConsumer] user %s tried to react to message %s outside room %s',
                    self.user.id, message_id, self.room_id,
                )
                return None

            reaction, created = MessageReaction.objects.get_or_create(
                message_id=message_id,
                user=self.user,
                defaults={'emoji': emoji}
            )
            if not created:
                if reaction.emoji == emoji:
                    reaction.delete()
                    return {'action': 'removed'}
                else:
                    reaction.emoji = emoji
                    reaction.save()
                    return {'action': 'changed'}
            return {'action': 'added'}
        except Exception:
            # Returning {'action': 'error'} used to be broadcast to the room as
            # though it were a real reaction event.
            logger.exception('[ChatConsumer] reaction failed')
            return None

    @database_sync_to_async
    def _soft_delete_message(self, message_id):
        try:
            from .models import Message
            msg = Message.objects.get(id=message_id, room_id=self.room_id, sender=self.user)
            msg.deleted_at = timezone.now()
            msg.content = None
            msg.save(update_fields=['deleted_at', 'content'])
            return True
        except Message.DoesNotExist:
            return False
        except Exception as e:
            logger.error(f"[ChatConsumer] delete failed: {e}")
            return False

    @database_sync_to_async
    def _edit_message(self, message_id, new_content):
        try:
            from .models import Message
            msg = Message.objects.get(id=message_id, room_id=self.room_id, sender=self.user)
            if msg.deleted_at:
                return False
            msg.content = new_content
            msg.edited_at = timezone.now()
            msg.save(update_fields=['content', 'edited_at'])
            return True
        except Message.DoesNotExist:
            return False
        except Exception as e:
            logger.error(f"[ChatConsumer] edit failed: {e}")
            return False
