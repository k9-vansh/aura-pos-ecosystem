"""Analytics tabs of the dashboard: Revenues, Clients, Services, Products,
Memberships and Value Cards.

Read-only. Every figure is worked out from the same rows Finance and the
Overview use, with the same rules:

  * a bill counts when it is Paid or Partial (Invoice.REVENUE_STATUSES);
  * "sales value" is what was sold before tax, after discounts
    (subtotal - discount), card-covered lines included;
  * money paid out of a value card, advance or cashback wallet is not new
    money (it was counted when the card or advance was sold), so it is taken
    off to get "collection".  Sales value - redeemed = collection excl. tax,
    plus GST and round-off = total collection - the Overview's headline figure.

Each endpoint takes start_date, end_date and center_id/centre selection like
the other dashboard endpoints.
"""
import calendar
from collections import defaultdict
from datetime import date as date_type, datetime, timedelta
from decimal import Decimal

from django.db.models import Count, DateField, F, Max, Min, Q, Sum
from django.db.models.functions import Cast, ExtractHour, ExtractMonth, ExtractYear, Coalesce
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from billing import payment_methods as pm
from billing.models import AdvancePayment, Invoice, InvoiceItem, Payment
from clients.models import Client, ClientMembership, ClientValueCard
from inventory.models import Product, StockTransaction
from pos_backend.permissions import IsOwner, module_permission
from pos_backend.scoping import coerce_center_id, safe_date_param, selected_center_ids
from salon_admin.dashboard_endpoints import (
    dashboard_tab, _apply_security, _payment_buckets, _redeemed_by, parse_report_date)
from services.models import CenterService, ServiceMaster

ZERO = Decimal('0')
SALES_VALUE = Sum(F('subtotal') - F('discount'))
LINE_VALUE = Sum(F('total_price') - F('bill_discount_share'))

TYPE_LABELS = {
    'servicemaster': 'Services', 'valuecard': 'Value cards', 'product': 'Products',
    'membership': 'Memberships', 'package': 'Packages',
}
TYPE_ORDER = ['Services', 'Value cards', 'Products', 'Memberships', 'Packages', 'Advances', 'Other']
WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']


# ─── small helpers ────────────────────────────────────────────────────────

def _f(x, places=2):
    return round(float(x or 0), places)


def _pct_change(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    if prev == 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def _share(part, whole):
    whole = float(whole or 0)
    return round(float(part or 0) / whole * 100, 1) if whole else 0.0


def _month_label(y, m):
    return f'{calendar.month_abbr[m]} \'{str(y)[-2:]}'


def _period(request):
    """(start, end, prev_start, prev_end) as dates. Defaults to this month so far."""
    today = date_type.today()
    start = parse_report_date(safe_date_param(request.GET.get('start_date'))) or today.replace(day=1)
    end = parse_report_date(safe_date_param(request.GET.get('end_date'))) or today
    if end < start:
        start, end = end, start
    days = (end - start).days + 1
    if start.day == 1 and (end.year, end.month) == (start.year, start.month):
        # A month to date: the same days of the previous month (1-20 Oct against 1-20 Sep).
        prev_start = (start - timedelta(days=1)).replace(day=1)
        last_day = calendar.monthrange(prev_start.year, prev_start.month)[1]
        prev_end = prev_start.replace(day=min(end.day, last_day))
    else:
        # Any other range: the equal-length window immediately before it.
        prev_end = start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=days - 1)
    return start, end, prev_start, prev_end


def _bills(request, start, end):
    """Settled bills in the signed-in user's centres between two dates."""
    qs = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES)
    qs = _apply_security(request, qs)
    return qs.filter(created_at__gte=start, created_at__lt=end + timedelta(days=1))


def _centres(request):
    """Centre ids the request is limited to, or None for every centre."""
    from salon_admin.models import Center
    qs = Center.objects.all()
    user = request.user
    perms = getattr(user.role, 'permissions', {}) or {}
    limited = False
    if not IsOwner.check_is_owner(user) and not perms.get('all_centers', False):
        limited = True
        if user.centers.exists():
            qs = qs.filter(id__in=user.centers.values_list('id', flat=True))
        elif getattr(user, 'center', None):
            qs = qs.filter(id=user.center.id)
        else:
            qs = qs.none()
    cid = coerce_center_id(request.GET.get('center_id'))
    if cid is not None:
        qs = qs.filter(id=cid)
        limited = True
    elif selected_center_ids():
        qs = qs.filter(id__in=list(selected_center_ids()))
        limited = True
    return qs, (list(qs.values_list('id', flat=True)) if limited else None)


def _by_selling_centre(request, qs, via='client__center'):
    """Rows that carry their own selling `center` (memberships, cards): keep the
    ones sold in scope, falling back to the client's centre when none was kept."""
    _, ids = _centres(request)
    if ids is None:
        return qs
    return qs.filter(Q(center__in=ids) | Q(center__isnull=True, **{f'{via}__in': ids}))


def _item_scope(request, start, end):
    """Q for InvoiceItem rows on settled bills in scope between two dates, written as
    direct conditions so the database can start from the (type, object) index and only
    visit the rows it needs, instead of first listing every bill in the window."""
    _, ids = _centres(request)
    q = Q(invoice__status__in=list(Invoice.REVENUE_STATUSES), invoice__created_at__gte=start,
          invoice__created_at__lt=end + timedelta(days=1))
    if ids is not None:
        q &= Q(invoice__center_id__in=ids)
    return q


_LIABILITY_NAMES = sorted({Payment.ADVANCE, Payment.VALUE_CARD, Payment.CASHBACK_WALLET} |
                          {k for k, v in pm.ALIASES.items() if v in Payment.LIABILITY_METHODS})


def _redeemed_months(request, start, end, buckets=('value_card', 'advance', 'cashback_wallet')):
    """{(year, month): money paid out of value cards, advances and cashback} - only those
    payments are read, which are a small share of all payments."""
    _, ids = _centres(request)
    q = Payment.objects.filter(is_tip=False, invoice__status__in=list(Invoice.REVENUE_STATUSES),
                               invoice__created_at__gte=start, invoice__created_at__lt=end + timedelta(days=1))
    if ids is not None:
        q = q.filter(invoice__center_id__in=ids)
    q = q.filter(Q(payment_method__in=_LIABILITY_NAMES) | Q(payment_method__istartswith='Value Card'))
    out = defaultdict(Decimal)
    for r in q.annotate(y=ExtractYear('invoice__created_at'), m=ExtractMonth('invoice__created_at')).values(
            'payment_method', 'y', 'm').annotate(t=Sum('amount')):
        if pm.report_bucket(r['payment_method']) in buckets:
            out[(r['y'], r['m'])] += Decimal(str(r['t'] or 0))
    return out


def _mask(phone):
    phone = str(phone or '')
    return ('X' * max(len(phone) - 4, 0) + phone[-4:]) if phone else ''


def _name(first, last):
    return f'{first or ""} {last or ""}'.strip()


def _client_names(ids):
    return {c['id']: c for c in Client.objects.filter(id__in=list(ids)).values('id', 'first_name', 'last_name', 'phone')}


def _last_visits(request, ids):
    qs = _apply_security(request, Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, client_id__in=list(ids)))
    return {r['client']: r['last'] for r in qs.values('client').annotate(last=Max('created_at'))}


def _spend_12m(request, ids, today):
    qs = _apply_security(request, Invoice.objects.filter(
        status__in=Invoice.REVENUE_STATUSES, client_id__in=list(ids),
        created_at__gte=today - timedelta(days=365)))
    return {r['client']: r['v'] for r in qs.values('client').annotate(v=SALES_VALUE)}


