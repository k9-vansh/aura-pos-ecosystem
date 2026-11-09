"""WebSocket authentication and the live-dashboard channels.

Every socket authenticates with the same token, and the same expiry rule, as
the REST API. Live dashboard events go only to the branches they belong to
(finding R18): the dashboard socket used to accept anyone, without a token, and
broadcast every branch's payments to every connected page.
"""

import datetime
import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

ALL_BRANCHES_GROUP = 'dashboard_all'


def branch_group(center_id):
    return f'dashboard_center_{int(center_id)}'


def token_from_scope(scope):
    query_string = scope.get('query_string', b'').decode('utf-8')
    for part in query_string.split('&'):
        if part.startswith('token='):
            return part[6:]
    return None


def user_from_token(token_key):
    """The active user for a token, or None. Same expiry as ExpiringTokenAuthentication."""
    if not token_key:
        return None
    from rest_framework.authtoken.models import Token

    token = Token.objects.select_related('user').filter(key=token_key).first()
    if token is None or not token.user.is_active:
        return None
    max_age_days = getattr(settings, 'API_TOKEN_MAX_AGE_DAYS', 7)
    if token.created < timezone.now() - datetime.timedelta(days=max_age_days):
        return None
    return token.user


def dashboard_groups_for(user):
    """The dashboard channels `user` may listen to, or [] if none."""
    from pos_backend.permissions import has_module_permission
    from pos_backend.scoping import user_center_ids

    # The live event carries a bill's amount, so it goes to whoever may see the
    # Overview or Revenue tab. The other tabs refresh when the screen is reloaded.
    if not (has_module_permission(user, 'dashboard', 'read', 'summary')
            or has_module_permission(user, 'dashboard', 'read', 'revenues')):
        return []
    center_ids = user_center_ids(user)
    if center_ids is None:          # superuser or an all-branches role
        return [ALL_BRANCHES_GROUP]
    return [branch_group(cid) for cid in center_ids]


def broadcast_dashboard(center_id, update_type, data):
    """Send a live dashboard event to the branch's channel and the all-branches one.

    Never raises: a missing channel layer must not fail a checkout.
    """
    try:
        from asgiref.sync import async_to_sync
        from channels.layers import get_channel_layer

        layer = get_channel_layer()
        if layer is None:
            return
        event = {'type': 'dashboard_update', 'update_type': update_type, 'data': data}
        groups = [ALL_BRANCHES_GROUP]
        if center_id:
            groups.append(branch_group(center_id))
        for group in groups:
            async_to_sync(layer.group_send)(group, event)
    except Exception:
        logger.warning('Dashboard broadcast failed', exc_info=True)
