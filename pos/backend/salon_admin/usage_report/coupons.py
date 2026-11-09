"""Brand coupons: stock, use, what the brands owe, and when the codes run out."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Sum

from marketing import coupons as rules
from marketing.models import CouponCode

from .base import LIVE, ZERO, brand_of, change, dec, f2, pct, person, promo_status, terms_of

BURN_DAYS = 14          # the run rate is the last two weeks' use


def used_codes(ctx, start, end, promo_ids):
    """Codes used in the period on bills that still stand, under every filter."""
    qs = CouponCode.objects.filter(
        promotion_id__in=promo_ids, is_used=True, used_at__gte=start, used_at__lt=end + timedelta(days=1),
        invoice__status__in=LIVE)
    return ctx.by_customer(ctx.in_centres(qs))


def coupons(ctx):
    if not ctx.wants('Coupon'):
        return {'frame': ctx.frame(), 'enabled': False, 'rows': [], 'totals': None}

    promos = {p.id: p for p in ctx.promotions().filter(promo_type='Coupon') if ctx.keep_promotion(p)}
    ids = list(promos)
    stock = {r['promotion']: r for r in CouponCode.objects.filter(promotion_id__in=ids).values('promotion').annotate(
        total=Count('id'), used=Count('id', filter=_q(is_used=True)), void=Count('id', filter=_q(is_void=True, is_used=False)))}
    owed_all = {r['promotion']: dec(r['t']) for r in ctx.in_centres(CouponCode.objects.filter(
        promotion_id__in=ids, is_used=True, invoice__status__in=LIVE)).values('promotion').annotate(t=Sum('amount_applied'))}
    recent = {r['promotion']: r['n'] for r in CouponCode.objects.filter(
        promotion_id__in=ids, is_used=True, invoice__status__in=LIVE,
        used_at__gte=ctx.today - timedelta(days=BURN_DAYS - 1)).values('promotion').annotate(n=Count('id'))}
    prev = {r['promotion']: r for r in ctx.in_centres(CouponCode.objects.filter(
        promotion_id__in=ids, is_used=True, invoice__status__in=LIVE,
        used_at__gte=ctx.prev_start, used_at__lt=ctx.prev_end + timedelta(days=1))).values('promotion').annotate(
        n=Count('id'), a=Sum('amount_applied'))}

    # Every code used in the period, once: what it paid, what it was worth, who, where, when.
    per = defaultdict(lambda: {'n': 0, 'applied': ZERO, 'wasted': ZERO, 'bills': ZERO, 'clients': set()})
    centre_rows = defaultdict(lambda: {'n': 0, 'applied': ZERO})
    month_rows = defaultdict(lambda: {'n': 0, 'applied': ZERO})
    by_client = defaultdict(lambda: {'n': 0, 'applied': ZERO, 'client': None})
    rows_used = used_codes(ctx, ctx.start, ctx.end, ids).select_related('promotion', 'invoice', 'client', 'center')
    for c in rows_used.iterator(chunk_size=2000):
        applied = dec(c.amount_applied)
        bill = dec(c.invoice.total_amount) if c.invoice_id else ZERO
        worth = rules.worth_of(c, bill)
        p = per[c.promotion_id]
        p['n'] += 1
        p['applied'] += applied
        p['wasted'] += max(worth - applied, ZERO)       # the part of a coupon a smaller bill could not take
        p['bills'] += bill
        if c.client_id:
            p['clients'].add(c.client_id)
        name = (c.center.display_name or c.center.center_name) if c.center_id else 'No centre'
        centre_rows[name]['n'] += 1
        centre_rows[name]['applied'] += applied
        key = (c.used_at.year, c.used_at.month)
        month_rows[key]['n'] += 1
        month_rows[key]['applied'] += applied
        if c.client_id:
            by_client[c.client_id]['n'] += 1
            by_client[c.client_id]['applied'] += applied
            by_client[c.client_id]['client'] = c.client

    rows = []
    for pid, promo in promos.items():
        s = stock.get(pid, {'total': 0, 'used': 0, 'void': 0})
        total, used = s['total'], s['used']
        unused = max(total - used - s['void'], 0)
        status = promo_status(promo, ctx.today)
        rate = recent.get(pid, 0) / BURN_DAYS
        days_left = round(unused / rate) if rate > 0 else None
        left_to_end = (promo.end_date - ctx.today).days if promo.end_date and promo.end_date >= ctx.today else 0
        p = per.get(pid)
        n = p['n'] if p else 0
        note = ''
        if status == 'running' and unused:
            if rate > 0 and days_left is not None and promo.end_date and days_left < left_to_end:
                note = f'At this rate the codes run out in about {days_left} days, before the offer ends.'
            elif rate > 0 and left_to_end:
                left = max(round(unused - rate * left_to_end), 0)
                note = f'About {left} codes may be left unused when the offer ends.'
            elif rate == 0:
                note = f'No code used in the last {BURN_DAYS} days.'
        elif status == 'ended' and unused:
            note = f'{unused} codes expired unused.'
        rows.append({
            'id': pid, 'name': promo.name, 'brand': brand_of(promo), 'terms': terms_of(promo), 'status': status,
            'level': promo.level, 'centre': (promo.center.display_name or promo.center.center_name) if promo.center else 'All centres',
            'start': str(promo.start_date), 'end': str(promo.end_date),
            'codes': total, 'used': used, 'unused': unused, 'void': s['void'], 'used_pct': pct(used, total),
            'used_in_period': n, 'applied': f2(p['applied']) if p else 0, 'wasted': f2(p['wasted']) if p else 0,
            'avg_bill': f2(p['bills'] / n) if p and n else 0, 'customers': len(p['clients']) if p else 0,
            'prev_used': prev.get(pid, {}).get('n', 0), 'used_change': change(n, prev.get(pid, {}).get('n', 0)),
            'brand_owes': f2(owed_all.get(pid, 0)), 'per_day': round(rate, 1), 'days_left': days_left,
            'expired_unused': unused if status == 'ended' else 0, 'note': note,
        })
    rows.sort(key=lambda r: (-r['used_in_period'], r['name']))

    brands = defaultdict(lambda: {'brand': '', 'offers': 0, 'codes': 0, 'used': 0, 'unused': 0, 'applied': 0.0, 'brand_owes': 0.0})
    for r in rows:
        b = brands[(r['brand'] or r['name']).lower()]
        b['brand'] = r['brand'] or r['name']
        b['offers'] += 1
        b['codes'] += r['codes']
        b['used'] += r['used']
        b['unused'] += r['unused']
        b['applied'] += r['applied']
        b['brand_owes'] += r['brand_owes']
    brand_rows = sorted(({**b, 'used_pct': pct(b['used'], b['codes']), 'applied': round(b['applied'], 2),
                          'brand_owes': round(b['brand_owes'], 2)} for b in brands.values()), key=lambda b: -b['brand_owes'])

    applied = sum((v['applied'] for v in per.values()), ZERO)
    prev_applied = sum((dec(v['a']) for v in prev.values()), ZERO)
    prev_n = sum(v['n'] for v in prev.values())
    n_used = sum(v['n'] for v in per.values())
    totals = {
        'offers': len(rows), 'codes': sum(r['codes'] for r in rows), 'used': sum(r['used'] for r in rows),
        'unused': sum(r['unused'] for r in rows), 'void': sum(r['void'] for r in rows),
        'used_in_period': n_used, 'used_prev': prev_n, 'used_change': change(n_used, prev_n),
        'applied': f2(applied), 'applied_prev': f2(prev_applied), 'applied_change': change(applied, prev_applied),
        'wasted': f2(sum((v['wasted'] for v in per.values()), ZERO)),
        'brand_owes': f2(sum(owed_all.values(), ZERO)),
        'customers': len(by_client), 'expired_unused': sum(r['expired_unused'] for r in rows),
        'running_out': sum(1 for r in rows if r['status'] == 'running' and r['days_left'] is not None
                           and r['end'] and (r['days_left'] < ((promos[r['id']].end_date - ctx.today).days if promos[r['id']].end_date else 10 ** 6))),
    }
    first = ctx.first_of_months(12)
    all_months = defaultdict(lambda: {'n': 0, 'applied': ZERO})
    for c in used_codes(ctx, first, ctx.end, ids).values('used_at', 'amount_applied').iterator(chunk_size=3000):
        k = (c['used_at'].year, c['used_at'].month)
        all_months[k]['n'] += 1
        all_months[k]['applied'] += dec(c['amount_applied'])
    trend = [{'month': ctx.month_label(k), 'used': all_months[k]['n'] if k in all_months else 0,
              'applied': f2(all_months[k]['applied']) if k in all_months else 0} for k in ctx.months(12)]
    top = sorted(by_client.values(), key=lambda v: (-v['n'], -v['applied']))[:10]
    return {
        'frame': ctx.frame(), 'enabled': True, 'totals': totals, 'rows': rows, 'brands': brand_rows,
        'by_centre': sorted(({'centre': k, 'used': v['n'], 'applied': f2(v['applied'])} for k, v in centre_rows.items()),
                            key=lambda r: -r['applied']),
        'trend': trend,
        'top_customers': [{**person(v['client']), 'coupons': v['n'], 'applied': f2(v['applied'])} for v in top],
        'note': ('"Brand owes" is the total of the coupons used. The POS does not yet record when a brand pays, '
                 'so settled and pending are not told apart.'),
    }


def _q(**kw):
    from django.db.models import Q
    return Q(**kw)
