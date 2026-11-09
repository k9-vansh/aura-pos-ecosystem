"""Cashback: given, spent, expired, and what is still owed to customers."""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.db.models.functions import ExtractMonth, ExtractYear

from billing.models import CashbackTransaction, Payment
from marketing.models import PromotionUsage

from .base import LIVE, ZERO, change, dec, f2, pct, person

EARNED = Q(kind='earned') | Q(kind='', amount__gt=0)
SPENT = Q(kind='spent') | Q(kind='', amount__lt=0)


def ledger(ctx):
    """The cashback ledger in the centres in scope, for the customers the filter names."""
    qs = ctx.centre_or_client_centre(CashbackTransaction.objects.all())
    return ctx.by_customer(qs)


def _window(qs, start, end):
    return qs.filter(created_at__gte=start, created_at__lt=end + timedelta(days=1))


def flows(qs):
    """What a window of the ledger holds: earned, spent, expired, reversed."""
    a = qs.aggregate(
        earned=Sum('amount', filter=EARNED), spent=Sum('amount', filter=SPENT),
        expired=Sum('amount', filter=Q(kind='expired')),
        clawed=Sum('amount', filter=Q(kind='reversal', amount__lt=0)),
        returned=Sum('amount', filter=Q(kind='reversal', amount__gt=0)))
    return {'earned': dec(a['earned']), 'spent': -dec(a['spent']), 'expired': -dec(a['expired']),
            'clawed_back': -dec(a['clawed']), 'spend_returned': dec(a['returned'])}


