from rest_framework import serializers
from .models import CustomUser, Message

class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = CustomUser
        fields = ('id', 'email', 'full_name', 'phone', 'designation', 'role', 'center', 'centers', 'password', 'is_active')
        extra_kwargs = {'password': {'write_only': True, 'required': False}}

    def validate(self, attrs):
        """A user needs a role (it decides what they may do) and a centre (it
        decides whose records they see). Without them the account either can do
        nothing or, for a no-centre user, saw every customer."""
        instance = self.instance
        if instance is not None and instance.is_superuser:
            return attrs
        errors = {}
        role = attrs['role'] if 'role' in attrs else (instance.role if instance else None)
        if role is None:
            errors['role'] = 'Choose a role for this user.'
        center = attrs['center'] if 'center' in attrs else (instance.center if instance else None)
        centers = list(attrs['centers']) if 'centers' in attrs else (
            list(instance.centers.all()) if instance else [])
        if not center and not centers:
            errors['centers'] = 'Choose at least one centre for this user.'
        if errors:
            raise serializers.ValidationError(errors)
        # Keep the "home" centre and the list in step.
        if not center and centers:
            attrs['center'] = centers[0]
        return attrs

    def validate_password(self, value):
        # A new account is held to the same rules as a password change; the
        # administrator used to be able to create users with "1" or "password".
        if self.instance is None and value:
            from django.contrib.auth.password_validation import validate_password
            from django.core.exceptions import ValidationError as DjangoValidationError
            try:
                validate_password(value)
            except DjangoValidationError as exc:
                raise serializers.ValidationError(list(exc.messages))
        return value

    def create(self, validated_data):
        centers = validated_data.pop('centers', [])
        try:
            user = CustomUser.objects.create_user(**validated_data)
            if centers:
                user.centers.set(centers)
            return user
        except Exception as e:
            raise serializers.ValidationError(str(e))

    def update(self, instance, validated_data):
        # Passwords should be changed via a dedicated endpoint, not general update
        validated_data.pop('password', None)
        centers = validated_data.pop('centers', None)
        
        user = super().update(instance, validated_data)
            
        if centers is not None:
            user.centers.set(centers)
            
        return user

class UserChatSerializer(serializers.ModelSerializer):
    center_name = serializers.SerializerMethodField()

    class Meta:
        model = CustomUser
        fields = ('id', 'full_name', 'email', 'center_name')

    def get_center_name(self, obj):
        if obj.center:
            return obj.center.display_name or obj.center.center_name
        centers = list(obj.centers.all())  # prefetched by the room list: no query per person
        if centers:
            c = centers[0]
            return c.display_name or c.center_name
        return "No Center"

from .models import CustomUser, Message, ChatRoom, MessageReaction

class ChatRoomSerializer(serializers.ModelSerializer):
    participants = UserChatSerializer(many=True, read_only=True)
    display_name = serializers.SerializerMethodField()

    class Meta:
        model = ChatRoom
        fields = ('id', 'name', 'display_name', 'is_group', 'participants')

    def get_display_name(self, obj):
        if obj.name:
            return obj.name
        request = self.context.get('request')
        if request and request.user:
            # .all() so the prefetched participants are used; .exclude() went back to
            # the database once per room.
            others = [p for p in obj.participants.all() if p.id != request.user.id]
            if others:
                return ", ".join([o.full_name or o.email for o in others])
        return "Chat Room"

class MessageReactionSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.full_name', read_only=True)
    user = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = MessageReaction
        fields = ('id', 'message', 'user', 'user_name', 'emoji', 'created_at')

class MessageSerializer(serializers.ModelSerializer):
    sender = serializers.PrimaryKeyRelatedField(read_only=True)
    sender_name = serializers.CharField(source='sender.full_name', read_only=True)
    receiver_name = serializers.SerializerMethodField()
    reactions_summary = serializers.SerializerMethodField()
    reply_to_preview = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = (
            'id', 'sender', 'sender_name', 'receiver', 'receiver_name', 'room',
            'content', 'image', 'timestamp', 'is_read', 'status', 'reply_to',
            'reply_to_preview', 'deleted_at', 'edited_at', 'reactions_summary'
        )

    def get_receiver_name(self, obj):
        if obj.receiver:
            return obj.receiver.full_name
        return None

    def get_reactions_summary(self, obj):
        reactions = obj.reactions.all()
        summary = {}
        for r in reactions:
            if r.emoji not in summary:
                summary[r.emoji] = []
            summary[r.emoji].append({'user_id': r.user_id, 'user_name': r.user.full_name})
        return summary
        
    def validate_reply_to(self, value):
        """A reply may only quote a message in the same room.

        `reply_to` is a writable field and nothing checked it, so the REST path
        had the same cross-room disclosure the websocket did: post a message to
        a room you belong to, quoting a message id from a room you do not, and
        `reply_to_preview` renders 100 characters of that private message back
        to your own room. Message ids are sequential, so nothing had to be
        guessed.
        """
        if value is None:
            return value

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or not user.is_authenticated:
            raise serializers.ValidationError('Authentication required.')

        # The room the new message is being posted into. On create it arrives as
        # `room_id` in the payload (the view resolves it); on update the
        # instance already knows.
        room_id = None
        if self.instance is not None:
            room_id = self.instance.room_id
        elif request is not None:
            room_id = (request.data.get('room_id')
                       or request.data.get('room')
                       or request.query_params.get('user_id'))

        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            raise serializers.ValidationError(
                'The room could not be determined, so the quoted message cannot '
                'be checked.')

        if value.room_id != room_id:
            raise serializers.ValidationError(
                'You can only reply to a message in the same conversation.')
        return value

    def get_reply_to_preview(self, obj):
        if not obj.reply_to:
            return None
        quoted = obj.reply_to
        if quoted.deleted_at:
            # Quoting used to resurrect the full text of a deleted message.
            content = '[Message deleted]'
        elif quoted.content:
            content = quoted.content[:100]
        else:
            content = '[Image]'
        return {
            'id': quoted.id,
            'sender_name': quoted.sender.full_name if quoted.sender else '',
            'content': content,
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.is_deleted:
            data['content'] = None
            data['image'] = None
        return data
