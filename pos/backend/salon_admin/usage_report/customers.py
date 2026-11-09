"""Customers: who marketing brings in and brings back, and what an offer does to a bill.

A bill is "marketing-linked" when it used any perk: a promotion, a membership, a brand
coupon (Gift Card), a value card, a package session or cashback.
"""
from collections import defaultdict
from datetime import timedelta
from functools import reduce
from operator import or_

from django.db.models import Count, F, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from billing.models import Invoice, InvoiceItem, Payment
from marketing.models import CouponCode, PromotionUsage
from salon_admin.dashboard_endpoints import _apply_security
from salon_admin.models import Center

from .base import LIVE, ZERO, change, dec, f2, pct, person

SALES = F('subtotal') - F('discount')
KINDS = (('promo', 'Offer / promotion'), ('member', 'Membership discount'), ('coupon', 'Brand coupon'),
         ('card', 'Value card'), ('pack', 'Package session'), ('cash', 'Cashback wallet'))


def perks(floor):
    """How to tell which bills (made on or after `floor`) used each kind of perk.

    Returns ({kind: Q}, linked_Q). The three kinds that live on other rows are `id IN (select ...)`
    conditions that do not refer to the bill outside them, so the database builds each list once and
    looks every bill up in it. (The same test written as one EXISTS per bill ran a query per bill:
    about 18 seconds for a year of 400,000 bills.)"""
    def paid_by(method):
        # A payment is written when its bill is completed, so a bill made on or after `floor` has its
        # payments on or after `floor` too: the (method, date) index answers this without touching bills.
        return Payment.objects.filter(payment_method=method, created_at__gte=floor).values('invoice_id')

    def items(**where):
        return InvoiceItem.objects.filter(invoice__created_at__gte=floor, **where).values('invoice_id')

    q = {
        'promo': Q(promotion__isnull=False),
        'member': Q(membership_used__isnull=False),
        'coupon': Q(id__in=paid_by(Payment.GIFT_CARD)),
        'card': Q(id__in=items(card_face__gt=0)),
        'pack': Q(id__in=items(is_redemption=True)),
        'cash': Q(id__in=paid_by(Payment.CASHBACK_WALLET)),
    }
    return q, reduce(or_, q.values())


def _bills(ctx, start, end):
    return ctx.by_customer(ctx.bills(start, end), 'client_id')


def stats(qs, floor):
    kinds, linked_q = perks(floor)
    a = qs.aggregate(
        bills=Count('id'), sales=Sum(SALES), linked=Count('id', filter=linked_q), linked_sales=Sum(SALES, filter=linked_q),
        **{k: Count('id', filter=kinds[k]) for k, _ in KINDS})
    bills, linked = a['bills'] or 0, a['linked'] or 0
    sales, linked_sales = dec(a['sales']), dec(a['linked_sales'])
    return {
        'bills': bills, 'linked': linked, 'linked_pct': pct(linked, bills), 'sales': sales, 'linked_sales': linked_sales,
        'linked_sales_pct': pct(linked_sales, sales),
        'avg_linked': linked_sales / linked if linked else ZERO,
        'avg_other': (sales - linked_sales) / (bills - linked) if bills > linked else ZERO,
        'kinds': {k: a[k] or 0 for k, _ in KINDS},
    }


def per_customer(qs, linked):
    """{customer id: how many of their bills in qs used a perk}, bills with no customer left out.

    One grouped read instead of one "how many different customers" query for each question."""
    return {r['client']: r['used'] for r in
            qs.exclude(client__isnull=True).values('client').annotate(used=Count('id', filter=linked))}


