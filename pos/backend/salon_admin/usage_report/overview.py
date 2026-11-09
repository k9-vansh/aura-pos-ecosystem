"""Overview: the whole of marketing on one page - what it costs, what it brings, what needs a look."""
from django.db.models import Q, Sum

from billing.models import Invoice, InvoiceItem
from salon_admin.analytics_endpoints import LINE_VALUE, _ct_id, _item_scope

from .base import LIVE, ZERO, cached, change, dec, f2, pct
from .cashback import cashback
from .coupons import coupons
from .customers import KINDS, SALES, _bills, customers, perks
from .forecast import _due
from .offers import offers

SOLD = (('Membership', 'membership', 'Memberships sold'), ('Package', 'package', 'Packages sold'),
        ('ValueCard', 'valuecard', 'Value cards sold'))


def _extra_cost(ctx, start, end):
    """Discounts that come from a membership or a value card (offers and cashback are counted elsewhere)."""
    bills = ctx.by_customer(ctx.bills(start, end), 'client_id')
    member = dec(bills.filter(membership_used__isnull=False).aggregate(t=Sum('discount'))['t']) if ctx.wants('Membership') else ZERO
    items = ctx.by_customer(InvoiceItem.objects.filter(_item_scope(ctx.request, start, end), card_face__gt=0), 'invoice__client_id')
    card = dec(items.aggregate(t=Sum('card_benefit'))['t']) if ctx.wants('ValueCard') else ZERO
    return member, card


def _sold(ctx, start, end):
    out = {}
    for perk, model, _label in SOLD:
        if not ctx.wants(perk):
            out[perk] = {'units': 0, 'value': ZERO}
            continue
        qs = ctx.by_customer(InvoiceItem.objects.filter(
            _item_scope(ctx.request, start, end), content_type_id=_ct_id(model), is_redemption=False), 'invoice__client_id')
        a = qs.aggregate(n=Sum('quantity'), v=LINE_VALUE)
        out[perk] = {'units': int(a['n'] or 0), 'value': dec(a['v'])}
    return out


