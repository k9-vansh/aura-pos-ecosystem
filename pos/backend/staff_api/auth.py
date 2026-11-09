"""Sign-in tokens of the staff app (see models.StaffToken). A staff member is not a POS user: their token opens only
the staff app's own screens, and only their own work."""
import datetime
import hashlib
import secrets

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed

from .models import StaffToken

MAX_DEVICES = 3


def fingerprint(staff):
    """Changes whenever the staff member's app PIN does, so a new PIN signs every phone out."""
    return hashlib.sha256((staff.app_password or '').encode()).hexdigest()[:32]


def days():
    return int(getattr(settings, 'STAFF_TOKEN_DAYS', 30))


def issue(staff):
    cutoff = timezone.now() - datetime.timedelta(days=days())
    StaffToken.objects.filter(staff=staff, created__lt=cutoff).delete()
    keep = list(StaffToken.objects.filter(staff=staff).order_by('-created').values_list('id', flat=True)[:MAX_DEVICES - 1])
    StaffToken.objects.filter(staff=staff).exclude(id__in=keep).delete()
    return StaffToken.objects.create(staff=staff, key=secrets.token_hex(20), fingerprint=fingerprint(staff))


class StaffUser:
    """What request.user is in the staff app: the staff member."""
    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __init__(self, staff):
        self.staff = staff
        self.pk = staff.pk


class StaffTokenAuthentication(BaseAuthentication):
    keyword = 'Token'

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != self.keyword.lower().encode():
            return None
        if len(parts) != 2:
            raise AuthenticationFailed('Invalid token.')
        try:
            token = StaffToken.objects.select_related('staff', 'staff__center').get(key=parts[1].decode())
        except (StaffToken.DoesNotExist, UnicodeError):
            raise AuthenticationFailed('Invalid token.')
        staff = token.staff
        if not staff.is_active or not staff.app_password:
            raise AuthenticationFailed('User inactive or deleted.')
        if token.created < timezone.now() - datetime.timedelta(days=days()):
            raise AuthenticationFailed('Token has expired.')
        if token.fingerprint != fingerprint(staff):
            raise AuthenticationFailed('Your PIN changed. Please sign in again.')
        now = timezone.now()
        if not token.last_used or now - token.last_used > datetime.timedelta(hours=1):
            StaffToken.objects.filter(pk=token.pk).update(last_used=now)
        return StaffUser(staff), token

    def authenticate_header(self, request):
        return self.keyword
