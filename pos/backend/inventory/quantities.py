"""Stock quantities are decimal to the thousandth (X8)."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

ZERO = Decimal('0.000')
_STEP = Decimal('0.001')


def qty(value):
    """Any number-ish value as a stock quantity: Decimal, 3 places."""
    if value is None or value == '':
        return ZERO
    try:
        number = Decimal(str(value))
        # "NaN" and "Infinity" parse as numbers, and quantize() lets a quiet NaN
        # through; the first comparison on it then raised and gave a 500.
        if not number.is_finite():
            raise InvalidOperation
        return number.quantize(_STEP, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        raise ValueError(f'{value!r} is not a quantity.')


def sap_decimals():
    """How many decimals SAP keeps in a quantity (Settings > SAP mapping; SAP's own setting, 2 here)."""
    try:
        from pos_backend import app_settings
        return int(app_settings.get('sap.quantity_decimals'))
    except Exception:                                               # noqa: BLE001
        return 2


def to_sap(value):
    """A quantity the way SAP will count it: rounded half up to the decimals SAP keeps.

    The POS keeps three decimals and SAP two, so a recipe that works out to 0.033 pcs would be taken as 0.033 here
    and rounded or refused by SAP. A quantity that is going to SAP is rounded to SAP's decimals first, so the POS
    stock and the SAP stock take out exactly the same number."""
    step = Decimal(1).scaleb(-sap_decimals())
    return qty(value).quantize(step, rounding=ROUND_HALF_UP)


def plain(value):
    """A quantity for text and SAP: '2.5', '12' - no trailing zeros."""
    text = format(qty(value), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text
