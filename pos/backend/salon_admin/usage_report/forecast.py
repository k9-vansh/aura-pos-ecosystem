"""Forecast: what is coming up, worked from what is held today and from recent use.

Nothing here is a prediction model. Each figure is a plain count of what falls due, or a
recent run rate carried forward, and the page says which.
"""
import calendar
from datetime import timedelta
from decimal import Decimal

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from clients.models import ClientMembership, ClientPackage, ClientValueCard
from marketing.models import Package, PromotionUsage
from salon_admin.analytics_endpoints import _by_selling_centre

from .base import LIVE, OFFER_TYPES, ZERO, dec, f2, num, promo_status, terms_of
from .cashback import ledger
from .coupons import coupons as coupon_report
from .customers import SALES, perks
from .packages import left_in, sessions_in

HORIZONS = (30, 60, 90)
RATE_DAYS = 14


def project(values, ahead=3):
    """A straight line through the months so far, carried `ahead` months on (never below zero)."""
    n = len(values)
    mx, my = (n - 1) / 2, sum(values) / n
    den = sum((x - mx) ** 2 for x in range(n)) or 1
    slope = sum((x - mx) * (v - my) for x, v in enumerate(values)) / den
    base = my - slope * mx
    return [max(base + slope * (n + i), 0.0) for i in range(ahead)], slope


def _due(ctx):
    """What falls due in 30 / 60 / 90 days, kind by kind."""
    today = ctx.today
    out = []

    def add(kind, rows):
        out.append({'kind': kind, 'horizons': rows})

    if ctx.wants('ValueCard'):
        live = ctx.by_customer(_by_selling_centre(ctx.request, ClientValueCard.objects.filter(
            is_active=True, expiry_date__gte=today, balance__gt=0)))
        rows = []
        for h in HORIZONS:
            a = live.filter(expiry_date__lte=today + timedelta(days=h)).aggregate(
                n=Count('id'), c=Count('client', distinct=True), t=Sum('balance'))
            rows.append({'days': h, 'count': a['n'], 'customers': a['c'], 'amount': f2(a['t']), 'unit': 'Rs unspent on cards'})
        add('Value cards expiring', rows)

    if ctx.wants('Package'):
        per_session = {p.id: dec(p.price) / sessions_in(p) for p in Package.objects.all()}
        held = _by_selling_centre(ctx.request, ClientPackage.objects.filter(is_active=True, expiry_date__gte=today))
        data = [(r['package_id'], r['client_id'], r['expiry_date'], left_in(r['services_remaining']))
                for r in ctx.by_customer(held).values('package_id', 'client_id', 'expiry_date', 'services_remaining')]
        rows = []
        for h in HORIZONS:
            limit = today + timedelta(days=h)
            sel = [d for d in data if d[2] <= limit and d[3] > 0]
            rows.append({'days': h, 'count': len(sel), 'customers': len({d[1] for d in sel}),
                         'sessions': sum(d[3] for d in sel),
                         'amount': f2(sum((per_session.get(d[0], ZERO) * d[3] for d in sel), ZERO)),
                         'unit': 'sessions not yet used'})
        add('Packages expiring', rows)

    if ctx.wants('Cashback'):
        holding = ledger(ctx).filter(Q(kind='earned') | Q(kind=''), remaining__gt=0, expires_on__isnull=False,
                                     expires_on__gte=today)
        rows = []
        for h in HORIZONS:
            a = holding.filter(expires_on__lte=today + timedelta(days=h)).aggregate(
                n=Count('id'), c=Count('client', distinct=True), t=Sum('remaining'))
            rows.append({'days': h, 'count': a['n'], 'customers': a['c'], 'amount': f2(a['t']), 'unit': 'Rs of cashback unspent'})
        add('Cashback expiring', rows)

    if ctx.wants('Membership'):
        mem = ctx.by_customer(_by_selling_centre(ctx.request, ClientMembership.objects.filter(
            is_active=True, expiry_date__gte=today))).values(
            'client_id', 'expiry_date', 'membership__pre_tax_price', 'membership__post_tax_price', 'membership__gst_percent')
        data = []
        for r in mem:
            pre = dec(r['membership__pre_tax_price'])
            if pre <= 0 and dec(r['membership__post_tax_price']) > 0:
                pre = dec(r['membership__post_tax_price']) * 100 / (100 + dec(r['membership__gst_percent']))
            data.append((r['client_id'], r['expiry_date'], pre))
        rows = []
        for h in HORIZONS:
            sel = [d for d in data if d[1] <= today + timedelta(days=h)]
            rows.append({'days': h, 'count': len(sel), 'customers': len({d[0] for d in sel}),
                         'amount': f2(sum((d[2] for d in sel), ZERO)), 'unit': 'Rs if all renew (before GST)'})
        add('Memberships ending', rows)
    return out


