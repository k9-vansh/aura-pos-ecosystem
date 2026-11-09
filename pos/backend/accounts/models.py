from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models

class CustomUserManager(BaseUserManager):
    """Manager for CustomUser.

    Emails are stored fully lowercased. Django's own `normalize_email` only
    lowercases the DOMAIN, leaving the local part as typed - so `Asha@x.com` and
    `asha@x.com` are different strings in the database.

    That is a genuine divergence between the two backends, and it points in
    opposite directions:

      * SQLite compares `=` byte-exactly, so both rows can exist AND someone
        who types their address with a capital cannot log in;
      * MySQL's default collation is case-INSENSITIVE, so the unique index
        rejects the second row - meaning a fixture or import that loads fine in
        development fails on the live database.

    Lowercasing on the way in makes both backends agree.
    """

    def normalize_email(self, email):
        return super().normalize_email(email or '').lower()

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError('The Email field must be set')
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        if password:
            user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError('Superuser must have is_staff=True.')
        if extra_fields.get('is_superuser') is not True:
            raise ValueError('Superuser must have is_superuser=True.')

        return self.create_user(email, password, **extra_fields)

class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20, blank=True, null=True)
    designation = models.CharField(max_length=255, blank=True, null=True)
    
    # Relationships
    role = models.ForeignKey('salon_admin.Role', on_delete=models.SET_NULL, null=True, blank=True)
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True)
    centers = models.ManyToManyField('salon_admin.Center', related_name='users', blank=True)

    # Required for Django admin
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['full_name']

    def __str__(self):
        return self.email

class ChatRoom(models.Model):
    name = models.CharField(max_length=255, blank=True, null=True)
    is_group = models.BooleanField(default=False)
    participants = models.ManyToManyField(CustomUser, related_name='chat_rooms')
    created_by = models.ForeignKey(CustomUser, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_rooms')
    avatar = models.ImageField(upload_to='chat_avatars/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.name or f"Room {self.id}"


class Message(models.Model):
    STATUS_SENT = 'sent'
    STATUS_DELIVERED = 'delivered'
    STATUS_READ = 'read'
    STATUS_CHOICES = [
        (STATUS_SENT, 'Sent'),
        (STATUS_DELIVERED, 'Delivered'),
        (STATUS_READ, 'Read'),
    ]

    sender = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='sent_messages')
    receiver = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='received_messages', null=True, blank=True)
    room = models.ForeignKey(ChatRoom, on_delete=models.CASCADE, related_name='messages', null=True, blank=True)
    content = models.TextField(blank=True, null=True)
    image = models.ImageField(upload_to='chat_images/', blank=True, null=True)
    timestamp = models.DateTimeField(auto_now_add=True)
    
    # WhatsApp-style features
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_SENT)
    reply_to = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='replies')
    deleted_at = models.DateTimeField(null=True, blank=True)  # Soft delete
    edited_at = models.DateTimeField(null=True, blank=True)
    
    # Keep is_read for backwards compatibility; status field is the canonical source going forward
    is_read = models.BooleanField(default=False)

    class Meta:
        ordering = ['timestamp']
        indexes = [
            models.Index(fields=['room', 'timestamp'], name='msg_room_ts_idx'),
            models.Index(fields=['sender', 'timestamp'], name='msg_sender_ts_idx'),
            models.Index(fields=['room', 'status'], name='msg_room_status_idx'),
        ]

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def __str__(self):
        if self.room:
            return f"From {self.sender.full_name} in {self.room.name} at {self.timestamp}"
        return f"From {self.sender.full_name} at {self.timestamp}"


class MessageReaction(models.Model):
    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='reactions')
    user = models.ForeignKey(CustomUser, on_delete=models.CASCADE, related_name='message_reactions')
    emoji = models.CharField(max_length=10)  # e.g. '👍', '❤️', '😂'
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('message', 'user')  # One reaction per user per message
        indexes = [
            models.Index(fields=['message'], name='reaction_msg_idx'),
        ]

    def __str__(self):
        return f"{self.user.full_name} reacted {self.emoji} on msg {self.message_id}"
