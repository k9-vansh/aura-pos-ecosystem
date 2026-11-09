"""Promotional coupons: a partner brand's codes, paid with at the till.

A brand (Blinkit, say) gives the business a list of coupon codes - or asks the
business to make some (10,000 random codes) - and the brand pays the business
for each one that is used. The codes live on a "Promotional Coupons" promotion.
At the till a customer's code is typed under the Gift Card payment method; it is
valid only while it is

  * a code we hold, not withdrawn, and not used before;
  * on a promotion that is switched on, inside its dates, and at this branch;
  * on a bill that reaches the promotion's minimum.

Using a code takes its worth (a fixed rupee amount, or a percentage of the bill
up to a cap) off what the customer pays, never more than the bill itself; what
the coupon is worth beyond that is lost, the coupon is spent. Cancelling the bill
puts the code back.
"""

import datetime
import logging
import re
import secrets

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

from pos_backend.money import D, money, ZERO

logger = logging.getLogger(__name__)

# No I, O, 0 or 1: a code read out over the phone or off a screen is not mistyped.
ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
MAX_PER_REQUEST = 50000
MIN_CODE_LENGTH = 4
MAX_CODE_LENGTH = 40
MAX_PREFIX_LENGTH = 8
MAX_COUPON_VALUE = D('1000000')
# SQLite refuses more than 999 bound values in one query.
_CHUNK = 500
# How many problems of each kind a report spells out; the counts are always exact.
_REPORT_LIMIT = 50

# Wrong codes typed by one till user: this many in a window locks the box for a while.
MAX_WRONG_CODES = 10
WRONG_CODE_WINDOW = 600


class CouponError(Exception):
    """A coupon cannot be used. The message is safe to show the cashier."""


def normalise(raw):
    """The form a code is looked up in: capitals and digits only."""
    return re.sub(r'[^A-Z0-9]', '', str(raw or '').upper())


def _config(promo):
    return promo.config if isinstance(promo.config, dict) else {}


# ── What a coupon is worth ────────────────────────────────────────────────

def worth_of(code, bill_total):
    """What `code` is worth on a bill of `bill_total`, before the bill caps it."""
    if code.value is not None:
        return money(code.value)
    config = _config(code.promotion)
    value = D(config.get('coupon_value'))
    if (config.get('coupon_value_type') or 'Flat') == 'Percentage':
        amount = money(D(bill_total) * value / D('100'))
        cap = D(config.get('coupon_max_discount'))
        if cap > 0 and amount > cap:
            amount = money(cap)
        return amount
    return money(value)


class Verdict:
    """A coupon that passed every check: the code, its worth, what it pays on this bill."""

    def __init__(self, code, worth, applies):
        self.code, self.worth, self.applies = code, worth, applies


def check(raw, *, center, bill_total, today=None, lock=False):
    """Raise CouponError unless `raw` may pay on a bill of `bill_total` at `center`.

    `lock=True` (inside a transaction) holds the code's row, so two tills that
    type the same code at once cannot both use it."""
    from marketing.models import CouponCode

    today = today or datetime.date.today()
    key = normalise(raw)
    if not key:
        raise CouponError('Enter the coupon code.')
    if not MIN_CODE_LENGTH <= len(key) <= MAX_CODE_LENGTH:
        raise CouponError('This code is not valid.')

    rows = CouponCode.objects.select_related('promotion', 'invoice')
    if lock:
        rows = rows.select_for_update(of=('self',))
    code = rows.filter(code_key=key).first()
    if code is None:
        raise CouponError('This code is not valid.')
    promo = code.promotion

    if code.is_void:
        raise CouponError('This coupon has been withdrawn.')
    if code.is_used:
        used_at = code.used_at
        if used_at is not None and timezone.is_aware(used_at):
            used_at = timezone.localtime(used_at)
        when = used_at.strftime('%d %b %Y') if used_at else 'earlier'
        bill = f' (bill {code.invoice.display_number})' if code.invoice_id and code.invoice.display_number else ''
        raise CouponError(f'This coupon was already used on {when}{bill}.')
    if promo.promo_type != 'Coupon':
        raise CouponError('This code is not valid.')
    if not promo.is_active:
        raise CouponError('This coupon offer is switched off.')
    if promo.start_date and today < promo.start_date:
        raise CouponError(f'This coupon can be used from {promo.start_date:%d %b %Y}.')
    if promo.end_date and today > promo.end_date:
        raise CouponError(f'This coupon expired on {promo.end_date:%d %b %Y}.')
    center_id = getattr(center, 'id', center)
    if promo.center_id and (not center_id or promo.center_id != center_id):
        raise CouponError('This coupon is not valid at this branch.')

    total = money(D(bill_total))
    minimum = D(_config(promo).get('coupon_min_bill'))
    if minimum > 0 and total < minimum:
        raise CouponError(f'This coupon needs a bill of at least Rs {money(minimum)}.')

    worth = worth_of(code, total)
    if worth <= 0:
        raise CouponError('This coupon has no value.')
    return Verdict(code, worth, worth if worth <= total else total)


