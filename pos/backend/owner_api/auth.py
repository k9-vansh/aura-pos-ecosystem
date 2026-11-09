"""Sign-in tokens of the owner app (see models.OwnerToken)."""
import datetime
import hashlib
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import OwnerToken

MAX_DEVICES = 10


def fingerprint(account):
    """Changes whenever the account's password does, so a changed password signs every device out."""
    return hashlib.sha256((account.password or '').encode()).hexdigest()[:32]


def days():
    return int(getattr(settings, 'OWNER_TOKEN_DAYS', 30))


def issue(account):
    # Old and surplus sign-ins of this person are tidied away; the newest MAX_DEVICES stay.
    cutoff = timezone.now() - datetime.timedelta(days=days())
    OwnerToken.objects.filter(account=account, created__lt=cutoff).delete()
    keep = list(OwnerToken.objects.filter(account=account).order_by('-created').values_list('id', flat=True)[:MAX_DEVICES - 1])
    OwnerToken.objects.filter(account=account).exclude(id__in=keep).delete()
    return OwnerToken.objects.create(account=account, key=secrets.token_hex(20), fingerprint=fingerprint(account))


class OwnerTokenAuthentication(BaseAuthentication):
    keyword = 'Token'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise AuthenticationFailed('Invalid token.')
        try:
            token = OwnerToken.objects.select_related('account', 'account__role').get(key=parts[1].decode())
        except (OwnerToken.DoesNotExist, UnicodeError):
            raise AuthenticationFailed('Invalid token.')
        user = token.account
        if not user.is_active:
            raise AuthenticationFailed('User inactive or deleted.')
        if token.created < timezone.now() - datetime.timedelta(days=days()):
            raise AuthenticationFailed('Token has expired.')
        if token.fingerprint != fingerprint(user):
            raise AuthenticationFailed('The password changed. Please sign in again.')
        now = timezone.now()
        if not token.last_used or now - token.last_used > datetime.timedelta(hours=1):
            OwnerToken.objects.filter(pk=token.pk).update(last_used=now)
        return user, token

    def authenticate_header(self, request):
        return self.keyword
