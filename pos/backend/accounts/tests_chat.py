"""
Chat websocket tests.

The websocket is a second authentication surface, and it had drifted from the
REST one. These tests pin the defects found reading `accounts/consumers.py`:

  * an expired token still opened a live chat session, because the consumer
    resolved tokens itself and never applied `API_TOKEN_MAX_AGE_DAYS`;
  * quoting a message id from another room broadcast that message's text into
    the caller's room (`_get_reply_preview` had no room filter);
  * a reply could be persisted pointing at a message in another room;
  * a reaction row could be attached to any message id in the database;
  * a message that failed to save vanished with no error to the sender;
  * an oversized body, a non-integer id and an unbounded read-receipt list were
    all accepted.

The test methods are `async def` on purpose. A WebsocketCommunicator holds a
future belonging to the event loop that created it, so wrapping each individual
call in `async_to_sync` puts every step in a different loop and the application
task is cancelled underneath it.
"""

import datetime

from asgiref.sync import sync_to_async
from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.authtoken.models import Token

from accounts.consumers import MAX_MESSAGE_CHARS, ChatConsumer, _as_id
from accounts.models import ChatRoom, CustomUser, Message, MessageReaction
from pos_backend.testing import chat_only_role


async def connect(room_id, token, timeout=5):
    """Open a communicator against the chat consumer. Returns (comm, accepted)."""
    comm = WebsocketCommunicator(
        ChatConsumer.as_asgi(),
        f'/ws/chat/{room_id}/?token={token}',
    )
    # `url_route` is normally supplied by the URLRouter; the consumer is being
    # instantiated directly here, so provide it.
    comm.scope['url_route'] = {'kwargs': {'room_id': str(room_id)}}
    connected, code = await comm.connect(timeout)
    return comm, connected, code


async def drain(comm, limit=6, timeout=1.0):
    """Collect up to `limit` frames, stopping when the socket goes quiet.

    `receive_nothing` is used to decide whether anything is waiting, rather than
    letting `receive_json_from` time out: on timeout asgiref cancels the
    application task, so the consumer would be dead before `disconnect()`.
    """
    frames = []
    for _ in range(limit):
        if await comm.receive_nothing(timeout, interval=0.02):
            break
        frames.append(await comm.receive_json_from(timeout))
    return frames


@sync_to_async
def refetch(model, pk):
    return model.objects.filter(pk=pk).first()


@sync_to_async
def count(queryset):
    return queryset.count()


@sync_to_async
def exists(queryset):
    return queryset.exists()


@sync_to_async
def first(queryset):
    return queryset.first()


class ChatSocketTestBase(TransactionTestCase):
    """TransactionTestCase: the consumer's database work happens in a thread."""

    reset_sequences = True

    def setUp(self):
        role = chat_only_role()
        self.alice = CustomUser.objects.create_user(
            email='alice@test.com', password='StrongPass!234', full_name='Alice', role=role)
        self.bob = CustomUser.objects.create_user(
            email='bob@test.com', password='StrongPass!234', full_name='Bob', role=role)
        self.mallory = CustomUser.objects.create_user(
            email='mallory@test.com', password='StrongPass!234', full_name='Mallory', role=role)

        self.alice_token = Token.objects.create(user=self.alice).key
        self.bob_token = Token.objects.create(user=self.bob).key
        self.mallory_token = Token.objects.create(user=self.mallory).key

        # The private room: Alice and Bob only.
        self.private = ChatRoom.objects.create(name='Alice & Bob')
        self.private.participants.set([self.alice, self.bob])

        # The room Mallory is in.
        self.shared = ChatRoom.objects.create(name='Team', is_group=True)
        self.shared.participants.set([self.alice, self.mallory])

        self.secret = Message.objects.create(
            sender=self.alice, room=self.private,
            content='Owner password is hunter2 and payroll runs Friday',
            status=Message.STATUS_SENT,
        )


