"""Offers: discounts, flat prices, trigger deals and cashback campaigns, one row each."""
from collections import Counter

from django.db.models import Count, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from marketing.models import PromotionUsage

from .base import LIVE, OFFER_TYPES, ZERO, change, dec, f2, pct, promo_status, terms_of

LOW_USE = 5            # fewer bills than this in the period: "Low use"
HIGH_COST_PCT = 30     # discount + cashback above this share of the bill value: "High cost"


def usage_rows(ctx, types, start, end, reversed_rows=False):
    """The promotion-use rows of the period that still stand (or, for the cancelled count,
    the ones a cancelled bill reversed), under every filter."""
    qs = PromotionUsage.objects.filter(promotion__promo_type__in=types, is_reversed=reversed_rows,
                                       date__gte=start, date__lte=end)
    if not reversed_rows:
        qs = qs.filter(invoice__status__in=LIVE)
    return ctx.by_customer(ctx.in_centres(qs))


def sums(qs, *group):
    return qs.values(*group).annotate(
        uses=Count('id'), clients=Count('client', distinct=True), before=Sum('bill_amount_before'),
        after=Sum('bill_amount_after'), cashback=Sum('cashback_awarded'))


def _discount(row):
    return max(dec(row['before']) - dec(row['after']), ZERO)


def block(rows):
    """Headline figures of some per-offer rows."""
    uses = sum(r['uses'] for r in rows)
    before = sum((dec(r['before']) for r in rows), ZERO)
    after = sum((dec(r['after']) for r in rows), ZERO)
    discount = sum((_discount(r) for r in rows), ZERO)
    cashback = sum((dec(r['cashback']) for r in rows), ZERO)
    return {'bills': uses, 'sales_before': before, 'net_sales': after, 'discount': discount, 'cashback': cashback,
            'cost': discount + cashback}