def _ct_id(model):
    """Content-type id of a sellable item type; -1 (matches nothing) if it does not exist.
    Filtering on the id lets the database use the (type, object) index directly."""
    from django.contrib.contenttypes.models import ContentType
    return ContentType.objects.filter(model=model).values_list('id', flat=True).first() or -1


def _type_of(model):
    return TYPE_LABELS.get(model, 'Other')


def _months_back(end, n):
    """First day of the month n-1 months before `end`'s month."""
    y, m = end.year, end.month - (n - 1)
    while m < 1:
        m += 12
        y -= 1
    return date_type(y, m, 1)


def _month_keys(first, end):
    keys, y, m = [], first.year, first.month
    while (y, m) <= (end.year, end.month):
        keys.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return keys


def _money_block(invoices):
    """Sales value, redemptions, GST and the collection figures for some bills."""
    agg = invoices.aggregate(sales=SALES_VALUE, billed=Sum('total_amount'), cgst=Sum('cgst'),
                             sgst=Sum('sgst'), igst=Sum('igst'), rounding=Sum('rounding'),
                             collected=Sum('paid_amount'), n=Count('id'))
    buckets = _payment_buckets(invoices)
    amt = lambda k: buckets.get(k, {}).get('amount', ZERO)
    redeemed = {'value_card': amt('value_card'), 'advance': amt('advance'), 'cashback': amt('cashback_wallet')}
    redeemed['total'] = sum(redeemed.values(), ZERO)
    sales = Decimal(str(agg['sales'] or 0))
    billed = Decimal(str(agg['billed'] or 0))
    gst = sum((Decimal(str(agg[k] or 0)) for k in ('cgst', 'sgst', 'igst')), ZERO)
    rounding = Decimal(str(agg['rounding'] or 0))
    total_collection = billed - redeemed['total']
    real = sum((v['amount'] for k, v in buckets.items() if k not in pm.LIABILITY_BUCKETS), ZERO)
    return {
        'bills': agg['n'] or 0, 'sales': sales, 'billed': billed, 'gst': gst, 'rounding': rounding,
        'redeemed': redeemed, 'collection_excl_tax': total_collection - gst - rounding,
        'total_collection': total_collection, 'real_money': real, 'buckets': buckets,
        'to_collect': max(billed - Decimal(str(agg['collected'] or 0)), ZERO),
    }


def _target(request, start, end):
    """The monthly target for the months the period covers, worked out the way the
    Finance register summary does: each month's entry in a centre's target history,
    and the centre's default target only when the range reaches the current or a
    future month."""
    qs, _ = _centres(request)
    today = date_type.today()
    months = _month_keys(start.replace(day=1), end)
    reaches_now = any((y, m) >= (today.year, today.month) for y, m in months)
    total = ZERO
    for c in qs:
        hist = c.monthly_targets_history or {}
        val = ZERO
        for y, m in months:
            try:
                val += Decimal(str(hist.get(f'{calendar.month_abbr[m]}-{y}', 0) or 0))
            except Exception:
                pass
        if val == 0 and reaches_now:
            val = Decimal(str(c.monthly_target or 0))
        total += val
    return total


def _advance_ledger(request, start, end):
    """Advances deposited and settled in the dates, from the advance ledger as Finance reads it.

    A deposit is counted at its PRE-TAX value. When the 'charge GST on advances' setting is on, the
    ledger credits the client what they paid, GST included (Rs 21,000 for Rs 20,000 + Rs 1,000 GST),
    while the bill's tax total already holds that Rs 1,000. Taking the ledger figure whole counted the
    GST twice in total collection, so the GST on a GST-inclusive credit is taken out here and enters
    once, through the tax.  Returns (deposited pre-tax, deposits count, settled).
    """
    from salon_admin.dashboard_endpoints import _scope_advances
    qs = _scope_advances(request, AdvancePayment.objects.all()).filter(
        created_at__gte=start, created_at__lt=end + timedelta(days=1))
    a = qs.aggregate(pos=Sum('amount', filter=Q(amount__gt=0)),
                     gst=Sum('gst_amount', filter=Q(amount__gt=0, gst_inclusive=True)),
                     neg=Sum('amount', filter=Q(amount__lt=0)), n=Count('id', filter=Q(amount__gt=0)))
    pos, gst, neg = (Decimal(str(a[k] or 0)) for k in ('pos', 'gst', 'neg'))
    return pos - gst, a['n'] or 0, abs(neg)