class ConnectionAuthTests(ChatSocketTestBase):
    async def test_no_token_is_refused(self):
        comm = WebsocketCommunicator(ChatConsumer.as_asgi(), f'/ws/chat/{self.shared.id}/')
        comm.scope['url_route'] = {'kwargs': {'room_id': str(self.shared.id)}}
        connected, code = await comm.connect(5)
        self.assertFalse(connected)
        self.assertEqual(code, 4001)

    async def test_garbage_token_is_refused(self):
        comm, connected, _ = await connect(self.shared.id, 'not-a-real-token')
        self.assertFalse(connected)

    async def test_non_participant_is_refused(self):
        """Mallory is not in the private room."""
        comm, connected, code = await connect(self.private.id, self.mallory_token)
        self.assertFalse(connected)
        self.assertEqual(code, 4003)

    async def test_participant_is_accepted(self):
        comm, connected, _ = await connect(self.private.id, self.alice_token)
        self.assertTrue(connected)
        await comm.disconnect()

    async def test_inactive_user_is_refused(self):
        @sync_to_async
        def deactivate():
            CustomUser.objects.filter(pk=self.alice.pk).update(is_active=False)

        await deactivate()
        comm, connected, _ = await connect(self.private.id, self.alice_token)
        self.assertFalse(connected)

    @override_settings(API_TOKEN_MAX_AGE_DAYS=7)
    async def test_expired_token_is_refused(self):
        """The websocket used to ignore token expiry that REST enforced.

        A token older than API_TOKEN_MAX_AGE_DAYS is rejected on every HTTP
        call by ExpiringTokenAuthentication. The consumer resolved tokens with
        its own `Token.objects.get(key=...)` and never looked at `created`, so a
        stale token from a device that had been logged out for months still
        opened a live chat session.
        """
        @sync_to_async
        def age_the_token():
            Token.objects.filter(user=self.alice).update(
                created=timezone.now() - datetime.timedelta(days=30))

        await age_the_token()
        comm, connected, _ = await connect(self.private.id, self.alice_token)
        self.assertFalse(connected)

    @override_settings(API_TOKEN_MAX_AGE_DAYS=7)
    async def test_fresh_token_still_works(self):
        comm, connected, _ = await connect(self.private.id, self.alice_token)
        self.assertTrue(connected)
        await comm.disconnect()


class ReplyPreviewLeakTests(ChatSocketTestBase):
    async def test_cannot_quote_a_message_from_another_room(self):
        """The cross-room disclosure.

        Mallory is a legitimate participant of `shared`, and not of `private`.
        She sends a message quoting `self.secret` - a message id from the
        private room. `_get_reply_preview` fetched it with no room filter, so
        the first 100 characters were broadcast to `shared`. Message ids are
        sequential, so nothing had to be guessed.
        """
        comm, connected, _ = await connect(self.shared.id, self.mallory_token)
        self.assertTrue(connected)

        await comm.send_json_to({'type': 'chat_message', 'content': 'look at this',
                                 'reply_to_id': self.secret.id})
        frames = await drain(comm)
        await comm.disconnect()

        blob = str(frames)
        self.assertNotIn('hunter2', blob)
        self.assertNotIn('payroll', blob)

        chat = [f for f in frames if f.get('type') == 'chat_message']
        self.assertEqual(len(chat), 1, frames)
        # The message is still delivered - only the quoted preview is dropped.
        self.assertEqual(chat[0]['content'], 'look at this')
        self.assertIsNone(chat[0]['reply_to'])

    async def test_cross_room_reply_link_is_not_persisted(self):
        comm, _, _ = await connect(self.shared.id, self.mallory_token)
        await comm.send_json_to({'type': 'chat_message', 'content': 'x',
                                 'reply_to_id': self.secret.id})
        await drain(comm)
        await comm.disconnect()

        saved = await first(Message.objects.filter(room=self.shared, sender=self.mallory))
        self.assertIsNotNone(saved)
        self.assertIsNone(saved.reply_to_id)

    async def test_same_room_reply_preview_still_works(self):
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'chat_message', 'content': 'noted',
                                 'reply_to_id': self.secret.id})
        frames = await drain(comm)
        await comm.disconnect()

        chat = [f for f in frames if f.get('type') == 'chat_message'][0]
        self.assertIsNotNone(chat['reply_to'])
        self.assertIn('hunter2', chat['reply_to']['content'])
        self.assertEqual(chat['reply_to']['sender_name'], 'Alice')

        saved = await refetch(Message, chat['id'])
        self.assertEqual(saved.reply_to_id, self.secret.id)

    async def test_quoting_a_deleted_message_does_not_resurrect_it(self):
        @sync_to_async
        def soft_delete():
            Message.objects.filter(pk=self.secret.pk).update(deleted_at=timezone.now())

        await soft_delete()

        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'chat_message', 'content': 'hm',
                                 'reply_to_id': self.secret.id})
        frames = await drain(comm)
        await comm.disconnect()

        chat = [f for f in frames if f.get('type') == 'chat_message'][0]
        self.assertEqual(chat['reply_to']['content'], '[Message deleted]')
        self.assertNotIn('hunter2', str(frames))

    async def test_reply_to_a_nonexistent_id_still_delivers_the_message(self):
        """This used to swallow the whole message.

        `reply_to_id` was written through unchecked, so a stale id raised an
        IntegrityError inside `_save_message`, which returned None, and
        `_handle_chat_message` returned without sending or reporting anything.
        The sender's message simply disappeared.
        """
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'chat_message', 'content': 'still sends',
                                 'reply_to_id': 99999999})
        frames = await drain(comm)
        await comm.disconnect()

        chat = [f for f in frames if f.get('type') == 'chat_message']
        self.assertEqual(len(chat), 1, frames)
        self.assertEqual(chat[0]['content'], 'still sends')
        self.assertTrue(await exists(
            Message.objects.filter(room=self.private, content='still sends')))


