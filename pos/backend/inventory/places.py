"""Which state a centre is in, and so whether a transfer between two centres crosses a state line.

A transfer inside one state is CGST + SGST (or nothing, between godowns of one GSTIN); one that crosses a state
line is IGST and travels with a tax invoice and an e-way bill. The state comes from the centre's GSTIN when it
has one (its first two digits), else from the state of its SAP business place.
"""
from django.core.cache import cache

GST_STATE_CODES = {
    '01': 'JK', '02': 'HP', '03': 'PB', '04': 'CH', '05': 'UK', '06': 'HR', '07': 'DL', '08': 'RJ', '09': 'UP',
    '10': 'BR', '11': 'SK', '12': 'AR', '13': 'NL', '14': 'MN', '15': 'MZ', '16': 'TR', '17': 'ML', '18': 'AS',
    '19': 'WB', '20': 'JH', '21': 'OR', '22': 'CG', '23': 'MP', '24': 'GJ', '26': 'DN', '27': 'MH', '28': 'AP',
    '29': 'KA', '30': 'GA', '31': 'LD', '32': 'KL', '33': 'TN', '34': 'PY', '35': 'AN', '36': 'TG', '37': 'AP',
    '38': 'LA',
}


_STATES_KEY = 'sap_bpl_states'          # the same entry sap_utils.bpl_states() keeps, so both fill it
_UNREADABLE_KEY = 'sap_bpl_states:unreadable'


def _business_place_states():
    """{business place name: its State code} from SAP, or {} when SAP cannot be read right now.

    Kept ten minutes. This runs inside a person's request (the transfers list asks it for every row), so it
    must never wait on SAP: it reads HANA (one small query, a fraction of a second) and, when that cannot be
    read, remembers for a minute that SAP could not be asked instead of waiting on it again for every row."""
    states = cache.get(_STATES_KEY)
    if states is not None:
        return states
    if cache.get(_UNREADABLE_KEY):
        return {}
    from pos_backend import hana_reads, sap_utils
    if not sap_utils.sap_enabled():
        return {}
    try:
        states = {name: (state or '').strip()
                  for name, state in hana_reads.rows('SELECT "BPLName","State" FROM "OBPL"')}
        cache.set(_STATES_KEY, states, 10 * 60)
        return states
    except hana_reads.HanaUnavailable:
        pass
    try:
        return sap_utils.bpl_states()
    except Exception:                                               # noqa: BLE001
        cache.set(_UNREADABLE_KEY, True, 60)
        return {}


def center_state(center):
    """The state code (UP, DL ...) of a centre, or '' when it cannot be told."""
    if center is None:
        return ''
    gstin = (center.gst_number or '').strip()
    if len(gstin) >= 2 and gstin[:2] in GST_STATE_CODES:
        return GST_STATE_CODES[gstin[:2]]
    branch = (center.sap_branch_name or '').strip()
    if not branch:
        return ''
    return (_business_place_states().get(branch) or '').strip()


_DETAILS_KEY = 'sap_bpl_details'


def business_place_details():
    """{business place name: {'id', 'state', 'gstin'}} from SAP (HANA), kept ten minutes; {} when SAP cannot be read.

    Each business place is one GSTIN of the company, which is what decides whether stock moving between two
    centres is a tax supply (different GSTIN) or a plain move (same GSTIN)."""
    found = cache.get(_DETAILS_KEY)
    if found is not None:
        return found
    from pos_backend import hana_reads, sap_utils
    if not sap_utils.sap_enabled():
        return {}
    try:
        rows = hana_reads.rows('SELECT "BPLId","BPLName","State","TaxIdNum" FROM "OBPL"')
    except hana_reads.HanaUnavailable:
        return {}
    found = {name: {'id': pid, 'state': (state or '').strip(), 'gstin': (gstin or '').strip().upper()}
             for pid, name, state, gstin in rows}
    cache.set(_DETAILS_KEY, found, 10 * 60)
    return found


def center_gst_code(center):
    """The two-digit GST state code (07 = Delhi, 06 = Haryana ...) a centre sells from: the first two digits of its own
    GSTIN, else of the GSTIN of its SAP business place; '' when neither is known (SAP off or unreadable)."""
    if center is None:
        return ''
    gstin = (getattr(center, 'gst_number', '') or '').strip()
    if len(gstin) >= 2:
        return gstin[:2]
    branch = (getattr(center, 'sap_branch_name', '') or '').strip()
    place = business_place_details().get(branch) if branch else None
    code = ((place or {}).get('gstin') or '')[:2]
    return code if code in GST_STATE_CODES else ''


def supply_kind(from_center, to_center):
    """How stock moving from one centre to another is treated for GST: (kind, why).

      'plain'      same business place (same GSTIN): a plain Inventory Transfer, no tax;
      'igst'       different GSTINs in different states: A/R invoice + A/P invoice with IGST;
      'cgst_sgst'  different GSTINs in the same state: the same two invoices with CGST + SGST;
      'unknown'    cannot be told (a centre has no branch, SAP has no GSTIN for a branch, or SAP cannot be read):
                   `why` says what to fix, and nothing is posted until it is.
    """
    a = (getattr(from_center, 'sap_branch_name', '') or '').strip()
    b = (getattr(to_center, 'sap_branch_name', '') or '').strip()
    if a == b:
        return 'plain', ''
    for centre, name in ((from_center, a), (to_center, b)):
        if not name:
            return 'unknown', f'{centre.display_name or centre.center_name} has no SAP branch set'
    details = business_place_details()
    if not details:
        return 'unknown', 'the SAP business places could not be read'
    for name in (a, b):
        if name not in details:
            return 'unknown', f'SAP has no business place called "{name}"'
        if not details[name]['gstin']:
            return 'unknown', f'the SAP business place "{name}" has no GSTIN'
    ga, gb = details[a]['gstin'], details[b]['gstin']
    if ga == gb:
        return 'plain', ''
    return ('igst' if ga[:2] != gb[:2] else 'cgst_sgst'), ''


def transfer_kind(from_center, to_center):
    """'same_state', 'interstate' or 'unknown' (a state could not be told)."""
    a, b = center_state(from_center), center_state(to_center)
    if not a or not b:
        return 'unknown'
    return 'same_state' if a == b else 'interstate'