def _advance_months(request, start, end):
    """({(y, m): advances received}, {(y, m): advances spent}) from the advance ledger,
    by the date each row was written - as the Finance register summary counts them."""
    from salon_admin.dashboard_endpoints import _scope_advances
    qs = _scope_advances(request, AdvancePayment.objects.all()).filter(
        created_at__gte=start, created_at__lt=end + timedelta(days=1))
    got, used = defaultdict(Decimal), defaultdict(Decimal)
    for r in qs.annotate(y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values('y', 'm').annotate(
            pos=Sum('amount', filter=Q(amount__gt=0)), neg=Sum('amount', filter=Q(amount__lt=0)),
            gst=Sum('gst_amount', filter=Q(amount__gt=0, gst_inclusive=True))):
        got[(r['y'], r['m'])] += Decimal(str(r['pos'] or 0)) - Decimal(str(r['gst'] or 0))   # pre-tax, as above
        used[(r['y'], r['m'])] += abs(Decimal(str(r['neg'] or 0)))
    return got, used


def _register(request, start, end):
    """The Finance register summary for the dates, flattened to the figures used here.
    It is Finance's own function, so these equal what Finance shows."""
    from django.core.cache import cache
    from finance.views import compute_register_summary
    from pos_backend.scoping import selection_key
    cid = coerce_center_id(request.GET.get('center_id'))
    # A period that has finished does not change (bar a back-dated edit), so it is kept for a
    # few minutes: the "versus previous period" figures need not be rebuilt on every refresh.
    key = f'an_reg_{request.user.id}_{cid}_{selection_key()}_{start}_{end}' if end < date_type.today() else None
    s = cache.get(key) if key else None
    if s is None:
        s = compute_register_summary(request.user, cid, start.isoformat(), end.isoformat())
        if key:
            cache.set(key, s, 300)
    D_ = lambda v: Decimal(str(v or 0))
    rev, red = s['revenues'], s['payment_redemptions']
    lines = D_(rev['taxable_value'])                  # the bills' lines, less bill-level discounts
    adv_in, adv_count, adv_used = _advance_ledger(request, start, end)       # deposits pre-tax; see there
    vc, cb = D_(red['value_card']), D_(red['cashback'])
    gst, rounding = D_(rev['total_tax']), D_(rev['rounding']['amount'])
    # The definitions the business uses (the old analytics page's "How these numbers are counted"):
    #   Sales value        everything sold before tax, advances deposited included, at full value
    #   Collection         sales value less what value cards / cashback paid. Advances settled stay IN,
    #                      because an advance is money. The monthly target is measured on this.
    #   Total collection   collection plus GST (and the paise the bills were rounded by)
    #   Received at desk   total collection less the part settled from earlier advances
    # Finance's register summary subtracts advances settled one step earlier, so its "collection before
    # tax" is this figure less advances settled; its "Including Tax" is this page's "received at the desk".
    sales = lines + adv_in
    collection = sales - vc - cb
    total = collection + gst + rounding
    return {
        'lines': lines, 'sales': sales,
        'value_card': vc, 'cashback': cb, 'advance_used': adv_used, 'advance_in': adv_in, 'advance_count': adv_count,
        'collection_excl_tax': collection, 'gst': gst, 'rounding': rounding,
        'total_collection': total, 'desk': total - adv_used,
        'refunds': D_(s['refunds']['refunds_issued']),
        'to_collect': D_(s['reconciliation']['outstanding_receivable']),
    }


# ─── Revenues ─────────────────────────────────────────────────────────────

@module_permission(**dashboard_tab('revenues'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_revenues(request):
    start, end, p_start, p_end = _period(request)
    days = (end - start).days + 1
    inv = _bills(request, start, end)
    cur = _register(request, start, end)
    prev = _register(request, p_start, p_end)
    buckets = _payment_buckets(inv)
    bills, p_bills = inv.count(), _bills(request, p_start, p_end).count()
    redeemed = cur['value_card'] + cur['cashback']
    line_disc = InvoiceItem.objects.filter(invoice__in=inv, is_redemption=False).aggregate(
        d=Sum('discount'), b=Sum('card_benefit'))
    discounts = (Decimal(str(inv.aggregate(d=Sum('discount'))['d'] or 0)) +
                 Decimal(str(line_disc['d'] or 0)) - Decimal(str(line_disc['b'] or 0)))

    # Daily sales value, from the same source Finance uses: the bill's lines, less the bill-level
    # discount. (Invoice.subtotal is meant to equal the lines; if a bill was imported or edited so it
    # does not, the chart must still agree with the reconciliation beside it.)
    from salon_admin.dashboard_endpoints import _scope_advances
    line_items = InvoiceItem.objects.filter(invoice__in=inv, is_redemption=False)
    # Advances are counted from the advance ledger (below), so the bill's own advance line is left out here.
    line_by_day = {r['d']: Decimal(str(r['v'] or 0)) for r in line_items.exclude(content_type__model='advancepayment')
                   .annotate(d=Cast('invoice__created_at', DateField())).values('d').annotate(v=Sum('total_price'))}
    disc_by_day = {r['d']: Decimal(str(r['v'] or 0)) for r in inv.annotate(d=Cast('created_at', DateField()))
                   .values('d').annotate(v=Sum('discount'))}
    adv_by_day = {r['d']: Decimal(str(r['v'] or 0)) - Decimal(str(r['g'] or 0)) for r in _scope_advances(
        request, AdvancePayment.objects.filter(amount__gt=0)).filter(
        created_at__gte=start, created_at__lt=end + timedelta(days=1)).annotate(
        d=Cast('created_at', DateField())).values('d').annotate(
        v=Sum('amount'), g=Sum('gst_amount', filter=Q(gst_inclusive=True)))}
    daily_rows = {day: line_by_day.get(day, ZERO) - disc_by_day.get(day, ZERO) + adv_by_day.get(day, ZERO)
                  for day in set(line_by_day) | set(disc_by_day) | set(adv_by_day)}
    # Do the bills' own totals agree with their lines (advance lines included)? Say so if not.
    invoice_totals = Decimal(str(inv.aggregate(s=Sum('subtotal'))['s'] or 0))
    line_totals = Decimal(str(line_items.aggregate(s=Sum('total_price'))['s'] or 0))
    mismatch = abs(invoice_totals - line_totals) > Decimal('0.5') * max(bills, 1)
    daily, dow_sum = [], defaultdict(Decimal)
    dow_days = defaultdict(int)
    d = start
    while d <= end:
        v = Decimal(str(daily_rows.get(d, 0) or 0))
        daily.append({'day': d.isoformat(), 'label': d.strftime('%d-%b'), 'weekday': WEEKDAYS[d.weekday()],
                      'value': _f(v), 'weekend': d.weekday() >= 5})
        dow_sum[d.weekday()] += v
        dow_days[d.weekday()] += 1
        d += timedelta(days=1)
    dow = [{'day': WEEKDAYS[i], 'value': _f(dow_sum[i] / dow_days[i]) if dow_days[i] else 0.0,
            'weekend': i >= 5} for i in range(7)]
    best = max(daily, key=lambda r: r['value'], default=None)
    open_days = [r for r in daily if r['value'] > 0]
    quiet = min(open_days, key=lambda r: r['value'], default=None)

    # Busiest hours (invoices closed by hour)
    hours = {r['h']: r['n'] for r in inv.annotate(h=ExtractHour('created_at')).values('h').annotate(n=Count('id'))}
    hour_rows = [{'hour': h, 'label': (f'{h % 12 or 12}{"am" if h < 12 else "pm"}'), 'invoices': hours.get(h, 0)}
                 for h in range(min(hours, default=10), max(hours, default=21) + 1)]
    top3 = sum(sorted(hours.values(), reverse=True)[:3])
    total_hours = sum(hours.values())

    # 12-month trend of collection excl. tax, built the way Finance builds it
    first = _months_back(end, 12)
    trend_inv = _bills(request, first, end)
    trend = {(r['y'], r['m']): Decimal(str(r['v'] or 0)) for r in trend_inv.annotate(
        y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values('y', 'm').annotate(v=SALES_VALUE)}
    got, _used = _advance_months(request, first, end)       # advances settled stay in collection
    for key in _month_keys(first, end):
        trend[key] = trend.get(key, ZERO) + got.get(key, ZERO)
    for key, amt in _redeemed_months(request, first, end, buckets=('value_card', 'cashback_wallet')).items():
        trend[key] = trend.get(key, ZERO) - amt
    trend_rows = [{'month': _month_label(y, m), 'value': _f(trend.get((y, m), 0)),
                   'partial': (y, m) == (end.year, end.month)} for y, m in _month_keys(first, end)]

    # Sales by type (every type is always listed)
    types = defaultdict(lambda: {'lines': 0, 'value': ZERO})
    for r in InvoiceItem.objects.filter(invoice__in=inv).exclude(content_type__model='advancepayment').values(
            'content_type__model').annotate(n=Count('id'), v=LINE_VALUE):
        t = types[_type_of(r['content_type__model'])]
        t['lines'] += r['n']
        t['value'] += Decimal(str(r['v'] or 0))
    # Advances are counted from the advance ledger (as Finance does), not from the bill's advance line.
    types['Advances']['lines'] = cur['advance_count']
    types['Advances']['value'] = cur['advance_in']
    type_total = sum((t['value'] for t in types.values()), ZERO)
    sales_by_type = [{'type': k, 'lines': types[k]['lines'] if k in types else 0,
                      'value': _f(types[k]['value']) if k in types else 0.0,
                      'share': _share(types[k]['value'], type_total) if k in types else 0.0}
                     for k in TYPE_ORDER if k in types or k != 'Other']

    # How the money came in
    real = {k: v for k, v in buckets.items() if k not in pm.LIABILITY_BUCKETS and v['amount']}
    real_total = sum((v['amount'] for v in real.values()), ZERO)
    modes = [{'mode': k, 'amount': _f(v['amount']), 'count': v['count'], 'share': _share(v['amount'], real_total)}
             for k, v in sorted(real.items(), key=lambda kv: -kv[1]['amount'])]
    # Only advances settled are listed beside the money modes (value cards and cashback are not money in).
    from_balances = [{'mode': k, 'amount': _f(v['amount']), 'count': v['count']}
                     for k, v in buckets.items() if k == 'advance' and v['amount']]

    # Monthly target and pace
    target = _target(request, start, end)
    today = date_type.today()
    cov_end = date_type(end.year, end.month, calendar.monthrange(end.year, end.month)[1])
    cov_days = (cov_end - start).days + 1
    gone_days = min(max((min(end, today) - start).days + 1, 0), cov_days)
    month_gone = gone_days / cov_days if cov_days else 0
    coll = cur['collection_excl_tax']
    pace = (coll / Decimal(str(month_gone))) if month_gone else coll

    def pack(m, n):
        return {'collection_excl_tax': _f(m['collection_excl_tax']), 'total_collection': _f(m['total_collection']),
                'sales_value': _f(m['sales']), 'invoices': n,
                'avg_bill': _f(m['total_collection'] / n) if n else 0.0}
    c_pack, p_pack = pack(cur, bills), pack(prev, p_bills)

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
                   'prev_start': p_start.isoformat(), 'prev_end': p_end.isoformat()},
        'kpis': {
            'target': _f(target), 'target_pct': round(float(coll / target * 100)) if target else 0,
            'month_gone_pct': round(month_gone * 100), 'pace': _f(pace),
            'target_gap': _f(target - coll) if target else 0.0,            # positive: short of the target
            'month_done': month_gone >= 1,
            'pace_pct': round(float(pace / target * 100)) if target else 0,
            'collection_excl_tax': c_pack['collection_excl_tax'], 'discounts': _f(discounts),
            'gst': _f(cur['gst']), 'gst_rate': _share(cur['gst'], coll),
            'total_collection': c_pack['total_collection'], 'desk': _f(cur['desk']),
            'advance_in': _f(cur['advance_in']), 'advance_used': _f(cur['advance_used']),
            'redeemed': _f(redeemed), 'redeemed_share': _share(redeemed, cur['sales']),
            'invoices': bills, 'per_day': round(bills / days) if days else 0,
            'avg_bill': c_pack['avg_bill'],
        },
        'reconciliation': {
            'sales_value': _f(cur['sales']), 'sales_lines': _f(cur['lines']), 'redeemed_value_cards': _f(cur['value_card']),
            'redeemed_cashback': _f(cur['cashback']), 'advance_in': _f(cur['advance_in']),
            'advance_used': _f(cur['advance_used']), 'refunds': _f(cur['refunds']),
            'collection_excl_tax': c_pack['collection_excl_tax'], 'gst': _f(cur['gst']),
            'round_off': _f(cur['rounding']), 'total_collection': c_pack['total_collection'],
            'desk': _f(cur['desk']), 'to_collect': _f(cur['to_collect']),
        },
        'daily': daily, 'dow': dow, 'best_day': best, 'quietest_day': quiet,
        'versus_previous': [
            {'label': 'Collection excl. tax', 'cur': c_pack['collection_excl_tax'], 'prev': p_pack['collection_excl_tax'],
             'change': _pct_change(c_pack['collection_excl_tax'], p_pack['collection_excl_tax']), 'money': True},
            {'label': 'Total collection', 'cur': c_pack['total_collection'], 'prev': p_pack['total_collection'],
             'change': _pct_change(c_pack['total_collection'], p_pack['total_collection']), 'money': True},
            {'label': 'Sales value', 'cur': c_pack['sales_value'], 'prev': p_pack['sales_value'],
             'change': _pct_change(c_pack['sales_value'], p_pack['sales_value']), 'money': True},
            {'label': 'Invoices', 'cur': c_pack['invoices'], 'prev': p_pack['invoices'],
             'change': _pct_change(c_pack['invoices'], p_pack['invoices']), 'money': False},
            {'label': 'Avg bill', 'cur': c_pack['avg_bill'], 'prev': p_pack['avg_bill'],
             'change': _pct_change(c_pack['avg_bill'], p_pack['avg_bill']), 'money': True},
        ],
        'trend': trend_rows, 'hours': hour_rows, 'top3_hours_share': _share(top3, total_hours),
        'sales_by_type': sales_by_type, 'modes': modes, 'from_balances': from_balances,
        'data_warning': ({'bills': bills, 'invoice_totals': _f(invoice_totals), 'line_totals': _f(line_totals)}
                         if mismatch else None),
    })


# ─── Clients ──────────────────────────────────────────────────────────────

@module_permission(**dashboard_tab('clients'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_clients(request):
    start, end, _ps, _pe = _period(request)
    inv = _bills(request, start, end)
    footfall = inv.count()
    known = inv.exclude(client__isnull=True)

    per_client = {r['client']: r for r in known.values('client', 'client__gender').annotate(
        visits=Count('id'), sales=SALES_VALUE, first=Min('created_at'), last=Max('created_at'))}
    unique = len(per_client)

    # Previous visit before the period, for new / repeat / win-back
    scoped_all = _apply_security(request, Invoice.objects.filter(
        status__in=Invoice.REVENUE_STATUSES, created_at__lt=start,
        client_id__in=known.values('client')))
    prev_visit = {r['client']: r['last'] for r in scoped_all.values('client').annotate(last=Max('created_at'))}

    buckets = {'first': 0, 'd30': 0, 'd60': 0, 'd90': 0, 'over90': 0}
    new_ids = set()
    for cid, r in per_client.items():
        last_prev = prev_visit.get(cid)
        if last_prev is None:
            buckets['first'] += 1
            new_ids.add(cid)
            continue
        gap = (r['first'].date() - last_prev.date()).days
        buckets['d30' if gap <= 30 else 'd60' if gap <= 60 else 'd90' if gap <= 90 else 'over90'] += 1
    new_clients = len(new_ids)
    sales_known = sum((Decimal(str(r['sales'] or 0)) for r in per_client.values()), ZERO)

    # New vs repeat by week (weeks start Monday)
    weekly = defaultdict(lambda: {'unique': set(), 'new': 0})
    for r in known.annotate(d=Cast('created_at', DateField())).values('client', 'd').distinct():
        wk = r['d'] - timedelta(days=r['d'].weekday())
        weekly[wk]['unique'].add(r['client'])
    for cid in new_ids:
        wk = per_client[cid]['first'].date()
        wk = wk - timedelta(days=wk.weekday())
        weekly[wk]['new'] += 1
    weeks = [{'week': wk.isoformat(), 'label': f'w/c {wk.strftime("%d-%b")}', 'unique': len(w['unique']),
              'new': w['new'], 'repeat': len(w['unique']) - w['new']} for wk, w in sorted(weekly.items())]

    gender = defaultdict(lambda: {'clients': 0, 'sales': ZERO})
    for r in per_client.values():
        g = (r['client__gender'] or '').strip().lower()
        g = 'Female' if g == 'female' else 'Male' if g == 'male' else 'Not stated'
        gender[g]['clients'] += 1
        gender[g]['sales'] += Decimal(str(r['sales'] or 0))
    genders = [{'gender': g, 'clients': gender[g]['clients'], 'share': _share(gender[g]['clients'], unique),
                'sales': _f(gender[g]['sales'])} for g in ('Female', 'Male', 'Not stated') if g in gender]

    # Top 25 clients
    top = sorted(per_client.values(), key=lambda r: -(r['sales'] or 0))[:25]
    ids = [r['client'] for r in top]
    names = _client_names(ids)
    redeemed = defaultdict(Decimal)
    for r in Payment.objects.filter(invoice__in=known.filter(client_id__in=ids), is_tip=False).values(
            'invoice__client', 'payment_method').annotate(t=Sum('amount')):
        if pm.is_liability_drawdown(r['payment_method']):
            redeemed[r['invoice__client']] += Decimal(str(r['t'] or 0))
    top_clients = [{
        'rank': i + 1, 'client_id': r['client'],
        'name': _name(names.get(r['client'], {}).get('first_name'), names.get(r['client'], {}).get('last_name')) or 'Client',
        'visits': r['visits'], 'sales': _f(r['sales']), 'redeemed': _f(redeemed[r['client']]),
        'last_visit': r['last'].strftime('%d-%b-%Y')} for i, r in enumerate(top)]

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'kpis': {
            'footfall': footfall, 'unique': unique,
            'visits_per_client': round(sum(r['visits'] for r in per_client.values()) / unique, 1) if unique else 0,
            'new': new_clients, 'new_share': _share(new_clients, unique),
            'repeat': unique - new_clients, 'repeat_share': _share(unique - new_clients, unique),
            'spend_per_client': _f(sales_known / unique) if unique else 0.0,
            'winback': buckets['over90'], 'winback_share': _share(buckets['over90'], unique),
        },
        'weeks': weeks, 'genders': genders,
        'frequency': [
            {'label': 'First visit ever', 'clients': buckets['first'], 'share': _share(buckets['first'], unique)},
            {'label': 'Back within 30 days', 'clients': buckets['d30'], 'share': _share(buckets['d30'], unique)},
            {'label': '31–60 days', 'clients': buckets['d60'], 'share': _share(buckets['d60'], unique)},
            {'label': '61–90 days', 'clients': buckets['d90'], 'share': _share(buckets['d90'], unique)},
            {'label': 'Over 90 days · win-back', 'clients': buckets['over90'], 'share': _share(buckets['over90'], unique)},
        ],
        'top_clients': top_clients,
    })


# ─── Services ─────────────────────────────────────────────────────────────

def _service_rows(inv):
    return {r['object_id']: r for r in InvoiceItem.objects.filter(
        invoice__in=inv, content_type_id=_ct_id('servicemaster')).values('object_id').annotate(
        done=Sum('quantity'), value=LINE_VALUE, lines=Count('id'), name=Max('description'))}


@module_permission(**dashboard_tab('services_products'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_services(request):
    start, end, p_start, p_end = _period(request)
    days = (end - start).days + 1
    inv = _bills(request, start, end)
    cur = _service_rows(inv)
    prev = _service_rows(_bills(request, p_start, p_end))
    masters = {m['id']: m for m in ServiceMaster.objects.filter(id__in=set(cur) | set(prev)).values(
        'id', 'name', 'category', 'default_price')}

    svc_items = InvoiceItem.objects.filter(invoice__in=inv, content_type_id=_ct_id('servicemaster'))
    k = svc_items.aggregate(
        invoices=Count('invoice', distinct=True),
        discounted=Count('id', filter=Q(discount__gt=F('card_benefit')) | Q(bill_discount_share__gt=0)))
    total_value = sum((Decimal(str(r['value'] or 0)) for r in cur.values()), ZERO)
    total_done = sum(int(r['done'] or 0) for r in cur.values())
    total_lines = sum(r['lines'] for r in cur.values())

    def nm(sid, row):
        return (masters.get(sid, {}).get('name')) or row.get('name') or f'Service #{sid}'

    top = []
    for rank, (sid, r) in enumerate(sorted(cur.items(), key=lambda kv: -(kv[1]['value'] or 0))[:50], 1):
        p = prev.get(sid)
        if p is None:
            move = 'new'
        else:
            ch = _pct_change(r['value'], p['value'])
            move = 'level' if ch is None or abs(ch) < 0.5 else ch
        top.append({'rank': rank, 'name': nm(sid, r), 'category': masters.get(sid, {}).get('category') or '—',
                    'done': int(r['done'] or 0), 'value': _f(r['value']),
                    'share': _share(r['value'], total_value), 'move': move})

    cats = defaultdict(lambda: {'done': 0, 'value': ZERO})
    for sid, r in cur.items():
        c = masters.get(sid, {}).get('category') or 'Other'
        cats[c]['done'] += int(r['done'] or 0)
        cats[c]['value'] += Decimal(str(r['value'] or 0))
    categories = [{'category': c, 'done': v['done'], 'value': _f(v['value']), 'share': _share(v['value'], total_value)}
                  for c, v in sorted(cats.items(), key=lambda kv: -kv[1]['value'])]

    dropped = [{'name': nm(sid, r), 'category': masters.get(sid, {}).get('category') or '—',
                'done': int(r['done'] or 0), 'value': _f(r['value'])}
               for sid, r in sorted(prev.items(), key=lambda kv: -(kv[1]['value'] or 0)) if sid not in cur][:10]
    dropped_total = len([sid for sid in prev if sid not in cur])

    # The menu: services this salon offers and when each last sold
    _, ids = _centres(request)
    listed = ServiceMaster.objects.all()
    if ids is not None:
        listed = listed.filter(Q(level='Organisation') | Q(centers__in=ids)).distinct()
        if len(ids) == 1:
            off = set(CenterService.objects.filter(center_id=ids[0], is_active=False).values_list('service_id', flat=True))
            listed = listed.exclude(id__in=off)
    price_override = {}
    if ids is not None and len(ids) == 1:
        price_override = {r['service_id']: r['price'] for r in CenterService.objects.filter(
            center_id=ids[0], price__isnull=False).values('service_id', 'price')}
    cutoff_day = end - timedelta(days=90)
    cutoff = datetime.combine(cutoff_day, datetime.min.time())
    svc_ct = Q(content_type_id=_ct_id('servicemaster'))
    sold_90 = set(InvoiceItem.objects.filter(svc_ct, _item_scope(request, cutoff_day + timedelta(days=1), end))
                  .values_list('object_id', flat=True).distinct())
    listed_rows = list(listed.values('id', 'name', 'category', 'default_price'))
    idle_ids = [m['id'] for m in listed_rows if m['id'] not in sold_90]
    # When each idle service last sold: only their own rows are read (few, by definition).
    last_sold = {}
    if idle_ids:
        for r in InvoiceItem.objects.filter(svc_ct, object_id__in=idle_ids).filter(
                _item_scope(request, date_type(2000, 1, 1), end)).values('object_id').annotate(
                last=Max('invoice__created_at')):
            last_sold[r['object_id']] = r['last']
    idle = []
    for m in listed_rows:
        if m['id'] in sold_90:
            continue
        last = last_sold.get(m['id'])
        idle.append({'name': m['name'], 'category': m['category'],
                     'price': _f(price_override.get(m['id'], m['default_price'])),
                     'last_sold': last.strftime('%d-%b-%Y') if last else None, '_k': last or datetime.min})
    listed_total = len(listed_rows)
    idle.sort(key=lambda r: (r['_k'], -r['price']))
    never = sum(1 for r in idle if r['last_sold'] is None)
    for r in idle:
        r.pop('_k')

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat(), 'days': days,
                   'prev_start': p_start.isoformat(), 'prev_end': p_end.isoformat()},
        'kpis': {
            'sales': _f(total_value), 'done': total_done, 'lines': total_lines,
            'per_day': round(total_lines / days, 1) if days else 0,
            'per_invoice': round(total_done / k['invoices'], 1) if k['invoices'] else 0,
            'avg_price': _f(total_value / total_done) if total_done else 0.0,
            'discounted_share': _share(k['discounted'], total_lines),
            'discounted_lines': k['discounted'],
            'distinct': len(cur), 'idle_90': len(idle), 'listed': listed_total,
        },
        'top': top, 'categories': categories, 'dropped': dropped, 'dropped_total': dropped_total,
        'idle': idle[:10], 'idle_never': never,
    })


