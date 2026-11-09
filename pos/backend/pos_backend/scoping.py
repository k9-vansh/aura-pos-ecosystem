"""
Single source of truth for multi-center data scoping.

The audit found the same ~10 line block copy-pasted across roughly fifteen
viewsets, and in most copies the final `else` was missing:

    if not is_owner and not perms.get('all_centers'):
        if user.centers.exists():   qs = qs.filter(center__in=...)
        elif user.center:           qs = qs.filter(center=...)
        # <- no else: no filter applied, ALL centers visible

A user with neither `centers` nor `center` populated (both nullable, so trivial
to create) therefore saw every center's data. Three viewsets did get it right
by returning .none(), which establishes that fail-closed is the intent.

Everything scoping-related now goes through `scope_to_centers`, which fails
closed by default.
"""

import contextvars

from pos_backend.permissions import IsOwner


# ─── Several centres picked at once ───────────────────────────────────────
#
# The location dropdown lets a person tick any number of centres. The screen
# sends `?center_ids=3,7,9` on the requests that read data. For the length of
# that one read request the choice is held here, and every scoping helper below
# honours it: it can only NARROW what the user may already see, never widen it.
# Writes never carry it, so saving a record is untouched.

_SELECTION = contextvars.ContextVar('pos_center_selection', default=None)


def parse_center_ids(raw):
    """'3,7,9' -> (3, 7, 9). Anything that is not a whole number is dropped."""
    out = []
    for part in str(raw or '').split(','):
        part = part.strip()
        if part.isdigit() and int(part) not in out:
            out.append(int(part))
    return tuple(out)


def selected_center_ids():
    """The centres picked for this request (two or more), or None."""
    return _SELECTION.get()


def narrow_to_selection(queryset, field='center'):
    """Keep only the picked centres' rows, when several are picked."""
    picked = _SELECTION.get()
    if not picked:
        return queryset
    return queryset.filter(**{f'{field}_id__in': list(picked)})


def selection_key():
    """A string for cache keys, so two selections never share an answer."""
    picked = _SELECTION.get()
    return ','.join(str(i) for i in sorted(picked)) if picked else ''


