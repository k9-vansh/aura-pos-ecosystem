"""
Chat REST tests.

The websocket and the REST API are two doors into the same data, and the same
mistakes had been made behind both. These pin the REST half:

  * `mark_read` filtered on `room_id` alone, so any signed-in user could clear
    the unread state of a conversation they had no part in;
  * `reply_to` was a writable field with no room check, giving the REST path the
    same cross-room disclosure the socket had - the quoted message's text comes
    back in `reply_to_preview`;
  * a quoted message that had been deleted had its text resurrected;
  * a message created over REST (the path image messages take) was never
    announced on the channel layer, so images appeared for nobody until reload.
"""

from channels.layers import get_channel_layer
from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import ChatRoom, CustomUser, Message
from pos_backend.testing import chat_only_role


class ChatRestTestBase(TestCase):
    def setUp(self):
        role = chat_only_role()
        self.alice = CustomUser.objects.create_user(
            email='alice@test.com', password='StrongPass!234', full_name='Alice', role=role)
        self.bob = CustomUser.objects.create_user(
            email='bob@test.com', password='StrongPass!234', full_name='Bob', role=role)
        self.mallory = CustomUser.objects.create_user(
            email='mallory@test.com', password='StrongPass!234', full_name='Mallory', role=role)

        self.private = ChatRoom.objects.create(name='Alice & Bob')
        self.private.participants.set([self.alice, self.bob])

        self.shared = ChatRoom.objects.create(name='Team', is_group=True)
        self.shared.participants.set([self.alice, self.mallory])

        self.secret = Message.objects.create(
            sender=self.alice, room=self.private,
            content='Owner password is hunter2 and payroll runs Friday',
            status=Message.STATUS_SENT,
        )

        self.as_alice = APIClient()
        self.as_alice.force_authenticate(self.alice)
        self.as_bob = APIClient()
        self.as_bob.force_authenticate(self.bob)
        self.as_mallory = APIClient()
        self.as_mallory.force_authenticate(self.mallory)


class MarkReadScopeTests(ChatRestTestBase):
    URL = '/accounts/api/chat/messages/mark_read/'

    def test_an_outsider_cannot_mark_a_room_read(self):
        response = self.as_mallory.post(self.URL, {'room_id': self.private.id})
        self.assertEqual(response.status_code, 403)

        self.secret.refresh_from_db()
        self.assertEqual(self.secret.status, Message.STATUS_SENT)

    def test_a_participant_can_mark_the_room_read(self):
        response = self.as_bob.post(self.URL, {'room_id': self.private.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['updated'], 1)

        self.secret.refresh_from_db()
        self.assertEqual(self.secret.status, Message.STATUS_READ)

    def test_own_messages_are_not_marked_read(self):
        response = self.as_alice.post(self.URL, {'room_id': self.private.id})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data['updated'], 0)

        self.secret.refresh_from_db()
        self.assertEqual(self.secret.status, Message.STATUS_SENT)

    def test_a_junk_room_id_is_a_400_not_a_500(self):
        for value in ('null', 'undefined', '', 'abc'):
            response = self.as_bob.post(self.URL, {'room_id': value})
            self.assertEqual(response.status_code, 400, value)
        # No room_id at all.
        self.assertEqual(self.as_bob.post(self.URL, {}).status_code, 400)

    def test_a_nonexistent_room_is_403_not_a_silent_success(self):
        response = self.as_bob.post(self.URL, {'room_id': 99999999})
        self.assertEqual(response.status_code, 403)


