"""How a client is identified: phone and GSTIN.

Owner decisions: clients are global (#11); a phone belongs to one client in
the whole business (F2); name and phone are required for new clients (F3);
B2B clients carry a GSTIN, used for their SAP partner and on the invoice
(Q1). The 34,334 legacy B2B records without a phone are kept as they are.
"""

import re

_GSTIN = re.compile(r'^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$')
_CHARS = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'

# GST state codes in use (01-38) plus 97 "other territory".
_STATE_CODES = {f'{n:02d}' for n in range(1, 39)} | {'97'}


def normalise_phone(raw):
    """'+91 98765-43210' -> '9876543210'. Non-numeric legacy codes (CUST...)
    are kept as they are, upper-cased. Blank -> ''."""
    text = str(raw or '').strip()
    if not text:
        return ''
    if re.sub(r'[\d\s+\-().]', '', text):
        return text.upper()                       # not a phone number: a legacy code
    digits = re.sub(r'\D', '', text)
    if len(digits) == 12 and digits.startswith('91'):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith('0'):
        digits = digits[1:]
    return digits


def phone_problem(normalised):
    """Why a NORMALISED phone cannot be a phone number, or None: a 10-digit
    Indian number (the +91 / leading 0 are already stripped)."""
    if not normalised.isdigit() or len(normalised) != 10:
        return 'Enter a 10-digit mobile number (digits only).'
    return None


def phone_key(raw):
    """The value the system-wide uniqueness rule is checked on, or None."""
    return normalise_phone(raw) or None


def gstin_checksum(first14):
    """The GSTIN check character (mod-36 Luhn, as GSTN computes it)."""
    total = 0
    for i, ch in enumerate(first14):
        value = _CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += value // 36 + value % 36
    return _CHARS[(36 - total % 36) % 36]


def gstin_problem(raw):
    """Why `raw` is not a valid GSTIN, or None. Blank is not a problem."""
    gstin = str(raw or '').strip().upper()
    if not gstin:
        return None
    match = _GSTIN.match(gstin)
    if not match:
        return 'A GSTIN is 15 characters: 2-digit state code, 10-character PAN, entity number, Z, check character.'
    if match.group(1) not in _STATE_CODES:
        return f'{match.group(1)} is not a GST state code.'
    if gstin_checksum(gstin[:14]) != gstin[14]:
        return 'This GSTIN fails its check character - please re-check it.'
    return None
