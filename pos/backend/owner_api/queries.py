"""What the owner app shows, worked out from the POS's own tables.

The headline figures come from the same function as Finance > Register Summary, so the owner's phone and the
till agree to the paisa. Every function takes the salons (`cids`) already checked by `scope.resolve`.
"""
import calendar
import datetime
from decimal import Decimal

from django.db.models import Count, F, Max, Min, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractWeekDay, ExtractYear, TruncDate

from billing import payment_methods as pm
from .cache import remember
from billing.models import AdvancePayment, Invoice, InvoiceItem
from clients.models import Client, ClientPackage, ClientValueCard

D = Decimal
# A line's sales value: its pre-tax total less its share of any bill-level discount (as on the POS dashboard).
LINE_VALUE = Sum(F('total_price') - F('bill_discount_share'))
ITEM_KINDS = {'servicemaster': 'services', 'product': 'products', 'membership': 'memberships',
              'valuecard': 'value_cards', 'package': 'packages'}
WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']


def num(value, places=2):
    return round(float(value or 0), places)


def month_bounds(year, month):
    return datetime.date(year, month, 1), datetime.date(year, month, calendar.monthrange(year, month)[1])


def month_back(year, month, k):
    index = year * 12 + (month - 1) - k
    return index // 12, index % 12 + 1


def invoices_for(cids, start, end):
    """Settled bills (paid or part-paid) of these salons between two days, both included."""
    return Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, center_id__in=cids,
                                  created_at__date__gte=start, created_at__date__lte=end)


def latest_bill_day(cids, upto=None):
    """The most recent day (up to `upto`, default today) on which these salons settled a bill, as text, or None.

    Lets a screen that opens on an empty day say so and offer the last day that has figures."""
    upto = upto or datetime.date.today()
    last = (Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, center_id__in=cids, created_at__date__lte=upto)
            .aggregate(last=Max('created_at'))['last'])
    return last.date().isoformat() if last else None


def month_target(cids, year, month):
    """The salons' target for a month: the month's own entry if one was set, else the standing monthly target."""
    from salon_admin.models import Center
    key = f'{calendar.month_abbr[month]}-{year}'
    total = D('0')
    for center in Center.objects.filter(id__in=cids).only('monthly_target', 'monthly_targets_history'):
        value = None
        raw = (center.monthly_targets_history or {}).get(key)
        try:
            value = D(str(raw)) if raw not in (None, '') else None
        except ArithmeticError:
            value = None
        total += value or D(str(center.monthly_target or 0))
    return total


def register(user, center_id, start, end):
    from finance.views import compute_register_summary
    return compute_register_summary(user, center_id, start.isoformat(), end.isoformat())


def _label(key):
    return {'credit_card': 'Card', 'upi': 'UPI', 'google_pay': 'Google Pay', 'phone_pe': 'PhonePe',
            'bharat_pe': 'BharatPe', 'cheque_net_banking': 'Cheque / net banking'}.get(key, key.replace('_', ' ').title())


def sales_block(user, cids, center_id, start, end):
    """Everything the Today and Month tops share, for [start, end]."""
    reg = register(user, center_id, start, end)
    revenue, pays = reg['revenues'], reg['payment_methods']
    bills = invoices_for(cids, start, end)
    last = bills.aggregate(last=Max('created_at'))['last']
    known = bills.exclude(client__isnull=True).values('client_id').distinct().count()
    walk_ins = bills.filter(client__isnull=True).count()

    cash, card = D(pays['cash']['amount']), D(pays['credit_card']['amount'])
    liability = sum((D(pays[b]['amount']) for b in pm.LIABILITY_BUCKETS), D('0'))
    other = sum((D(pays[k]['amount']) for k in pm.REPORT_BUCKETS
                 if k not in ('cash', 'credit_card') and k not in pm.LIABILITY_BUCKETS), D('0'))

    kinds = {k: {'count': 0, 'amount': 0.0} for k in ('services', 'products', 'memberships', 'value_cards', 'packages')}
    rows = (InvoiceItem.objects.filter(invoice__in=bills, is_redemption=False)
            .values('content_type__model').annotate(n=Sum('quantity'), amount=LINE_VALUE))
    for row in rows:
        kind = ITEM_KINDS.get(row['content_type__model'])
        if kind:
            kinds[kind] = {'count': int(row['n'] or 0), 'amount': num(row['amount'])}

    return {
        'sales_ex_tax': num(revenue['collection_before_tax']),
        'sales_incl_tax': num(revenue['including_tax']),
        'bills': bills.count(),
        'clients': known + walk_ins,
        'last_bill': last.strftime('%I:%M %p').lstrip('0') if last else None,
        'payments': {
            'cash': num(cash), 'card': num(card), 'other': num(other),
            'by_method': [{'key': k, 'label': _label(k), 'amount': num(pays[k]['amount']), 'count': pays[k]['count']}
                          for k in pm.REPORT_BUCKETS if D(pays[k]['amount']) != 0],
        },
        'breakdown': kinds,
        # Paid from cards, advances and gift cards: not new money, so shown apart.
        'redemptions': num(liability + D(pays['gift_card']['amount'])),
        'discounts': num(-D(revenue['discounts']['amount'])),
        'tips': num(revenue['tips']['amount']),
    }


