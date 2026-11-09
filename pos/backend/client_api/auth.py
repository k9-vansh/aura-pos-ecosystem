"""Sign-in tokens of the client app (see models.ClientToken). A client is a customer of the salon, not a POS user:
their token opens only the client app's own screens, and only their own records."""
import datetime
import hashlib
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import ClientToken

MAX_DEVICES = 5


def fingerprint(client):
    """Changes whenever the client's PIN does, so a changed PIN signs every phone out."""
    return hashlib.sha256((client.pin_sealed or '').encode()).hexdigest()[:32]


def days():
    return int(getattr(settings, 'CLIENT_TOKEN_DAYS', 60))


def issue(client):
    cutoff = timezone.now() - datetime.timedelta(days=days())
    ClientToken.objects.filter(client=client, created__lt=cutoff).delete()
    keep = list(ClientToken.objects.filter(client=client).order_by('-created').values_list('id', flat=True)[:MAX_DEVICES - 1])
    ClientToken.objects.filter(client=client).exclude(id__in=keep).delete()
    return ClientToken.objects.create(client=client, key=secrets.token_hex(20), fingerprint=fingerprint(client))


class ClientUser:
    """What request.user is in the client app: the client, answering the few things DRF asks of a user."""
    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __init__(self, client):
        self.client = client
        self.pk = client.pk


class ClientTokenAuthentication(BaseAuthentication):
    keyword = 'Token'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise AuthenticationFailed('Invalid token.')
        try:
            token = ClientToken.objects.select_related('client', 'client__center').get(key=parts[1].decode())
        except (ClientToken.DoesNotExist, UnicodeError):
            raise AuthenticationFailed('Invalid token.')
        client = token.client
        if not client.is_active:
            raise AuthenticationFailed('User inactive or deleted.')
        if token.created < timezone.now() - datetime.timedelta(days=days()):
            raise AuthenticationFailed('Token has expired.')
        if token.fingerprint != fingerprint(client):
            raise AuthenticationFailed('Your PIN changed. Please sign in again.')
        now = timezone.now()
        if not token.last_used or now - token.last_used > datetime.timedelta(hours=1):
            ClientToken.objects.filter(pk=token.pk).update(last_used=now)
        return ClientUser(client), token

    def authenticate_header(self, request):
        return self.keyword
