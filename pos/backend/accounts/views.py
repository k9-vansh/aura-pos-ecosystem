from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.authtoken.models import Token
from rest_framework.response import Response
from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.views import APIView
from rest_framework import serializers
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from django.db.models import Q
from django.conf import settings
import logging
import os

from pos_backend.throttles import LoginRateThrottle
from pos_backend.permissions import IsOwner, HasModulePermission
from .models import CustomUser, Message, ChatRoom, MessageReaction
from .serializers import UserSerializer, MessageSerializer, UserChatSerializer, MessageReactionSerializer

logger = logging.getLogger(__name__)


def visible_users(user):
    """Users `user` may see: everyone for the superuser or an all-branches
    role, otherwise those sharing one of its branches. Fails closed."""
    queryset = CustomUser.objects.all().select_related('role', 'center') \
        .prefetch_related('centers').order_by('full_name')
    perms = getattr(getattr(user, 'role', None), 'permissions', {}) or {}
    if IsOwner.check_is_owner(user) or perms.get('all_centers', False):
        return queryset
    if user.centers.exists():
        return queryset.filter(
            Q(center__in=user.centers.all()) | Q(centers__in=user.centers.all())
        ).distinct()
    if getattr(user, 'center', None):
        return queryset.filter(Q(center=user.center) | Q(centers=user.center)).distinct()
    return queryset.none()


class UserViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    serializer_class = UserSerializer
    permission_module = 'admin'
    permission_submodule = 'users'
    # Admin is a set of unrelated screens: holding Admin > Bills must not
    # list every login. The activity log filters by user, so Logs reads too.
    permission_strict_read = True
    permission_read_also = [('logs', 'activity')]

    def get_queryset(self):
        return visible_users(self.request.user)

    def _check_delegation(self, serializer, existing=None):
        """Admin > Users is a trusted grant: whoever holds it may assign any role
        and any centre (owner decision, 19 Oct 2026). The one thing kept is that
        only the superuser can change the superuser."""
        if existing is not None and existing.is_superuser and not IsOwner.check_is_owner(self.request.user):
            raise PermissionDenied('Only the superuser can change the superuser.')

    def perform_create(self, serializer):
        self._check_delegation(serializer)
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        is_owner = IsOwner.check_is_owner(user)

        # Determine if the user has update permissions for admin > users
        from pos_backend.permissions import has_module_permission
        has_user_update_perm = has_module_permission(user, 'admin', 'update', 'users')

        if not is_owner and not has_user_update_perm:
            for f in ('role', 'center', 'centers'):
                serializer.validated_data.pop(f, None)
            if serializer.instance.pk != user.pk:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("You may only edit your own profile.")
        else:
            self._check_delegation(serializer, serializer.instance)
        serializer.save()

    def destroy(self, request, *args, **kwargs):
        """Users are deactivated, never deleted (owner decision #7).

        Deleting a user cascaded into or orphaned the records they created
        (bills, logs, chat); deactivating keeps every record and simply stops
        them signing in.
        """
        from rest_framework import status as http_status

        instance = self.get_object()
        if instance.pk == request.user.pk:
            return Response({'detail': 'You cannot deactivate your own account.'},
                            status=http_status.HTTP_400_BAD_REQUEST)
        if instance.is_superuser and not request.user.is_superuser:
            raise PermissionDenied('Only the superuser can deactivate the superuser.')
        if instance.is_active:
            instance.is_active = False
            instance.save(update_fields=['is_active'])
        # Signed-in sessions end now, not when the token expires.
        Token.objects.filter(user=instance).delete()
        return Response(UserSerializer(instance, context={'request': request}).data)



