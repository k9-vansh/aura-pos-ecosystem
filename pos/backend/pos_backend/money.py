"""
Single source of truth for monetary arithmetic.

Every module that touches money must import from here rather than doing its own
Decimal/float juggling. The audit found three distinct classes of bug that all
trace back to not having this module:

  * Decimal * float -> TypeError (500s in billing and the incentive engine)
  * Python round() on a float -> banker's rounding, so the frontend showed
    Rs 101 while the backend stored Rs 100 for a Rs 100.50 total
  * unquantized division leaking paise when splitting a line across staff

Rules:
  * D(x)      - build a Decimal from anything (None -> 0) without ever raising
  * money(x)  - quantize to 2dp, ROUND_HALF_UP (Indian invoicing convention)
  * rupees(x) - quantize to whole rupees, ROUND_HALF_UP (invoice grand totals)
  * pct(a, p) - a * p / 100, all Decimal, quantized to 2dp
  * split(total, n) - split without losing or inventing paise
"""

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

ZERO = Decimal('0.00')
CENT = Decimal('0.01')
ONE = Decimal('1')
HUNDRED = Decimal('100')


def D(value, default=ZERO):
    """Coerce anything to Decimal. Never raises.

    None, '', 'null', 'NaN', 'Infinity' and unparseable input all return
    `default`. This replaces the many `Decimal(str(x))` call sites that blew up
    on a null database column (the promotion.discount_value 500 in production).
    """
    if value is None:
        return default
    if isinstance(value, Decimal):
        # Reject NaN / Infinity, which compare falsely against every threshold
        return default if not value.is_finite() else value
    if isinstance(value, bool):
        return default
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError, ArithmeticError):
        return default
    return default if not d.is_finite() else d


def money(value):
    """Quantize to 2 decimal places, rounding half away from zero."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


def rupees(value):
    """Quantize to whole rupees, rounding half away from zero.

    This is what an invoice grand total is rounded to. It deliberately matches
    JavaScript's Math.round() for positive values so the amount the customer is
    shown is the amount that gets stored.
    """
    return D(value).quantize(ONE, rounding=ROUND_HALF_UP)


def pct(amount, percent):
    """`percent` percent of `amount`, as 2dp money. Safe against None."""
    return money(D(amount) * D(percent) / HUNDRED)


def non_negative(value):
    """Clamp to zero. Use where a negative is meaningless, not where it's a bug."""
    d = money(value)
    return ZERO if d < ZERO else d


def split(total, parts):
    """Split `total` into `parts` shares that sum back to exactly `total`.

    Plain division leaks paise: Rs 100 / 3 stored at 2dp gives 33.33 x 3 =
    99.99. This distributes the remainder one paisa at a time across the first
    shares, so sum(split(x, n)) == money(x) always holds.

    Returns a list of Decimals, longest-first by value.
    """
    n = int(parts or 0)
    if n <= 0:
        return []
    total = money(total)
    base = (total / n).quantize(CENT, rounding=ROUND_HALF_UP)
    shares = [base] * n
    drift = total - (base * n)
    # drift is a whole number of paise, positive or negative
    steps = int((drift / CENT).to_integral_value())
    step = CENT if steps > 0 else -CENT
    for i in range(abs(steps)):
        shares[i % n] += step
    return shares


def split_weighted(total, weights):
    """Split `total` in proportion to `weights`, summing back to exactly `total`.

    Each share is rounded to paise and the last share takes whatever the
    others leave, so no paisa is gained or lost. All-zero weights split evenly.
    """
    weights = [max(D(w), ZERO) for w in weights]
    if not weights:
        return []
    weight_sum = sum(weights, ZERO)
    total = money(total)
    if weight_sum <= 0:
        return split(total, len(weights))
    shares = [money(total * w / weight_sum) for w in weights[:-1]]
    shares.append(money(total - sum(shares, ZERO)))
    return shares


def quantize_ratio(numerator, denominator):
    """Safe ratio for proportional allocation. Returns Decimal, 0 if undefined.

    Kept at full Decimal precision (not quantized) because it is a multiplier,
    not a stored amount.
    """
    num = D(numerator)
    den = D(denominator)
    if den == 0:
        return ZERO
    return num / den