# ── Typing wrong codes ────────────────────────────────────────────────────

def _throttle_key(user):
    return f'coupon-wrong:{getattr(user, "pk", None) or "anon"}'


def ensure_not_locked(user):
    if (cache.get(_throttle_key(user)) or 0) >= MAX_WRONG_CODES:
        raise CouponError('Too many wrong codes were typed. Wait a few minutes and try again.')


def note_wrong_code(user):
    key = _throttle_key(user)
    if cache.add(key, 1, WRONG_CODE_WINDOW):
        return
    try:
        cache.incr(key)
    except ValueError:                                   # expired between the two calls
        cache.set(key, 1, WRONG_CODE_WINDOW)


def clear_wrong_codes(user):
    cache.delete(_throttle_key(user))


def check_for_till(raw, *, center, bill_total, user=None, today=None):
    """The till's "Check" button: a verdict, or a CouponError after counting the
    wrong code against `user`."""
    ensure_not_locked(user)
    try:
        verdict = check(raw, center=center, bill_total=bill_total, today=today)
    except CouponError as exc:
        # A code that exists but is used or expired is a real customer's problem,
        # not guessing; only codes we do not know count towards the lock.
        if str(exc) in ('This code is not valid.', 'Enter the coupon code.'):
            if normalise(raw):
                note_wrong_code(user)
        raise
    clear_wrong_codes(user)
    return verdict


# ── Using and releasing ───────────────────────────────────────────────────

def redeem_for_payment(payment, invoice):
    """Spend the code typed on this Gift Card payment, exactly once.

    Runs inside the checkout's transaction: if the code was taken by another till
    in the meantime, or the amount is more than the coupon pays, the whole
    checkout is refused and nothing is written."""
    from rest_framework.exceptions import ValidationError

    amount = money(payment.amount)
    try:
        verdict = check(payment.reference, center=invoice.center, bill_total=invoice.total_amount,
                        lock=True)
    except CouponError as exc:
        raise ValidationError({'payments': str(exc)})
    if amount > verdict.applies:
        raise ValidationError({'payments': (
            f'This coupon pays at most Rs {verdict.applies} on this bill; the Gift Card '
            f'amount is Rs {amount}.')})

    code = verdict.code
    code.is_used = True
    code.used_at = timezone.now()
    code.invoice = invoice
    code.payment = payment
    code.client = invoice.client
    code.center = invoice.center
    code.amount_applied = amount
    code.save(update_fields=['is_used', 'used_at', 'invoice', 'payment', 'client', 'center',
                             'amount_applied'])
    if payment.reference != code.code:
        payment.reference = code.code
        payment.save(update_fields=['reference'])
    return code


def release_for_payment(payment):
    """Put the code of a cancelled Gift Card payment back, unused."""
    from marketing.models import CouponCode

    released = 0
    for code in CouponCode.objects.select_for_update().filter(payment=payment, is_used=True):
        code.is_used = False
        code.used_at = None
        code.invoice = None
        code.payment = None
        code.client = None
        code.center = None
        code.amount_applied = ZERO
        code.times_released += 1
        code.save(update_fields=['is_used', 'used_at', 'invoice', 'payment', 'client', 'center',
                                 'amount_applied', 'times_released'])
        released += 1
    return released


# ── Making and loading codes ──────────────────────────────────────────────

def _parse_value(text):
    from decimal import InvalidOperation
    try:
        value = D(str(text).replace(',', '').replace('₹', '').replace('Rs', '').strip())
    except (InvalidOperation, ValueError, TypeError):
        return None
    return value if value.is_finite() else None


def parse_list(text):
    """Split pasted or uploaded text into (rows, invalid).

    One code a line: `CODE`, or `CODE,500` when that coupon carries its own
    value. A header line ("code") is skipped. Commas, tabs and semicolons all
    separate the columns."""
    rows, invalid = [], []
    for number, line in enumerate(str(text or '').splitlines(), start=1):
        line = line.strip().strip('﻿')
        if not line:
            continue
        parts = [p.strip().strip('"\'') for p in re.split(r'[,\t;]', line)]
        raw_code = parts[0]
        if number == 1 and raw_code.lower() in ('code', 'codes', 'coupon', 'coupon code', 'coupon_code'):
            continue
        key = normalise(raw_code)
        if len(key) < MIN_CODE_LENGTH:
            invalid.append({'line': number, 'text': line[:60], 'reason': f'A code needs at least {MIN_CODE_LENGTH} letters or digits.'})
            continue
        if len(key) > MAX_CODE_LENGTH or len(raw_code) > MAX_CODE_LENGTH:
            invalid.append({'line': number, 'text': line[:60], 'reason': f'A code is at most {MAX_CODE_LENGTH} characters.'})
            continue
        value = None
        if len(parts) > 1 and parts[1]:
            value = _parse_value(parts[1])
            if value is None or value <= 0 or value > MAX_COUPON_VALUE:
                invalid.append({'line': number, 'text': line[:60], 'reason': 'The amount after the code is not a valid rupee amount.'})
                continue
        rows.append((number, raw_code, key, money(value) if value is not None else None))
    return rows, invalid


