"""Verify: the numbers in this report checked against each other.

Every check compares two figures that must agree, shows both and the gap, and lists up to
ten of the rows that disagree so someone can open them. The checks use the centres,
dates and customer filter in force; balances (a card's balance, cashback owed) are checked
whole, for every customer who has a row in scope.
"""
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db.models import Count, F, Q, Sum
from django.utils import timezone

from billing.models import CashbackTransaction, Invoice, InvoiceItem, Payment
from clients.models import ClientPackage, ClientValueCard
from marketing.models import CouponCode, PromotionUsage
from salon_admin.analytics_endpoints import _by_selling_centre, _ct_id

from .base import LIVE, ZERO, dec, f2
from .cashback import ledger
from .customers import perks
from .packages import left_in, sessions_in

TOL = Decimal('0.05')
SAMPLES = 10
SAP_GRACE_MINUTES = 30


def rs(x):
    return f'{dec(x):.2f}'


def _bill(row):
    return row.get('invoice_number') or f'#{row["id"]}'


def check(key, title, detail, expected=None, actual=None, bad=None, samples=None, info=False, unit='Rs'):
    """One result. Money checks pass when the two figures are within five paise; count checks when `bad` is 0."""
    if info:
        status = 'info'
    elif bad is not None:
        status = 'ok' if bad == 0 else 'fail'
    else:
        status = 'ok' if abs(dec(expected) - dec(actual)) <= TOL else 'fail'
    return {
        'key': key, 'title': title, 'detail': detail, 'status': status, 'unit': unit,
        'expected': f2(expected) if expected is not None else None, 'actual': f2(actual) if actual is not None else None,
        'difference': f2(dec(actual) - dec(expected)) if expected is not None and actual is not None else None,
        'bad': bad, 'samples': (samples or [])[:SAMPLES],
    }