def overview(ctx):
    # The same answers their own tabs give, so a slow one worked out here is not worked out again there.
    cust = cached(ctx, 'customers', customers)
    off = cached(ctx, 'offers', offers)
    cash = cached(ctx, 'cashback', cashback)
    cou = cached(ctx, 'coupons', coupons)
    ct, ot = cust['totals'], off.get('totals') or {}
    cb, kt = cash.get('totals') or {}, cou.get('totals') or {}

    member, card = _extra_cost(ctx, ctx.start, ctx.end)
    member_p, card_p = _extra_cost(ctx, ctx.prev_start, ctx.prev_end)
    cost = {
        'offers': ot.get('discount', 0), 'cashback': cb.get('earned', 0), 'membership': f2(member), 'card': f2(card),
    }
    cost_prev = {'offers': ot.get('discount_prev', 0), 'cashback': cb.get('earned_prev', 0),
                 'membership': f2(member_p), 'card': f2(card_p)}
    total_cost, total_cost_prev = sum(cost.values()), sum(cost_prev.values())

    sold, sold_prev = _sold(ctx, ctx.start, ctx.end), _sold(ctx, ctx.prev_start, ctx.prev_end)
    sold_rows = [{'perk': perk, 'label': label, 'units': sold[perk]['units'], 'units_prev': sold_prev[perk]['units'],
                  'units_change': change(sold[perk]['units'], sold_prev[perk]['units']),
                  'value': f2(sold[perk]['value']), 'value_prev': f2(sold_prev[perk]['value'])}
                 for perk, _model, label in SOLD]
    sold_value = sum((sold[p]['value'] for p, _m, _l in SOLD), ZERO)
    sold_value_prev = sum((sold_prev[p]['value'] for p, _m, _l in SOLD), ZERO)

    # Sales on bills that used each kind of perk (a bill using two counts under both).
    by_kind, _linked = perks(ctx.start)
    a = _bills(ctx, ctx.start, ctx.end).aggregate(**{k: Sum(SALES, filter=by_kind[k]) for k, _ in KINDS})
    mix = [{'key': k, 'label': label, 'bills': next(x['bills'] for x in cust['kinds'] if x['key'] == k),
            'sales': f2(a[k])} for k, label in KINDS]

    due30 = {row['kind']: row['horizons'][0] for row in _due(ctx) if row['horizons']}
    def many(n, word):
        return f'{n} {word}{"" if n == 1 else "s"}'

    alerts = []
    if ot.get('offers_unused'):
        alerts.append({'level': 'warn', 'section': 'offers', 'text': f'Offers on but unused in these dates: {ot["offers_unused"]}.'})
    if ot.get('cost_pct', 0) > 30:
        alerts.append({'level': 'warn', 'section': 'offers', 'text': f'Offers gave away {ot["cost_pct"]}% of the value of the bills they sat on.'})
    if kt.get('running_out'):
        alerts.append({'level': 'warn', 'section': 'coupons', 'text': f'Coupon offers that will run out of codes before they end: {kt["running_out"]}.'})
    if kt.get('expired_unused'):
        alerts.append({'level': 'info', 'section': 'coupons', 'text': f'{many(kt["expired_unused"], "coupon code")} expired unused.'})
    if cb.get('expiring_30'):
        alerts.append({'level': 'info', 'section': 'cashback', 'text': f'Rs {cb["expiring_30"]:,.0f} of customers\' cashback expires within 30 days.'})
    for kind, row in due30.items():
        if row['count']:
            alerts.append({'level': 'info', 'section': 'forecast', 'text': f'{kind}: {row["count"]} in the next 30 days ({row["unit"]}: {row["amount"]:,.0f}).'})
    if off.get('totals') is not None and ot.get('cancelled'):
        alerts.append({'level': 'info', 'section': 'offers', 'text': f'Bills that used an offer and were cancelled: {ot["cancelled"]}.'})

    head = {
        'linked_bills': ct['linked'], 'linked_bills_prev': None, 'linked_bills_change': ct['linked_change'],
        'bills': ct['bills'], 'linked_pct': ct['linked_pct'], 'linked_pct_prev': ct['linked_pct_prev'],
        'linked_sales': ct['linked_sales'], 'linked_sales_prev': ct['linked_sales_prev'],
        'linked_sales_change': ct['linked_sales_change'], 'linked_sales_pct': ct['linked_sales_pct'],
        'cost': f2(total_cost), 'cost_prev': f2(total_cost_prev), 'cost_change': change(total_cost, total_cost_prev),
        'cost_split': cost, 'sales_per_rupee': round(ct['linked_sales'] / total_cost, 1) if total_cost else None,
        'cost_pct_of_linked': pct(total_cost, ct['linked_sales'] + total_cost),
        'coupons_applied': kt.get('applied', 0), 'coupons_applied_prev': kt.get('applied_prev', 0),
        'coupons_change': kt.get('applied_change'), 'brand_owes': kt.get('brand_owes', 0),
        'cashback_owed': cb.get('owed_now', 0),
        'sold_value': f2(sold_value), 'sold_value_prev': f2(sold_value_prev), 'sold_change': change(sold_value, sold_value_prev),
        'new_customers': ct['new_customers'], 'new_with_perk': ct['new_with_perk'], 'new_with_perk_pct': ct['new_with_perk_pct'],
        'avg_linked': ct['avg_linked'], 'avg_other': ct['avg_other'], 'uplift_pct': ct['uplift_pct'],
    }
    cost_trend = {r['month']: r for r in (off.get('trend') or [])}
    cash_trend = {r['month']: r for r in (cash.get('trend') or [])}
    trend = [{'month': t['month'], 'linked_sales': t['linked_sales'], 'sales': t['sales'],
              'cost': round(cost_trend.get(t['month'], {}).get('discount', 0) + cash_trend.get(t['month'], {}).get('earned', 0), 2)}
             for t in cust['trend']]
    return {
        'frame': ctx.frame(), 'enabled': True, 'head': head, 'sold': sold_rows, 'mix': mix, 'trend': trend,
        'top_offers': (off.get('rows') or [])[:5], 'worst_offers': sorted(
            [r for r in (off.get('rows') or []) if r['verdict'] != 'Working'], key=lambda r: -r['cost'])[:5],
        'alerts': alerts, 'filters_note': ctx.filters_text(),
        'note': ('"Cost" is the discount given by offers, memberships and value cards plus the cashback credited. Brand coupons '
                 'are paid for by the brand, so they are shown apart. Sales are before GST and after discount.'),
    }
