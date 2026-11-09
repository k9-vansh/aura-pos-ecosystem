"""WhatsApp broadcasts: what was sent, and who came in within a week of it."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from billing.models import Invoice
from clients.models import Client
from marketing.models import WhatsAppCampaign, WhatsAppMessage
from salon_admin.dashboard_endpoints import _apply_security

from .base import LIVE, ZERO, change, dec, f2, pct

WINDOW_DAYS = 7          # a customer who is billed within a week of a broadcast is counted as a response
MAX_CAMPAIGNS = 30
_CHUNK = 400             # SQLite refuses more than 999 values in one query


def bucket(status):
    """The delivery text a message carries, as one of a few groups."""
    s = (status or '').strip().lower()
    if s.startswith('deliver') or s.startswith('read'):
        return 'Delivered'
    if s.startswith('sent'):
        return 'Sent'
    if s.startswith('queued'):
        return 'Queued'
    if s.startswith('fail') or s.startswith('error'):
        return 'Failed'
    if s.startswith('not sent') or s.startswith('skip'):
        return 'Not sent'
    return 'Other'


def _grouped(qs, *keys):
    out = defaultdict(lambda: defaultdict(int))
    for r in qs.values(*keys, 'status').annotate(n=Count('id')):
        out[tuple(r[k] for k in keys)][bucket(r['status'])] += r['n']
    return out


def _responses(ctx, campaign, phones):
    """Customers (of those messaged) billed within a week of the broadcast, and what they bought."""
    if not phones:
        return 0, ZERO
    clients = []
    for i in range(0, len(phones), _CHUNK):
        clients += list(Client.objects.filter(phone__in=phones[i:i + _CHUNK]).values_list('id', flat=True))
    billed, sales = set(), ZERO
    start = campaign.created_at
    for i in range(0, len(clients), _CHUNK):
        bills = _apply_security(ctx.request, Invoice.objects.filter(
            status__in=LIVE, client_id__in=clients[i:i + _CHUNK], created_at__gte=start,
            created_at__lt=start + timedelta(days=WINDOW_DAYS)))
        for r in bills.values('client').annotate(v=Sum('subtotal') - Sum('discount')):
            billed.add(r['client'])
            sales += dec(r['v'])
    return len(billed), sales


def whatsapp(ctx):
    msgs = WhatsAppMessage.objects.filter(date__gte=ctx.start, date__lte=ctx.end)
    prev = WhatsAppMessage.objects.filter(date__gte=ctx.prev_start, date__lte=ctx.prev_end)
    camps = WhatsAppCampaign.objects.filter(created_at__gte=ctx.start, created_at__lt=ctx.end + timedelta(days=1))
    if ctx.ids is not None:
        msgs, prev = msgs.filter(center_id__in=ctx.ids), prev.filter(center_id__in=ctx.ids)
        camps = camps.filter(Q(center_id__in=ctx.ids) | Q(center__isnull=True))
    if ctx.q:
        msgs = msgs.filter(message__icontains=ctx.q)
        camps = camps.filter(message__icontains=ctx.q)

    total_status = _grouped(msgs, 'date')
    overall = defaultdict(int)
    for per_day in total_status.values():
        for k, v in per_day.items():
            overall[k] += v
    sent_now = sum(overall.values())
    prev_total = prev.count()

    per_campaign = _grouped(msgs.filter(campaign__isnull=False), 'campaign')
    rows = []
    for c in camps.select_related('center').order_by('-created_at')[:MAX_CAMPAIGNS]:
        counts = per_campaign.get((c.id,), {})
        n = sum(counts.values())
        transmitted = n - counts.get('Not sent', 0) - counts.get('Queued', 0) - counts.get('Other', 0)
        billed = None
        sales = None
        if transmitted > 0:
            phones = list(WhatsAppMessage.objects.filter(campaign=c).exclude(status__istartswith='not sent')
                          .values_list('client_phone', flat=True).distinct())
            billed, spent = _responses(ctx, c, phones)
            sales = f2(spent)
        rows.append({
            'id': c.id, 'sent_on': c.created_at.strftime('%Y-%m-%d %H:%M'), 'message': c.message[:140],
            'centre': (c.center.display_name or c.center.center_name) if c.center else 'All centres',
            'recipients': c.recipients, 'messages': n, 'delivered': counts.get('Delivered', 0), 'sent': counts.get('Sent', 0),
            'queued': counts.get('Queued', 0), 'failed': counts.get('Failed', 0), 'not_sent': counts.get('Not sent', 0),
            'responded': billed, 'response_pct': pct(billed, transmitted) if billed is not None else None,
            'sales_7d': sales, 'status': c.status,
        })

    by_centre = []
    for r in msgs.values('center__display_name', 'center__center_name').annotate(n=Count('id')).order_by('-n'):
        by_centre.append({'centre': r['center__display_name'] or r['center__center_name'] or 'No centre', 'messages': r['n']})

    first = ctx.first_of_months(12)
    window = WhatsAppMessage.objects.filter(date__gte=first, date__lte=ctx.end)
    if ctx.ids is not None:
        window = window.filter(center_id__in=ctx.ids)
    monthly = {(r['y'], r['m']): r['n'] for r in window.annotate(y=ExtractYear('date'), m=ExtractMonth('date')).values(
        'y', 'm').annotate(n=Count('id'))}
    trend = [{'month': ctx.month_label(k), 'messages': monthly.get(k, 0)} for k in ctx.months(12)]

    totals = {
        'campaigns': camps.count(), 'messages': sent_now, 'messages_prev': prev_total,
        'messages_change': change(sent_now, prev_total),
        'delivered': overall.get('Delivered', 0), 'sent': overall.get('Sent', 0), 'queued': overall.get('Queued', 0),
        'failed': overall.get('Failed', 0), 'not_sent': overall.get('Not sent', 0), 'other': overall.get('Other', 0),
        'delivered_pct': pct(overall.get('Delivered', 0) + overall.get('Sent', 0), sent_now),
        'people': msgs.values('client_phone').distinct().count(),
    }
    return {
        'frame': ctx.frame(), 'enabled': True, 'totals': totals, 'rows': rows, 'by_centre': by_centre, 'trend': trend,
        'note': (f'A "response" is a customer billed within {WINDOW_DAYS} days after the broadcast. It shows who came in '
                 'after the message, not that the message made them come. Delivery states show only what the '
                 'WhatsApp provider has reported; while WhatsApp is not set up messages are recorded as "Not sent".'),
    }