class CenterSelectionMiddleware:
    """Holds `?center_ids=` for the length of a read request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        picked = ()
        if request.method in ('GET', 'HEAD'):
            picked = parse_center_ids(request.GET.get('center_ids'))
        token = _SELECTION.set(picked if len(picked) > 1 else None)
        try:
            return self.get_response(request)
        finally:
            _SELECTION.reset(token)


def user_center_ids(user):
    """Return the set of center ids this user may see, or None for 'all'.

    None means unrestricted (owner or the all_centers permission). An empty set
    means the user is assigned to no center and must see nothing. When several
    centres are picked for this request the answer is narrowed to them.
    """
    allowed = _user_allowed_center_ids(user)
    picked = _SELECTION.get()
    if not picked:
        return allowed
    if allowed is None:
        return set(picked)
    return allowed & set(picked)


def _user_allowed_center_ids(user):
    if not user or not getattr(user, 'is_authenticated', False):
        return set()

    if IsOwner.check_is_owner(user):
        return None

    role = getattr(user, 'role', None)
    perms = getattr(role, 'permissions', None) or {}
    if perms.get('all_centers', False):
        return None

    ids = set()
    try:
        ids.update(user.centers.values_list('id', flat=True))
    except Exception:
        pass
    own = getattr(user, 'center_id', None)
    if own:
        ids.add(own)
    return ids


def safe_date_param(raw, default=None):
    """Validate a YYYY-MM-DD query param, returning it as a STRING.

    Report views read `start_date` / `end_date` from the request and use them
    two ways: passed to `strptime`, and passed straight into an ORM filter. An
    unparseable value reaching the ORM raises Django's ValidationError - a 500
    from one malformed URL, a stale bookmark or a frontend glitch.

    A string is returned rather than a date object so both downstream uses keep
    working. Junk yields `default` (usually None, which the surrounding
    `if start_date:` guards then skip).
    """
    import datetime as _dt

    if raw in (None, '', 'null', 'undefined', 'None'):
        return default
    text = str(raw).strip()
    try:
        _dt.datetime.strptime(text, '%Y-%m-%d')
    except (TypeError, ValueError):
        return default
    return text


def can_use_center(user, center_id):
    """True if `user` is allowed to act on `center_id`."""
    if center_id in (None, '', 'null'):
        return False
    allowed = user_center_ids(user)
    if allowed is None:
        return True
    try:
        return int(center_id) in allowed
    except (TypeError, ValueError):
        return False


def coerce_center_id(raw):
    """Parse a center_id query param. Returns int or None.

    The frontend sends the literal string 'null' in several places, and
    `int('null')` was a live 500 in the inventory viewset.
    """
    if raw in (None, '', 'null', 'undefined', 'None'):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    # A number no database id can hold (a pasted phone number, 10**20) made SQLite
    # raise OverflowError - a 500 from one odd query string. No branch has it.
    if abs(value) > 2147483647:
        return None
    return value


def scope_to_centers(queryset, user, field='center', requested_center=None):
    """Restrict `queryset` to the centers `user` may see. Fails closed.

    `field` is the path to the Center FK ('center', 'client__center', ...), or
    'id' / 'pk' when the queryset IS of Centers.
    `requested_center` narrows further, but only to a center the user may
    already see - a request for a forbidden center yields .none() rather than
    being silently ignored.
    """
    allowed = user_center_ids(user)
    requested = coerce_center_id(requested_center)

    # Scoping the Center model itself: the lookup is the primary key, not
    # `<field>_id`, which would build the nonexistent 'id_id'.
    if field in ('id', 'pk'):
        exact, in_list = field, f'{field}__in'
    else:
        exact, in_list = f'{field}_id', f'{field}_id__in'

    if allowed is None:
        if requested is not None:
            return queryset.filter(**{exact: requested})
        return queryset

    if not allowed:
        return queryset.none()

    if requested is not None:
        if requested not in allowed:
            return queryset.none()
        return queryset.filter(**{exact: requested})

    return queryset.filter(**{in_list: allowed})


def assert_can_write_center(user, center):
    """Raise PermissionDenied unless `user` may write to `center`.

    Replaces the several hand-rolled create/update checks, one of which
    (DailyClosingViewSet.create) was missing its single-center branch entirely
    and let any user overwrite any center's day-end cash record.
    """
    from rest_framework.exceptions import PermissionDenied

    allowed = user_center_ids(user)
    if allowed is None:
        return

    center_id = getattr(center, 'id', center)
    if center_id is None:
        raise PermissionDenied('A center is required for this action.')
    try:
        center_id = int(center_id)
    except (TypeError, ValueError):
        raise PermissionDenied('Invalid center.')

    if center_id not in allowed:
        raise PermissionDenied('You are not assigned to this center.')


def resolve_write_center(user, *candidates):
    """Pick the center to write to, validating the user may use it.

    Tries each candidate in order; falls back to the user's only center if they
    have exactly one. Raises PermissionDenied if nothing valid resolves.
    """
    from rest_framework.exceptions import PermissionDenied

    allowed = user_center_ids(user)

    for cand in candidates:
        cid = coerce_center_id(getattr(cand, 'id', cand))
        if cid is None:
            continue
        if allowed is None or cid in allowed:
            return cid
        raise PermissionDenied('You are not assigned to this center.')

    if allowed is None:
        raise PermissionDenied('A center is required for this action.')
    if len(allowed) == 1:
        return next(iter(allowed))
    if not allowed:
        raise PermissionDenied('You are not assigned to any center.')
    raise PermissionDenied('A center is required for this action.')


def advances_in_centers(queryset, center_ids):
    """Advance-ledger rows belonging to these branches.

    The branch that took the advance is on the row. Older rows have none, and
    fall back to the client's own branch. The dashboards used the bill's or the
    staff member's branch and the register summary the client's, so one advance
    could be counted at a different branch on each screen.
    """
    from django.db.models import Q
    ids = list(center_ids)
    return queryset.filter(Q(center_id__in=ids) | Q(center__isnull=True, client__center_id__in=ids))