class ReactionScopeTests(ChatSocketTestBase):
    async def test_cannot_react_to_a_message_in_another_room(self):
        """`get_or_create(message_id=...)` accepted any id in the table."""
        comm, _, _ = await connect(self.shared.id, self.mallory_token)
        await comm.send_json_to({'type': 'reaction', 'message_id': self.secret.id,
                                 'emoji': '\U0001F44D'})
        frames = await drain(comm)
        await comm.disconnect()

        self.assertFalse(await exists(
            MessageReaction.objects.filter(message=self.secret)))
        self.assertEqual([f for f in frames if f.get('type') == 'reaction_update'], [])

    async def test_reaction_in_own_room_is_saved_and_broadcast(self):
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'reaction', 'message_id': self.secret.id,
                                 'emoji': '\U0001F44D'})
        frames = await drain(comm)
        await comm.disconnect()

        update = [f for f in frames if f.get('type') == 'reaction_update']
        self.assertEqual(len(update), 1, frames)
        self.assertEqual(update[0]['action'], 'added')
        self.assertEqual(update[0]['emoji'], '\U0001F44D')
        self.assertTrue(await exists(MessageReaction.objects.filter(
            message=self.secret, user=self.bob, emoji='\U0001F44D')))

    async def test_same_emoji_twice_removes_it(self):
        @sync_to_async
        def seed():
            MessageReaction.objects.create(
                message=self.secret, user=self.bob, emoji='\U0001F44D')

        await seed()
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'reaction', 'message_id': self.secret.id,
                                 'emoji': '\U0001F44D'})
        frames = await drain(comm)
        await comm.disconnect()

        update = [f for f in frames if f.get('type') == 'reaction_update'][0]
        self.assertEqual(update['action'], 'removed')
        self.assertFalse(await exists(
            MessageReaction.objects.filter(message=self.secret)))

    async def test_a_different_emoji_replaces_it(self):
        @sync_to_async
        def seed():
            MessageReaction.objects.create(
                message=self.secret, user=self.bob, emoji='\U0001F44D')

        @sync_to_async
        def stored():
            return MessageReaction.objects.get(
                message=self.secret, user=self.bob).emoji

        await seed()
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'reaction', 'message_id': self.secret.id,
                                 'emoji': '❤'})
        frames = await drain(comm)
        await comm.disconnect()

        update = [f for f in frames if f.get('type') == 'reaction_update'][0]
        self.assertEqual(update['action'], 'changed')
        self.assertEqual(await stored(), '❤')

    async def test_an_overlong_emoji_is_rejected_not_broadcast(self):
        """`emoji` is max_length=10; a longer value raised a DataError on MySQL,
        which was swallowed into {'action': 'error'} and broadcast anyway."""
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'reaction', 'message_id': self.secret.id,
                                 'emoji': 'x' * 400})
        frames = await drain(comm)
        await comm.disconnect()

        self.assertEqual([f for f in frames if f.get('type') == 'reaction_update'], [])
        self.assertFalse(await exists(
            MessageReaction.objects.filter(message=self.secret)))


