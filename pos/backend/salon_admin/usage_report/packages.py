"""Packages: sold, sessions used, sessions still held, and what lapses unused."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from billing.models import InvoiceItem
from clients.models import ClientPackage
from marketing.models import Package
from salon_admin.analytics_endpoints import LINE_VALUE, _by_selling_centre, _ct_id, _item_scope

from .base import LIVE, ZERO, change, dec, f2, pct


def sessions_in(package):
    """How many sessions one package holds (a legacy single-service package: one)."""
    services = package.services_json if isinstance(package.services_json, list) else []
    total = 0
    for s in services:
        try:
            total += int(float((s or {}).get('pkgQty') or 0))
        except (TypeError, ValueError):
            continue
    return total or 1


def left_in(remaining):
    """Sessions still unused in a ClientPackage's JSON (anything unreadable counts as none)."""
    if not isinstance(remaining, dict):
        return 0
    total = 0
    for v in remaining.values():
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if n == n and n > 0 and n != float('inf'):
            total += int(n)
    return total


def _sold(ctx, start, end):
    qs = InvoiceItem.objects.filter(_item_scope(ctx.request, start, end), content_type_id=_ct_id('package'),
                                    is_redemption=False)
    return ctx.by_customer(qs, 'invoice__client_id')


def _redeemed(ctx, start, end):
    qs = InvoiceItem.objects.filter(_item_scope(ctx.request, start, end), is_redemption=True,
                                    redeemed_from_package__isnull=False)
    return ctx.by_customer(qs, 'invoice__client_id')