class CustomAuthToken(ObtainAuthToken):
    throttle_classes = [LoginRateThrottle]

    def post(self, request, *args, **kwargs):
        # Guarantee demo user exists on demand
        who = request.data.get('username') or request.data.get('email')
        pwd = request.data.get('password')
        if who == 'admin@aurasalon.io' and pwd == 'admin123':
            from django.core.management import call_command
            try:
                call_command('seed_demo_accounts')
            except Exception:
                pass

        from django.conf import settings as dj_settings
        from pos_backend.permission_tree import normalise_permissions

        serializer = self.serializer_class(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        user = serializer.validated_data['user']

        # Reissue the token on every login.
        #
        # get_or_create() returned the SAME token with its original `created`
        # timestamp, which meant enabling ExpiringTokenAuthentication would have
        # locked every user out permanently after API_TOKEN_MAX_AGE_DAYS, since
        # logging in again handed back the same stale token. Deleting and
        # recreating also invalidates sessions on other devices at sign-in,
        # which is the behaviour you want from a shared salon terminal.
        Token.objects.filter(user=user).delete()
        token = Token.objects.create(user=user)

        max_age_days = getattr(dj_settings, 'API_TOKEN_MAX_AGE_DAYS', 7)
        response_data = {
            'token': token.key,
            'token_expires_in_days': max_age_days,
            'user_id': user.pk,
            'email': user.email,
            'full_name': user.full_name,
            'is_superuser': user.is_superuser,
            'role': user.role.name if user.role else None,
            # Normalised to the current tree, so every screen checks one shape
            # (legacy keys such as marketing.campaigns are already resolved).
            'permissions': normalise_permissions(user.role.permissions if user.role else {}),
            'center_id': user.center.id if user.center else None,
            'centers': list(user.centers.values_list('id', flat=True)),
        }

        # Signing in no longer logs in to SAP (decision D7). It made every login
        # wait up to 10 s on SAP, put an SAP superuser session into each
        # browser, and nothing in the POS ever used it. SAP sessions belong to
        # the background workers.
        return Response(response_data)


class ChangePasswordView(APIView):
    """Let a signed-in user change their own password.

    UserSerializer.update() pops `password` with a comment saying it should be
    changed "via a dedicated endpoint" - which did not exist, so a user could
    never change their own password outside the Django admin.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError

        current = request.data.get('current_password') or ''
        new = request.data.get('new_password') or ''
        confirm = request.data.get('confirm_password')

        if not current or not new:
            return Response(
                {'detail': 'current_password and new_password are required.'}, status=400
            )
        if confirm is not None and new != confirm:
            return Response({'confirm_password': 'The passwords do not match.'}, status=400)

        user = request.user
        if not user.check_password(current):
            return Response({'current_password': 'That is not your current password.'}, status=400)
        if current == new:
            return Response(
                {'new_password': 'The new password must differ from the current one.'}, status=400
            )

        try:
            validate_password(new, user=user)
        except DjangoValidationError as exc:
            return Response({'new_password': list(exc.messages)}, status=400)

        user.set_password(new)
        user.save(update_fields=['password'])

        # Invalidate every outstanding token so a stolen one stops working.
        Token.objects.filter(user=user).delete()
        new_token = Token.objects.create(user=user)

        return Response({
            'detail': 'Password changed. Other devices have been signed out.',
            'token': new_token.key,
        })


class ResetStaffUserPasswordView(APIView):
    """Reset another user's password: needs Admin > Users > "reset password"."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, user_id):
        from django.contrib.auth.password_validation import validate_password
        from django.core.exceptions import ValidationError as DjangoValidationError
        from pos_backend.permissions import require_action

        require_action(request.user, 'admin', 'users', 'reset_password')

        new = request.data.get('new_password') or ''
        if not new:
            return Response({'new_password': 'A new password is required.'}, status=400)

        # Only users this account can see (its branches), never the superuser
        # unless the caller is the superuser.
        target = visible_users(request.user).filter(pk=user_id).first()
        if not target:
            return Response({'detail': 'User not found.'}, status=404)
        if target.is_superuser and not request.user.is_superuser:
            raise PermissionDenied("Only the superuser can reset the superuser's password.")

        try:
            validate_password(new, user=target)
        except DjangoValidationError as exc:
            return Response({'new_password': list(exc.messages)}, status=400)

        target.set_password(new)
        target.save(update_fields=['password'])
        Token.objects.filter(user=target).delete()

        return Response({'detail': f'Password reset for {target.email}. Their sessions were ended.'})


# Chat is a module like any other (finding R13): read to see conversations,
# create to send. Editing or deleting one's OWN message, reacting and marking
# read count as part of taking part, so they need `create` - ownership of the
# message is still checked by the view.
CHAT_PARTICIPATION_VERBS = {
    'update': 'create', 'partial_update': 'create', 'destroy': 'create',
    'mark_read': 'read',
}


class ChatUserListView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'chat'
    permission_submodule = 'messages'

    def get(self, request):
        from .models import ChatRoom, CustomUser
        from .serializers import ChatRoomSerializer
        from django.db import transaction
        from django.db.models import Count, OuterRef, Q, Subquery

        me = request.user

        # Make sure every person this user may talk to has a one-to-one room.
        if me.is_superuser:
            target_users = CustomUser.objects.exclude(id=me.id)
        else:
            target_users = CustomUser.objects.filter(is_superuser=True)

        existing_partner_ids = set(
            CustomUser.objects.filter(chat_rooms__in=me.chat_rooms.filter(is_group=False))
            .exclude(id=me.id)
            .values_list('id', flat=True)
        )
        for u in target_users.exclude(id__in=existing_partner_ids):
            with transaction.atomic():
                # Look again inside the transaction: several requests arrive at once
                # when the app opens, and each used to create its own room for the
                # same pair (the dev database held six rooms between two people).
                already = (ChatRoom.objects.filter(is_group=False, participants=me)
                           .filter(participants=u).exists())
                if not already:
                    room = ChatRoom.objects.create(is_group=False)
                    room.participants.add(me, u)

        last_message = Message.objects.filter(room_id=OuterRef('pk')).order_by('-timestamp')
        rooms = list(
            me.chat_rooms.prefetch_related('participants', 'participants__center', 'participants__centers')
            .annotate(
                last_message_content=Subquery(last_message.values('content')[:1]),
                last_message_time_annotated=Subquery(last_message.values('timestamp')[:1]),
                last_message_image=Subquery(last_message.values('image')[:1]),
                unread=Count(
                    'messages',
                    filter=Q(messages__status__in=[Message.STATUS_SENT, Message.STATUS_DELIVERED],
                             messages__deleted_at__isnull=True) & ~Q(messages__sender=me),
                    distinct=True,
                ),
            )
        )

        # Hide what is not a conversation: a one-to-one room with nobody else in it,
        # and a second room for the same person that has never been used.
        listed, seen_pairs = [], set()
        for room in sorted(rooms, key=lambda r: (r.last_message_time_annotated is None, r.id)):
            others = tuple(sorted(p.id for p in room.participants.all() if p.id != me.id))
            if not room.is_group:
                if not others:
                    continue
                if others in seen_pairs and room.last_message_time_annotated is None:
                    continue
                seen_pairs.add(others)
            listed.append(room)

        # One list, serialised once: the rows and the extra fields can never get out of step.
        data = ChatRoomSerializer(listed, many=True, context={'request': request}).data
        out = []
        for room, row in zip(listed, data):
            row = dict(row)
            row['full_name'] = row.get('display_name')
            row['email'] = ''
            row['center_name'] = 'Group' if row.get('is_group') else 'Private'
            row['unread_count'] = room.unread
            if room.last_message_time_annotated:
                row['last_message'] = (room.last_message_content
                                       or ('[Image]' if room.last_message_image else None))
                row['last_message_time'] = room.last_message_time_annotated
            else:
                row['last_message'] = None
                row['last_message_time'] = None
            out.append(row)

        import datetime
        epoch = datetime.datetime.min  # USE_TZ is off: stored times are naive
        out.sort(key=lambda r: r['last_message_time'] or epoch, reverse=True)
        return Response(out)

class UnreadMessageCountView(APIView):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'chat'
    permission_submodule = 'messages'

    def get(self, request):
        count = Message.objects.filter(room__participants=request.user, is_read=False).exclude(sender=request.user).count()
        return Response({'count': count})

from rest_framework.pagination import CursorPagination

class MessageCursorPagination(CursorPagination):
    ordering = '-timestamp'
    page_size = 50

def broadcast_chat_message(message, payload=None):
    """Push a REST-created message onto the room's websocket group.

    Best effort: a chat notification must never be the reason a message fails
    to save, so a channel-layer problem is logged and swallowed.
    """
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer

    try:
        layer = get_channel_layer()
        if layer is None:
            return

        reply_to = None
        if message.reply_to_id and message.reply_to:
            quoted = message.reply_to
            reply_to = {
                'id': quoted.id,
                'sender_name': quoted.sender.full_name if quoted.sender else '',
                'content': ('[Message deleted]' if quoted.deleted_at
                            else (quoted.content[:100] if quoted.content else '[Image]')),
            }

        event = {
            'type': 'chat_message',
            'id': message.id,
            'content': message.content or '',
            'sender_id': message.sender_id,
            'sender_name': message.sender.full_name if message.sender else '',
            'timestamp': message.timestamp.isoformat() if message.timestamp else None,
            'status': message.status,
            'reply_to': reply_to,
        }
        if payload and payload.get('image'):
            event['image'] = payload['image']
        elif getattr(message, 'image', None):
            event['image'] = message.image.url

        async_to_sync(layer.group_send)(f'chat_{message.room_id}', event)
    except Exception:
        logger.exception('[Chat] failed to broadcast message %s', message.pk)


class MessageViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'chat'
    permission_submodule = 'messages'
    permission_action_verbs = CHAT_PARTICIPATION_VERBS
    serializer_class = MessageSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    pagination_class = MessageCursorPagination

    def get_queryset(self):
        """Messages in rooms this user belongs to.

        The old version returned `none()` unless a `room_id` query param was
        present, so retrieving, editing or deleting a single message by id
        always 404'd - the URL for a detail route carries no room_id.
        """
        user = self.request.user
        queryset = (
            Message.objects
            .filter(room__participants=user)
            .select_related('sender', 'receiver', 'room', 'reply_to', 'reply_to__sender')
            .prefetch_related('reactions', 'reactions__user')
        )

        room_id = (
            self.request.query_params.get('room_id')
            or self.request.query_params.get('user_id')
        )
        if room_id:
            try:
                queryset = queryset.filter(room_id=int(room_id))
            except (TypeError, ValueError):
                return queryset.none()
        return queryset

    def perform_update(self, serializer):
        """Only the sender may edit their own message."""
        if serializer.instance.sender_id != self.request.user.id:
            raise PermissionDenied('You can only edit your own messages.')
        from django.utils import timezone
        serializer.save(edited_at=timezone.now())

    def perform_destroy(self, instance):
        """Soft delete, and only by the sender."""
        from django.utils import timezone
        if instance.sender_id != self.request.user.id:
            raise PermissionDenied('You can only delete your own messages.')
        instance.deleted_at = timezone.now()
        instance.save(update_fields=['deleted_at'])

    @action(detail=False, methods=['post'])
    def mark_read(self, request):
        """Mark this room's incoming messages as read.

        The filter was `Message.objects.filter(room_id=room_id, ...)` with no
        membership check, so any signed-in user could post an arbitrary room id
        and clear the unread state of a conversation they had no part in - and
        every genuine recipient then lost their unread badge with no way to get
        it back.
        """
        user = self.request.user
        try:
            room_id = int(request.data.get('room_id'))
        except (TypeError, ValueError):
            return Response({'error': 'room_id required'}, status=400)

        if not ChatRoom.objects.filter(id=room_id, participants=user).exists():
            raise PermissionDenied('You are not a participant of this room.')

        updated = (Message.objects
                   .filter(room_id=room_id,
                           status__in=[Message.STATUS_SENT, Message.STATUS_DELIVERED])
                   .exclude(sender=user)
                   .update(status=Message.STATUS_READ, is_read=True))
        return Response({'status': 'ok', 'updated': updated})

    def perform_create(self, serializer):
        """Create a message over REST - this is the path image messages take.

        The save was never announced on the channel layer, so an image sent from
        the composer appeared for nobody until the page was reloaded: the other
        participant saw nothing, and the sender saw nothing either because the
        component clears the composer without appending anything locally. Text
        messages go over the websocket and were broadcast; images silently did
        not.
        """
        from django.utils import timezone

        raw_room = (self.request.data.get('room_id')
                    or self.request.query_params.get('user_id')
                    or self.request.data.get('receiver'))
        try:
            room_id = int(raw_room)
        except (TypeError, ValueError):
            raise PermissionDenied('room_id is required')

        room = ChatRoom.objects.filter(id=room_id, participants=self.request.user).first()
        if not room:
            raise PermissionDenied('You are not a participant of this room')

        message = serializer.save(sender=self.request.user, room=room,
                                  receiver=None, status=Message.STATUS_SENT)
        room.updated_at = timezone.now()
        room.save(update_fields=['updated_at'])

        broadcast_chat_message(message, serializer.data)

class MessageReactionViewSet(viewsets.ModelViewSet):
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'chat'
    permission_submodule = 'messages'
    permission_action_verbs = CHAT_PARTICIPATION_VERBS
    serializer_class = MessageReactionSerializer

    def get_queryset(self):
        user = self.request.user
        return MessageReaction.objects.filter(message__room__participants=user)

    def create(self, request, *args, **kwargs):
        """Toggle a reaction, returning a success status.

        This used to raise `serializers.ValidationError({"status": "removed"})`
        to report success, so a working toggle returned HTTP 400 and every
        caller had to treat 400 as OK. The toggle now returns 200 with an
        explicit `action`.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        message = serializer.validated_data.get('message')
        emoji = serializer.validated_data.get('emoji')

        if not ChatRoom.objects.filter(
            id=message.room_id, participants=request.user
        ).exists():
            raise PermissionDenied('You are not a participant of this room.')

        existing = MessageReaction.objects.filter(
            message=message, user=request.user
        ).first()

        if existing and existing.emoji == emoji:
            existing.delete()
            return Response({'action': 'removed', 'emoji': emoji,
                             'message': message.id}, status=200)

        if existing:
            existing.emoji = emoji
            existing.save(update_fields=['emoji'])
            return Response({'action': 'updated',
                             **self.get_serializer(existing).data}, status=200)

        reaction = serializer.save(user=request.user)
        return Response({'action': 'added',
                         **self.get_serializer(reaction).data}, status=201)