class EditDeleteScopeTests(ChatSocketTestBase):
    async def test_cannot_delete_someone_elses_message(self):
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'delete_message', 'message_id': self.secret.id})
        frames = await drain(comm)
        await comm.disconnect()

        fresh = await refetch(Message, self.secret.pk)
        self.assertIsNone(fresh.deleted_at)
        self.assertEqual([f for f in frames if f.get('type') == 'message_deleted'], [])

    async def test_cannot_edit_someone_elses_message(self):
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'edit_message', 'message_id': self.secret.id,
                                 'content': 'I never said that'})
        await drain(comm)
        await comm.disconnect()

        fresh = await refetch(Message, self.secret.pk)
        self.assertIn('hunter2', fresh.content)

    async def test_own_message_can_be_deleted(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'delete_message', 'message_id': self.secret.id})
        frames = await drain(comm)
        await comm.disconnect()

        fresh = await refetch(Message, self.secret.pk)
        self.assertIsNotNone(fresh.deleted_at)
        self.assertIsNone(fresh.content)
        self.assertEqual(
            len([f for f in frames if f.get('type') == 'message_deleted']), 1)

    async def test_own_message_can_be_edited(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'edit_message', 'message_id': self.secret.id,
                                 'content': 'corrected'})
        await drain(comm)
        await comm.disconnect()

        fresh = await refetch(Message, self.secret.pk)
        self.assertEqual(fresh.content, 'corrected')
        self.assertIsNotNone(fresh.edited_at)

    async def test_a_deleted_message_cannot_be_edited_back(self):
        @sync_to_async
        def soft_delete():
            Message.objects.filter(pk=self.secret.pk).update(
                deleted_at=timezone.now(), content=None)

        await soft_delete()

        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'edit_message', 'message_id': self.secret.id,
                                 'content': 'back from the dead'})
        await drain(comm)
        await comm.disconnect()

        fresh = await refetch(Message, self.secret.pk)
        self.assertIsNone(fresh.content)


class MalformedInputTests(ChatSocketTestBase):
    async def test_a_non_integer_id_does_not_break_the_socket(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        for payload in (
            {'type': 'reaction', 'message_id': 'abc', 'emoji': '\U0001F44D'},
            {'type': 'delete_message', 'message_id': {'nested': 1}},
            {'type': 'edit_message', 'message_id': None, 'content': 'x'},
            {'type': 'chat_message', 'content': 'ok', 'reply_to_id': 'abc'},
            {'type': 'read_receipt', 'message_ids': 'not-a-list'},
            {'type': 'read_receipt', 'message_ids': [None, 'x', -1, 0]},
            {'type': 'unknown_type', 'content': 'x'},
        ):
            await comm.send_json_to(payload)
        frames = await drain(comm, limit=10)
        await comm.disconnect()

        # The one valid message got through, so the socket survived every
        # malformed frame around it.
        self.assertTrue(
            any(f.get('type') == 'chat_message' and f.get('content') == 'ok'
                for f in frames), frames)

    async def test_invalid_json_is_ignored(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_to(text_data='{not json')
        await comm.send_json_to({'type': 'chat_message', 'content': 'after garbage'})
        frames = await drain(comm)
        await comm.disconnect()
        self.assertTrue(any(f.get('content') == 'after garbage' for f in frames))

    async def test_an_oversized_message_is_refused_with_a_reason(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'chat_message',
                                 'content': 'x' * (MAX_MESSAGE_CHARS + 1)})
        frames = await drain(comm)
        await comm.disconnect()

        errors = [f for f in frames if f.get('type') == 'error']
        self.assertEqual(len(errors), 1, frames)
        self.assertIn('too long', errors[0]['message'])
        self.assertFalse(await exists(
            Message.objects.filter(content__startswith='xxxx')))

    async def test_an_empty_message_is_ignored(self):
        before = await count(Message.objects.all())
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'chat_message', 'content': '   '})
        await drain(comm, limit=2, timeout=0.5)
        await comm.disconnect()
        self.assertEqual(await count(Message.objects.all()), before)

    async def test_read_receipts_only_touch_this_room(self):
        """A read receipt for another room's message must not mark it read."""
        @sync_to_async
        def seed_other():
            return Message.objects.create(
                sender=self.alice, room=self.shared, content='team note')

        other = await seed_other()
        comm, _, _ = await connect(self.private.id, self.bob_token)
        await comm.send_json_to({'type': 'read_receipt',
                                 'message_ids': [other.id, self.secret.id]})
        await drain(comm, limit=3, timeout=0.8)
        await comm.disconnect()

        self.assertEqual((await refetch(Message, other.pk)).status, Message.STATUS_SENT)
        self.assertEqual(
            (await refetch(Message, self.secret.pk)).status, Message.STATUS_READ)

    async def test_a_user_does_not_mark_their_own_message_read(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'read_receipt',
                                 'message_ids': [self.secret.id]})
        await drain(comm, limit=3, timeout=0.8)
        await comm.disconnect()

        self.assertEqual(
            (await refetch(Message, self.secret.pk)).status, Message.STATUS_SENT)

    def test_as_id_helper(self):
        self.assertEqual(_as_id('7'), 7)
        self.assertEqual(_as_id(7), 7)
        self.assertIsNone(_as_id('abc'))
        self.assertIsNone(_as_id(None))
        self.assertIsNone(_as_id(0))
        self.assertIsNone(_as_id(-3))
        self.assertIsNone(_as_id({'a': 1}))
        self.assertIsNone(_as_id([1]))