def liabilities(cids):
    """What clients still hold at these salons: value cards, unused package services and advances."""
    today = datetime.date.today()
    held_by = Q(center_id__in=cids) | Q(center__isnull=True, client__center_id__in=cids)
    cards = (ClientValueCard.objects.filter(held_by, is_active=True, expiry_date__gte=today, balance__gt=0)
             .aggregate(total=Sum('balance')))['total']
    from salon_admin.dashboard_endpoints import _outstanding_package_liability
    packages = _outstanding_package_liability(
        ClientPackage.objects.filter(held_by, is_active=True, expiry_date__gte=today)
        .only('id', 'services_remaining', 'expiry_date'))['total']
    held = [D(str(r['bal'] or 0)) for r in
            AdvancePayment.objects.filter(held_by).values('client').annotate(bal=Sum('amount')) if (r['bal'] or 0) > 0]
    advances = sum(held, D('0'))
    return {'value_cards': num(cards), 'packages': num(packages), 'advances': num(advances),
            'total': num(D(str(cards or 0)) + D(str(packages or 0)) + advances)}


def staff_name(first, last):
    return f'{first or ""} {last or ""}'.strip()


def credited_by_staff(cids, start, end):
    """{staff id: figures} of sales credited to each team member between two days, by the POS's own rule
    (Finance > Staff Performance): a line shared by two stylists is split as the till saved it, and cards,
    memberships and packages are "other". Bills that were cancelled or are drafts are not counted.

    A line with one named person goes whole to them, summed in the database; only lines shared between several
    people are worked out one by one (that is what keeps a month of a big business quick)."""
    from finance.views import credited_shares
    base = InvoiceItem.objects.filter(invoice__in=invoices_for(cids, start, end), is_redemption=False)
    rows = {}

    def row_for(member_id, first, last, salon):
        return rows.setdefault(member_id, {'id': member_id, 'name': staff_name(first, last) or 'Unknown',
                                           'salon': salon or '', 'services': D('0'), 'products': D('0'),
                                           'other': D('0'), 'items': 0})

    def kind_of(model):
        return 'services' if model == 'servicemaster' else 'products' if model == 'product' else 'other'

    solo = (base.filter(staff__isnull=False, staff_members__isnull=True)
            .values('staff_id', 'staff__first_name', 'staff__last_name', 'staff__center__display_name',
                    'staff__center__center_name', 'content_type__model')
            .annotate(amount=Sum('total_price'), qty=Sum('quantity')))
    for r in solo:
        row = row_for(r['staff_id'], r['staff__first_name'], r['staff__last_name'],
                      r['staff__center__display_name'] or r['staff__center__center_name'])
        row[kind_of(r['content_type__model'])] += D(str(r['amount'] or 0))
        row['items'] += int(r['qty'] or 0)

    shared = (base.filter(staff_members__isnull=False).distinct()
              .select_related('staff__center', 'invoice__staff', 'content_type')
              .prefetch_related('staff_members__center'))
    for item in shared:
        kind = kind_of(item.content_type.model if item.content_type_id else '')
        for member, revenue_share, _tax in credited_shares(item):
            row = row_for(member.pk, member.first_name, member.last_name,
                          (member.center.display_name or member.center.center_name) if member.center_id else '')
            row[kind] += D(revenue_share)
            row['items'] += int(D(item.quantity))
    return rows


