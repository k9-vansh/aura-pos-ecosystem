"""
Single source of truth for classifying payment methods.

Two production bugs came from classifying by substring, in two different files
that disagreed with each other:

  * finance/views.py bucketed reports with `'cash' in pm`, and 'cash' is a
    substring of 'cashback wallet' - so wallet redemptions were reported as
    cash and the drawer could never be reconciled.
  * billing/views.py routed wallet deductions with `'wallet' in pm`, which also
    matches 'cashback wallet' - so paying by cashback debited the customer's
    ADVANCE balance instead.

Everything now normalises to a canonical method via `canonical()` and compares
against exact constants. `ALIASES` is the only place loose input is tolerated,
and it maps to canonical values rather than guessing at call sites.
"""

from billing.models import Payment

# Kind of settlement, used for both wallet routing and report bucketing.
KIND_CASH = 'cash'
KIND_CARD = 'card'
KIND_DIGITAL = 'digital'
KIND_BANK = 'bank'
KIND_VOUCHER = 'voucher'
KIND_ADVANCE = 'advance'
KIND_VALUE_CARD = 'value_card'
KIND_CASHBACK = 'cashback_wallet'
KIND_GIFT_CARD = 'gift_card'
KIND_OTHER = 'other'

# Canonical method -> (report bucket key, settlement kind)
METHOD_SPEC = {
    Payment.CASH:            ('cash',               KIND_CASH),
    Payment.CREDIT_CARD:     ('credit_card',        KIND_CARD),
    Payment.DEBIT_CARD:      ('credit_card',        KIND_CARD),
    Payment.UPI:             ('upi',                KIND_DIGITAL),
    Payment.GOOGLE_PAY:      ('google_pay',         KIND_DIGITAL),
    Payment.PHONEPE:         ('phone_pe',           KIND_DIGITAL),
    Payment.PAYTM:           ('paytm',              KIND_DIGITAL),
    Payment.BHARATPE:        ('bharat_pe',          KIND_DIGITAL),
    Payment.CHEQUE:          ('cheque_net_banking', KIND_BANK),
    Payment.NET_BANKING:     ('cheque_net_banking', KIND_BANK),
    Payment.NEARBUY:         ('nearbuy',            KIND_VOUCHER),
    Payment.ADVANCE:         ('advance',            KIND_ADVANCE),
    Payment.VALUE_CARD:      ('value_card',         KIND_VALUE_CARD),
    Payment.CASHBACK_WALLET: ('cashback_wallet',    KIND_CASHBACK),
    Payment.GIFT_CARD:       ('gift_card',          KIND_GIFT_CARD),
    Payment.OTHER:           ('other',              KIND_OTHER),
}

# The full set of report buckets, so a summary always has every key present.
REPORT_BUCKETS = (
    'cash', 'credit_card', 'upi', 'google_pay', 'phone_pe', 'paytm',
    'bharat_pe', 'cheque_net_banking', 'nearbuy', 'value_card',
    'cashback_wallet', 'advance', 'gift_card', 'other',
)

# Buckets that draw down a liability rather than bringing in new money.
LIABILITY_BUCKETS = frozenset({'advance', 'value_card', 'cashback_wallet'})

# Loose historical spellings -> canonical. Exact-match on the lowercased key
# only; no substring logic anywhere.
ALIASES = {
    'cash': Payment.CASH,
    'card': Payment.CREDIT_CARD,
    'credit card': Payment.CREDIT_CARD,
    'creditcard': Payment.CREDIT_CARD,
    'debit card': Payment.DEBIT_CARD,
    'debitcard': Payment.DEBIT_CARD,
    'upi': Payment.UPI,
    'google pay': Payment.GOOGLE_PAY,
    'googlepay': Payment.GOOGLE_PAY,
    'gpay': Payment.GOOGLE_PAY,
    'g pay': Payment.GOOGLE_PAY,
    'phonepe': Payment.PHONEPE,
    'phone pe': Payment.PHONEPE,
    'paytm': Payment.PAYTM,
    'bharatpe': Payment.BHARATPE,
    'bharat pe': Payment.BHARATPE,
    'cheque': Payment.CHEQUE,
    'check': Payment.CHEQUE,
    'net banking': Payment.NET_BANKING,
    'netbanking': Payment.NET_BANKING,
    'neft': Payment.NET_BANKING,
    'rtgs': Payment.NET_BANKING,
    'imps': Payment.NET_BANKING,
    'bank transfer': Payment.NET_BANKING,
    'nearbuy': Payment.NEARBUY,
    'near buy': Payment.NEARBUY,
    'advance': Payment.ADVANCE,
    'wallet': Payment.ADVANCE,
    'advance wallet': Payment.ADVANCE,
    'value card': Payment.VALUE_CARD,
    'valuecard': Payment.VALUE_CARD,
    'cashback wallet': Payment.CASHBACK_WALLET,
    'cashback': Payment.CASHBACK_WALLET,
    'cashback card': Payment.CASHBACK_WALLET,
    'gift card': Payment.GIFT_CARD,
    'giftcard': Payment.GIFT_CARD,
    'coupon': Payment.GIFT_CARD,
    'other': Payment.OTHER,
}