def verify(ctx):
    start, end = ctx.start, ctx.end
    until = end + timedelta(days=1)
    live_bills = ctx.by_customer(ctx.bills(start, end), 'client_id')
    results = []

    # ── Brand coupons ───────────────────────────────────────────────────
    codes_used = ctx.by_customer(ctx.in_centres(CouponCode.objects.filter(
        is_used=True, invoice__status__in=LIVE, used_at__gte=start, used_at__lt=until)))
    gift = ctx.by_customer(ctx.in_centres(Payment.objects.filter(
        payment_method=Payment.GIFT_CARD, is_tip=False, invoice__status__in=LIVE, created_at__gte=start, created_at__lt=until),
        'invoice__center_id'), 'invoice__client_id')
    results.append(check(
        'coupon_money', 'Coupons used = Gift Card payments',
        'The amount the used coupon codes paid, against the Gift Card payments taken on bills in the same dates.',
        expected=gift.aggregate(t=Sum('amount'))['t'], actual=codes_used.aggregate(t=Sum('amount_applied'))['t']))

    cancelled_used = list(CouponCode.objects.filter(is_used=True, invoice__status__in=Invoice.VOID_STATUSES).values(
        'code', 'invoice__invoice_number', 'invoice_id')[:200])
    stuck = list(CouponCode.objects.filter(is_used=False, invoice__isnull=False).values('code', 'invoice_id')[:200])
    no_payment = list(CouponCode.objects.filter(is_used=True, payment__isnull=True).values('code', 'invoice_id')[:200])
    no_code = list(gift.filter(coupon_codes__isnull=True).values('id', 'invoice_id', 'reference')[:200])
    bad = len(cancelled_used) + len(stuck) + len(no_payment) + len(no_code)
    samples = ([f'{c["code"]}: used on a cancelled bill (#{c["invoice_id"]})' for c in cancelled_used]
               + [f'{c["code"]}: marked unused but still attached to bill #{c["invoice_id"]}' for c in stuck]
               + [f'{c["code"]}: used but no payment recorded' for c in no_payment]
               + [f'Gift Card payment {c["id"]} on bill #{c["invoice_id"]} has no coupon code ({c["reference"] or "no reference"})' for c in no_code])
    results.append(check(
        'coupon_links', 'Every coupon is tied to one live bill',
        'A used code must sit on a bill that stands and a Gift Card payment; a cancelled bill must have released its code; '
        'every Gift Card payment must carry a code. This counts the rows that break one of those.', bad=bad, samples=samples, unit='rows'))

    # ── Offers ──────────────────────────────────────────────────────────
    promo_bills = live_bills.filter(promotion__isnull=False)
    usage = ctx.by_customer(ctx.in_centres(PromotionUsage.objects.filter(
        is_reversed=False, invoice__status__in=LIVE, invoice_id__in=promo_bills.values('id'))))
    by_bill = {r['invoice']: max(dec(r['b']) - dec(r['a']), ZERO) for r in usage.values('invoice').annotate(
        b=Sum('bill_amount_before'), a=Sum('bill_amount_after'))}
    off, samples = [], []
    for r in promo_bills.values('id', 'invoice_number', 'discount'):
        got = by_bill.get(r['id'])
        if got is None:
            off.append(r)
            samples.append(f'Bill {_bill(r)}: carries an offer but no usage was recorded')
        elif abs(dec(r['discount']) - got) > TOL:
            off.append(r)
            samples.append(f'Bill {_bill(r)}: bill discount {rs(r["discount"])} but the offer recorded {rs(got)}')
    results.append(check(
        'promo_discount', 'Offer discount on bills = discount recorded by the offer',
        'For each bill that used an offer: the discount on the bill against what the offer recorded. A gap means a bill '
        'was changed after the offer was applied, or the offer was not recorded.',
        expected=promo_bills.aggregate(t=Sum('discount'))['t'], actual=sum(by_bill.values(), ZERO), bad=len(off), samples=samples, unit='bills'))
    # check() prefers `bad`; show the money gap too.
    results[-1]['expected'] = f2(promo_bills.aggregate(t=Sum('discount'))['t'])
    results[-1]['actual'] = f2(sum(by_bill.values(), ZERO))
    results[-1]['difference'] = f2(dec(results[-1]['actual']) - dec(results[-1]['expected']))

    stale = list(PromotionUsage.objects.filter(is_reversed=False, invoice__status__in=Invoice.VOID_STATUSES).values(
        'id', 'promotion__name', 'invoice_id')[:200])
    results.append(check(
        'promo_cancelled', 'Cancelled bills leave no live offer use',
        'An offer used on a bill that was later cancelled must be marked reversed (so the customer\'s limit is given back and '
        'it stops counting).', bad=len(stale), unit='rows',
        samples=[f'{s["promotion__name"]}: still counted on cancelled bill #{s["invoice_id"]}' for s in stale]))

    # ── Cashback ────────────────────────────────────────────────────────
    scoped = ledger(ctx)
    wallet = ctx.by_customer(ctx.in_centres(Payment.objects.filter(
        payment_method=Payment.CASHBACK_WALLET, is_tip=False, invoice__status__in=LIVE, created_at__gte=start, created_at__lt=until),
        'invoice__center_id'), 'invoice__client_id')
    spent_rows = scoped.filter(Q(kind='spent') | Q(kind='', amount__lt=0), invoice__status__in=LIVE,
                               created_at__gte=start, created_at__lt=until)
    results.append(check(
        'cashback_spend', 'Cashback spent = cashback-wallet payments',
        'The cashback taken off customers\' wallets, against the Cashback Wallet payments on bills in the same dates.',
        expected=wallet.aggregate(t=Sum('amount'))['t'], actual=-dec(spent_rows.aggregate(t=Sum('amount'))['t'])))

    given = ctx.by_customer(ctx.in_centres(PromotionUsage.objects.filter(
        is_reversed=False, invoice__status__in=LIVE, date__gte=start, date__lte=end, cashback_awarded__gt=0)))
    earned_rows = scoped.filter(Q(kind='earned') | Q(kind='', amount__gt=0), invoice__status__in=LIVE,
                                created_at__gte=start, created_at__lt=until)
    results.append(check(
        'cashback_earned', 'Cashback credited = cashback the offers awarded',
        'The cashback written to customers\' wallets, against what the cashback offers recorded as awarded on the same bills.',
        expected=given.aggregate(t=Sum('cashback_awarded'))['t'], actual=earned_rows.aggregate(t=Sum('amount'))['t']))

    in_scope = scoped.values('client')
    rows = (CashbackTransaction.objects.filter(client__in=in_scope).values('client')
            .annotate(total=Sum('amount'), left=Sum('remaining', filter=Q(kind='earned')), legacy=Count('id', filter=Q(kind=''))))
    off, samples, legacy = 0, [], 0
    for r in rows:
        if r['legacy']:
            legacy += 1
            continue
        if abs(dec(r['total']) - dec(r['left'])) > TOL:
            off += 1
            samples.append(f'Customer #{r["client"]}: wallet balance {rs(r["total"])} but credits still hold {rs(r["left"])}')
    detail = ('For each customer: the wallet balance (every row added up) against what their cashback credits still hold. '
              'These must be equal.')
    if legacy:
        detail += f' {legacy} customers with old-style rows were left out.'
    results.append(check('cashback_balance', 'Cashback balances add up', detail, bad=off, samples=samples, unit='customers'))

    # ── Value cards ─────────────────────────────────────────────────────
    cards = ctx.by_customer(_by_selling_centre(ctx.request, ClientValueCard.objects.filter(
        face_value__isnull=False, source_invoice__status__in=LIVE)))
    spent = {r['value_card_id']: dec(r['t']) for r in Payment.objects.filter(
        value_card_id__isnull=False, invoice__status__in=LIVE, card_face__isnull=False).values('value_card_id').annotate(t=Sum('card_face'))}
    off, samples = 0, []
    for c in cards.values('id', 'face_value', 'balance', 'forfeited_amount'):
        left = dec(c['face_value']) - spent.get(c['id'], ZERO) - dec(c['forfeited_amount'])
        if abs(dec(c['balance']) - left) > TOL:
            off += 1
            samples.append(f'Card VC-{c["id"]:06d}: balance {rs(c["balance"])} but face value less spent and lost is {rs(left)}')
    results.append(check(
        'card_balance', 'Value-card balances add up',
        'For each card: its balance against the face value loaded, less what bills spent and what expired unused.',
        bad=off, samples=samples, unit='cards'))

    # ── Packages ────────────────────────────────────────────────────────
    since = ctx.today - timedelta(days=365)
    cps = list(ctx.by_customer(_by_selling_centre(ctx.request, ClientPackage.objects.filter(
        created_at__date__gte=since, source_invoice__status__in=LIVE, package__isnull=False))).select_related('package')[:3000])
    bought = {(r['invoice'], r['object_id']): int(r['n'] or 0) for r in InvoiceItem.objects.filter(
        content_type_id=_ct_id('package'), invoice_id__in=[c.source_invoice_id for c in cps]).values('invoice', 'object_id').annotate(n=Sum('quantity'))}
    used = {r['redeemed_from_package']: int(r['n'] or 0) for r in InvoiceItem.objects.filter(
        is_redemption=True, invoice__status__in=LIVE, redeemed_from_package__in=[c.id for c in cps]).values(
        'redeemed_from_package').annotate(n=Sum('quantity'))}
    off, samples = 0, []
    for c in cps:
        units = bought.get((c.source_invoice_id, c.package_id))
        if not units:
            continue
        expected = units * sessions_in(c.package) - used.get(c.id, 0)
        left = left_in(c.services_remaining)
        if left != expected:
            off += 1
            samples.append(f'{c.package.name} (customer #{c.client_id}): {left} sessions left but sold less used is {expected}')
    results.append(check(
        'package_sessions', 'Package sessions add up',
        'For each package sold in the last year: sessions left against sessions sold less sessions used on bills that stand.',
        bad=off, samples=samples, unit='packages'))

    # ── Memberships ─────────────────────────────────────────────────────
    off, samples = 0, []
    for r in live_bills.filter(membership_used__isnull=False).values(
            'id', 'invoice_number', 'created_at', 'membership_used__start_date', 'membership_used__expiry_date'):
        day = r['created_at'].date()
        if not (r['membership_used__start_date'] <= day <= r['membership_used__expiry_date']):
            off += 1
            samples.append(f'Bill {_bill(r)}: membership discount given on {day}, outside its dates')
    results.append(check(
        'membership_dates', 'Membership discounts were within the membership\'s dates',
        'Every bill that took a membership discount must fall between that membership\'s start and expiry.',
        bad=off, samples=samples, unit='bills'))

    # ── SAP ─────────────────────────────────────────────────────────────
    unsent = live_bills.filter(sap_sync_status=False).filter(perks(ctx.start)[1])
    n = unsent.count()
    if getattr(settings, 'SAP_ENABLED', False):
        # A bill still in flight is normal; one that has been waiting half an hour is not.
        stuck = unsent.filter(created_at__lte=timezone.now() - timedelta(minutes=SAP_GRACE_MINUTES))
        fresh = n - stuck.count()
        results.append(check(
            'sap_pending', 'Marketing bills reached SAP',
            f'Bills in these dates that used an offer or perk and are still not in SAP after {SAP_GRACE_MINUTES} minutes. '
            'Look at them in Admin > SAP Sync.' + (f' {fresh} newer bills are still on their way.' if fresh else ''),
            bad=stuck.count(), unit='bills',
            samples=[f'Bill {_bill(r)}' for r in stuck.values('id', 'invoice_number')[:SAMPLES]]))
    else:
        results.append(check('sap_pending', 'Marketing bills reached SAP',
                             f'SAP posting is switched off here, so {n} marketing bills are waiting; nothing to compare.',
                             info=True, bad=n, unit='bills'))

    passed = sum(1 for r in results if r['status'] == 'ok')
    failed = sum(1 for r in results if r['status'] == 'fail')
    return {
        'frame': ctx.frame(), 'enabled': True, 'checks': results,
        'summary': {'total': len(results), 'passed': passed, 'failed': failed, 'info': len(results) - passed - failed},
        'note': 'Each check compares two figures that have to agree. Green: they do. Red: open the listed rows.',
    }