def _coupon_runs(ctx):
    """Each running coupon offer: codes left, how fast they go, when they run out, what the brand will owe."""
    if not ctx.wants('Coupon'):
        return []
    runs = []
    for r in coupon_report(ctx)['rows']:
        if r['status'] != 'running' or not r['unused']:
            continue
        end = r['end'] if r['end'] and r['end'] != 'None' else None
        days_to_end = None
        if end:
            from datetime import date
            days_to_end = max((date.fromisoformat(end) - ctx.today).days, 0)
        avg = (r['brand_owes'] / r['used']) if r['used'] else 0
        expected = r['per_day'] * days_to_end if days_to_end is not None else None
        will_use = min(expected, r['unused']) if expected is not None else None
        runs.append({
            'name': r['name'], 'brand': r['brand'], 'unused': r['unused'], 'per_day': r['per_day'], 'ends': end,
            'days_left_codes': r['days_left'], 'days_to_end': days_to_end,
            'runs_out_first': bool(r['days_left'] is not None and days_to_end is not None and r['days_left'] < days_to_end),
            'expected_uses': round(will_use) if will_use is not None else None,
            'expected_left': round(r['unused'] - will_use) if will_use is not None else None,
            'expected_owed': f2(will_use * avg) if will_use is not None else None,
        })
    return runs


def _offer_runs(ctx):
    """Each running offer: what it has cost so far and what it will cost by its end date at today's pace."""
    types = [t for t in OFFER_TYPES if ctx.wants(t)]
    if not types:
        return []
    promos = {p.id: p for p in ctx.promotions().filter(promo_type__in=types) if ctx.keep_promotion(p)
              and promo_status(p, ctx.today) == 'running'}
    base = ctx.in_centres(PromotionUsage.objects.filter(
        promotion_id__in=list(promos), is_reversed=False, invoice__status__in=LIVE))
    sofar = {r['promotion']: r for r in base.values('promotion').annotate(
        n=Count('id'), before=Sum('bill_amount_before'), after=Sum('bill_amount_after'), cb=Sum('cashback_awarded'))}
    recent = {r['promotion']: r for r in base.filter(date__gte=ctx.today - timedelta(days=RATE_DAYS - 1)).values('promotion').annotate(
        n=Count('id'), before=Sum('bill_amount_before'), after=Sum('bill_amount_after'), cb=Sum('cashback_awarded'))}
    runs = []
    for pid, p in promos.items():
        s, r = sofar.get(pid), recent.get(pid)
        cost_so_far = (max(dec(s['before']) - dec(s['after']), ZERO) + dec(s['cb'])) if s else ZERO
        per_day = ((max(dec(r['before']) - dec(r['after']), ZERO) + dec(r['cb'])) / RATE_DAYS) if r else ZERO
        days_left = max((p.end_date - ctx.today).days, 0) if p.end_date else None
        runs.append({
            'name': p.name, 'type': p.promo_type, 'terms': terms_of(p), 'ends': str(p.end_date) if p.end_date else None,
            'bills_so_far': s['n'] if s else 0, 'cost_so_far': f2(cost_so_far), 'cost_per_day': f2(per_day),
            'days_left': days_left, 'cost_to_go': f2(per_day * days_left) if days_left is not None else None,
            'cost_at_end': f2(cost_so_far + per_day * days_left) if days_left is not None else None,
        })
    runs.sort(key=lambda x: -(x['cost_at_end'] or 0))
    return runs


def _sales_outlook(ctx):
    """Marketing-linked sales month by month, and a straight-line look at the next three."""
    today = ctx.today
    first_this = today.replace(day=1)
    start = first_this
    for _ in range(6):
        start = (start - timedelta(days=1)).replace(day=1)
    qs = ctx.by_customer(ctx.bills(start, today), 'client_id')
    _by_kind, linked = perks(start)
    got = {(r['y'], r['m']): r for r in qs.annotate(y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values(
        'y', 'm').annotate(linked=Sum(SALES, filter=linked), total=Sum(SALES))}
    months, y, m = [], start.year, start.month
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    history = [{'month': ctx.month_label(k), 'linked': f2(got[k]['linked']) if k in got else 0,
                'total': f2(got[k]['total']) if k in got else 0, 'partial': k == (today.year, today.month)} for k in months]
    complete = [h['linked'] for h in history if not h['partial'] and h['total'] > 0]
    mtd = history[-1] if history else {'linked': 0, 'total': 0}
    days_in = calendar.monthrange(today.year, today.month)[1]
    run_rate = round(mtd['linked'] / today.day * days_in, 2) if today.day else 0
    out = {'history': history, 'this_month_so_far': mtd['linked'], 'this_month_run_rate': run_rate,
           'months_of_history': len(complete), 'projection': None, 'confidence': None}
    if len(complete) >= 3:
        values, slope = project([float(v) for v in complete[-6:]])
        out['projection'] = []
        y, m = today.year, today.month
        for v in values:
            m += 1
            if m > 12:
                m, y = 1, y + 1
            out['projection'].append({'month': ctx.month_label((y, m)), 'linked': round(v, 2)})
        out['trend_per_month'] = round(slope, 2)
        out['confidence'] = 'medium' if len(complete) >= 6 else 'low'
    return out


def forecast(ctx):
    return {
        'frame': ctx.frame(), 'enabled': True, 'due': _due(ctx), 'coupons': _coupon_runs(ctx), 'offers': _offer_runs(ctx),
        'sales': _sales_outlook(ctx),
        'note': ('These look forward from today, whatever dates are picked above (the centre and customer filters still apply). '
                 'Money that "falls due" is a count of what customers still hold and when it ends: it is not a promise it will '
                 f'be used. Offer costs carry the last {RATE_DAYS} days\' pace to the end date. The sales outlook is a '
                 'straight line through the last complete months, so treat it as a rough guide, and it needs three full months.'),
    }
