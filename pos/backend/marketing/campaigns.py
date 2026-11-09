"""WhatsApp broadcast campaigns: who may be messaged, and the sending itself.

Kept out of the view so the same rules answer the preview ("how many people
will get this?") and the send, and so a large audience can be prepared by a
background worker instead of inside the web request.
"""

import datetime
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

# Up to this many recipients are prepared while the request waits (so the
# answer says exactly how many were queued); a bigger audience is handed to a
# worker, because it is one database write per customer.
INLINE_LIMIT = 200

# The same text to the same audience inside this window is treated as a
# double-click or a second operator doing the same thing.
DUPLICATE_WINDOW = datetime.timedelta(minutes=10)


def scope_key(center_ids):
    return 'all' if center_ids is None else ','.join(str(i) for i in sorted(center_ids))


def personalise(message, client):
    """`{name}` becomes the customer's first name ("there" when unknown)."""
    first = (getattr(client, 'first_name', '') or '').strip() or 'there'
    return message.replace('{name}', first)


def _base(center_ids):
    """Customers who could be messaged at all: active, not blacklisted, with a phone."""
    from clients.models import Client

    qs = (Client.objects.filter(is_active=True, is_blacklisted=False)
          .exclude(phone__isnull=True).exclude(phone=''))
    if center_ids is not None:
        qs = qs.filter(center_id__in=list(center_ids))
    return qs


def iter_audience(center_ids):
    """Yield (client, reason) for everyone with a phone; reason is None when they
    may be messaged, else 'dnd' or 'consent'.

    DND is judged by the notification service's own rule (trimmed, any case), so
    the broadcast and the automatic reminders can never disagree about who is on
    it. The default value is the string 'NOT ON DND', which a substring match
    would wrongly treat as DND, so only whole words count.
    """
    from notifications.service import client_on_dnd

    qs = _base(center_ids).select_related('center').only(
        'id', 'phone', 'first_name', 'last_name', 'center', 'dnd_status', 'whatsapp_consent')
    for client in qs.iterator(chunk_size=500):
        if not (client.phone or '').strip():
            continue
        if client_on_dnd(client):
            yield client, 'dnd'
        elif not client.whatsapp_consent:
            yield client, 'consent'
        else:
            yield client, None


def count_audience(center_ids):
    counts = {'eligible': 0, 'skipped_no_consent': 0, 'skipped_dnd': 0, 'without_branch': 0}
    for client, reason in iter_audience(center_ids):
        if reason == 'dnd':
            counts['skipped_dnd'] += 1
        elif reason == 'consent':
            counts['skipped_no_consent'] += 1
        else:
            counts['eligible'] += 1
            if not client.center_id:
                counts['without_branch'] += 1
    return counts


def recent_duplicate(message, center_ids):
    from .models import WhatsAppCampaign

    since = timezone.now() - DUPLICATE_WINDOW
    return (WhatsAppCampaign.objects
            .filter(scope_key=scope_key(center_ids), message=message, created_at__gte=since)
            .order_by('-created_at').first())


def run_campaign(campaign_id, center_ids):
    """Write one message row per eligible customer and queue the real send.

    Safe to run again after a crash: customers who already have a row in this
    campaign are skipped, so nobody is messaged twice.
    Returns how many customers were handled.
    """
    from notifications import integrations
    from notifications.service import _queue

    from .models import WhatsAppCampaign, WhatsAppMessage

    campaign = WhatsAppCampaign.objects.filter(pk=campaign_id).first()
    if campaign is None:
        return 0

    ready = integrations.is_ready('whatsapp')
    status_text = 'Queued' if ready else 'Not sent - WhatsApp not set up'
    done_phones = set(WhatsAppMessage.objects.filter(campaign=campaign)
                      .values_list('client_phone', flat=True))
    now = timezone.now()
    handled = 0
    pending = []

    for client, reason in iter_audience(center_ids):
        if reason is not None or client.phone in done_phones:
            continue
        done_phones.add(client.phone)
        handled += 1
        body = personalise(campaign.message, client)
        row = WhatsAppMessage(
            campaign=campaign, center=client.center, date=now.date(), time=now.time(),
            client_name=client.full_name, client_phone=client.phone,
            message=body, status=status_text)
        if not ready:
            pending.append(row)
            if len(pending) >= 1000:
                WhatsAppMessage.objects.bulk_create(pending)
                pending = []
            continue
        row.save()
        _queue('client.campaign', 'whatsapp', client.phone, client=client,
               center_id=client.center_id, body=body, template_kind='general',
               extra_payload={'whatsapp_message_id': row.pk})
    if pending:
        WhatsAppMessage.objects.bulk_create(pending)

    WhatsAppCampaign.objects.filter(pk=campaign.pk).update(status=WhatsAppCampaign.STATUS_DONE)
    return handled
