"""A staff member's own work, worked out the way the POS credits it (Finance > Staff Performance): a line with one
named person goes whole to them, a line shared between several is split as the till saved it. Cancelled bills and drafts
are never counted. Redeemed lines (paid from a card, package or membership) are shown on their own and are not sales."""
import datetime
from decimal import Decimal

from django.db.models import Q

from billing.models import Invoice, InvoiceItem

KINDS = {'servicemaster': 'services', 'product': 'products', 'membership': 'memberships', 'valuecard': 'cards', 'package': 'packages'}
ORDER = ('services', 'products', 'memberships', 'cards', 'packages')
ZERO = Decimal('0')


def num(value):
    return float(value or 0)


def lines(staff, start, end):
    """[{kind, redeemed, client, item, amount, qty, day, time}] of this person's lines between two days (both included),
    oldest first. `amount` is their share of the line."""
    from finance.views import credited_shares
    items = (InvoiceItem.objects
             .filter(invoice__status__in=Invoice.REVENUE_STATUSES, invoice__created_at__date__gte=start, invoice__created_at__date__lte=end)
             .filter(Q(staff=staff) | Q(staff_members=staff)).distinct()
             .select_related('invoice', 'invoice__client', 'invoice__center', 'content_type', 'staff')
             .prefetch_related('staff_members').order_by('invoice__created_at', 'id'))
    out = []
    for item in items:
        mine = next((share for member, share, _tax in credited_shares(item) if member.pk == staff.pk), None)
        if mine is None:
            continue
        when = item.invoice.created_at
        client = item.invoice.client
        out.append({'kind': KINDS.get(item.content_type.model if item.content_type_id else '', 'services'),
                    'redeemed': bool(item.is_redemption), 'client': client.full_name if client else 'Walk-in',
                    'item': item.description or 'Item', 'amount': mine, 'qty': int(item.quantity or 1),
                    'day': when.date(), 'time': when.strftime('%I:%M %p').lstrip('0'),
                    'salon': (item.invoice.center.display_name or item.invoice.center.center_name) if item.invoice.center_id else ''})
    return out


def summary(rows):
    """The Job Card table: count and amount per kind, the total sold, and what was redeemed."""
    table = {k: {'count': 0, 'amount': ZERO} for k in ORDER}
    redeemed = {'count': 0, 'amount': ZERO}
    for row in rows:
        if row['redeemed']:
            redeemed['count'] += row['qty']
            redeemed['amount'] += row['amount']
            continue
        table[row['kind']]['count'] += row['qty']
        table[row['kind']]['amount'] += row['amount']
    total = sum((v['amount'] for v in table.values()), ZERO)
    return {'total': num(total),
            'kinds': [{'key': k, 'count': table[k]['count'], 'amount': num(table[k]['amount'])} for k in ORDER],
            'redeemed': {'count': redeemed['count'], 'amount': num(redeemed['amount'])}}


def month_bounds(year, month):
    first = datetime.date(year, month, 1)
    nxt = datetime.date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - datetime.timedelta(days=1)