def top_staff(cids, start, end, limit=5):
    people = [{'id': r['id'], 'name': r['name'], 'revenue': num(r['services'] + r['products'] + r['other']),
               'items': r['items']} for r in credited_by_staff(cids, start, end).values()]
    return {'by_revenue': sorted(people, key=lambda p: -p['revenue'])[:limit],
            'by_items': sorted(people, key=lambda p: -p['items'])[:limit]}


def today_view(user, cids, center_id, day):
    block = sales_block(user, cids, center_id, day, day)
    from salon_admin.models import Center
    target = month_target(cids, day.year, day.month)
    days = calendar.monthrange(day.year, day.month)[1]
    daily = target / days if target else D('0')
    done = D(str(block['sales_ex_tax']))
    best = top_staff(cids, day, day, 1)
    return {
        'date': day.isoformat(), 'is_today': day == datetime.date.today(), **block,
        'latest_day': latest_bill_day(cids, day) if not block['bills'] else day.isoformat(),
        'daily_target': num(daily), 'target_pct': round(float(done / daily * 100)) if daily else None,
        'clients_all_time': Client.objects.filter(center_id__in=cids).count(),
        'liabilities': remember('liabilities', cids, {}, 300, lambda: liabilities(cids)),
        'top': {'revenue': (best['by_revenue'] or [None])[0], 'items': (best['by_items'] or [None])[0]},
        'salon_count': Center.objects.filter(id__in=cids).count(),
    }


def _visit_months(cids, first_month, last_month):
    """{(year, month): set of client ids who had a settled bill then}, for the months from first to last."""
    start, _ = month_bounds(*first_month)
    _, end = month_bounds(*last_month)
    rows = (invoices_for(cids, start, end).exclude(client__isnull=True)
            .annotate(y=ExtractYear('created_at'), mo=ExtractMonth('created_at'))
            .values_list('client_id', 'y', 'mo').distinct())
    visits = {}
    for client, y, mo in rows:
        visits.setdefault((y, mo), set()).add(client)
    return visits


def month_view(user, cids, center_id, year, month):
    start, last_day = month_bounds(year, month)
    today = datetime.date.today()
    current = (year, month) == (today.year, today.month)
    end = min(today, last_day) if current else last_day
    block = sales_block(user, cids, center_id, start, end)

    target = month_target(cids, year, month)
    done = D(str(block['sales_ex_tax']))
    elapsed = (end - start).days + 1
    projection = None
    if current and done > 0:
        projection = num(done / elapsed * last_day.day, 0)

    bills = invoices_for(cids, start, end)
    # "New" = the client's first settled bill at these salons falls in the month (walk-ins count as new visitors).
    new = (Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, center_id__in=cids, client__isnull=False)
           .values('client').annotate(first=Min('created_at'))
           .filter(first__date__gte=start, first__date__lte=end).count())
    walk_ins = bills.filter(client__isnull=True).count()
    new += walk_ins
    repeat = max(block['clients'] - new, 0)

    per_day = {r['d']: r for r in bills.annotate(d=TruncDate('created_at')).values('d')
               .annotate(sub=Sum('subtotal'), disc=Sum('discount'), n=Count('id'))}
    daily = []
    for n in range(1, last_day.day + 1):
        day = datetime.date(year, month, n)
        row = per_day.get(day)
        daily.append({'day': n, 'sales': num((row['sub'] or 0) - (row['disc'] or 0)) if row else (0.0 if day <= end else None),
                      'bills': row['n'] if row else 0})

    weekday = [0] * 7
    for r in bills.annotate(dow=ExtractWeekDay('created_at')).values('dow').annotate(n=Count('id')):
        if r['dow']:
            weekday[r['dow'] - 1] = r['n']

    window = [month_back(year, month, k) for k in (4, 3, 2, 1, 0)]
    visits = _visit_months(cids, window[0], window[-1])
    retention = []
    for i in range(1, 5):
        before, now = visits.get(window[i - 1], set()), visits.get(window[i], set())
        retention.append({'month': '%04d-%02d' % window[i],
                          'pct': round(len(before & now) / len(before) * 100) if before else None})

    gender_rows = (invoices_for(cids, month_bounds(*window[1])[0], last_day)
                   .annotate(y=ExtractYear('created_at'), mo=ExtractMonth('created_at'))
                   .values('client__gender', 'y', 'mo').annotate(n=Count('client', distinct=True)))
    gender = {'%04d-%02d' % w: {'female': 0, 'male': 0, 'other': 0} for w in window[1:]}
    for r in gender_rows:
        bucket = gender.get('%04d-%02d' % (r['y'], r['mo']))
        if bucket is not None:
            kind = (r['client__gender'] or '').lower()
            bucket[kind if kind in ('female', 'male') else 'other'] += r['n']

    return {
        'month': '%04d-%02d' % (year, month), 'current': current, 'through': end.isoformat(), **block,
        'target': num(target), 'target_pct': round(float(done / target * 100)) if target else None,
        'projection': projection,
        'new_clients': new, 'repeat_clients': repeat,
        'daily': daily, 'weekday': [{'day': WEEKDAYS[i], 'bills': weekday[i]} for i in range(7)],
        'retention': retention, 'gender': [{'month': k, **v} for k, v in gender.items()],
    }


