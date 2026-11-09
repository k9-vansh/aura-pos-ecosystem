"""Raise a notification: the bell, e-mail and WhatsApp, off the request path.

    from notifications.service import notify
    notify('approval.requested', title='...', body='...', center=center, users=approvers)

The bell row is written at once; e-mail and WhatsApp are `DeliveryLog` rows plus
a background job each, so a slow mail server never holds a till. Everything is
created in the caller's transaction: if the business change rolls back, nothing
is sent.
"""

import logging

from django.db import transaction

from notifications.events import EVENTS
from notifications.models import DeliveryLog, Notification, NotificationRule

logger = logging.getLogger(__name__)


# ─── Rules ──────────────────────────────────────────────────────────────────

class _Rule:
    def __init__(self, event, row=None):
        spec = EVENTS[event]
        self.event = event
        self.enabled = row.enabled if row else True
        self.bell = row.bell if row else spec['bell']
        self.email = row.email if row else spec['email']
        self.whatsapp = row.whatsapp if row else spec['whatsapp']
        self.role_ids = list(row.role_ids) if row else []
        self.user_ids = list(row.user_ids) if row else []
        self.people = spec['people']
        self.template = spec['template']


def rule_for(event):
    return _Rule(event, NotificationRule.objects.filter(event=event).first())


def configured_users(rule, center_id=None):
    """Users named by the rule: by role (who can see the branch) and one by one."""
    from accounts.models import CustomUser
    from pos_backend.scoping import user_center_ids

    found = {}
    if rule.user_ids:
        for user in CustomUser.objects.filter(pk__in=rule.user_ids, is_active=True):
            found[user.pk] = user
    if rule.role_ids:
        for user in CustomUser.objects.filter(role_id__in=rule.role_ids, is_active=True):
            if center_id is not None:
                allowed = user_center_ids(user)
                if allowed is not None and center_id not in allowed:
                    continue
            found[user.pk] = user
    return list(found.values())


# ─── Raising ────────────────────────────────────────────────────────────────

def _resolve(value, user):
    return value(user) if callable(value) else value


def notify(event, *, title, body, url='', center=None, users=(), data=None,
           email_subject=None, email_body=None, whatsapp_body=None):
    """Tell people about `event`. `body`, `email_body` and `whatsapp_body` may be
    a function of the user (an approver's own link). Returns the users told."""
    rule = rule_for(event)
    if not rule.enabled:
        return []
    center_id = getattr(center, 'pk', center)

    recipients = {u.pk: u for u in users if u is not None and getattr(u, 'is_active', True)}
    for user in configured_users(rule, center_id):
        recipients.setdefault(user.pk, user)

    for user in recipients.values():
        text = _resolve(body, user)
        if rule.bell:
            note = Notification.objects.create(
                user=user, event=event, title=title[:200], body=text, url=url[:300],
                data=data or {}, center_id=center_id)
            _push(user.pk, {'type': 'notification', 'notification': serialise(note)})
        if rule.email and user.email:
            subject = (email_subject or title)[:200]
            mail_text = _resolve(email_body, user) or text
            _queue(event, 'email', user.email, user=user, center_id=center_id, subject=subject, body=mail_text,
                   html=_designed(event, subject, mail_text, user=user, url=url, data=data, center_id=center_id))
        if rule.whatsapp and user.phone:
            _queue(event, 'whatsapp', user.phone, user=user, center_id=center_id,
                   body=_resolve(whatsapp_body, user) or text, template_kind=rule.template)
    return list(recipients.values())


def serialise(note):
    return {'id': note.pk, 'event': note.event, 'title': note.title, 'body': note.body,
            'url': note.url, 'data': note.data, 'read': note.read_at is not None,
            'created_at': note.created_at.isoformat() if note.created_at else None}


def _designed(event, subject, text, *, user=None, client=None, url='', data=None, center_id=None):
    """The designed (HTML) version of an e-mail; '' if it cannot be made, so the plain text still goes."""
    try:
        from notifications import email_layout
        from salon_admin.models import Center

        center = Center.objects.filter(pk=center_id).first() if center_id else None
        return email_layout.render(event, subject, text, user=user, client=client, url=url, data=data,
                                   center=center)['html']
    except Exception:                                                       # noqa: BLE001
        logger.warning('could not design the e-mail for %s', event, exc_info=True)
        return ''


def _queue(event, channel, recipient, *, user=None, client=None, center_id=None, subject='',
           body='', template_kind='', dedupe_key=None, extra_payload=None, html=''):
    from notifications import integrations
    from pos_backend.jobs import enqueue

    template = integrations.template_for(template_kind) if (channel == 'whatsapp' and template_kind) else ''
    log = DeliveryLog.objects.create(
        event=event, channel=channel, recipient=recipient[:200], user=user, client=client,
        center_id=center_id, subject=subject, body=body, html=html, template=template,
        params=[body] if template else [], dedupe_key=dedupe_key)
    enqueue('notifications.deliver', {'log_id': log.pk, **(extra_payload or {})}, dedupe_key=f'notif:{log.pk}',
            max_attempts=5)
    return log


# ─── Clients ────────────────────────────────────────────────────────────────

DND_WORDS = {'ON DND', 'DND', 'ON', 'YES'}


def client_on_dnd(client):
    return str(client.dnd_status or '').strip().upper() in DND_WORDS


def client_channels(client, wanted=('whatsapp', 'email')):
    """The channels a client may be messaged on: consent given, not on DND, address on file."""
    out = []
    if not client.is_active or client.is_blacklisted:
        return out
    if 'whatsapp' in wanted and client.phone and client.whatsapp_consent and not client_on_dnd(client):
        out.append('whatsapp')
    if 'email' in wanted and client.email:
        out.append('email')
    return out


def send_to_client(event, client, *, subject, body, key, template_kind='reminder',
                   channels=('whatsapp', 'email')):
    """Message a client, at most once per `key` and channel. Returns the channels queued."""
    queued = []
    for channel in client_channels(client, channels):
        dedupe = f'{key}:{channel}'
        if DeliveryLog.objects.filter(dedupe_key=dedupe).exists():
            continue
        recipient = client.phone if channel == 'whatsapp' else client.email
        mail_html = (_designed(event, subject, body, client=client, center_id=client.center_id)
                     if channel == 'email' else '')
        with transaction.atomic():
            _queue(event, channel, recipient, client=client,
                   center_id=client.center_id, subject=subject, body=body,
                   template_kind=template_kind, dedupe_key=dedupe, html=mail_html)
        queued.append(channel)
    return queued


# ─── Live push ──────────────────────────────────────────────────────────────

def user_group(user_id):
    return f'user_{int(user_id)}'


def _push(user_id, payload):
    """Send `payload` to the user's open pages, after the transaction commits."""
    def send():
        try:
            from asgiref.sync import async_to_sync
            from channels.layers import get_channel_layer

            layer = get_channel_layer()
            if layer is not None:
                async_to_sync(layer.group_send)(
                    user_group(user_id), {'type': 'user.event', 'payload': payload})
        except Exception:                                      # noqa: BLE001
            logger.debug('live push failed', exc_info=True)

    transaction.on_commit(send)


push_to_user = _push
