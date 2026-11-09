"""Which salons a request is about, and which day or month it names."""
import datetime

from rest_framework.exceptions import PermissionDenied, ValidationError

from pos_backend.scoping import coerce_center_id, user_center_ids
from salon_admin.models import Center


def allowed_centers(user):
    """The active salons this user may see, as a queryset."""
    qs = Center.objects.all()
    allowed = user_center_ids(user)
    if allowed is not None:
        qs = qs.filter(id__in=allowed)
    return qs.order_by('display_name', 'center_name')


def center_name(center):
    return center.display_name or center.center_name


def resolve(user, raw):
    """(center ids, the one centre id or None for "all my salons", a label).

    A salon the user may not see is refused, never quietly widened or dropped."""
    qs = allowed_centers(user)
    text = str(raw or '').strip().lower()
    if text in ('', 'all'):
        ids = list(qs.values_list('id', flat=True))
        if not ids:
            raise PermissionDenied('No salon is assigned to you.')
        return ids, None, 'All salons'
    cid = coerce_center_id(text)
    center = qs.filter(id=cid).first() if cid is not None else None
    if center is None:
        raise PermissionDenied('You cannot see that salon.')
    return [center.id], center.id, center_name(center)


def parse_day(raw, default=None):
    if not raw:
        return default or datetime.date.today()
    try:
        return datetime.datetime.strptime(str(raw).strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        raise ValidationError({'date': 'Use a date like 2026-10-31.'})


def parse_month(raw):
    """(year, month) of "2026-10"; this month when empty."""
    if not raw:
        today = datetime.date.today()
        return today.year, today.month
    try:
        parsed = datetime.datetime.strptime(str(raw).strip()[:7], '%Y-%m')
    except ValueError:
        raise ValidationError({'month': 'Use a month like 2026-10.'})
    return parsed.year, parsed.month