# ─── Products ─────────────────────────────────────────────────────────────

@module_permission(**dashboard_tab('services_products'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_products(request):
    start, end, _ps, _pe = _period(request)
    inv = _bills(request, start, end)
    bills = inv.count()
    rows = {r['object_id']: r for r in InvoiceItem.objects.filter(
        invoice__in=inv, content_type_id=_ct_id('product'), is_redemption=False).values('object_id').annotate(
        units=Sum('quantity'), value=LINE_VALUE, name=Max('description'))}
    prods = {p['id']: p for p in Product.objects.filter(id__in=list(rows)).values(
        'id', 'name', 'brand', 'category', 'current_stock', 'unit')}
    attach = InvoiceItem.objects.filter(invoice__in=inv, content_type_id=_ct_id('product'),
                                        is_redemption=False).values('invoice').distinct().count()
    total_value = sum((Decimal(str(r['value'] or 0)) for r in rows.values()), ZERO)
    total_units = sum(int(r['units'] or 0) for r in rows.values())

    def stock(pid):
        s = prods.get(pid, {}).get('current_stock')
        return None if s is None else float(s)

    top = []
    for rank, (pid, r) in enumerate(sorted(rows.items(), key=lambda kv: -(kv[1]['value'] or 0))[:50], 1):
        p = prods.get(pid, {})
        top.append({'rank': rank, 'name': p.get('name') or r['name'] or f'Product #{pid}',
                    'sub': p.get('category') or '', 'brand': p.get('brand') or '',
                    'units': int(r['units'] or 0), 'value': _f(r['value']),
                    'share': _share(r['value'], total_value), 'in_stock': stock(pid)})

    def grouped(field, blank):
        g = defaultdict(lambda: {'units': 0, 'value': ZERO})
        for pid, r in rows.items():
            key = (prods.get(pid, {}).get(field) or '').strip() or blank
            g[key]['units'] += int(r['units'] or 0)
            g[key]['value'] += Decimal(str(r['value'] or 0))
        return [{'name': k, 'units': v['units'], 'value': _f(v['value']), 'share': _share(v['value'], total_value)}
                for k, v in sorted(g.items(), key=lambda kv: -kv[1]['value'])]

    problems = sorted(
        ({'name': prods[pid]['name'], 'sold': int(r['units'] or 0), 'on_hand': float(prods[pid]['current_stock'] or 0)}
         for pid, r in rows.items() if pid in prods and (prods[pid]['current_stock'] or 0) <= 0),
        key=lambda r: (r['on_hand'], -r['sold']))
    negative = sum(1 for p in problems if p['on_hand'] < 0)

    # Backbar check-outs: products taken from stock for use on clients
    _, ids = _centres(request)
    co = StockTransaction.objects.filter(transaction_type='CHECKOUT', created_at__gte=start,
                                         created_at__lt=end + timedelta(days=1))
    if ids is not None:
        co = co.filter(center_id__in=ids)
    co_units, co_value, co_prods = ZERO, ZERO, set()
    for r in co.values('product', 'product__mrp').annotate(q=Sum('quantity_change')):
        q = abs(Decimal(str(r['q'] or 0)))
        co_units += q
        co_value += q * Decimal(str(r['product__mrp'] or 0))
        co_prods.add(r['product'])
    co_count = co.count()

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'kpis': {'sales': _f(total_value), 'units': total_units, 'attach_rate': _share(attach, bills),
                 'attach_invoices': attach, 'invoices': bills, 'distinct': len(rows),
                 'used_products': len(co_prods), 'used_checkouts': co_count, 'negative': len(problems),
                 'negative_below_zero': negative},
        'top': top, 'categories': grouped('category', 'No category'), 'brands': grouped('brand', 'No brand'),
        'problems': problems[:25], 'problems_total': len(problems),
        'used': {'units': _f(co_units, 3), 'products': len(co_prods), 'checkouts': co_count, 'value': _f(co_value)},
    })


