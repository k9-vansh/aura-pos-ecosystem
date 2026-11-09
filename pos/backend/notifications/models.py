from django.conf import settings
from django.db import models


class IntegrationConfig(models.Model):
    """One outside service the POS talks to (e-mail, WhatsApp). Everything the
    owner types in Admin > Integrations is kept here, encrypted as one blob, so
    a database dump never carries a usable secret. See `integrations.py`."""

    key = models.CharField(max_length=40, unique=True)
    enabled = models.BooleanField(default=False)
    blob = models.TextField(blank=True, default='')
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)
    last_test_at = models.DateTimeField(null=True, blank=True)
    last_test_ok = models.BooleanField(null=True, blank=True)
    last_test_message = models.CharField(max_length=300, blank=True, default='')

    def __str__(self):
        return self.key


class NotificationRule(models.Model):
    """Who hears about an event, and through which channel. A missing row means
    the event's built-in default (see `events.py`)."""

    event = models.CharField(max_length=60, unique=True)
    enabled = models.BooleanField(default=True)
    bell = models.BooleanField(default=True)
    email = models.BooleanField(default=False)
    whatsapp = models.BooleanField(default=False)
    role_ids = models.JSONField(default=list, blank=True)
    user_ids = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.event


class Notification(models.Model):
    """One line in a user's bell."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='notifications')
    event = models.CharField(max_length=60)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, default='')
    url = models.CharField(max_length=300, blank=True, default='')
    data = models.JSONField(default=dict, blank=True)
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['user', 'read_at', '-created_at'], name='notif_user_unread_idx')]


class DeliveryLog(models.Model):
    """An e-mail or WhatsApp message, to a user or a client, and what became of it."""

    QUEUED, SENT, FAILED, SKIPPED = 'queued', 'sent', 'failed', 'skipped'

    event = models.CharField(max_length=60)
    channel = models.CharField(max_length=20)
    recipient = models.CharField(max_length=200)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                             on_delete=models.SET_NULL, related_name='+')
    client = models.ForeignKey('clients.Client', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='+')
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True,
                               on_delete=models.SET_NULL, related_name='+')
    subject = models.CharField(max_length=200, blank=True, default='')
    body = models.TextField(blank=True, default='')
    # The designed version of an e-mail (the plain `body` goes along as its text part).
    html = models.TextField(blank=True, default='')
    template = models.CharField(max_length=80, blank=True, default='')
    params = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=10, default=QUEUED)
    error = models.CharField(max_length=500, blank=True, default='')
    provider_ref = models.CharField(max_length=120, blank=True, default='')
    # A message that must go out at most once (a reminder for one expiry).
    dedupe_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['status', '-created_at'], name='delivery_status_idx'),
                   models.Index(fields=['event', '-created_at'], name='delivery_event_idx')]