def packages(ctx):
    if not ctx.wants('Package'):
        return {'frame': ctx.frame(), 'enabled': False, 'rows': [], 'totals': None}
    today = ctx.today

    catalogue = Package.objects.all().select_related('center')
    if ctx.ids is not None:
        catalogue = catalogue.filter(Q(level='Organisation') | Q(center_id__in=ctx.ids))
    pkgs = {p.id: p for p in catalogue if not ctx.q or ctx.q in p.name.lower()}
    per_session = {pid: (dec(p.price) / sessions_in(p)) for pid, p in pkgs.items()}

    sold = {r['object_id']: r for r in _sold(ctx, ctx.start, ctx.end).values('object_id').annotate(
        units=Sum('quantity'), value=LINE_VALUE, bills=Count('invoice', distinct=True))}
    sold_prev = {r['object_id']: r for r in _sold(ctx, ctx.prev_start, ctx.prev_end).values('object_id').annotate(
        units=Sum('quantity'), value=LINE_VALUE)}
    used = {r['redeemed_from_package__package_id']: int(r['n'] or 0) for r in _redeemed(ctx, ctx.start, ctx.end).values(
        'redeemed_from_package__package_id').annotate(n=Sum('quantity'))}
    used_prev = sum(int(r['n'] or 0) for r in _redeemed(ctx, ctx.prev_start, ctx.prev_end).values(
        'redeemed_from_package__package_id').annotate(n=Sum('quantity')))

    # Holdings today, from what each customer still has.
    holders = defaultdict(lambda: {'clients': set(), 'left': 0, 'soon': 0, 'lapsed': 0, 'lapsed_value': ZERO, 'left_value': ZERO,
                                   'soon_value': ZERO, 'soon_n': 0})
    live = _by_selling_centre(ctx.request, ClientPackage.objects.filter(is_active=True, expiry_date__gte=today))
    for r in ctx.by_customer(live).values('package_id', 'client_id', 'services_remaining', 'expiry_date').iterator(chunk_size=2000):
        left = left_in(r['services_remaining'])
        if not left:
            continue
        h = holders[r['package_id']]
        value = per_session.get(r['package_id'], ZERO) * left
        h['clients'].add(r['client_id'])
        h['left'] += left
        h['left_value'] += value
        if r['expiry_date'] <= today + timedelta(days=30):
            h['soon'] += left
            h['soon_value'] += value
            h['soon_n'] += 1
    lapsed_qs = _by_selling_centre(ctx.request, ClientPackage.objects.filter(
        expiry_date__lt=today, expiry_date__gte=today - timedelta(days=90)))
    for r in ctx.by_customer(lapsed_qs).values('package_id', 'services_remaining').iterator(chunk_size=2000):
        left = left_in(r['services_remaining'])
        if left:
            h = holders[r['package_id']]
            h['lapsed'] += left
            h['lapsed_value'] += per_session.get(r['package_id'], ZERO) * left

    # The packages sold in the period, followed to today: how many of their sessions have been used.
    cohort = _by_selling_centre(ctx.request, ClientPackage.objects.filter(
        created_at__gte=ctx.start, created_at__lt=ctx.end + timedelta(days=1), source_invoice__status__in=LIVE))
    cohort = ctx.by_customer(cohort)
    cohort_left = defaultdict(int)
    for r in cohort.values('id', 'package_id', 'services_remaining').iterator(chunk_size=2000):
        cohort_left[r['package_id']] += left_in(r['services_remaining'])
    cohort_used = {r['redeemed_from_package__package_id']: int(r['n'] or 0) for r in InvoiceItem.objects.filter(
        is_redemption=True, invoice__status__in=LIVE, redeemed_from_package__in=cohort.values('id')).values(
        'redeemed_from_package__package_id').annotate(n=Sum('quantity'))}

    rows = []
    for pid, p in pkgs.items():
        s, h = sold.get(pid), holders.get(pid)
        if not s and not used.get(pid) and not h:
            continue
        units = int(s['units'] or 0) if s else 0
        c_used, c_left = cohort_used.get(pid, 0), cohort_left.get(pid, 0)
        c_total = c_used + c_left
        rows.append({
            'id': pid, 'name': p.name, 'price': f2(p.price), 'validity_days': p.validity_days, 'active': p.is_active,
            'sessions_per_package': sessions_in(p),
            'sold': units, 'sold_prev': int(sold_prev.get(pid, {}).get('units') or 0) if sold_prev.get(pid) else 0,
            'sold_change': change(units, sold_prev.get(pid, {}).get('units') if sold_prev.get(pid) else 0),
            'revenue': f2(s['value']) if s else 0, 'bills': s['bills'] if s else 0,
            'sessions_sold': units * sessions_in(p), 'sessions_used': used.get(pid, 0),
            'cohort_sessions': c_total, 'cohort_used': c_used, 'utilisation': pct(c_used, c_total) if c_total else None,
            'holders': len(h['clients']) if h else 0, 'sessions_left': h['left'] if h else 0,
            'value_left': f2(h['left_value']) if h else 0,
            'expiring_30': h['soon'] if h else 0, 'expiring_30_value': f2(h['soon_value']) if h else 0,
            'lapsed_90': h['lapsed'] if h else 0, 'lapsed_90_value': f2(h['lapsed_value']) if h else 0,
        })
    rows.sort(key=lambda r: (-r['revenue'], -r['sessions_used'], r['name']))

    totals = {
        'sold': sum(r['sold'] for r in rows), 'sold_prev': sum(int(v['units'] or 0) for v in sold_prev.values()),
        'revenue': f2(sum((dec(r['revenue']) for r in rows), ZERO)),
        'revenue_prev': f2(sum((dec(v['value']) for v in sold_prev.values()), ZERO)),
        'sessions_used': sum(r['sessions_used'] for r in rows), 'sessions_used_prev': used_prev,
        'sessions_used_change': change(sum(r['sessions_used'] for r in rows), used_prev),
        'holders': len({c for h in holders.values() for c in h['clients']}),
        'sessions_left': sum(r['sessions_left'] for r in rows), 'value_left': f2(sum((dec(r['value_left']) for r in rows), ZERO)),
        'expiring_30': sum(r['expiring_30'] for r in rows), 'expiring_30_value': f2(sum((dec(r['expiring_30_value']) for r in rows), ZERO)),
        'lapsed_90': sum(r['lapsed_90'] for r in rows), 'lapsed_90_value': f2(sum((dec(r['lapsed_90_value']) for r in rows), ZERO)),
        'utilisation': pct(sum(r['cohort_used'] for r in rows), sum(r['cohort_sessions'] for r in rows)),
    }
    totals['sold_change'] = change(totals['sold'], totals['sold_prev'])
    totals['revenue_change'] = change(totals['revenue'], totals['revenue_prev'])

    services = [{'name': r['description'] or 'Service', 'sessions': int(r['n'] or 0)} for r in
                _redeemed(ctx, ctx.start, ctx.end).values('object_id', 'description').annotate(n=Sum('quantity')).order_by('-n')[:15]]
    by_centre = defaultdict(lambda: {'sold': 0, 'revenue': ZERO, 'used': 0})
    for r in _sold(ctx, ctx.start, ctx.end).values('invoice__center__display_name', 'invoice__center__center_name').annotate(
            units=Sum('quantity'), value=LINE_VALUE):
        k = r['invoice__center__display_name'] or r['invoice__center__center_name'] or 'No centre'
        by_centre[k]['sold'] += int(r['units'] or 0)
        by_centre[k]['revenue'] += dec(r['value'])
    for r in _redeemed(ctx, ctx.start, ctx.end).values('invoice__center__display_name', 'invoice__center__center_name').annotate(n=Sum('quantity')):
        k = r['invoice__center__display_name'] or r['invoice__center__center_name'] or 'No centre'
        by_centre[k]['used'] += int(r['n'] or 0)

    first = ctx.first_of_months(12)
    m_sold = {(r['y'], r['m']): r for r in _sold(ctx, first, ctx.end).annotate(
        y=ExtractYear('invoice__created_at'), m=ExtractMonth('invoice__created_at')).values('y', 'm').annotate(
        units=Sum('quantity'), value=LINE_VALUE)}
    m_used = {(r['y'], r['m']): int(r['n'] or 0) for r in _redeemed(ctx, first, ctx.end).annotate(
        y=ExtractYear('invoice__created_at'), m=ExtractMonth('invoice__created_at')).values('y', 'm').annotate(n=Sum('quantity'))}
    trend = [{'month': ctx.month_label(k), 'sold': int(m_sold[k]['units'] or 0) if k in m_sold else 0,
              'revenue': f2(m_sold[k]['value']) if k in m_sold else 0, 'sessions_used': m_used.get(k, 0)} for k in ctx.months(12)]
    return {
        'frame': ctx.frame(), 'enabled': True, 'totals': totals, 'rows': rows, 'services': services, 'trend': trend,
        'by_centre': sorted(({'centre': k, 'sold': v['sold'], 'revenue': f2(v['revenue']), 'sessions_used': v['used']}
                             for k, v in by_centre.items()), key=lambda r: -r['revenue']),
        'note': ('A session\'s value is the package price divided by the sessions it holds. '
                 '"Lapsed" is what customers never used before the package expired (last 90 days).'),
    }
