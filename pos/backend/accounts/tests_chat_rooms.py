"""
The chat room list (`/accounts/api/chat/users/`).

The screen showed five rooms all called "Chat Room" next to the one real
conversation. Causes, each pinned here:

  * opening the app fires several list requests at once and each one created its
    own one-to-one room for the same pair, so the pair ended up with several rooms;
  * a one-to-one room with only the viewer in it has no name to show;
  * the list queried the database again for every room's name;
  * the room rows and their extra fields were read from two separate evaluations of
    the queryset, so a change between them could attach one room's last message to
    another room.
"""

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import ChatRoom, CustomUser, Message
from pos_backend.testing import chat_only_role

URL = '/accounts/api/chat/users/'


class RoomListTests(TestCase):
    def setUp(self):
        role = chat_only_role()
        self.owner = CustomUser.objects.create_superuser(
            email='owner@test.com', password='StrongPass!234', full_name='Owner')
        self.alice = CustomUser.objects.create_user(
            email='alice@test.com', password='StrongPass!234', full_name='Alice', role=role)
        self.bob = CustomUser.objects.create_user(
            email='bob@test.com', password='StrongPass!234', full_name='Bob', role=role)
        self.as_owner = APIClient()
        self.as_owner.force_authenticate(self.owner)
        self.as_alice = APIClient()
        self.as_alice.force_authenticate(self.alice)

    def rows(self, client):
        res = client.get(URL)
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_repeated_requests_do_not_pile_up_rooms(self):
        for _ in range(4):
            self.rows(self.as_owner)
        pair = ChatRoom.objects.filter(is_group=False, participants=self.owner).filter(participants=self.alice)
        self.assertEqual(pair.count(), 1)

    def test_the_owner_sees_each_person_once_by_name(self):
        names = sorted(r['display_name'] for r in self.rows(self.as_owner))
        self.assertEqual(names, ['Alice', 'Bob'])

    def test_a_one_to_one_room_with_nobody_else_is_hidden(self):
        orphan = ChatRoom.objects.create(is_group=False)
        orphan.participants.add(self.alice)
        self.assertEqual([r for r in self.rows(self.as_alice) if r['display_name'] == 'Chat Room'], [])

    def test_a_second_unused_room_for_the_same_person_is_hidden_but_a_used_one_is_kept(self):
        first = ChatRoom.objects.create(is_group=False)
        first.participants.add(self.alice, self.bob)
        spare = ChatRoom.objects.create(is_group=False)
        spare.participants.add(self.alice, self.bob)
        with_bob = lambda rows: [r for r in rows if r['display_name'] == 'Bob']
        self.assertEqual(len(with_bob(self.rows(self.as_alice))), 1)

        Message.objects.create(sender=self.bob, room=spare, content='hello')
        rows = with_bob(self.rows(self.as_alice))
        self.assertEqual([r['id'] for r in rows], [spare.id])
        self.assertEqual(rows[0]['last_message'], 'hello')

    def test_unread_counts_only_other_peoples_unread_messages(self):
        room = ChatRoom.objects.create(is_group=False)
        room.participants.add(self.alice, self.bob)
        Message.objects.create(sender=self.bob, room=room, content='1', status=Message.STATUS_SENT)
        Message.objects.create(sender=self.bob, room=room, content='2', status=Message.STATUS_DELIVERED)
        Message.objects.create(sender=self.bob, room=room, content='3', status=Message.STATUS_READ)
        Message.objects.create(sender=self.alice, room=room, content='mine', status=Message.STATUS_SENT)
        row = next(r for r in self.rows(self.as_alice) if r['id'] == room.id)
        self.assertEqual(row['unread_count'], 2)

    def test_last_message_belongs_to_the_right_room(self):
        a = ChatRoom.objects.create(name='A', is_group=True)
        a.participants.add(self.alice, self.bob)
        b = ChatRoom.objects.create(name='B', is_group=True)
        b.participants.add(self.alice, self.bob)
        Message.objects.create(sender=self.bob, room=a, content='in A')
        Message.objects.create(sender=self.bob, room=b, content='in B')
        by_name = {r['display_name']: r['last_message'] for r in self.rows(self.as_alice)}
        self.assertEqual((by_name['A'], by_name['B']), ('in A', 'in B'))

    def test_the_number_of_queries_does_not_grow_with_the_number_of_rooms(self):
        def count():
            with CaptureQueriesContext(connection) as q:
                self.rows(self.as_alice)
            return len(q)

        for i in range(2):
            room = ChatRoom.objects.create(name=f'G{i}', is_group=True)
            room.participants.add(self.alice, self.bob)
        few = count()
        for i in range(2, 12):
            room = ChatRoom.objects.create(name=f'G{i}', is_group=True)
            room.participants.add(self.alice, self.bob)
        many = count()
        self.assertLessEqual(many, few + 4)