def offers(ctx):
    types = [t for t in OFFER_TYPES if ctx.wants(t)]
    if not types:
        return {'frame': ctx.frame(), 'enabled': False, 'rows': [], 'totals': None}

    promos = {p.id: p for p in ctx.promotions().filter(promo_type__in=types) if ctx.keep_promotion(p)}
    narrow = (lambda qs: qs.filter(promotion_id__in=list(promos))) if len(promos) <= 900 else (lambda qs: qs)

    this = narrow(usage_rows(ctx, types, ctx.start, ctx.end))
    cur = {r['promotion']: r for r in sums(this, 'promotion')}
    prev = {r['promotion']: r for r in sums(narrow(usage_rows(ctx, types, ctx.prev_start, ctx.prev_end)), 'promotion')}
    # Who is a new customer is worked out here, from one small read of the offers' own use rows, rather than
    # by putting the (long) list of new customers through the database once for every question.
    pairs = set(this.values_list('promotion', 'client').distinct())
    new = ctx.new_ids()
    fresh = Counter(p for p, c in pairs if c in new)
    cancelled = dict(narrow(usage_rows(ctx, types, ctx.start, ctx.end, reversed_rows=True)).values('promotion')
                     .annotate(c=Count('id')).values_list('promotion', 'c'))

    rows, unused = [], []
    for pid, promo in promos.items():
        status = promo_status(promo, ctx.today)
        overlaps = (not promo.start_date or promo.start_date <= ctx.end) and (not promo.end_date or promo.end_date >= ctx.start)
        row = cur.get(pid)
        if row is None:
            if overlaps and status in ('running', 'ended'):
                unused.append({'id': pid, 'name': promo.name, 'type': promo.promo_type, 'terms': terms_of(promo),
                               'status': status, 'start': str(promo.start_date), 'end': str(promo.end_date)})
            continue
        b = block([row])
        before_uses = prev.get(pid, {}).get('uses', 0)
        cost_pct = pct(b['cost'], b['sales_before'])
        if row['uses'] < LOW_USE:
            verdict = 'Low use'
        elif cost_pct > HIGH_COST_PCT:
            verdict = 'High cost'
        else:
            verdict = 'Working'
        rows.append({
            'id': pid, 'name': promo.name, 'type': promo.promo_type, 'terms': terms_of(promo),
            'level': promo.level, 'centre': (promo.center.display_name or promo.center.center_name) if promo.center else 'All centres',
            'start': str(promo.start_date), 'end': str(promo.end_date), 'status': status,
            'bills': row['uses'], 'customers': row['clients'], 'new_customers': fresh.get(pid, 0),
            'sales_before': f2(b['sales_before']), 'discount': f2(b['discount']), 'cashback': f2(b['cashback']),
            'cost': f2(b['cost']), 'net_sales': f2(b['net_sales']), 'cost_pct': cost_pct,
            'sales_per_rupee': round(float(b['net_sales'] / b['cost']), 1) if b['cost'] > 0 else None,
            'avg_bill': f2(b['net_sales'] / row['uses']) if row['uses'] else 0,
            'prev_bills': before_uses, 'bills_change': change(row['uses'], before_uses),
            'cancelled': cancelled.get(pid, 0), 'limit_per_client': promo.max_usage_per_client,
            'verdict': verdict,
        })
    rows.sort(key=lambda r: -r['net_sales'])

    this_b, prev_b = block([cur[p] for p in cur if p in promos]), block([prev[p] for p in prev if p in promos])
    totals = {
        'bills': this_b['bills'], 'bills_prev': prev_b['bills'], 'bills_change': change(this_b['bills'], prev_b['bills']),
        'net_sales': f2(this_b['net_sales']), 'net_sales_prev': f2(prev_b['net_sales']),
        'net_sales_change': change(this_b['net_sales'], prev_b['net_sales']),
        'discount': f2(this_b['discount']), 'discount_prev': f2(prev_b['discount']),
        'discount_change': change(this_b['discount'], prev_b['discount']),
        'cashback': f2(this_b['cashback']), 'cashback_prev': f2(prev_b['cashback']),
        'cost': f2(this_b['cost']), 'cost_prev': f2(prev_b['cost']), 'cost_change': change(this_b['cost'], prev_b['cost']),
        'cost_pct': pct(this_b['cost'], this_b['sales_before']),
        'customers': len({c for _p, c in pairs}),
        'new_customers': len({c for _p, c in pairs if c in new}),
        'cancelled': sum(cancelled.get(p, 0) for p in promos),
        'offers_running': sum(1 for p in promos.values() if promo_status(p, ctx.today) == 'running'),
        'offers_unused': len(unused),
    }

    by_centre = []
    for r in sums(this, 'center', 'center__display_name', 'center__center_name').order_by('-after'):
        b = block([r])
        by_centre.append({'centre': r['center__display_name'] or r['center__center_name'] or 'No centre',
                          'bills': r['uses'], 'customers': r['clients'], 'discount': f2(b['discount']),
                          'cashback': f2(b['cashback']), 'net_sales': f2(b['net_sales'])})

    first = ctx.first_of_months(12)
    monthly = {}
    trend_qs = narrow(usage_rows(ctx, types, first, ctx.end))
    for r in sums(trend_qs.annotate(y=ExtractYear('date'), m=ExtractMonth('date')), 'y', 'm'):
        monthly[(r['y'], r['m'])] = r
    trend = []
    for key in ctx.months(12):
        r = monthly.get(key)
        b = block([r]) if r else block([])
        trend.append({'month': ctx.month_label(key), 'bills': b['bills'], 'discount': f2(b['discount']),
                      'cashback': f2(b['cashback']), 'net_sales': f2(b['net_sales'])})

    mix = {}
    for r in rows:
        m = mix.setdefault(r['type'], {'type': r['type'], 'offers': 0, 'bills': 0, 'cost': 0.0, 'net_sales': 0.0})
        m['offers'] += 1
        m['bills'] += r['bills']
        m['cost'] += r['cost']
        m['net_sales'] += r['net_sales']
    return {
        'frame': ctx.frame(), 'enabled': True, 'totals': totals, 'rows': rows, 'unused': unused,
        'by_centre': by_centre, 'trend': trend,
        'by_type': [{**m, 'cost': round(m['cost'], 2), 'net_sales': round(m['net_sales'], 2)} for m in mix.values()],
    }