def cashback(ctx):
    if not ctx.wants('Cashback'):
        return {'frame': ctx.frame(), 'enabled': False, 'totals': None}
    today = ctx.today
    base = ledger(ctx)
    cur, prev = flows(_window(base, ctx.start, ctx.end)), flows(_window(base, ctx.prev_start, ctx.prev_end))
    net_spent = max(cur['spent'] - cur['spend_returned'], ZERO)

    # What customers still hold, by when it expires.
    holding = base.filter(Q(kind='earned') | Q(kind=''), remaining__gt=0)
    buckets = [('Overdue (past its date)', None, -1), ('Next 7 days', 0, 7), ('8 to 30 days', 8, 30),
               ('31 to 90 days', 31, 90), ('More than 90 days', 91, 10 ** 6)]
    ageing = []
    for label, lo, hi in buckets:
        q = holding.filter(expires_on__isnull=False)
        if lo is None:
            q = q.filter(expires_on__lt=today)
        else:
            q = q.filter(expires_on__gte=today + timedelta(days=lo), expires_on__lte=today + timedelta(days=min(hi, 36500)))
        a = q.aggregate(t=Sum('remaining'), n=Count('id'), c=Count('client', distinct=True))
        ageing.append({'label': label, 'amount': f2(a['t']), 'credits': a['n'], 'customers': a['c']})
    never = holding.filter(expires_on__isnull=True).aggregate(t=Sum('remaining'), n=Count('id'), c=Count('client', distinct=True))
    ageing.append({'label': 'Never expires', 'amount': f2(never['t']), 'credits': never['n'], 'customers': never['c']})
    held = holding.aggregate(t=Sum('remaining'), c=Count('client', distinct=True))
    soon = holding.filter(expires_on__gte=today, expires_on__lte=today + timedelta(days=30)).aggregate(t=Sum('remaining'))

    holders = defaultdict(lambda: {'amount': ZERO, 'soon': ZERO, 'client': None})
    for r in holding.select_related('client').order_by('-remaining')[:3000]:
        h = holders[r.client_id]
        h['amount'] += dec(r.remaining)
        h['client'] = r.client
        if r.expires_on and r.expires_on <= today + timedelta(days=30):
            h['soon'] += dec(r.remaining)
    top_holders = [{**person(v['client']), 'balance': f2(v['amount']), 'expiring_30': f2(v['soon'])}
                   for v in sorted(holders.values(), key=lambda v: -v['amount'])[:10]]

    # Bills paid with the cashback wallet.
    paid = ctx.by_customer(Payment.objects.filter(
        payment_method=Payment.CASHBACK_WALLET, is_tip=False, invoice__status__in=LIVE,
        invoice__created_at__gte=ctx.start, invoice__created_at__lt=ctx.end + timedelta(days=1)), 'invoice__client_id')
    if ctx.ids is not None:
        paid = paid.filter(invoice__center_id__in=ctx.ids)
    pa = paid.aggregate(amount=Sum('amount'), bills=Count('invoice', distinct=True), billed=Sum('invoice__total_amount'),
                        customers=Count('invoice__client', distinct=True))
    pa_prev = ctx.in_centres(Payment.objects.filter(
        payment_method=Payment.CASHBACK_WALLET, is_tip=False, invoice__status__in=LIVE,
        invoice__created_at__gte=ctx.prev_start, invoice__created_at__lt=ctx.prev_end + timedelta(days=1)),
        'invoice__center_id').aggregate(amount=Sum('amount'))

    # Which campaigns gave it.
    campaigns = []
    given = ctx.by_customer(ctx.in_centres(PromotionUsage.objects.filter(
        cashback_awarded__gt=0, is_reversed=False, invoice__status__in=LIVE, date__gte=ctx.start, date__lte=ctx.end)))
    for r in given.values('promotion__name').annotate(n=Count('id'), t=Sum('cashback_awarded'), after=Sum('bill_amount_after'),
                                                      c=Count('client', distinct=True)).order_by('-t')[:15]:
        campaigns.append({'campaign': r['promotion__name'], 'bills': r['n'], 'customers': r['c'], 'cashback': f2(r['t']),
                          'of_sales_pct': pct(r['t'], r['after'])})

    by_centre = []
    window = _window(base, ctx.start, ctx.end)
    for r in window.values('center__display_name', 'center__center_name').annotate(
            earned=Sum('amount', filter=EARNED), spent=Sum('amount', filter=SPENT), expired=Sum('amount', filter=Q(kind='expired'))):
        by_centre.append({'centre': r['center__display_name'] or r['center__center_name'] or 'No centre',
                          'earned': f2(r['earned']), 'spent': f2(-dec(r['spent'])), 'expired': f2(-dec(r['expired']))})
    by_centre.sort(key=lambda r: -r['earned'])

    monthly = defaultdict(lambda: {'earned': ZERO, 'spent': ZERO, 'expired': ZERO})
    first = ctx.first_of_months(12)
    for r in _window(base, first, ctx.end).annotate(y=ExtractYear('created_at'), m=ExtractMonth('created_at')).values(
            'y', 'm', 'kind').annotate(pos=Sum('amount', filter=Q(amount__gt=0)), neg=Sum('amount', filter=Q(amount__lt=0))):
        m = monthly[(r['y'], r['m'])]
        if r['kind'] == 'expired':
            m['expired'] += -dec(r['neg'])
        elif r['kind'] == 'reversal':
            continue
        elif r['kind'] == 'spent' or (r['kind'] == '' and dec(r['neg'])):
            m['spent'] += -dec(r['neg'])
        if r['kind'] in ('earned', ''):
            m['earned'] += dec(r['pos'])
    trend = [{'month': ctx.month_label(k), 'earned': f2(monthly[k]['earned']), 'spent': f2(monthly[k]['spent']),
              'expired': f2(monthly[k]['expired'])} if k in monthly else
             {'month': ctx.month_label(k), 'earned': 0, 'spent': 0, 'expired': 0} for k in ctx.months(12)]

    totals = {
        'earned': f2(cur['earned']), 'earned_prev': f2(prev['earned']), 'earned_change': change(cur['earned'], prev['earned']),
        'spent': f2(net_spent), 'spent_prev': f2(max(prev['spent'] - prev['spend_returned'], ZERO)),
        'spent_change': change(net_spent, max(prev['spent'] - prev['spend_returned'], ZERO)),
        'expired': f2(cur['expired']), 'expired_prev': f2(prev['expired']), 'clawed_back': f2(cur['clawed_back']),
        'used_pct': pct(net_spent, cur['earned']),
        'owed_now': f2(held['t']), 'holders': held['c'] or 0, 'expiring_30': f2(soon['t']),
        'wallet_bills': pa['bills'] or 0, 'wallet_paid': f2(pa['amount']), 'wallet_paid_prev': f2(pa_prev['amount']),
        'wallet_paid_change': change(pa['amount'], pa_prev['amount']),
        'wallet_avg_bill': f2(dec(pa['billed']) / pa['bills']) if pa['bills'] else 0,
        'wallet_customers': pa['customers'] or 0,
    }
    return {'frame': ctx.frame(), 'enabled': True, 'totals': totals, 'ageing': ageing, 'top_holders': top_holders,
            'campaigns': campaigns, 'by_centre': by_centre, 'trend': trend,
            'note': ('"Owed now" is the cashback customers can still spend: a liability of the business. '
                     'Spent is net of bills cancelled afterwards.')}