class BroadcastShapeTests(ChatSocketTestBase):
    async def test_read_receipt_notification_carries_an_integer_room_id(self):
        """`room_id` was `int(self.room_id)` in this one payload and a string
        everywhere else, so the same room reached the client in two shapes.

        Alice listens on her own user group while Bob reads her message.
        """
        alice_comm, connected, _ = await connect(self.private.id, self.alice_token)
        self.assertTrue(connected)
        bob_comm, _, _ = await connect(self.private.id, self.bob_token)

        await drain(alice_comm, limit=2, timeout=0.5)  # Bob's user_online frame
        await bob_comm.send_json_to({'type': 'read_receipt',
                                     'message_ids': [self.secret.id]})

        frames = await drain(alice_comm, limit=4, timeout=1.5)
        await bob_comm.disconnect()
        await alice_comm.disconnect()

        read = [f for f in frames if f.get('type') == 'messages_read']
        self.assertEqual(len(read), 1, frames)
        self.assertIsInstance(read[0]['room_id'], int)
        self.assertEqual(read[0]['room_id'], self.private.id)
        self.assertEqual(read[0]['reader_id'], self.bob.id)

    async def test_typing_events_are_not_echoed_to_the_sender(self):
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'typing'})
        frames = await drain(comm, limit=3, timeout=0.6)
        await comm.disconnect()
        self.assertEqual([f for f in frames if f.get('type') == 'typing'], [])

    async def test_typing_events_reach_the_other_participant(self):
        alice_comm, _, _ = await connect(self.private.id, self.alice_token)
        bob_comm, _, _ = await connect(self.private.id, self.bob_token)
        await drain(alice_comm, limit=2, timeout=0.5)

        await bob_comm.send_json_to({'type': 'typing'})
        frames = await drain(alice_comm, limit=3, timeout=1.2)
        await bob_comm.disconnect()
        await alice_comm.disconnect()

        typing = [f for f in frames if f.get('type') == 'typing']
        self.assertEqual(len(typing), 1, frames)
        self.assertTrue(typing[0]['is_typing'])
        self.assertEqual(typing[0]['user_id'], self.bob.id)

    async def test_message_send_updates_the_room_timestamp(self):
        original = (await refetch(ChatRoom, self.private.pk)).updated_at
        comm, _, _ = await connect(self.private.id, self.alice_token)
        await comm.send_json_to({'type': 'chat_message', 'content': 'bump'})
        await drain(comm)
        await comm.disconnect()
        self.assertGreater(
            (await refetch(ChatRoom, self.private.pk)).updated_at, original)