def _existing(keys):
    from marketing.models import CouponCode

    found = {}
    keys = list(keys)
    for i in range(0, len(keys), _CHUNK):
        for row in (CouponCode.objects.filter(code_key__in=keys[i:i + _CHUNK])
                    .select_related('promotion').only('code_key', 'promotion__name')):
            found[row.code_key] = row.promotion.name
    return found


@transaction.atomic
def import_codes(promotion, text):
    """Load a brand's own list of codes onto `promotion`.

    Good lines are added; the rest are reported, never silently dropped: a code
    typed twice in the list, a code already held (here or under another
    promotion - one code, one meaning), and lines that are not codes."""
    from marketing.models import CouponCode

    rows, invalid = parse_list(text)
    if len(rows) + len(invalid) > MAX_PER_REQUEST:
        raise CouponError(f'Load at most {MAX_PER_REQUEST:,} codes at a time.')

    seen, repeated, fresh = {}, [], []
    for number, raw_code, key, value in rows:
        if key in seen:
            repeated.append({'line': number, 'code': raw_code, 'first_line': seen[key]})
            continue
        seen[key] = number
        fresh.append((raw_code, key, value))

    held = _existing(seen)
    already, to_add = [], []
    for raw_code, key, value in fresh:
        if key in held:
            already.append({'code': raw_code, 'promotion': held[key]})
        else:
            to_add.append(CouponCode(promotion=promotion, code=raw_code, code_key=key, value=value,
                                     source=CouponCode.SOURCE_IMPORTED))
    CouponCode.objects.bulk_create(to_add, batch_size=_CHUNK)
    return {
        'added': len(to_add),
        'repeated_in_list': len(repeated), 'repeated_examples': repeated[:_REPORT_LIMIT],
        'already_held': len(already), 'already_held_examples': already[:_REPORT_LIMIT],
        'invalid': len(invalid), 'invalid_examples': invalid[:_REPORT_LIMIT],
    }


def _random_code(prefix):
    left = ''.join(secrets.choice(ALPHABET) for _ in range(4))
    right = ''.join(secrets.choice(ALPHABET) for _ in range(4))
    parts = [prefix, left, right] if prefix else [left, right]
    return '-'.join(parts), prefix + left + right


@transaction.atomic
def generate_codes(promotion, quantity, prefix=''):
    """Make `quantity` random, unique codes for `promotion` (BLK-7KQ2-M9XP)."""
    from marketing.models import CouponCode

    try:
        quantity = int(quantity)
    except (TypeError, ValueError):
        raise CouponError('Enter how many codes to make.')
    if quantity < 1:
        raise CouponError('Enter how many codes to make (1 or more).')
    if quantity > MAX_PER_REQUEST:
        raise CouponError(f'Make at most {MAX_PER_REQUEST:,} codes at a time.')
    prefix = normalise(prefix)
    if len(prefix) > MAX_PREFIX_LENGTH:
        raise CouponError(f'The prefix is at most {MAX_PREFIX_LENGTH} letters or digits.')

    made = {}
    while len(made) < quantity:
        batch = {}
        while len(batch) < quantity - len(made):
            code, key = _random_code(prefix)
            if key not in made:
                batch[key] = code
        for key in _existing(batch):                     # astronomically rare, but never reuse a code
            batch.pop(key, None)
        made.update(batch)
    CouponCode.objects.bulk_create(
        [CouponCode(promotion=promotion, code=code, code_key=key, source=CouponCode.SOURCE_GENERATED)
         for key, code in made.items()],
        batch_size=_CHUNK)
    return {'added': len(made)}


def counts(promotion):
    """How many codes a promotion holds, and in what state."""
    from django.db.models import Count, Q

    row = promotion.codes.aggregate(
        total=Count('id'),
        used=Count('id', filter=Q(is_used=True)),
        void=Count('id', filter=Q(is_void=True, is_used=False)))
    unused = row['total'] - row['used'] - row['void']
    return {'total': row['total'], 'used': row['used'], 'void': row['void'], 'unused': unused}