def customers(ctx):
    cur = stats(_bills(ctx, ctx.start, ctx.end), ctx.start)
    prev = stats(_bills(ctx, ctx.prev_start, ctx.prev_end), ctx.prev_start)
    this = _bills(ctx, ctx.start, ctx.end)
    kinds, linked = perks(min(ctx.prev_start, ctx.today - timedelta(days=180)))

    # One pass over each period's bills, grouped by customer, answers every "how many people" below.
    in_this = per_customer(this, linked)
    fresh = ctx.new_ids()
    new_n = len(fresh)
    new_with_perk = sum(1 for c, used in in_this.items() if used and c in fresh)
    people = len(in_this)
    linked_people = sum(1 for used in in_this.values() if used)

    # Do customers who used a perk last period come back, compared with the ones who did not?
    before = per_customer(_bills(ctx, ctx.prev_start, ctx.prev_end), linked)
    used_before = {c for c, used in before.items() if used}
    plain_before = set(before) - used_before
    # "Came back" = a bill in the centres in scope this period, whatever the customer filter says.
    back_ids = set(in_this) if ctx.customer == 'all' else set(
        ctx.bills().exclude(client__isnull=True).values_list('client', flat=True).distinct())
    cohorts = []
    for label, cohort in (('Used an offer or perk', used_before), ('Did not', plain_before)):
        size, back = len(cohort), len(cohort & back_ids)
        cohorts.append({'label': label, 'customers': size, 'came_back': back, 'came_back_pct': pct(back, size)})

    # Of this period's customers, those who only ever come with an offer (last 180 days, at least three bills).
    # Every customer's last 180 days is read once (not once more per customer in the period) and matched here.
    window = _apply_security(ctx.request, Invoice.objects.filter(
        status__in=LIVE, client__isnull=False, created_at__gte=ctx.today - timedelta(days=180)))
    regulars = only_offers = 0
    for r in window.values('client').annotate(n=Count('id'), k=Count('id', filter=linked)).filter(n__gte=3):
        if r['client'] in in_this:
            regulars += 1
            if r['n'] == r['k']:
                only_offers += 1

    # Who got the most out of marketing in the period.
    got = defaultdict(lambda: {'discount': ZERO, 'cashback': ZERO, 'coupons': ZERO, 'client': None})
    usage = ctx.by_customer(ctx.in_centres(PromotionUsage.objects.filter(
        is_reversed=False, invoice__status__in=LIVE, date__gte=ctx.start, date__lte=ctx.end, client__isnull=False)))
    for r in usage.select_related('client').iterator(chunk_size=2000):
        g = got[r.client_id]
        g['client'] = r.client
        g['discount'] += max(dec(r.bill_amount_before) - dec(r.bill_amount_after), ZERO)
        g['cashback'] += dec(r.cashback_awarded)
    coupons_used = ctx.by_customer(ctx.in_centres(CouponCode.objects.filter(
        is_used=True, invoice__status__in=LIVE, used_at__gte=ctx.start, used_at__lt=ctx.end + timedelta(days=1),
        client__isnull=False)))
    for r in coupons_used.select_related('client').iterator(chunk_size=2000):
        g = got[r.client_id]
        g['client'] = r.client
        g['coupons'] += dec(r.amount_applied)
    top = sorted(got.values(), key=lambda g: -(g['discount'] + g['cashback'] + g['coupons']))[:10]

    by_centre = []
    _k, linked_now = perks(ctx.start)
    # Grouped by the centre's id, then named: grouping by the names joins the centre table for every bill.
    rows = list(this.values('center').annotate(
        bills=Count('id'), linked=Count('id', filter=linked_now), sales=Sum(SALES), linked_sales=Sum(SALES, filter=linked_now)))
    names = {c.id: c.display_name or c.center_name for c in Center.objects.filter(id__in=[r['center'] for r in rows if r['center']])}
    for r in rows:
        by_centre.append({'centre': names.get(r['center']) or 'No centre', 'bills': r['bills'],
                          'linked': r['linked'], 'linked_pct': pct(r['linked'], r['bills']),
                          'linked_sales': f2(r['linked_sales']), 'sales': f2(r['sales'])})
    by_centre.sort(key=lambda r: (-r['linked_sales'], r['centre']))

    first = ctx.first_of_months(12)
    _k, linked_year = perks(first)
    monthly = {(r['y'], r['m']): r for r in _bills(ctx, first, ctx.end).annotate(
        y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values('y', 'm').annotate(
        bills=Count('id'), linked=Count('id', filter=linked_year), sales=Sum(SALES), linked_sales=Sum(SALES, filter=linked_year))}
    trend = [{'month': ctx.month_label(k), 'bills': monthly[k]['bills'] if k in monthly else 0,
              'linked': monthly[k]['linked'] if k in monthly else 0,
              'sales': f2(monthly[k]['sales']) if k in monthly else 0,
              'linked_sales': f2(monthly[k]['linked_sales']) if k in monthly else 0} for k in ctx.months(12)]

    totals = {
        'bills': cur['bills'], 'linked': cur['linked'], 'linked_pct': cur['linked_pct'],
        'linked_pct_prev': prev['linked_pct'], 'linked_change': change(cur['linked'], prev['linked']),
        'linked_sales': f2(cur['linked_sales']), 'linked_sales_prev': f2(prev['linked_sales']),
        'linked_sales_change': change(cur['linked_sales'], prev['linked_sales']), 'linked_sales_pct': cur['linked_sales_pct'],
        'avg_linked': f2(cur['avg_linked']), 'avg_other': f2(cur['avg_other']),
        'uplift_pct': change(cur['avg_linked'], cur['avg_other']),
        'customers': people, 'customers_with_perk': linked_people,
        'new_customers': new_n, 'new_with_perk': new_with_perk, 'new_with_perk_pct': pct(new_with_perk, new_n),
        'regulars': regulars, 'only_with_offers': only_offers, 'only_with_offers_pct': pct(only_offers, regulars),
    }
    return {
        'frame': ctx.frame(), 'enabled': True, 'totals': totals,
        'kinds': [{'key': k, 'label': label, 'bills': cur['kinds'][k], 'bills_prev': prev['kinds'][k],
                   'change': change(cur['kinds'][k], prev['kinds'][k])} for k, label in KINDS],
        'cohorts': cohorts, 'by_centre': by_centre, 'trend': trend,
        'top_customers': [{**person(g['client']), 'discount': f2(g['discount']), 'cashback': f2(g['cashback']),
                           'coupons': f2(g['coupons']), 'total': f2(g['discount'] + g['cashback'] + g['coupons'])} for g in top],
        'note': ('A bill is marketing-linked when it used an offer, membership, brand coupon, value card, package '
                 'session or cashback. "New" means the customer\'s first bill in the centres chosen falls in the period.'),
    }