# ─── Memberships ──────────────────────────────────────────────────────────

def _member_windows(request, client_subquery):
    """{client_id: [(start, expiry)]} of memberships a bill's client could have held."""
    qs = _by_selling_centre(request, ClientMembership.objects.filter(client_id__in=client_subquery))
    qs = qs.exclude(source_invoice__status__in=list(Invoice.VOID_STATUSES))
    out = defaultdict(list)
    for r in qs.values('client_id', 'start_date', 'expiry_date'):
        out[r['client_id']].append((r['start_date'], r['expiry_date']))
    return out


def _held(windows, client_id, day):
    return any(s <= day <= e for s, e in windows.get(client_id, ()))


@module_permission(**dashboard_tab('finance', also=('usage_report',)))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_memberships(request):
    start, end, _ps, _pe = _period(request)
    today = date_type.today()
    inv = _bills(request, start, end)
    bills = inv.count()
    base = _by_selling_centre(request, ClientMembership.objects.exclude(
        source_invoice__status__in=list(Invoice.VOID_STATUSES)))

    # Who counts as a member: held a membership on the day of the bill
    windows = _member_windows(request, inv.exclude(client__isnull=True).values('client'))
    member_ids, other_ids = [], []
    seen = {'m': set(), 'o': set()}
    stats = {'m': {'visits': 0, 'sales': ZERO}, 'o': {'visits': 0, 'sales': ZERO}}
    for r in inv.exclude(client__isnull=True).annotate(v=F('subtotal') - F('discount')).values(
            'id', 'client', 'created_at', 'v', 'membership_used'):
        side = 'm' if _held(windows, r['client'], r['created_at'].date()) else 'o'
        (member_ids if side == 'm' else other_ids).append(r['id'])
        seen[side].add(r['client'])
        stats[side]['visits'] += 1
        stats[side]['sales'] += Decimal(str(r['v'] or 0))

    # What members saved: the discounts on bills that applied a membership
    applied = inv.filter(membership_used__isnull=False)
    saved = (Decimal(str(applied.aggregate(d=Sum('discount'))['d'] or 0)) +
             Decimal(str(InvoiceItem.objects.filter(invoice__in=applied).aggregate(
                 d=Sum(F('discount') - F('card_benefit')))['d'] or 0)))

    sold = InvoiceItem.objects.filter(invoice__in=inv, content_type_id=_ct_id('membership')).aggregate(
        n=Sum('quantity'), v=LINE_VALUE)

    valid_now = base.filter(start_date__lte=today, expiry_date__gte=today)
    members_now = valid_now.values('client').distinct().count()
    ever_billed = _apply_security(request, Invoice.objects.filter(
        status__in=Invoice.REVENUE_STATUSES)).exclude(client__isnull=True).values('client').distinct().count()
    soon = today + timedelta(days=30)
    expiring = valid_now.filter(expiry_date__lte=soon)
    lapsed_year = base.filter(expiry_date__lt=today, expiry_date__gte=today - timedelta(days=365))

    # Spend per visit
    def per_visit(side):
        s = stats[side]
        return _f(s['sales'] / s['visits']) if s['visits'] else 0.0
    mv, ov = per_visit('m'), per_visit('o')

    # What members buy vs everyone else
    def mix(ids):
        out = defaultdict(Decimal)
        if ids:
            for r in InvoiceItem.objects.filter(invoice_id__in=ids).values('content_type__model').annotate(v=LINE_VALUE):
                out[_type_of(r['content_type__model'])] += Decimal(str(r['v'] or 0))
        return out
    m_mix, o_mix = mix(member_ids), mix(other_ids)
    m_tot, o_tot = sum(m_mix.values(), ZERO), sum(o_mix.values(), ZERO)
    buy = [{'type': t, 'members': _f(m_mix.get(t, 0)), 'members_share': _share(m_mix.get(t, 0), m_tot),
            'others': _f(o_mix.get(t, 0)), 'others_share': _share(o_mix.get(t, 0), o_tot)}
           for t in TYPE_ORDER if t in m_mix or t in o_mix]

    # Top services for members
    svc = {r['object_id']: r for r in (InvoiceItem.objects.filter(
        invoice_id__in=member_ids, content_type_id=_ct_id('servicemaster')).values('object_id').annotate(
        done=Sum('quantity'), value=LINE_VALUE, saved=Sum(F('discount') - F('card_benefit') + F('bill_discount_share')),
        name=Max('description')) if member_ids else [])}
    masters = {m['id']: m for m in ServiceMaster.objects.filter(id__in=list(svc)).values('id', 'name', 'category')}
    top_services = [{'rank': i, 'name': masters.get(sid, {}).get('name') or r['name'],
                     'category': masters.get(sid, {}).get('category') or '', 'done': int(r['done'] or 0),
                     'value': _f(r['value']), 'saved': _f(max(Decimal(str(r['saved'] or 0)), ZERO))}
                    for i, (sid, r) in enumerate(sorted(svc.items(), key=lambda kv: -(kv[1]['saved'] or 0))[:25], 1)]

    # 12-month chart
    first = _months_back(end, 12)
    sav = defaultdict(Decimal)
    scope12 = _item_scope(request, first, end)
    ym = dict(y=ExtractYear('invoice__created_at'), m=ExtractMonth('invoice__created_at'))
    for r in InvoiceItem.objects.filter(scope12, invoice__membership_used__isnull=False).annotate(**ym).values(
            'y', 'm').annotate(d=Sum(F('discount') - F('card_benefit'))):
        sav[(r['y'], r['m'])] += Decimal(str(r['d'] or 0))
    app12 = _bills(request, first, end).filter(membership_used__isnull=False)
    for r in app12.annotate(y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values('y', 'm').annotate(d=Sum('discount')):
        sav[(r['y'], r['m'])] += Decimal(str(r['d'] or 0))
    msold = defaultdict(Decimal)
    for r in InvoiceItem.objects.filter(scope12, content_type_id=_ct_id('membership')).annotate(**ym).values(
            'y', 'm').annotate(v=LINE_VALUE):
        msold[(r['y'], r['m'])] += Decimal(str(r['v'] or 0))
    chart = [{'month': _month_label(y, m), 'saved': _f(sav.get((y, m), 0)), 'sold': _f(msold.get((y, m), 0)),
              'partial': (y, m) == (end.year, end.month)} for y, m in _month_keys(first, end)]

    # Renewal calls
    def calls(qs, kind):
        rows = list(qs.values('client_id', 'client__first_name', 'client__last_name', 'client__phone',
                              'membership__name', 'expiry_date').order_by('-expiry_date' if kind == 'lapsed' else 'expiry_date')[:400])
        ids = {r['client_id'] for r in rows}
        last, spend = _last_visits(request, ids), _spend_12m(request, ids, today)
        out = []
        for r in rows:
            days = (today - r['expiry_date']).days if kind == 'lapsed' else (r['expiry_date'] - today).days
            lv = last.get(r['client_id'])
            out.append({'client_id': r['client_id'], 'name': _name(r['client__first_name'], r['client__last_name']) or '·',
                        'phone': _mask(r['client__phone']), 'membership': r['membership__name'],
                        'expires': r['expiry_date'].strftime('%d-%b-%Y'), 'days': days,
                        'last_visit': lv.strftime('%d-%b-%Y') if lv else None,
                        'spend_12m': _f(spend.get(r['client_id'], 0))})
        return out
    holders_now = set(valid_now.values_list('client_id', flat=True))
    lapsed_qs = lapsed_year.exclude(client_id__in=holders_now)

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'kpis': {
            'members': members_now, 'clients_ever': ever_billed, 'members_share': _share(members_now, ever_billed),
            'member_visits': stats['m']['visits'], 'visit_share': _share(stats['m']['visits'], bills),
            'members_visited': len(seen['m']), 'sold': int(sold['n'] or 0), 'sold_value': _f(sold['v']),
            'saved': _f(saved), 'saved_per_visit': _f(saved / stats['m']['visits']) if stats['m']['visits'] else 0.0,
            'spend_member': mv, 'spend_other': ov, 'spend_change': _pct_change(mv, ov) if stats['m']['visits'] and stats['o']['visits'] else None,
            'expiring_30': expiring.count(), 'lapsed_year': lapsed_qs.values('client').distinct().count(),
        },
        'chart': chart, 'chart_saved_12m': _f(sum(sav.values(), ZERO)), 'chart_sold_12m': _f(sum(msold.values(), ZERO)),
        'compare': {
            'clients': [len(seen['m']), len(seen['o'])],
            'visits': [stats['m']['visits'], stats['o']['visits']],
            'visits_per_client': [round(stats['m']['visits'] / len(seen['m']), 2) if seen['m'] else 0,
                                  round(stats['o']['visits'] / len(seen['o']), 2) if seen['o'] else 0],
            'spend_per_client': [_f(stats['m']['sales'] / len(seen['m'])) if seen['m'] else 0.0,
                                 _f(stats['o']['sales'] / len(seen['o'])) if seen['o'] else 0.0],
            'sales': [_f(stats['m']['sales']), _f(stats['o']['sales'])],
        },
        'buy': buy, 'top_services': top_services,
        'expiring': calls(expiring, 'expiring'), 'lapsed': calls(lapsed_qs, 'lapsed'),
    })


