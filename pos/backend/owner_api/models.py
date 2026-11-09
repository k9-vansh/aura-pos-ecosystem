import re

from django.contrib.auth.hashers import check_password, make_password
from django.db import models


class OwnerAccount(models.Model):
    """A person who signs in to the owner app. Their credentials are their own (mobile number and a password made for
    this app); they are NOT POS users and cannot sign in to the POS, and the app only ever reads the POS's data.

    What they may open comes from a Role, the same Roles & Permissions the POS uses (the Owner App ticks), and which
    salons they see from the role's "all salons" flag or the list below. The few attributes at the bottom let the
    POS's own scoping and permission helpers treat an account like a user."""
    name = models.CharField(max_length=255)
    phone = models.CharField(max_length=15, unique=True)
    email = models.EmailField(blank=True, default='')
    password = models.CharField(max_length=128)
    role = models.ForeignKey('salon_admin.Role', null=True, blank=True, on_delete=models.SET_NULL, related_name='owner_accounts')
    centers = models.ManyToManyField('salon_admin.Center', blank=True, related_name='owner_accounts')
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_login = models.DateTimeField(null=True, blank=True)

    # Read by the POS's scoping and permission helpers.
    is_superuser = False
    is_staff = False
    center_id = None
    center = None

    def __str__(self):
        return f'{self.name} ({self.phone})'

    @property
    def is_authenticated(self):
        return True

    @property
    def is_anonymous(self):
        return False

    @property
    def full_name(self):
        return self.name

    @staticmethod
    def clean_phone(value):
        return re.sub(r'\D', '', str(value or ''))[-10:]

    def set_password(self, raw):
        self.password = make_password(raw)

    def check_password(self, raw):
        return bool(self.password) and check_password(raw, self.password)

    def save(self, *args, **kwargs):
        self.phone = self.clean_phone(self.phone)
        super().save(*args, **kwargs)


class OwnerToken(models.Model):
    """A sign-in of the owner app on one device. Per device, so signing in on a phone does not sign the same person out
    of their laptop. It stops working when the password changes (`fingerprint`), when the account is switched off,
    after `OWNER_TOKEN_DAYS` (default 30), or when they sign out."""
    key = models.CharField(max_length=40, unique=True)
    account = models.ForeignKey(OwnerAccount, on_delete=models.CASCADE, related_name='tokens')
    fingerprint = models.CharField(max_length=32, blank=True, default='')
    created = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)


class AiKey(models.Model):
    """The key the business pasted for one AI service (OpenRouter, Claude, OpenAI ...). One row per service; the key is
    sealed, never shown again (only its last four characters), and one row is "in use" (`is_primary`) - the weekly
    report asks it first and falls back to the others. See `ai_providers`."""
    provider = models.CharField(max_length=20, unique=True)
    sealed_key = models.TextField()
    last4 = models.CharField(max_length=4, blank=True, default='')
    model = models.CharField(max_length=120, blank=True, default='')
    base_url = models.CharField(max_length=300, blank=True, default='')
    is_primary = models.BooleanField(default=False)
    updated_by = models.CharField(max_length=255, blank=True, default='')
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f'{self.provider} (…{self.last4})'


class OwnerWeeklyReport(models.Model):
    """One written weekly report of a salon (Friday to Thursday).

    `numbers` holds every figure of the week as the POS worked it out; `narrative` is what the AI wrote about
    exactly those figures. Both are kept, so an old report reads the same whatever the bills do later.
    """
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='owner_reports')
    week_start = models.DateField()
    week_end = models.DateField()
    numbers = models.JSONField(default=dict)
    narrative = models.JSONField(default=dict, blank=True)
    model_name = models.CharField(max_length=120, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('center', 'week_start')]
        ordering = ['-week_start']