class ReplyToScopeTests(ChatRestTestBase):
    URL = '/accounts/api/chat/messages/'

    def test_cannot_quote_a_message_from_another_room(self):
        """The REST half of the cross-room disclosure."""
        response = self.as_mallory.post(self.URL, {
            'room_id': self.shared.id,
            'content': 'look at this',
            'reply_to': self.secret.id,
        })
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('hunter2', str(response.data))
        self.assertFalse(
            Message.objects.filter(room=self.shared, content='look at this').exists())

    def test_can_quote_a_message_in_the_same_room(self):
        response = self.as_bob.post(self.URL, {
            'room_id': self.private.id,
            'content': 'noted',
            'reply_to': self.secret.id,
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['reply_to_preview']['id'], self.secret.id)
        self.assertIn('hunter2', response.data['reply_to_preview']['content'])

    def test_a_deleted_quote_is_not_resurrected(self):
        reply = Message.objects.create(
            sender=self.bob, room=self.private, content='noted',
            reply_to=self.secret)
        self.secret.deleted_at = self.secret.timestamp
        self.secret.save(update_fields=['deleted_at'])

        response = self.as_bob.get(self.URL, {'room_id': self.private.id})
        self.assertEqual(response.status_code, 200)

        body = str(response.data)
        self.assertNotIn('hunter2', body)
        rows = response.data.get('results', response.data)
        quoted = [r for r in rows if r['id'] == reply.id][0]
        self.assertEqual(quoted['reply_to_preview']['content'], '[Message deleted]')

    def test_a_deleted_message_hides_its_own_content(self):
        self.secret.deleted_at = self.secret.timestamp
        self.secret.save(update_fields=['deleted_at'])

        response = self.as_bob.get(self.URL, {'room_id': self.private.id})
        rows = response.data.get('results', response.data)
        row = [r for r in rows if r['id'] == self.secret.id][0]
        self.assertIsNone(row['content'])

    def test_an_outsider_cannot_post_into_a_room(self):
        response = self.as_mallory.post(self.URL, {
            'room_id': self.private.id, 'content': 'hello',
        })
        self.assertEqual(response.status_code, 403)

    def test_a_junk_room_id_is_rejected(self):
        for value in ('null', 'abc', ''):
            response = self.as_bob.post(self.URL, {'room_id': value, 'content': 'x'})
            self.assertIn(response.status_code, (400, 403), value)

    def test_an_outsider_cannot_list_a_room(self):
        response = self.as_mallory.get(self.URL, {'room_id': self.private.id})
        self.assertEqual(response.status_code, 200)
        rows = response.data.get('results', response.data)
        self.assertEqual(rows, [])


@override_settings(
    CHANNEL_LAYERS={'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}})
class BroadcastOnRestCreateTests(ChatRestTestBase):
    URL = '/accounts/api/chat/messages/'

    def subscribe(self):
        """Join the room's group as a fake consumer so the broadcast is observable.

        `group_send` on the in-memory layer only delivers to channels that have
        joined the group, so the listener has to exist before the POST.
        """
        layer = get_channel_layer()
        async_to_sync(layer.group_add)(f'chat_{self.private.id}', 'test-listener')
        return layer

    def next_event(self, layer, timeout=3):
        """Receive one event, or fail - `receive` alone blocks forever."""
        import asyncio

        async def _get():
            return await asyncio.wait_for(layer.receive('test-listener'), timeout)

        return async_to_sync(_get)()

    def test_a_rest_created_message_is_broadcast_to_the_room(self):
        """Image messages go over REST; they used to reach nobody until reload."""
        layer = self.subscribe()
        response = self.as_bob.post(self.URL, {
            'room_id': self.private.id, 'content': 'sent over rest',
        })
        self.assertEqual(response.status_code, 201, response.data)

        event = self.next_event(layer)
        self.assertEqual(event['type'], 'chat_message')
        self.assertEqual(event['content'], 'sent over rest')
        self.assertEqual(event['sender_id'], self.bob.id)
        self.assertEqual(event['id'], response.data['id'])

    def test_the_broadcast_carries_the_reply_preview(self):
        layer = self.subscribe()
        self.as_bob.post(self.URL, {
            'room_id': self.private.id, 'content': 'quoting',
            'reply_to': self.secret.id,
        })
        event = self.next_event(layer)
        self.assertIsNotNone(event['reply_to'])
        self.assertEqual(event['reply_to']['id'], self.secret.id)

    def test_a_broadcast_failure_does_not_lose_the_message(self):
        """The notification is best effort; the save is not."""
        with override_settings(CHANNEL_LAYERS={}):
            response = self.as_bob.post(self.URL, {
                'room_id': self.private.id, 'content': 'saved anyway',
            })
        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            Message.objects.filter(room=self.private, content='saved anyway').exists())