# ─── Value cards ──────────────────────────────────────────────────────────

def _card_spend_payments(inv):
    """Value-card payments on these bills: face value taken off the cards."""
    return Payment.objects.filter(invoice__in=inv, is_tip=False, payment_method__istartswith='Value Card')


FACE = Coalesce('card_face', 'amount')


@module_permission(**dashboard_tab('finance', also=('usage_report',)))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def analytics_value_cards(request):
    start, end, _ps, _pe = _period(request)
    today = date_type.today()
    soon = today + timedelta(days=30)
    inv = _bills(request, start, end)
    cards = _by_selling_centre(request, ClientValueCard.objects.all())

    live = cards.filter(is_active=True, expiry_date__gte=today, balance__gt=0)
    live_agg = live.aggregate(b=Sum('balance'), n=Count('id'), c=Count('client', distinct=True))

    pay = _card_spend_payments(inv)
    spent = pay.aggregate(t=Sum(FACE), inv=Count('invoice', distinct=True), cl=Count('invoice__client', distinct=True))
    spent_face = Decimal(str(spent['t'] or 0))

    sold_items = InvoiceItem.objects.filter(invoice__in=inv, content_type_id=_ct_id('valuecard'))
    sold = sold_items.aggregate(n=Sum('quantity'), v=LINE_VALUE)
    sold_n = int(sold['n'] or 0)
    sold_value = Decimal(str(sold['v'] or 0))
    face_expr = Coalesce('face_value', 'value_card__value')
    face_sold = Decimal(str(cards.filter(created_at__gte=start, created_at__lt=end + timedelta(days=1))
                            .aggregate(t=Sum(face_expr))['t'] or 0))

    expiring = live.filter(expiry_date__lte=soon)
    exp_agg = expiring.aggregate(b=Sum('balance'), n=Count('id'))
    expired_qs = cards.filter(expiry_date__lt=today, expiry_date__gte=today - timedelta(days=30)).filter(
        Q(balance__gt=0) | Q(forfeited_amount__gt=0))
    expired_agg = expired_qs.aggregate(b=Sum('balance'), f=Sum('forfeited_amount'), n=Count('id'))

    # 12 months: sold vs spent (face value both sides)
    first = _months_back(end, 12)
    window_cards = cards.filter(created_at__gte=first, created_at__lt=end + timedelta(days=1))
    sold_m, spent_m = defaultdict(Decimal), defaultdict(Decimal)
    for r in window_cards.annotate(y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values('y', 'm').annotate(t=Sum(face_expr)):
        sold_m[(r['y'], r['m'])] += Decimal(str(r['t'] or 0))
    for r in _card_spend_payments(_bills(request, first, end)).annotate(
            y=ExtractYear('invoice__created_at'), m=ExtractMonth('invoice__created_at')).values('y', 'm').annotate(t=Sum(FACE)):
        spent_m[(r['y'], r['m'])] += Decimal(str(r['t'] or 0))
    keys = _month_keys(first, end)
    chart = [{'month': _month_label(y, m), 'sold': _f(sold_m.get((y, m), 0)), 'spent': _f(spent_m.get((y, m), 0)),
              'partial': (y, m) == (end.year, end.month)} for y, m in keys]
    sold_12, spent_12 = sum(sold_m.values(), ZERO), sum(spent_m.values(), ZERO)
    best_key = max(keys, key=lambda k: sold_m.get(k, 0), default=None)

    # Where card credit goes
    covered = InvoiceItem.objects.filter(invoice__in=inv, value_card__isnull=False, is_redemption=False)
    rows = list(covered.values('content_type__model', 'object_id').annotate(
        lines=Count('id'), done=Sum('quantity'), face=Sum('card_face'),
        taxable=Sum(F('card_taxable') + F('card_benefit')), gross=Sum(F('total_price') + F('card_benefit')),
        name=Max('description')))
    masters = {m['id']: m for m in ServiceMaster.objects.filter(
        id__in={r['object_id'] for r in rows if r['content_type__model'] == 'servicemaster'}).values('id', 'name', 'category')}
    gross_all = {r['object_id']: Decimal(str(r['v'] or 0)) for r in InvoiceItem.objects.filter(
        invoice__in=inv, content_type_id=_ct_id('servicemaster'), is_redemption=False,
        object_id__in=list(masters)).values('object_id').annotate(v=Sum(F('total_price') + F('card_benefit')))}
    cats, per_svc = defaultdict(lambda: {'lines': 0, 'face': ZERO}), []
    for r in rows:
        is_svc = r['content_type__model'] == 'servicemaster'
        m = masters.get(r['object_id'], {})
        cat = (m.get('category') or 'Other') if is_svc else _type_of(r['content_type__model'])
        cats[cat]['lines'] += r['lines']
        cats[cat]['face'] += Decimal(str(r['face'] or 0))
        if is_svc:
            full = gross_all.get(r['object_id'], ZERO)
            per_svc.append({'name': m.get('name') or r['name'], 'category': m.get('category') or '',
                            'done': int(r['done'] or 0), 'paid': _f(r['face']), '_f': full,
                            'of_sales': min(100.0, _share(r['taxable'], full))})
    face_total = sum((c['face'] for c in cats.values()), ZERO)
    spend_cats = [{'category': k, 'lines': v['lines'], 'face': _f(v['face']), 'share': _share(v['face'], face_total)}
                  for k, v in sorted(cats.items(), key=lambda kv: -kv[1]['face'])]
    per_svc.sort(key=lambda r: -r['paid'])
    for r in per_svc:
        r.pop('_f')

    # Top balances, with the nearest expiry and last visit
    by_client = list(live.values('client').annotate(n=Count('id'), b=Sum('balance'), near=Min('expiry_date')).order_by('-b')[:25])
    ids = [r['client'] for r in by_client]
    names, last = _client_names(ids), _last_visits(request, ids)
    top_balances = [{'rank': i, 'client_id': r['client'],
                     'name': _name(names.get(r['client'], {}).get('first_name'), names.get(r['client'], {}).get('last_name')) or 'Client',
                     'cards': r['n'], 'balance': _f(r['b']), 'nearest_expiry': r['near'].strftime('%d-%b-%Y'),
                     'last_visit': last[r['client']].strftime('%d-%b-%Y') if last.get(r['client']) else None}
                    for i, r in enumerate(by_client, 1)]

    # Cards to call about
    def calls(qs, expired):
        rows = list(qs.values('client_id', 'client__first_name', 'client__last_name', 'client__phone',
                              'value_card__title', 'balance', 'forfeited_amount', 'expiry_date').order_by('expiry_date')[:300])
        lv = _last_visits(request, {r['client_id'] for r in rows})
        out = []
        for r in rows:
            days = (today - r['expiry_date']).days if expired else (r['expiry_date'] - today).days
            bal = Decimal(str(r['balance'] or 0)) + Decimal(str(r['forfeited_amount'] or 0)) if expired else r['balance']
            out.append({'client_id': r['client_id'], 'name': _name(r['client__first_name'], r['client__last_name']) or '·',
                        'phone': _mask(r['client__phone']), 'card': r['value_card__title'], 'balance': _f(bal),
                        'expires': r['expiry_date'].strftime('%d-%b-%Y'), 'days': days,
                        'last_visit': lv[r['client_id']].strftime('%d-%b-%Y') if lv.get(r['client_id']) else None})
        return out

    return Response({
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'kpis': {
            'outstanding': _f(live_agg['b']), 'live_cards': live_agg['n'] or 0, 'live_clients': live_agg['c'] or 0,
            'spent': _f(spent_face), 'spent_invoices': spent['inv'] or 0,
            'spent_per_invoice': _f(spent_face / spent['inv']) if spent['inv'] else 0.0,
            'sold': sold_n, 'sold_value': _f(sold_value), 'redeeming_clients': spent['cl'] or 0,
            'expiring_value': _f(exp_agg['b']), 'expiring_cards': exp_agg['n'] or 0,
            'expired_value': _f(Decimal(str(expired_agg['b'] or 0)) + Decimal(str(expired_agg['f'] or 0))),
            'expired_cards': expired_agg['n'] or 0,
        },
        'chart': chart,
        'chart_sold_12m': _f(sold_12), 'chart_spent_12m': _f(spent_12),
        'spend_categories': spend_cats[:15], 'spend_categories_total': len(spend_cats),
        'spend_total': _f(face_total),
        'economics': {
            'avg_card_sold': _f(sold_value / sold_n) if sold_n else 0.0, 'sold_cards': sold_n,
            'per_invoice': _f(spent_face / spent['inv']) if spent['inv'] else 0.0, 'invoices': spent['inv'] or 0,
            'per_client': _f(spent_face / spent['cl']) if spent['cl'] else 0.0, 'clients': spent['cl'] or 0,
            'liability_change': _f(face_sold - spent_face),
            'liability_change_12m': _f(sold_12 - spent_12),
            'avg_month_spent': _f(spent_12 / 12), 'avg_month_sold': _f(sold_12 / 12),
            'best_month': _month_label(*best_key) if best_key and sold_m.get(best_key) else None,
            'best_month_value': _f(sold_m.get(best_key, 0)) if best_key else 0.0,
            'face_sold_period': _f(face_sold),
        },
        'top_balances': top_balances,
        'services': per_svc[:15], 'services_total': len(per_svc),
        'expiring': calls(expiring, False), 'expired': calls(expired_qs, True),
    })

