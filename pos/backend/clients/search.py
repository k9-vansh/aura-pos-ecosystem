"""Finding a customer by typing: names, GSTIN and phone numbers from the START.

Customers are shared by every centre (owner decision #11), so the till searches all of them. "Any part of the name or
phone" cannot use an index, so at 1.09 crore customers every keystroke read the whole table (27-36 s, and it starved
the other tills). The rule is now the one an index answers:

  * a name is matched from its start, first or last name ("ash" finds Asha and Ashok, "iyer" finds Meenakshi Iyer);
    a GSTIN from its start ("27AAAC");
  * a phone is matched from its first digits and from its last digits ("9876" or "3210");
  * it takes 3 letters or 4 digits to start a search; further words only narrow it down, and a narrowing word may
    start ANY word of the name ("asha 98", "kraze noida" for "Kraze / Wellness-Sec 46, Noida");
  * at most 20 customers come back, newest first.

The answer is the same customer rows as before (cards, memberships, packages, cashback and advance balance included):
only the way the rows are FOUND changed. `center_id` (the Admin > Clients screen looking at one centre) keeps the old
any-part search, which a single centre's customers are few enough for.
"""

import re
from django.db.models import Q

from clients.identity import normalise_phone

MIN_LETTERS = 3
MIN_DIGITS = 4
CAP = 20
LOOK = 200          # rows read from each index before the newest are kept
MAX_TERMS = 5

_DIGITS = re.compile(r'^[0-9]+$')


def _is_number(term):
    return bool(_DIGITS.match(term))


def usable(term):
    """Long enough to start a search with."""
    return len(term) >= (MIN_DIGITS if _is_number(term) else MIN_LETTERS)


def _branches(term):
    """The index lookups one typed word can mean (a customer matching any of them matches the word)."""
    if _is_number(term):
        return [Q(phone__istartswith=term), Q(phone_reversed__istartswith=term[::-1])]
    if term[:1].isdigit():                                  # a GSTIN starts with its 2-digit state code
        return [Q(gst_number__istartswith=term)]
    return [Q(first_name__istartswith=term), Q(last_name__istartswith=term)]


def _narrowing(term):
    """What a word that only NARROWS an already found set may match: it may start any word of the name ("noida" in
    "Wellness-Sec 46, Noida"), the GSTIN, or either end of the phone. Not an index lookup, so never the first step."""
    match = (Q(first_name__istartswith=term) | Q(first_name__icontains=' ' + term)
             | Q(last_name__istartswith=term) | Q(last_name__icontains=' ' + term))
    if _is_number(term):
        match |= Q(phone__istartswith=term) | Q(phone_reversed__istartswith=term[::-1])
    elif term[:1].isdigit():
        match |= Q(gst_number__istartswith=term)
    return match


def terms_of(text):
    """What was typed, as the words to look for. A phone typed with +91, spaces or dashes is one number."""
    text = (text or '').strip()
    compact = normalise_phone(text)
    if compact and compact.isdigit():
        return [compact]
    return text.split()[:MAX_TERMS]


def find_ids(text, base):
    """Ids of the customers `text` can mean, newest first, at most CAP; None when it is too short to search.

    `base` is the customers that may be found (active, inside the picked centres). A number (phone digits) or else the
    longest word drives the search through an index; every other word narrows the candidates. If that word starts no
    name at all (it is the second word of a last name), the next one drives instead.
    """
    terms = terms_of(text)
    # A number narrows the most (a phone has one owner; "Anil" has thousands), then the longer word.
    drivers = sorted({t for t in terms if usable(t)}, key=lambda t: (_is_number(t), len(t)), reverse=True)
    if not drivers:
        return None
    for drive in drivers:
        narrowed = base
        rest = list(terms)
        rest.remove(drive)
        for other in rest:
            narrowed = narrowed.filter(_narrowing(other))
        ids = set()
        for branch in _branches(drive):
            ids.update(narrowed.filter(branch).order_by().values_list('id', flat=True)[:LOOK])
        if ids:
            return sorted(ids, reverse=True)[:CAP]
    return []