def invoice_rows(user, cids, day, limit=500):
    rows = (invoices_for(cids, day, day).select_related('client', 'center').order_by('-created_at')[:limit])
    return [{'id': i.id, 'time': i.created_at.strftime('%I:%M %p').lstrip('0'), 'number': i.display_number,
             'client': (i.client.full_name if i.client_id else 'Walk-in'), 'amount': num(i.total_amount),
             'status': i.status, 'salon': i.center.display_name or i.center.center_name if i.center_id else ''}
            for i in rows]


def invoice_detail(invoice):
    from billing.models import Payment
    return {
        'id': invoice.id, 'number': invoice.display_number, 'status': invoice.status,
        'date': invoice.created_at.strftime('%d %b %Y, %I:%M %p'),
        'salon': (invoice.center.display_name or invoice.center.center_name) if invoice.center_id else '',
        'client': invoice.client.full_name if invoice.client_id else 'Walk-in',
        'items': [{'name': it.description, 'qty': it.quantity, 'amount': num(it.total_price),
                   'tax': num(it.tax_amount), 'redeemed': it.is_redemption}
                  for it in invoice.items.all().order_by('id')],
        'subtotal': num(invoice.subtotal), 'discount': num(invoice.discount),
        'tax': num(sum((D(str(i.tax_amount or 0)) for i in invoice.items.all()), D('0'))),
        'rounding': num(invoice.rounding), 'total': num(invoice.total_amount), 'paid': num(invoice.paid_amount),
        'payments': [{'method': p.payment_method, 'amount': num(p.amount), 'tip': p.is_tip}
                     for p in Payment.objects.filter(invoice=invoice).order_by('id')],
    }


PERIODS = ('today', 'yesterday', 'this_month', 'last_month')


def period_range(period):
    today = datetime.date.today()
    if period == 'yesterday':
        day = today - datetime.timedelta(days=1)
        return day, day
    if period == 'last_month':
        y, m = month_back(today.year, today.month, 1)
        return month_bounds(y, m)
    if period == 'this_month':
        return today.replace(day=1), today
    return today, today


def staff_view(cids, period):
    from appointments.models import AppointmentService
    start, end = period_range(period)
    revenues = []
    for r in credited_by_staff(cids, start, end).values():
        services, products, other = num(r['services']), num(r['products']), num(r['other'])
        revenues.append({'id': r['id'], 'name': r['name'], 'salon': r['salon'], 'services': services,
                         'products': products, 'other': other, 'total': round(services + products + other, 2)})
    usage = (AppointmentService.objects
             .filter(appointment__center_id__in=cids, appointment__date__gte=start, appointment__date__lte=end,
                     staff__isnull=False)
             .exclude(appointment__status__in=('Cancelled', 'No-show'))
             .values('staff_id', 'staff__first_name', 'staff__last_name', 'staff__center__display_name')
             .annotate(minutes=Sum('duration'), appts=Count('appointment', distinct=True)))
    hours = [{'id': r['staff_id'], 'name': staff_name(r['staff__first_name'], r['staff__last_name']),
              'salon': r['staff__center__display_name'] or '', 'hours': round((r['minutes'] or 0) / 60, 1),
              'appts': r['appts']} for r in usage]
    return {'period': period, 'from': start.isoformat(), 'to': end.isoformat(),
            'revenues': sorted(revenues, key=lambda r: -r['total']), 'usage': sorted(hours, key=lambda r: -r['hours'])}