def canonical(method):
    """Normalise any payment-method string to a canonical Payment constant.

    Unrecognised input maps to Payment.OTHER rather than accidentally matching
    a wallet - an unknown method must never move a customer's balance.
    """
    if not method:
        return Payment.OTHER
    raw = str(method).strip()
    if raw in METHOD_SPEC:
        return raw
    key = ' '.join(raw.lower().split())
    if key in ALIASES:
        return ALIASES[key]
    # "Value Card 42" - a legacy frontend format that embedded the card id.
    if key.startswith('value card'):
        return Payment.VALUE_CARD
    return Payment.OTHER


def report_bucket(method):
    """Which report column this method belongs in."""
    return METHOD_SPEC[canonical(method)][0]


def settlement_kind(method):
    """How this method settles: cash, card, digital, advance, cashback, ..."""
    return METHOD_SPEC[canonical(method)][1]


def is_advance(method):
    return settlement_kind(method) == KIND_ADVANCE


def is_cashback(method):
    return settlement_kind(method) == KIND_CASHBACK


def is_value_card(method):
    return settlement_kind(method) == KIND_VALUE_CARD


def is_gift_card(method):
    return settlement_kind(method) == KIND_GIFT_CARD


def is_cash(method):
    return settlement_kind(method) == KIND_CASH


def is_wallet(method):
    """Advance, cashback or value card: spends a balance the client holds."""
    return settlement_kind(method) in (KIND_ADVANCE, KIND_CASHBACK, KIND_VALUE_CARD)


# Names the Settings list may use -> the methods they allow. "Card" is how the
# till has always shown credit and debit cards.
_SETTING_TOKENS = {
    'card': (Payment.CREDIT_CARD, Payment.DEBIT_CARD),
    'credit card': (Payment.CREDIT_CARD,),
    'debit card': (Payment.DEBIT_CARD,),
}


def allowed_at(center):
    """The money methods a branch takes, from Admin > Settings (B4).
    Advance, cashback and value cards are always allowed - they spend a
    balance the client already holds - and so is a brand's gift card, which
    only works with a valid code."""
    from pos_backend import app_settings

    raw = app_settings.get('billing.payment_methods', center=center) or ''
    allowed = {Payment.GIFT_CARD}
    for token in raw.split(','):
        key = ' '.join(token.lower().split())
        if not key:
            continue
        allowed.update(_SETTING_TOKENS.get(key, (canonical(token),)))
    return allowed


def is_liability_drawdown(method):
    """True if this method spends an existing balance rather than adding money."""
    return report_bucket(method) in LIABILITY_BUCKETS


def extract_value_card_id(method, explicit_id=None):
    """Resolve a value card id, preferring the explicit field.

    The frontend historically encoded the id into the method string
    ("Value Card 42") and parsed it back out; when that parse produced NaN the
    id arrived as null, the backend's `and payment.value_card_id` guard was
    false, and the card was never debited. This keeps the legacy parse as a
    fallback so old clients still deduct correctly.
    """
    if explicit_id not in (None, '', 'null'):
        try:
            return int(explicit_id)
        except (TypeError, ValueError):
            pass
    if not method:
        return None
    key = ' '.join(str(method).strip().lower().split())
    if key.startswith('value card'):
        tail = key[len('value card'):].strip()
        if tail.isdigit():
            return int(tail)
    return None
