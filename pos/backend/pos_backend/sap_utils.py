import urllib.request
from inventory.quantities import plain as plain_qty
import urllib.error
import urllib.parse
import json
import ssl
import logging
import threading
from decimal import Decimal
from django.db import connection
from django.core.cache import cache

logger = logging.getLogger(__name__)

from django.conf import settings as _settings

# Connection details come from settings, which read them from backend/.env.
SAP_URL_BASE = _settings.SAP_SERVICE_LAYER_URL
SAP_CREDENTIALS = {
    "CompanyDB": _settings.SAP_COMPANY_DB,
    "Password": _settings.SAP_PASSWORD,
    "UserName": _settings.SAP_USERNAME,
}


class SapDisabled(Exception):
    """SAP_ENABLED is off (or SAP is not configured): nothing is sent."""


def sap_enabled() -> bool:
    return bool(getattr(_settings, 'SAP_ENABLED', False))


def _sap_error_text(body) -> str:
    """SAP's error as a person reads it: '-5002: Tax code IGST@12 not found'.

    The Service Layer answers {"error": {"code": ..., "message": {"value": ...}}};
    anything else is returned as it came, shortened.
    """
    text = str(body or '')
    try:
        err = (json.loads(text) or {}).get('error') or {}
        message = err.get('message')
        if isinstance(message, dict):
            message = message.get('value')
        if message:
            code = err.get('code')
            return f"{code}: {message}" if code not in (None, '') else str(message)
    except (ValueError, AttributeError, TypeError):
        pass
    return text[:500]


def _sap(key):
    """A value from Admin > Settings > SAP mapping.

    The defaults there are the accounts and codes this file used to have
    written into it, so an untouched system posts exactly as before.
    """
    from pos_backend import app_settings
    return app_settings.get(key)


def _post_timeout():
    """Seconds to wait for SAP to answer a document post (Settings > SAP mapping; 60 unless changed).

    15 was too short for a bill with many lines: the post timed out, was retried ten times and gave up
    with the bill never in SAP. A timed-out post is looked up in SAP before it is sent again, so waiting
    longer never posts anything twice."""
    try:
        return int(_sap('sap.post_timeout_seconds'))
    except Exception:                                               # noqa: BLE001
        return 60


def _warehouse_code(center):
    """The SAP warehouse a branch posts to: its own code, nothing else.

    There is no fallback warehouse. A made-up code posts the stock to a
    warehouse the SAP company does not have (or the wrong one); a branch
    without a code fails its posting with this message in Admin > SAP Sync
    until the branch is given the warehouse code SAP uses for it.
    """
    code = (getattr(center, 'sap_warehouse_code', None) or '').strip() if center else ''
    if not code:
        name = getattr(center, 'display_name', None) or getattr(center, 'center_name', None) or 'This document'
        raise ValueError(f"{name} has no SAP warehouse code. Set it on the branch, then retry.")
    return code


_UPI_FAMILY = ('UPI', 'Google Pay', 'PhonePe', 'Paytm', 'BharatPe')


def payment_account(method):
    """The SAP account a payment of `method` is posted to (Q18).

    The method's own account in Settings › SAP mapping when one is set;
    otherwise the account that has always covered it - cash to the cash
    account, the UPI family to the UPI account, the rest to "other online"."""
    from pos_backend import app_settings
    from billing import payment_methods as pm

    canonical = pm.canonical(method)
    key = app_settings.payment_account_key(canonical)
    own = app_settings.get(key) if key in app_settings.BY_KEY else ''
    if own:
        return own
    if canonical == 'Cash':
        return _sap('sap.account.cash')
    if canonical in _UPI_FAMILY:
        return _sap('sap.account.upi')
    return _sap('sap.account.transfer_default')

def get_hana_batches(item_code, wh_code, required_quantity, strict=False):
    """
    Query SAP HANA directly for the batches to take for a given item in a warehouse.
    Returns a list of dicts: [{'BatchNumber': '...', 'Quantity': ...}]

    The batch that expires first is taken first, then the oldest received - the
    same order the POS uses (FEFO), so SAP and the POS empty the same batches.

    With strict=True a HANA that cannot be read (driver missing, host down)
    raises with that reason; otherwise it is logged and [] comes back, which
    SAP then refuses with a message about batches that hides the real cause.
    """
    import logging
    from decimal import Decimal
    
    batches = []
    qty_left = Decimal(str(required_quantity))
    if qty_left <= 0 or not sap_enabled():
        return batches

    conn = None
    try:
        from hdbcli import dbapi
        from django.conf import settings
        conn = dbapi.connect(
            address=settings.HANA_ADDRESS,
            port=settings.HANA_PORT,
            user=settings.HANA_USER,
            password=settings.HANA_PASSWORD,
            currentSchema=settings.HANA_SCHEMA,
        )
        cursor = conn.cursor()
        
        cursor.execute(
            'SELECT "BatchNum", "Quantity" FROM "OIBT" '
            'WHERE "ItemCode" = ? AND "WhsCode" = ? AND "Quantity" > 0 '
            'ORDER BY CASE WHEN "ExpDate" IS NULL THEN 1 ELSE 0 END, "ExpDate" ASC, "InDate" ASC',
            (item_code, wh_code)
        )
        
        for row in cursor.fetchall():
            batch_num = row[0]
            batch_qty = Decimal(str(row[1]))
            take = min(qty_left, batch_qty)
            batches.append({
                "BatchNumber": batch_num,
                "Quantity": float(take)
            })
            qty_left -= take
            if qty_left <= 0:
                break
                
    except Exception as e:
        logging.getLogger(__name__).error(f"HANA Batch Query Error: {e}")
        if strict:
            why = ('the HANA driver (hdbcli) is not installed on this server'
                   if isinstance(e, ImportError) else f'{type(e).__name__}: {str(e)[:120]}')
            raise RuntimeError(f"Could not read the SAP batches for {item_code} in {wh_code}: {why}") from e
    finally:
        # One connection per call; it used to be left open, one per invoice
        # line, until the garbage collector got to it.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    return batches


def batch_managed(item_code):
    """True when SAP manages this item by batch (asked once, remembered 10 minutes).

    Nearly the whole item master is (1,924 of 1,927 inventory items in the
    test company), and SAP refuses a receipt or an issue of such an item that
    does not name its batches. A plain item must not be sent any.
    """
    from inventory.sap_items import batch_key, remember_batch_flags
    key = batch_key(item_code)
    flag = cache.get(key)
    if flag is None and remember_batch_flags():
        # One HANA query filled the answer for every item; no SAP call per item.
        flag = cache.get(key)
    if flag is None:
        quoted = urllib.parse.quote(str(item_code).replace("'", "''"), safe='')
        data = _send_sap_request(
            'GET', f"{SAP_URL_BASE}/Items('{quoted}')?$select=ItemCode,ManageBatchNumbers", timeout=10).json() or {}
        flag = data.get('ManageBatchNumbers') == 'tYES'
        cache.set(key, flag, 600)
    return flag


def issue_batches(item_code, wh_code, quantity):
    """BatchNumbers for a line that takes stock OUT of SAP: [] for a plain item.

    Raises, with the reason, when SAP's batches cannot be read or hold less than
    the line needs, so the posting waits in Admin > SAP Sync with a message a
    person can act on.
    """
    needed = Decimal(str(quantity))
    if needed <= 0 or not sap_enabled() or not batch_managed(item_code):
        return []
    batches = get_hana_batches(item_code, wh_code, needed, strict=True)
    have = sum((Decimal(str(b['Quantity'])) for b in batches), Decimal('0'))
    if have < needed:
        raise ValueError(f"SAP holds only {plain_qty(have)} of {item_code} in batches at {wh_code}; "
                         f"{plain_qty(needed)} is needed. Receive or count the stock in SAP first.")
    return batches


def _local_date(moment):
    """The calendar day (local time) of a datetime; today for None."""
    import datetime
    from django.utils import timezone
    if moment is None:
        return datetime.date.today()
    if not isinstance(moment, datetime.datetime):
        return moment
    if timezone.is_aware(moment):
        moment = timezone.localtime(moment)
    return moment.date()


def receipt_batches(item_code, quantity, batch=None, lot=None, on=None):
    """BatchNumbers for a line that brings stock INTO SAP: None for a plain item.

    A batch-managed item needs a batch on every receipt. The POS batch (or the
    old lot) is used when it has a number, with its expiry; goods that came in
    without one go in as POS-<day>.
    """
    if not batch_managed(item_code):
        return None
    number, expiry, made = '', None, None
    if batch is not None and batch.batch_number:
        number, expiry = batch.batch_number, batch.expiry_date
    elif lot is not None and lot.lot_number:
        number, expiry, made = lot.lot_number, lot.expiry_date, lot.manufacturing_date
    else:
        number = f"POS-{_local_date(on).strftime('%Y%m%d')}"
        expiry = getattr(batch, 'expiry_date', None)
    row = {"BatchNumber": number[:36], "Quantity": plain_qty(quantity)}
    if made:
        row["ManufacturingDate"] = str(made)
    if expiry:
        row["ExpiryDate"] = str(expiry)
    return [row]

def run_sap_sync_async(func, *args, **kwargs):
    """Queue an SAP posting. Nothing is sent from the request.

    Writes a background job (the outbox) in the caller's transaction: it
    exists exactly when the bill, payment or stock move it describes
    committed, and `manage.py run_workers` posts it. The name is kept so the
    callers did not change.

    It used to start a thread after the commit. A restart between the commit
    and the thread finishing lost the posting without a trace, and up to 400
    tills would each hold a thread waiting on SAP. With SAP switched off the
    job simply waits until SAP is back.
    """
    _enqueue_failed_sync(func, *args, **kwargs)


def _enqueue_failed_sync(func, *args, error=None, **kwargs):
    """Queue the posting `func(*args)` describes (see run_sap_sync_async).

    The queue type comes from an explicit table in sap_jobs. The old substring
    matching sent every journal-entry function to 'unknown', and the retry
    worker deleted 'unknown' rows unprocessed, so a failed value card,
    membership, package or advance journal never reached SAP.
    """
    from pos_backend import sap_jobs

    target = sap_jobs.queue_object(func, args)
    if target is None:
        logger.error(f"SAP sync {getattr(func, '__name__', func)} has no queue type; it was not queued")
        return
    object_type, object_id = target
    obj = args[1] if object_type == 'marketing_je' and len(args) > 1 else args[0]
    sap_jobs.enqueue(object_type, object_id, error or '', obj=obj)


# SAP's message for the last request it refused on this thread, so a failed
# job shows "-5002: Tax code IGST@12 not found" rather than "did not accept".
_last_error = threading.local()


def clear_last_sap_error():
    _last_error.text = ''


def last_sap_error() -> str:
    return getattr(_last_error, 'text', '') or ''


# ─── Tax Code Resolution ─────────────────────────────────────────────────────

# This maps GST rate percentages to SAP tax codes exactly as configured in
# your SAP B1 company. Adjust the values to match your SAP tax code names.
# Once a product's SalesTaxCode / PurchaseTaxCode is set in the SAP Item
# master, every downstream document (invoice, GRPO, credit note, stock entry)
# inherits the tax code automatically. We do NOT pass TaxCode on individual
# document lines — that would override the master and cause drift.
_SAP_IGST_CODES: dict[int, str] = {
    0:  "IGST@0",
    5:  "IGST@5",
    12: "IGST@12",
    18: "IGST@18",
    28: "IGST@28",
}


def _gst_tax_code(gst_percent) -> str:
    """
    Return the SAP tax code for a given GST percentage.

    Rounds to the nearest integer bucket. Unknown rates fall back to the
    GST-exempt code so that SAP never rejects the document with a missing
    tax code — the accountant can correct the item master afterward.
    """
    try:
        pct = int(round(float(gst_percent or 0)))
    except (TypeError, ValueError):
        pct = 0
    return _SAP_IGST_CODES.get(pct, _SAP_IGST_CODES.get(0, "Exempt"))
SAP_PHONE_MAX = 10  # SAP's phone UDFs reject anything longer (error -8112)


def _sap_phone(value):
    """A client phone as SAP's phone UDFs accept it.

    Keeps the last SAP_PHONE_MAX digits, so '+91 98765 43210' and '09876543210'
    both become '9876543210'. A number the POS saved with extra digits used to
    fail the whole posting with -8112 on every retry; the SAP field is only a
    contact note, so it is shortened rather than blocking the bill.
    """
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    return digits[-SAP_PHONE_MAX:]


def _get_udf_fields(invoice):
    """Generate UDF fields for SAP payloads based on the invoice data."""
    udfs = {}
    if not invoice:
        return udfs
    
    # Staff Name
    if hasattr(invoice, 'staff') and invoice.staff:
        first = getattr(invoice.staff, 'first_name', '')
        last = getattr(invoice.staff, 'last_name', '')
        udfs['U_STAF_NAME'] = f"{first} {last}".strip()
    
    # POS Invoice Number
    display_num = getattr(invoice, 'display_number', None)
    udfs['U_POSINNO'] = str(display_num) if display_num else str(invoice.id)
    
    # Client Details
    if hasattr(invoice, 'client') and invoice.client:
        c_first = getattr(invoice.client, 'first_name', '')
        c_last = getattr(invoice.client, 'last_name', '')
        udfs['U_CUST_NAME'] = f"{c_first} {c_last}".strip() or "Walk-in"
        udfs['U_CUST_PH'] = _sap_phone(getattr(invoice.client, 'phone', ''))
    else:
        udfs['U_CUST_NAME'] = "Walk-in"
        udfs['U_CUST_PH'] = ""
        
    return udfs

    return udfs

def _get_je_udf_fields(invoice):
    """Generate UDF fields for JournalEntryLines using the specific DB column names created by SAP Admin."""
    udfs = {}
    if not invoice:
        return udfs
    
    if hasattr(invoice, 'client') and invoice.client:
        c_first = getattr(invoice.client, 'first_name', '')
        c_last = getattr(invoice.client, 'last_name', '')
        udfs['U_B2C_NAME'] = f"{c_first} {c_last}".strip() or "Walk-in"
        udfs['U_B2C_PH'] = _sap_phone(getattr(invoice.client, 'phone', ''))
    else:
        udfs['U_B2C_NAME'] = "Walk-in"
        udfs['U_B2C_PH'] = ""
        
    return udfs
_bp_cache_lock = __import__('threading').Lock()

def _get_all(path, timeout=15):
    """Every row of a Service Layer list, following its paging.

    The Service Layer returns lists 20 rows at a time with an
    ``odata.nextLink``. Reading only the first page silently missed every
    business place or warehouse location after the 20th.
    """
    rows, url = [], f"{SAP_URL_BASE}/{path}"
    while url:
        data = _send_sap_request('GET', url, timeout=timeout).json() or {}
        rows.extend(data.get('value') or [])
        nxt = data.get('odata.nextLink') or data.get('@odata.nextLink')
        url = (nxt if nxt.startswith('http') else f"{SAP_URL_BASE}/{nxt}") if nxt else None
    return rows


def bpl_states():
    """{business place name: its State code}, kept ten minutes. Raises when SAP cannot be read."""
    states = cache.get('sap_bpl_states')
    if states is None:
        states = {bp['BPLName']: (bp.get('State') or '').strip()
                  for bp in _get_all('BusinessPlaces?$select=BPLID,BPLName,State', timeout=8)}
        cache.set('sap_bpl_states', states, 10 * 60)
    return states


def _walk_in_address(card_code, branch_name):
    """For the shared B2C partner: the Ship-To / Bill-To address of the branch's state.

    Walk-ins are all booked to one SAP partner, and SAP works out CGST+SGST or IGST from the state of the
    address on the document. With only the partner's default address, every branch outside that one state
    was refused ("For interstate transactions you must choose IGST"). A walk-in buys at the branch, so the
    document names the partner's address called after the branch's state (UK, HR, DL ...; the business
    place's State). When SAP has no such address the bill waits with the exact thing to add, instead of
    being posted with the wrong tax. A client with a partner of their own keeps SAP's own addresses.
    """
    if not branch_name or branch_name == 'No Branch' or card_code != _sap('sap.b2c_card_code'):
        return {}
    addresses = cache.get('sap_b2c_addresses:' + str(card_code))
    try:
        states = bpl_states()
        if addresses is None:
            quoted = urllib.parse.quote(str(card_code).replace("'", "''"), safe='')
            data = _send_sap_request(
                'GET', f"{SAP_URL_BASE}/BusinessPartners('{quoted}')?$select=CardCode,BPAddresses", timeout=10).json() or {}
            addresses = {}
            for a in data.get('BPAddresses') or []:
                addresses.setdefault(a.get('AddressName'), set()).add(a.get('AddressType'))
            cache.set('sap_b2c_addresses:' + str(card_code), addresses, 5 * 60)
    except Exception as e:                                            # noqa: BLE001
        raise ValueError(f"Could not read the branch states / customer addresses from SAP: {str(e)[:160]}")
    state = states.get(branch_name)
    if not state:
        raise ValueError(f"SAP's business place '{branch_name}' has no State, so the sale's state is unknown. "
                         f"Fill the State of that business place in SAP, then retry.")
    have = addresses.get(state) or set()
    if not {'bo_ShipTo', 'bo_BillTo'} <= have:
        raise ValueError(f"SAP's customer {card_code} has no Ship-To and Bill-To address named '{state}' "
                         f"(the state of {branch_name}). Add them in SAP (Business Partner {card_code} > "
                         f"Addresses, Address Name = {state}, State = {state}), then retry.")
    return {"ShipToCode": state, "PayToCode": state}


def _resolve_bpl_id(branch_name, default=1):
    """
    Resolve a SAP BusinessPlace name to its BPLID.

    Results are cached for 10 minutes (in Django's cache) so we never hit
    the BusinessPlaces endpoint more than once per request burst.

    'No Branch' and None/'' are treated as unset and return `default`.
    """
    if not branch_name or branch_name == 'No Branch':
        return default

    cache_key = 'sap_bpl_map'
    bpl_map = cache.get(cache_key)

    if bpl_map is None:
        with _bp_cache_lock:
            # Double-checked locking — another thread may have populated by now
            bpl_map = cache.get(cache_key)
            if bpl_map is None:
                try:
                    bpl_map = {bp['BPLName']: bp['BPLID']
                               for bp in _get_all('BusinessPlaces?$select=BPLID,BPLName', timeout=8)}
                    cache.set(cache_key, bpl_map, 10 * 60)  # 10 minutes
                    logger.info(f"SAP: Cached {len(bpl_map)} BusinessPlaces")
                except Exception as e:
                    # A branch that is named but cannot be looked up must NOT fall
                    # back to business place 1: the document would post to the
                    # wrong branch's books and look like a success. Fail, and the
                    # job retries when SAP can be asked. (A caller that passes
                    # default=None is only listing and can do without.)
                    logger.warning(f"SAP: Could not fetch BusinessPlaces: {e}")
                    if default is None:
                        return None
                    raise ValueError(
                        f"Could not look up SAP business place '{branch_name}': {str(e)[:160]}")

    if branch_name in bpl_map:
        return bpl_map[branch_name]
    if default is None:
        return None
    raise ValueError(
        f"SAP has no business place named '{branch_name}'. Fix the branch's SAP branch name "
        f"(Admin > Centres) so it matches SAP, then retry.")




import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Global connection pool
_sap_session = requests.Session()
_retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[ 500, 502, 503, 504 ])
_sap_session.mount('http://', HTTPAdapter(pool_connections=10, pool_maxsize=50, max_retries=_retries))
_sap_session.mount('https://', HTTPAdapter(pool_connections=10, pool_maxsize=50, max_retries=_retries))
_sap_session.verify = bool(getattr(_settings, 'SAP_VERIFY_TLS', False))

def get_sap_session(force_refresh=False):
    if not sap_enabled():
        return None, None
    if not force_refresh:
        sess = cache.get('sap_b1session')
        route = cache.get('sap_routeid')
        if sess:
            return sess, route

    login_url = f"{SAP_URL_BASE}/Login"
    try:
        response = _sap_session.post(login_url, json=SAP_CREDENTIALS, timeout=10)
        response.raise_for_status()
        cookies = response.cookies
        b1session = cookies.get('B1SESSION')
        routeid = cookies.get('ROUTEID')

        if b1session:
            # SAP sessions typically last 30 minutes, we cache for 25 mins
            cache.set('sap_b1session', b1session, 25 * 60)
            cache.set('sap_routeid', routeid, 25 * 60)

        return b1session, routeid
    except Exception as e:
        logger.error(f"SAP Login Failed: {e}")
        # So the failed job says WHY (wrong password, SAP down) instead of a bare
        # "could not obtain SAP session".
        _last_error.text = f"SAP login failed: {_sap_error_text(getattr(getattr(e, 'response', None), 'text', '') or e)}"
        return None, None

def _cookie_dict(b1session, routeid):
    cookies = {}
    if b1session:
        cookies['B1SESSION'] = b1session
    if routeid:
        cookies['ROUTEID'] = routeid
    return cookies

def _send_sap_request(method, url, timeout=15, **kwargs):
    """Send SAP request and automatically refresh token and retry if 401 Unauthorized."""
    if not sap_enabled():
        raise SapDisabled("SAP is switched off (SAP_ENABLED=0 or not configured)")
    b1session, routeid = get_sap_session()
    if not b1session:
        raise Exception("Could not obtain SAP session")

    kwargs['cookies'] = _cookie_dict(b1session, routeid)
    response = _sap_session.request(method, url, timeout=timeout, **kwargs)

    if response.status_code == 401:
        logger.info("SAP Session expired (401). Refreshing token silently...")
        b1session, routeid = get_sap_session(force_refresh=True)
        if b1session:
            kwargs['cookies'] = _cookie_dict(b1session, routeid)
            response = _sap_session.request(method, url, timeout=timeout, **kwargs)

    # A 404 on a GET is a lookup ("is it there yet?"), not a refusal.
    if response.status_code >= 400 and not (method == 'GET' and response.status_code == 404):
        _last_error.text = f"HTTP {response.status_code} - {_sap_error_text(getattr(response, 'text', ''))}"
    response.raise_for_status()
    return response

def check_sap_bp_exists(card_code):
    """Return True if the BusinessPartner already exists in SAP."""
    try:
        url = f"{SAP_URL_BASE}/BusinessPartners('{card_code}')"
        _send_sap_request('GET', url, timeout=10)
        return True
    except requests.exceptions.HTTPError as e:
        if e.response.status_code == 404:
            return False
        raise
    except Exception:
        return False


_BP_STATE_CODES = {
    '01': 'JK', '02': 'HP', '03': 'PB', '04': 'CH', '05': 'UT', '06': 'HR',
    '07': 'DL', '08': 'RJ', '09': 'UP', '10': 'BR', '11': 'SK', '12': 'AR',
    '13': 'NL', '14': 'MN', '15': 'MZ', '16': 'TR', '17': 'ML', '18': 'AS',
    '19': 'WB', '20': 'JH', '21': 'OR', '22': 'CG', '23': 'MP', '24': 'GJ',
    '26': 'DN', '27': 'MH', '28': 'AP', '29': 'KA', '30': 'GA', '31': 'LD',
    '32': 'KL', '33': 'TN', '34': 'PY', '35': 'AN', '36': 'TG', '37': 'AP',
    '38': 'LA',
}


def _is_gstin(value):
    text = (value or '').strip()
    return len(text) == 15 and text[:2] in _BP_STATE_CODES


def _bp_branch_assignments():
    """The partner is available in every business place, the way SAP's own partners are set up
    (each of them carries one row per business place). [] when SAP's business places cannot be read."""
    try:
        _resolve_bpl_id('__warm_the_list__', default=None)           # fills the cache of {name: id}
        ids = sorted(set((cache.get('sap_bpl_map') or {}).values()))
    except Exception:                                               # noqa: BLE001
        return []
    return [{"BPLID": bpl, "DisabledForBP": "tNO"} for bpl in ids]


def _bp_addresses(state_code, gstin='', street='', city='', pin_code=''):
    """Ship-to and bill-to addresses. SAP keeps a partner's GSTIN on its ADDRESSES (CRD1.GSTRegnNo), not only on
    the partner: without it SAP treats the partner as an unregistered buyer or seller on every GST document."""
    addresses = []
    for kind in ("bo_ShipTo", "bo_BillTo"):
        row = {"AddressName": "Default", "AddressType": kind, "State": state_code, "Country": "IN"}
        if street:
            row["Street"] = street[:100]
        if city:
            row["City"] = city[:100]
        if pin_code:
            row["ZipCode"] = str(pin_code)[:20]
        if _is_gstin(gstin):
            row["GSTIN"] = gstin.strip().upper()
            row["GstType"] = "gstRegularTDSISD"
        addresses.append(row)
    return addresses


def _is_duplicate_body(body):
    lowered = (body or '').lower()
    return ('"code" : -10' in body or '"code" : -2035' in body or '"code" : -5002' in body
            or 'already exists' in lowered or 'already assigned' in lowered)


def _post_business_partner(rich, plain):
    """POST the partner with its group, branch assignments and addresses; when SAP refuses THOSE (not because
    the partner exists already) post the plain partner instead. Returns (response, note): the note says what
    was left out, so the sync message tells a person the partner needs finishing in SAP."""
    url = f"{SAP_URL_BASE}/BusinessPartners"
    try:
        return _send_sap_request('POST', url, json=rich, timeout=_post_timeout()), ''
    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '') or ''
        if getattr(e.response, 'status_code', 0) != 400 or _is_duplicate_body(body):
            raise
        logger.warning(f"SAP refused the full partner ({_sap_error_text(body)[:200]}); posting the plain one.")
        note = f" - created WITHOUT group / branch assignment / GSTIN on the address: {_sap_error_text(body)[:120]}"
        return _send_sap_request('POST', url, json=plain, timeout=_post_timeout()), note


def _update_business_partner(card_code, name, phone, email, tax_id=None):
    """Bring a partner the POS made earlier up to date with what the POS says now (name, phone, e-mail,
    tax id). Addresses are not touched: SAP replaces a whole address list on PATCH."""
    fields = {"CardName": name, "Phone1": phone or "", "EmailAddress": email or ""}
    if tax_id:
        fields["FederalTaxID"] = tax_id
    _send_sap_request('PATCH', f"{SAP_URL_BASE}/BusinessPartners('{card_code}')", json=fields,
                      timeout=_post_timeout())


def create_sap_business_partner(client):
    """
    Sync a POS client to SAP as a Business Partner.
    Walk-in customers don't have GST/PAN – we use PANNOTAVBL as the official
    SAP India placeholder. No TDS/deductee fields are sent because retail
    customers are not subject to TDS withholding.
    """
    try:
        if not (hasattr(client, 'gst_number') and client.gst_number and len(str(client.gst_number)) >= 15):
            logger.info(f"SAP: Client {client.id} is B2C (no valid GST) - skipping individual BP creation in favor of generic B2C BP")
            client.sap_sync_status = True
            client.sap_sync_message = "Mapped to generic B2C"
            client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        card_code = f"C{client.id:06d}"

        # A partner the POS made earlier: bring SAP up to date with what the POS says now (name, phone, e-mail,
        # GSTIN), instead of leaving the first values in SAP for ever.
        if check_sap_bp_exists(card_code):
            logger.info(f"SAP BP '{card_code}' already exists – updating its details")
            _update_business_partner(card_code, client.full_name or f"Client {client.id}", client.phone,
                                     client.email, str(client.gst_number).strip().upper())
            client.sap_sync_status = True
            client.sap_sync_message = "Already exists in SAP - details updated"
            client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        # Derive PAN from GSTIN (positions 3-12) if the customer provided it,
        # otherwise use a dummy PAN format (5 letters, 4 numbers, 1 letter).
        # Walk-in customers will never have a GST/PAN – that is perfectly normal.
        # Common GST state codes to SAP state codes
        gst_state_map = {
            '01': 'JK', '02': 'HP', '03': 'PB', '04': 'CH', '05': 'UT', '06': 'HR',
            '07': 'DL', '08': 'RJ', '09': 'UP', '10': 'BR', '11': 'SK', '12': 'AR',
            '13': 'NL', '14': 'MN', '15': 'MZ', '16': 'TR', '17': 'ML', '18': 'AS',
            '19': 'WB', '20': 'JH', '21': 'OR', '22': 'CG', '23': 'MP', '24': 'GJ',
            '26': 'DN', '27': 'MH', '28': 'AP', '29': 'KA', '30': 'GA', '31': 'LD',
            '32': 'KL', '33': 'TN', '34': 'PY', '35': 'AN', '36': 'TG', '37': 'AP',
            '38': 'LA',
        }
        
        # Determine the default state based on the Client's Center's GST or Region
        default_state = "UP"
        if client.center:
            if client.center.gst_number and len(client.center.gst_number) == 15:
                default_state = gst_state_map.get(client.center.gst_number[:2], "UP")
            elif 'delhi' in (client.center.region or '').lower() or 'delhi' in (client.center.center_name or '').lower():
                default_state = "DL"
            elif 'haryana' in (client.center.region or '').lower() or 'gurgaon' in (client.center.center_name or '').lower():
                default_state = "HR"

        # Derive State from GSTIN for B2B customers, default to Center's State for B2C
        state_code = default_state
        if client.gst_number and len(client.gst_number) == 15:
            pan = client.gst_number[2:12]
            federal_tax_id = client.gst_number
            state_code = gst_state_map.get(client.gst_number[:2], default_state)
        else:
            pan = "AAAAA0000A"
            federal_tax_id = None

        payload = {
            "CardCode": card_code,
            "CardName": client.full_name or f"Client {client.id}",
            "CardType": "cCustomer",
            "Phone1": client.phone or "",
            "EmailAddress": client.email or "",
            "DebitorAccount": _sap("sap.account.debtor"),
            "BPFiscalTaxIDCollection": [
                {
                    "TaxId0": pan,
                    "AddrType": "bo_ShipTo"
                }
            ],
            "BPAddresses": [
                {
                    "AddressName": "Default",
                    "AddressType": "bo_ShipTo",
                    "State": state_code,
                    "Country": "IN"
                },
                {
                    "AddressName": "Default",
                    "AddressType": "bo_BillTo",
                    "State": state_code,
                    "Country": "IN"
                }
            ]
        }

        if federal_tax_id:
            payload["FederalTaxID"] = federal_tax_id

        # The full partner: customer group, GSTIN on the addresses (where SAP keeps it) and a branch assignment
        # for every business place. If SAP refuses any of that, the plain partner above is posted and the sync
        # message says what is missing.
        rich = dict(payload)
        rich["GroupCode"] = int(_sap('sap.b2b_customer_group'))
        rich["BPAddresses"] = _bp_addresses(state_code, client.gst_number)
        assignments = _bp_branch_assignments()
        if assignments:
            rich["BPBranchAssignment"] = assignments

        response, note = _post_business_partner(rich, payload)
        if response.status_code in (200, 201):
            logger.info(f"SAP: Created Business Partner '{card_code}' for client {client.id}")
            client.sap_sync_status = True
            client.sap_sync_message = ("Success" + note)[:500]
            client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        logger.error(f"SAP: Unexpected status {response.status_code} creating BP")
        return False

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        
        is_duplicate = False
        body_lower = body.lower()
        if '"code" : -10' in body or '"code" : -2035' in body or 'already exists' in body_lower or 'already assigned' in body_lower:
            is_duplicate = True

        if is_duplicate and check_sap_bp_exists(f"C{client.id:06d}"):
            logger.info(f"SAP BP 'C{client.id:06d}' already exists – marking synced")
            client.sap_sync_status = True
            client.sap_sync_message = "Already exists in SAP"
            client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True
            
        logger.error(f"SAP Business Partner creation error: HTTP {e.response.status_code} – {body}")

        client.sap_sync_status = False
        client.sap_sync_message = f"HTTP {e.response.status_code}: {body[:300]}"
        client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except Exception as e:
        logger.error(f"SAP Business Partner creation error: {e}")
        client.sap_sync_status = False
        client.sap_sync_message = str(e)[:300]
        client.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False


def check_sap_item_exists(item_code):
    try:
        url = f"{SAP_URL_BASE}/Items('{item_code}')?$select=ItemCode"
        response = _send_sap_request('GET', url, timeout=5)
        return response.status_code == 200
    except Exception:
        return False

def create_sap_marketing_item(item_code, item_name, price, gst_percent):
    if check_sap_item_exists(item_code):
        return True
    try:
        tax_code = _gst_tax_code(gst_percent)
        payload = {
            "ItemCode": item_code,
            "ItemName": item_name[:100],
            "ItemsGroupCode": _sap("sap.items_group_code"),
            "PurchaseItem": "tYES",
            "SalesItem": "tYES",
            "InventoryItem": "tNO",
            "VatLiable": "tYES",
            "GSTRelevnt": "tYES",
            "ArTaxCode": tax_code,
            "ApTaxCode": tax_code,
        }
        if price > 0:
            payload["ItemPrices"] = [{"PriceList": _sap("sap.price_list"), "Price": float(price)}]

        url = f"{SAP_URL_BASE}/Items"
        response = _send_sap_request('POST', url, json=payload, timeout=_post_timeout())
        if response.status_code in (200, 201):
            logger.info(f"SAP: Created Marketing Item '{item_code}'")
            return True
        return False
    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '').lower()
        if '"code" : -10' in body or 'already exists' in body:
            return True
        logger.error(f"SAP Marketing Item creation error: {body}")
        return False
    except Exception as e:
        logger.error(f"SAP Marketing Item creation error: {e}")
        return False

def _item_group_code(category):
    """SAP's item group number for a POS product category (Retail -> RETAIL, Technical ->
    TECHNICAL ...). The groups are read from SAP and kept for ten minutes; a category SAP has no
    group for falls back to the group chosen in Settings."""
    fallback = _sap("sap.items_group_code")
    wanted = (category or '').strip().upper()
    if not wanted:
        return fallback
    names = cache.get('sap_item_groups')
    if names is None:
        try:
            names = {g['GroupName'].strip().upper(): g['Number']
                     for g in _get_all('ItemGroups?$select=Number,GroupName', timeout=8)}
            cache.set('sap_item_groups', names, 10 * 60)
        except Exception as exc:                                    # noqa: BLE001
            logger.warning('SAP: could not read item groups: %s', exc)
            return fallback
    return names.get(wanted, fallback)


def _item_udfs(product):
    """The item master's own fields for tax rate, brand and sub group (what SAP's items carry)."""
    udfs = {}
    gst = product.gst_percent
    if gst is not None:
        udfs['U_TAXRT'] = str(int(gst)) if float(gst) == int(gst) else str(gst)
    if product.brand:
        udfs['U_BRAND'] = str(product.brand)[:100]
    if product.sub_category:
        udfs['U_SUB_GR'] = str(product.sub_category)[:100]
    return udfs


def _item_hsn(product):
    """Point the SAP item at its row of the HSN list. Without one SAP refuses every GST document for the
    item ("You must select HSN in line 1"). Nothing is sent when the product has no HSN or SAP's list has no
    such code."""
    from pos_backend import sap_hsn

    entry = sap_hsn.hsn_entry(product.sac_code)
    return {"ChapterID": entry} if entry else {}


def create_sap_item(product):
    payload = {}
    try:

        item_code = product.product_code or f"P{product.id:06d}"
        tax_code = _gst_tax_code(product.gst_percent)

        payload = {
            "ItemCode": item_code,
            "ItemName": product.name or f"Product {product.id}",
            "ItemsGroupCode": _item_group_code(product.category),
            **_item_udfs(product),
            **_item_hsn(product),
            "PurchaseItem": "tYES",
            "SalesItem": "tYES",
            "InventoryItem": "tYES",
            # Like the rest of the item master (1,924 of 1,927 are batch-managed),
            # so receipts and issues name batches the same way for every item.
            "ManageBatchNumbers": "tYES",
            "VatLiable": "tYES",
            "PurchaseUnit": "Pcs",
            "PurchaseItemsPerUnit": 1,
            "SalesUnit": "Pcs",
            "SalesItemsPerUnit": 1,
            "InventoryUoMEntry": -1,
            "UoMGroupEntry": -1,
            # The unit the POS counts the product in (PCS, ML ...). Left out, SAP keeps the item with no unit, and
            # the unit sync then turns a POS product in ml back into pcs.
            "InventoryUOM": (product.unit or 'pcs').strip().upper()[:20],
            "GSTRelevnt": "tYES",
            # Tax codes set once on the item master. SAP inherits these
            # automatically in all downstream documents (invoices, GRPOs,
            # credit notes). We never override TaxCode on individual lines.
            "ArTaxCode": tax_code,
            "ApTaxCode": tax_code,
        }
        
        if product.barcode:
            payload["BarCode"] = product.barcode
        
        price = float(product.price or 0)
        if price > 0:
            payload["ItemPrices"] = [{"PriceList": _sap("sap.price_list"), "Price": price}]

        url = f"{SAP_URL_BASE}/Items"
        response = _send_sap_request('POST', url, json=payload, timeout=_post_timeout())
        if response.status_code in (200, 201):
            logger.info(f"SAP: Created Item '{item_code}' for product {product.id}")
            product.sap_sync_status = True
            product.sap_sync_message = "Success"
            product.product_code = item_code
            product.save(update_fields=['sap_sync_status', 'sap_sync_message', 'product_code'])
            return True

        return False

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        
        is_duplicate = False
        body_lower = body.lower()
        if '"code" : -10' in body or '"code" : -2035' in body or '"code" : -5002' in body or 'already exists' in body_lower or 'already assigned' in body_lower:
            is_duplicate = True

        if is_duplicate and check_sap_item_exists(item_code):
            logger.info(f"SAP Item '{item_code}' already exists – patching tax codes from master")
            # Keep the SAP item master's tax codes in sync with the POS product's
            # current gst_percent. This handles the case where a product's GST%
            # was changed after the item was first created in SAP.
            tax_code = _gst_tax_code(product.gst_percent)
            patch_payload = {"ItemName": product.name or f"Product {product.id}",
                             "ArTaxCode": tax_code, "ApTaxCode": tax_code, **_item_udfs(product),
                             **_item_hsn(product)}
            if product.barcode:
                patch_payload["BarCode"] = product.barcode
            price = float(product.price or 0)
            if price > 0:
                patch_payload["ItemPrices"] = [{"PriceList": _sap("sap.price_list"), "Price": price}]
            try:
                _send_sap_request(
                    'PATCH', f"{SAP_URL_BASE}/Items('{item_code}')",
                    json=patch_payload,
                    timeout=10,
                )
            except Exception as patch_err:
                # The item is in SAP but still carries the old name, tax codes
                # and price: say so and retry, instead of reporting success.
                logger.warning(f"SAP: Could not patch fields for item '{item_code}': {patch_err}")
                product.sap_sync_status = False
                product.sap_sync_message = ("Already in SAP, but its details could not be updated: "
                                            + _sap_error_text(getattr(getattr(patch_err, 'response', None), 'text', '') or patch_err))[:300]
                product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
                return False
            product.sap_sync_status = True
            product.sap_sync_message = "Already exists in SAP – name, tax codes and price updated"
            product.product_code = product.product_code or item_code
            product.save(update_fields=['sap_sync_status', 'sap_sync_message', 'product_code'])
            return True

        if not is_duplicate and 'InventoryUOM' in body and payload.get('InventoryUOM'):
            # SAP does not take the unit as text on this item: post it without, and say so.
            logger.warning("SAP: InventoryUOM not accepted; retrying without it")
            try:
                retry = {k: v for k, v in payload.items() if k != 'InventoryUOM'}
                if _send_sap_request('POST', f"{SAP_URL_BASE}/Items", json=retry, timeout=_post_timeout()).status_code in (200, 201):
                    product.sap_sync_status = True
                    product.sap_sync_message = "Success (SAP did not accept the unit of measure: set it on the item in SAP)"
                    product.product_code = item_code
                    product.save(update_fields=['sap_sync_status', 'sap_sync_message', 'product_code'])
                    return True
            except Exception as retry_err:                          # noqa: BLE001
                logger.error(f"SAP Item retry without the unit failed: {retry_err}")

        if not is_duplicate and any(k in body for k in ('U_TAXRT', 'U_BRAND', 'U_SUB_GR')) and _item_udfs(product):
            # This SAP company has no such item field: post the item without the extras.
            logger.warning("SAP: item fields U_TAXRT/U_BRAND/U_SUB_GR not accepted; retrying without them")
            try:
                retry = {k: v for k, v in payload.items() if k not in ('U_TAXRT', 'U_BRAND', 'U_SUB_GR')}
                if _send_sap_request('POST', f"{SAP_URL_BASE}/Items", json=retry, timeout=_post_timeout()).status_code in (200, 201):
                    product.sap_sync_status = True
                    product.sap_sync_message = "Success (this SAP has no tax-rate / brand item fields)"
                    product.product_code = item_code
                    product.save(update_fields=['sap_sync_status', 'sap_sync_message', 'product_code'])
                    return True
            except Exception as retry_err:                          # noqa: BLE001
                logger.error(f"SAP Item retry without extras failed: {retry_err}")
        logger.error(f"SAP Item creation error: HTTP {e.response.status_code} – {body}")
        product.sap_sync_status = False
        product.sap_sync_message = f"HTTP {e.response.status_code}: {body[:300]}"
        product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except Exception as e:
        logger.error(f"SAP Item creation error: {e}")
        product.sap_sync_status = False
        product.sap_sync_message = str(e)[:300]
        product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False


def _item_is_switched_off(product):
    """True when no branch of the POS sells this item any more. SAP keeps ONE item for all branches, so it is
    frozen only when every branch's copy is switched off, and unfrozen as soon as one is switched on."""
    from inventory.models import Product
    code = (product.product_code or '').strip()
    if not code:
        return not product.is_active
    return not Product.objects.filter(product_code__iexact=code, is_active=True).exists()


def update_sap_item(product):
    try:
        item_code = product.product_code
        if not item_code:
            return False

        tax_code = _gst_tax_code(product.gst_percent)
        price = float(product.price or 0)

        payload = {
            "ItemName": product.name or f"Product {product.id}",
            "ItemsGroupCode": _item_group_code(product.category),
            "ArTaxCode": tax_code,
            "ApTaxCode": tax_code,
            # Switched off in the item master = inactive (frozen) in SAP, and back again when switched on.
            "Frozen": "tYES" if _item_is_switched_off(product) else "tNO",
            **_item_udfs(product),
            **_item_hsn(product),
        }
        if product.barcode:
            payload["BarCode"] = product.barcode
        
        if price > 0:
            payload["ItemPrices"] = [{"PriceList": _sap("sap.price_list"), "Price": price}]

        url = f"{SAP_URL_BASE}/Items('{item_code}')"
        response = _send_sap_request('PATCH', url, json=payload, timeout=10)
        if response.status_code in (204, 200, 201):
            logger.info(f"SAP: Updated Item '{item_code}' for product {product.id}")
            product.sap_sync_status = True
            product.sap_sync_message = "Success"
            product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        return False
    except requests.exceptions.HTTPError as e:
        if getattr(e.response, 'status_code', 0) == 404:
            # Never reached SAP (its first posting failed or is still waiting): send the whole item instead.
            return create_sap_item(product)
        logger.error(f"SAP: Error updating item: {e}")
        product.sap_sync_status = False
        product.sap_sync_message = (f"HTTP {e.response.status_code}: "
                                    f"{_sap_error_text(getattr(e.response, 'text', ''))}")[:255]
        product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
    except Exception as e:
        logger.error(f"SAP: Error updating item: {e}")
        product.sap_sync_status = False
        product.sap_sync_message = str(e)[:255]
        product.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def _service_item_fields(service):
    """What SAP's service items carry (read from the existing ones): the group, the SAC row, tax rate,
    brand and sub group. A service is a sales item that is not stocked."""
    from pos_backend import sap_hsn

    fields = {"ItemName": (service.name or f"Service {service.id}")[:100],
              "ItemsGroupCode": _item_group_code(service.category)}
    udfs = {}
    if service.tax_percentage is not None:
        gst = float(service.tax_percentage)
        udfs['U_TAXRT'] = str(int(gst)) if gst == int(gst) else str(gst)
    if service.brand:
        udfs['U_BRAND'] = str(service.brand)[:100]
    if service.sub_category:
        udfs['U_SUB_GR'] = str(service.sub_category)[:100]
    entry = sap_hsn.sac_entry(service.hsn_code or service.sac_code)
    if entry is not None:
        fields["SACEntry"] = entry
    return fields, udfs


def create_sap_service(service):
    """Send a service made (or changed) in the POS to SAP as a non-stock sales item, so a bill for it can be
    posted. Already there: its name, group, tax rate and SAC are brought up to date instead."""
    item_code = service.service_code or f"S{service.id:06d}"

    def done(message):
        service.sap_sync_status = True
        service.sap_sync_message = message
        update = ['sap_sync_status', 'sap_sync_message']
        if service.service_code != item_code:
            service.service_code = item_code
            update.append('service_code')
        service.save(update_fields=update)
        return True

    def failed(message):
        service.sap_sync_status = False
        service.sap_sync_message = str(message)[:300]
        service.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    try:
        fields, udfs = _service_item_fields(service)
        base = {"ItemCode": item_code, **fields,
                "InventoryItem": "tNO", "SalesItem": "tYES", "PurchaseItem": "tNO",
                "VatLiable": "tYES", "GSTRelevnt": "tYES", "GSTTaxCategory": "gtc_Regular"}
        exists = check_sap_item_exists(item_code)
        if exists:
            _send_sap_request('PATCH', f"{SAP_URL_BASE}/Items('{item_code}')", json={**fields, **udfs}, timeout=10)
            return done("Already in SAP: name, group, tax rate and SAC updated")
        try:
            _send_sap_request('POST', f"{SAP_URL_BASE}/Items", json={**base, **udfs}, timeout=_post_timeout())
        except requests.exceptions.HTTPError as e:
            body = getattr(e.response, 'text', '') or ''
            if udfs and any(k in body for k in udfs):
                # This SAP company has no such item field: send the item without the extras.
                _send_sap_request('POST', f"{SAP_URL_BASE}/Items", json=base, timeout=_post_timeout())
                return done("Success (this SAP has no tax-rate / brand item fields)")
            raise
        logger.info(f"SAP: Created service item '{item_code}'")
        return done("Success")
    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '') or ''
        if 'already exists' in body.lower() or '"code" : -10' in body or '"code" : -2035' in body:
            return done("Already exists in SAP")
        logger.error(f"SAP service item error: HTTP {e.response.status_code} - {body}")
        return failed(f"HTTP {e.response.status_code}: {_sap_error_text(body)}")
    except Exception as e:                                            # noqa: BLE001
        logger.error(f"SAP service item error: {e}")
        return failed(e)


def create_sap_vendor(vendor):
    """
    Sync a POS Vendor to SAP as a Business Partner (Supplier).
    """
    try:
        card_code = vendor.vendor_code or f"V{vendor.id:06d}"

        # If BP already exists in SAP just mark it synced and move on
        if check_sap_bp_exists(card_code):
            logger.info(f"SAP Vendor '{card_code}' already exists – bringing its details up to date")
            # A vendor changed in the POS: send what the POS says now. Blank fields are left alone, so a
            # partner SAP already fills in is never emptied.
            fields = {"CardName": vendor.name or f"Vendor {vendor.id}"}
            if vendor.phone:
                fields["Phone1"] = vendor.phone
            if vendor.email:
                fields["EmailAddress"] = vendor.email
            if vendor.cst_number:
                fields["FederalTaxID"] = vendor.cst_number
            _send_sap_request('PATCH', f"{SAP_URL_BASE}/BusinessPartners('{card_code}')", json=fields,
                              timeout=_post_timeout())
            vendor.sap_sync_status = True
            vendor.sap_sync_message = "Already exists in SAP - name, phone, e-mail and tax id updated"
            vendor.vendor_code = card_code
            vendor.save(update_fields=['sap_sync_status', 'sap_sync_message', 'vendor_code'])
            return True

        pan = vendor.pan_number if vendor.pan_number else "AAAAA0000A"
        federal_tax_id = vendor.cst_number if vendor.cst_number else None

        payload = {
            "CardCode": card_code,
            "CardName": vendor.name or f"Vendor {vendor.id}",
            "CardType": "cSupplier",
            "Phone1": vendor.phone or "",
            "EmailAddress": vendor.email or "",
            "BPFiscalTaxIDCollection": [
                {
                    "TaxId0": pan,
                    "AddrType": "bo_ShipTo"
                }
            ]
        }

        if federal_tax_id:
            payload["FederalTaxID"] = federal_tax_id

        # The full vendor: vendor group, a branch assignment for every business place and, when it has a GSTIN,
        # its addresses carrying that GSTIN (where SAP keeps it). If SAP refuses any of that, the plain vendor
        # above is posted and the sync message says what is missing.
        rich = dict(payload)
        rich["GroupCode"] = int(_sap('sap.vendor_group'))
        if _is_gstin(vendor.cst_number):
            rich["BPAddresses"] = _bp_addresses(_BP_STATE_CODES[vendor.cst_number.strip()[:2]], vendor.cst_number,
                                                street=vendor.address or '', city=vendor.city or '',
                                                pin_code=vendor.pin_code or '')
        assignments = _bp_branch_assignments()
        if assignments:
            rich["BPBranchAssignment"] = assignments

        response, note = _post_business_partner(rich, payload)
        if response.status_code in (200, 201):
            logger.info(f"SAP: Created Vendor '{card_code}' for vendor {vendor.id}")
            vendor.sap_sync_status = True
            vendor.sap_sync_message = ("Success" + note)[:500]
            vendor.vendor_code = card_code
            vendor.save(update_fields=['sap_sync_status', 'sap_sync_message', 'vendor_code'])
            return True

        logger.error(f"SAP: Unexpected status {response.status_code} creating Vendor")
        return False

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        
        is_duplicate = False
        body_lower = body.lower()
        if '"code" : -10' in body or '"code" : -2035' in body or '"code" : -5002' in body or 'already exists' in body_lower or 'already assigned' in body_lower:
            is_duplicate = True

        if is_duplicate and check_sap_bp_exists(card_code):
            logger.info(f"SAP Vendor '{card_code}' already exists – marking synced")
            vendor.sap_sync_status = True
            vendor.sap_sync_message = "Already exists in SAP"
            vendor.vendor_code = card_code
            vendor.save(update_fields=['sap_sync_status', 'sap_sync_message', 'vendor_code'])
            return True
            
        logger.error(f"SAP Vendor creation error: HTTP {e.response.status_code} – {body}")

        vendor.sap_sync_status = False
        vendor.sap_sync_message = f"HTTP {e.response.status_code}: {body[:300]}"
        vendor.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except Exception as e:
        logger.error(f"SAP Vendor creation error: {e}")
        vendor.sap_sync_status = False
        vendor.sap_sync_message = str(e)[:300]
        vendor.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False






def _resolve_sap_card_code(client):
    """The SAP business partner a client's documents post to. The one rule.

    * a client whose phone is a legacy SAP code (``CUST...``) keeps that code;
    * a client with a GSTIN (B2B) has a partner of their own, ``C<id:06d>``;
    * everyone else, walk-ins included, is the B2C partner from Settings.

    There used to be four versions of this: the invoice sent walk-ins to
    ``WALKIN`` while their payments went to ``B2C``, so SAP could not apply a
    walk-in's payment to their invoice, and the advance journal named every
    client ``C<id>`` whether or not that partner existed.
    """
    if client is not None:
        phone = getattr(client, 'phone', '') or ''
        if phone.startswith('CUST'):
            return phone
        gstin = getattr(client, 'gst_number', '') or ''
        if len(str(gstin)) >= 15:
            return f"C{client.id:06d}"
    return _sap('sap.b2c_card_code')

def _sap_priced_lines(item, item_code, wh_code):
    """(line, extra_line or None) for one POS bill line, priced as the bill.

    The line's own net value - after its line discount and its share of the
    bill discount - in Decimal (X11). The bill discount used to go to SAP as
    one header DiscountPercent, which also discounted products when the offer
    was for services only. A value card's part is taxed at the card's rate
    (F5); when that rate differs from the line's and the card paid only part
    of the line, the rest goes as a second line of its own.
    """
    qty = Decimal(int(item.quantity or 1))
    net = Decimal(str(item.total_price)) - Decimal(str(item.bill_discount_share or 0))
    tax_pct = getattr(item, 'tax_percentage', 0)
    card_part = (Decimal(str(item.card_taxable or 0))
                 if Decimal(str(item.card_face or 0)) > 0 else Decimal('0'))
    card_pct = getattr(item, 'card_tax_percentage', 0)
    extra_line = None
    if card_part > 0:
        rest = net - card_part
        if rest > 0 and Decimal(str(card_pct)) != Decimal(str(tax_pct)):
            extra_line = {
                "ItemCode": item_code,
                "Quantity": "1",
                "UnitPrice": str(rest.quantize(Decimal('0.01'))),
                "WarehouseCode": wh_code,
                "_TaxPercent": tax_pct,
            }
            net = card_part
        tax_pct = card_pct
    line = {
        "ItemCode": item_code,
        "Quantity": str(item.quantity),
        "UnitPrice": str((net / qty).quantize(Decimal('0.000001'))),
        "WarehouseCode": wh_code,
        "_TaxPercent": tax_pct,
    }
    return line, extra_line


def create_sap_invoice(invoice):
    """
    Sync a POS Invoice to SAP as an A/R Invoice.
    """
    try:
        if invoice.sap_doc_entry:
            return True
            
        # Idempotency check: Does SAP already have this invoice?
        # If SAP cannot be asked, do not post. An earlier attempt whose reply
        # was lost may already have created it; posting anyway is how the same
        # bill reached SAP twice. The retry queue asks again later.
        try:
            from pos_backend.sap_ledger import odata_filter, odata_str
            pos_no = invoice.display_number or str(invoice.id)
            check_url = (f"{SAP_URL_BASE}/Invoices?$select=DocEntry&$filter="
                         + odata_filter(f"U_POSINNO eq {odata_str(pos_no)}"))
            res = _send_sap_request('GET', check_url, timeout=10)
            existing = (res.json() or {}).get('value') or []
        except Exception as e:
            logger.warning(f"SAP Idempotency check failed for Invoice {invoice.id}: {e}")
            invoice.sap_sync_status = False
            invoice.sap_sync_message = f"Could not check SAP for an existing copy; will retry. {str(e)[:200]}"
            invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return False
        if existing:
            existing_entry = existing[0].get('DocEntry')
            logger.info(f"SAP: Invoice {invoice.id} already exists in SAP as DocEntry {existing_entry}")
            invoice.sap_doc_entry = existing_entry
            invoice.sap_sync_status = True
            invoice.sap_sync_message = "Success (Recovered from SAP)"
            invoice.save(update_fields=['sap_doc_entry', 'sap_sync_status', 'sap_sync_message'])
            from pos_backend.sap_ledger import mark_done
            mark_done(f"invoice:{invoice.id}", existing_entry, note='Recovered from SAP')
            # The payment step runs after the invoice; recovering the invoice
            # must not strand it. It posts only what is not already in SAP.
            if existing_entry and invoice.paid_amount > 0:
                run_sap_sync_async(create_sap_incoming_payment, invoice)
            return True

        client = None
        if invoice.client_id:
            from clients.models import Client
            client = Client.objects.filter(pk=invoice.client_id).first()
        card_code = _resolve_sap_card_code(client)

        # A B2B client's own partner is created first if SAP lacks it. The
        # shared B2C partner is never created from here.
        if client is not None and card_code == f"C{client.id:06d}" and not check_sap_bp_exists(card_code):
            if client:
                logger.info(f"SAP: BP '{card_code}' not found – auto-creating before invoice")
                bp_ok = create_sap_business_partner(client)
                if not bp_ok:
                    logger.error(f"SAP: BP creation failed for client {invoice.client_id}; Invoice skipped")
                    invoice.sap_sync_status = False
                    invoice.sap_sync_message = "BP sync failed – invoice skipped"
                    invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
                    return False

        # Build invoice lines – only inventory products
        document_lines = []
        
        # Use center's warehouse or default
        wh_code = _warehouse_code(invoice.center)

        for item in invoice.items.select_related('content_type').all():
            if not item.content_type or item.content_type.app_label not in ['inventory', 'services', 'marketing']:
                continue

            item_code = None
            if item.content_type.app_label == 'inventory':
                from inventory.models import Product
                product = Product.objects.filter(pk=item.object_id).first()
                if product:
                    item_code = product.product_code or f"P{product.id:06d}"
            elif item.content_type.app_label == 'services':
                from services.models import ServiceMaster
                service = ServiceMaster.objects.filter(pk=item.object_id).first()
                if service:
                    item_code = service.service_code or f"S{service.id:06d}"
            elif item.content_type.app_label == 'marketing':
                # Marketing items (Value Card, Membership, Package) are synced individually 
                # as separate AR Invoices + Journal Entries during finalize_invoice.
                # Skip them here to prevent duplicate billing in SAP.
                continue

            if not item_code:
                # A paid line whose product or service has been removed cannot be
                # described to SAP. Skipping it posted the bill short and reported
                # success; fail so it shows in Admin > SAP Sync. (A Rs 0 line
                # carries no value, so leaving it out changes nothing.)
                if Decimal(str(item.total_price or 0)) != 0:
                    raise ValueError(
                        f"'{item.description}' on this bill is no longer in the catalogue, so SAP "
                        f"cannot be told what was sold. Restore the item, then retry.")
                continue

            current_index = len(document_lines)
            
            # The items a service used go on the invoice under the service, at no price:
            # SAP takes their stock off the same document. For a service whose recipe the POS
            # owns these are the products chosen on the bill (what `consumables` holds: the
            # fixed items, the colour and shampoo picked, any extras; never a technical item).
            # For one whose recipe is the SAP Sales BOM they are that BOM's lines.
            children = []
            if item.content_type.app_label == 'services' and service:
                if service.recipe_source == 'pos':
                    from services import recipes
                    children = [(code, float(amount), plain_qty(amount))
                                for code, amount in recipes.deductions(item.consumables or []).items()]
                else:
                    from services.models import deducting_boms
                    for bom in deducting_boms(service.id, invoice.center):
                        child_qty = float(item.quantity * bom.quantity)
                        children.append((bom.product_code, child_qty, str(child_qty)))
            is_bom = bool(children)

            line, extra_line = _sap_priced_lines(item, item_code, wh_code)

            # Every item is its own row: the service, then each product it used, one after another.
            # (They used to go as a bill of materials, the service as a "tree" with its products under it.)
            # A service that is a sales BOM in SAP's item master must say it is NOT to be exploded, or SAP
            # adds its own rows for the BOM and wants batches for them.
            if item.content_type.app_label == 'services':
                line["TreeType"] = "iNotATree"
            document_lines.append(line)
            if is_bom:
                for child_code, child_qty, child_text in children:
                    child_line = {
                        "ItemCode": child_code,
                        "Quantity": child_text,
                        "UnitPrice": "0",
                        "WarehouseCode": wh_code,
                        "TreeType": "iNotATree",
                        "FreeText": f"Used in {item.description}"[:100],
                        "_Ingredient": True,          # ours only: stripped before sending, used to find these rows
                    }
                    batches = issue_batches(child_code, wh_code, child_qty)
                    if batches:
                        child_line["BatchNumbers"] = batches
                    document_lines.append(child_line)
            else:
                batches = issue_batches(item_code, wh_code, float(item.quantity))
                if batches:
                    line["BatchNumbers"] = batches
            if extra_line is not None:
                document_lines.append(extra_line)


        if not document_lines:
            logger.info(f"SAP: No product lines in invoice {invoice.id} – skipping Invoice")
            invoice.sap_sync_status = True
            invoice.sap_sync_message = "Skipped - No Products"
            invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        import datetime
        today_str = str(invoice.created_at.date() if invoice.created_at else datetime.date.today())
        
        bpl_id = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

        # The bill discount is inside each line's price (bill_discount_share).
        # Bills saved before Phase 6 have no shares, so theirs still goes as
        # one header percentage.
        total_discount_percent = 0.0
        has_shares = invoice.items.filter(bill_discount_share__gt=0).exists()
        if not has_shares and Decimal(str(invoice.subtotal)) > 0 and Decimal(str(invoice.discount or 0)) > 0:
            total_discount_percent = float(
                (Decimal(str(invoice.discount)) / Decimal(str(invoice.subtotal)) * 100)
                .quantize(Decimal('0.000001')))

        payload = {
            "CardCode": card_code,
            "DocDate": today_str,
            "DocDueDate": today_str,
            "BPL_IDAssignedToInvoice": bpl_id,
            "DocumentLines": document_lines,
            **_walk_in_address(card_code, invoice.center.sap_branch_name if invoice.center else None),
            **_get_udf_fields(invoice)
        }
        
        if total_discount_percent > 0:
            payload["DiscountPercent"] = round(total_discount_percent, 6)
            
        # Integrate POS Rounding
        if hasattr(invoice, 'rounding') and invoice.rounding and float(invoice.rounding) != 0.0:
            payload["Rounding"] = "tYES"
            payload["RoundingDiffAmount"] = float(invoice.rounding)

        def send_invoice(payload):
            import copy
            safe_payload = copy.deepcopy(payload)
            for line in safe_payload.get('DocumentLines', []):
                _apply_invoice_tax_code(invoice, line, line.pop('_TaxPercent', 0))
                line.pop('_Ingredient', None)

            import json
            logger.info(f"SAP Payload: {json.dumps(safe_payload)}")
            return _send_sap_request('POST', f"{SAP_URL_BASE}/Invoices", json=safe_payload, timeout=_post_timeout())

        # Fallbacks that change only HOW a line is described to SAP are kept.
        # The ones that changed WHAT was posted - rewriting 12% to 5%, stamping
        # HSN 1, patching SAP item masters, flipping IGST/CGST against the
        # GSTINs - are gone: such a bill now fails with SAP's message in
        # Admin > SAP Sync, where the SAP team fixes the master data once.
        stripped_ingredients = False
        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = send_invoice(payload)
                if response.status_code in (200, 201):
                    logger.info(f"SAP: Created Invoice for POS invoice {invoice.id}")
                    data = response.json()
                    doc_entry = data.get('DocEntry')
                    invoice.sap_sync_status = True
                    invoice.sap_sync_message = (
                        "Success - posted without its service ingredients: SAP has no cost "
                        "for one of them" if stripped_ingredients else "Success")
                    invoice.sap_doc_entry = doc_entry
                    invoice.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_doc_entry'])
                    # Recorded, so the payment step knows this invoice's
                    # payments are posted under the new rules.
                    from pos_backend.sap_ledger import mark_done
                    mark_done(f"invoice:{invoice.id}", doc_entry)

                    # Now trigger incoming payment if there is payment
                    if doc_entry and invoice.paid_amount > 0:
                        run_sap_sync_async(create_sap_incoming_payment, invoice)

                    return True
                break
            except requests.exceptions.HTTPError as e:
                body = getattr(e.response, 'text', '')
                logger.error(f"Attempt {attempt} failed with: {body}")
                # Fallback: Item cost not found (10001287)
                if ("10001287" in body) and attempt < max_retries - 1:
                    logger.warning(f"SAP item cost error. Stripping ingredients to allow revenue sync.")
                    stripped_ingredients = True
                    # The ingredients are not issued by this invoice, so say which they were on the invoice
                    # itself, instead of leaving a bare service line.
                    left_out = '; '.join(f"{l.get('ItemCode')} x{l.get('Quantity')}"
                                         for l in payload['DocumentLines'] if l.get('_Ingredient'))
                    payload['Comments'] = ((payload.get('Comments') or f"POS bill {invoice.display_number}.")
                                           + f" Ingredients NOT issued in SAP (no cost found): {left_out}")[:254]
                    payload['DocumentLines'] = [line for line in payload['DocumentLines'] if not line.get('_Ingredient')]
                    continue

                # Fallback 1: UoM errors (1470000315 or 1470000365)
                if ("UoM" in body or "1470000315" in body or "1470000365" in body) and attempt < max_retries - 1:
                    logger.warning(f"SAP UoM error, applying fix. Body: {body}")
                    uom_entry = 1 if attempt == 0 else -1
                    for line in payload['DocumentLines']:
                        line['UoMEntry'] = uom_entry
                    continue

                # Anything else (a tax code SAP lacks, an item without HSN,
                # a GSTIN state mismatch) is SAP's to fix: fail with its text.
                raise Exception(f"HTTP {e.response.status_code} - {_sap_error_text(body)}")

        logger.error(f"SAP: Unexpected status {response.status_code} creating Invoice")
        return False

    except Exception as e:
        logger.error(f"SAP Invoice creation error: {e}")
        invoice.sap_sync_status = False
        invoice.sap_sync_message = str(e)[:300]
        invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def _invoice_card_code(invoice):
    """The CardCode create_sap_invoice posts this invoice to (the one rule)."""
    client = None
    if invoice.client_id:
        from clients.models import Client
        client = Client.objects.filter(pk=invoice.client_id).first()
    return _resolve_sap_card_code(client)


def _apply_invoice_tax_code(invoice, line, tax_percent):
    """Set the GST tax code on a document line from the rate the POS charged.

    Intra-state (branch and client GSTIN in the same state, or a B2C client)
    is CG+SG@<rate>; otherwise IGST@<rate>. Sales-BOM ingredients and free
    lines are @0.

    Two things this no longer does (blueprint X7):
      * rewrite 12% to 5%. The bill charged the client 12%; SAP was told 5%,
        so SAP's tax never matched what was collected. A line SAP cannot take
        now fails with SAP's own message in Admin > SAP Sync instead.
      * send ``HSNEntry: 1`` on every line, which stamped one arbitrary HSN
        code on every item of a GST invoice. SAP now takes each line's HSN
        from its item master.
    """
    # The state the bill is raised from: the centre's own GSTIN, else the GSTIN of its SAP business place (no POS
    # centre has a GSTIN of its own, so a Haryana centre was taken for Delhi and a Haryana B2B client got IGST).
    # Only when neither is known does it default to Delhi, as the invoice does.
    from inventory.places import center_gst_code
    center_state = (center_gst_code(invoice.center) if invoice.center else '') or "07"
    client_state = center_state  # B2C defaults to the local state
    if invoice.client and getattr(invoice.client, 'gst_number', None) and len(invoice.client.gst_number) >= 2:
        client_state = invoice.client.gst_number[:2]
    is_intrastate = (str(center_state) == str(client_state))

    try:
        tax_pct = int(round(float(tax_percent or 0)))
    except (TypeError, ValueError):
        tax_pct = 0
    if 'TaxCode' not in line:
        free = bool(line.get('_Ingredient')) or line.get('UnitPrice', "1") == "0"
        suffix = "0" if free or tax_pct <= 0 else str(tax_pct)
        line['TaxCode'] = f"CG+SG@{suffix}" if is_intrastate else f"IGST@{suffix}"
    return line


def _link_to_base_invoice(doc_entry, lines):
    """Point credit-memo lines at the A/R invoice lines they reverse.

    Reads the invoice from SAP and matches lines by item code in order
    (ingredient lines of a sales BOM are skipped; the credit memo never
    carries them). A linked line copies price, tax and warehouse from its base
    line, so those are not sent. Raises if SAP cannot be read; the job retries.
    """
    res = _send_sap_request(
        'GET', f"{SAP_URL_BASE}/Invoices({int(doc_entry)})?$select=DocumentStatus,DocumentLines", timeout=15)
    data = res.json() or {}
    # SAP refuses to base a credit memo on a CLOSED invoice ("One of the base
    # documents has already been closed"), and a bill paid at the till is
    # closed in SAP. Linking a paid bill needs its incoming payment cancelled
    # first - an accounting choice for the SAP team (blueprint C5) - so until
    # then a paid bill's credit memo stays unlinked, as it always was.
    can_link = data.get('DocumentStatus') != 'bost_Close'
    open_lines = {}
    for base in data.get('DocumentLines') or []:
        if base.get('TreeType') == 'iIngredient' or str(base.get('FreeText') or '').startswith('Used in '):
            continue
        open_lines.setdefault(base.get('ItemCode'), []).append(base)

    # SAP does not copy the G/L account from the base line: it looks for the
    # item group's returns account, which the salon item groups do not have
    # ("G/L account is missing" - no credit memo had ever reached SAP). The
    # credit goes back to the account the sale went to, unless the SAP
    # mapping names a returns account.
    returns_account = _sap('sap.account.sales_returns')

    for line in lines:
        candidates = open_lines.get(line.get('ItemCode')) or []
        if not candidates:
            continue
        base = candidates.pop(0)
        account = returns_account or base.get('AccountCode')
        if account:
            line['AccountCode'] = account
        if not can_link:
            continue
        line['BaseType'] = 13  # A/R Invoice
        line['BaseEntry'] = int(doc_entry)
        line['BaseLine'] = base.get('LineNum')
        for copied in ('UnitPrice', 'TaxCode', 'WarehouseCode'):
            line.pop(copied, None)
    return lines


def create_sap_credit_note(invoice):
    """
    Sync a refunded/cancelled POS invoice to SAP as an A/R Credit Memo.

    The original card-code (CLI-{id:04d}) and item-code (PRD-{id:04d} / SRV-{id:04d})
    formats were inconsistent with every other SAP function in this file, which all
    use C{id:06d} and P{id:06d} respectively. Those mismatches cause guaranteed 404s
    from SAP whenever a credit note is attempted. This version uses the canonical
    formats and also uses the same dynamic tax code logic as create_sap_invoice.
    """
    try:
        if not invoice.sap_doc_entry:
            # We can't credit note an invoice that hasn't synced to SAP yet.
            # Enqueue so the scheduler retries once the invoice itself is synced.
            invoice.sap_sync_message = "Cannot create credit note: invoice not yet synced to SAP"
            invoice.save(update_fields=['sap_sync_message'])
            return False

        # The same customer the invoice was posted to. This used C{client id}
        # for every client, but a B2C client's invoice goes to the generic B2C
        # partner, so SAP rejected the credit note for a partner that does
        # not exist.
        card_code = _invoice_card_code(invoice)
        wh_code = _warehouse_code(invoice.center)

        document_lines = []
        for item in invoice.items.select_related('content_type').all():
            if not item.content_type or item.content_type.app_label not in ['inventory', 'services']:
                continue

            item_code = None
            if item.content_type.app_label == 'inventory':
                from inventory.models import Product
                product = Product.objects.filter(pk=item.object_id).first()
                if product:
                    item_code = product.product_code or f"P{product.id:06d}"
            elif item.content_type.app_label == 'services':
                from services.models import ServiceMaster
                service = ServiceMaster.objects.filter(pk=item.object_id).first()
                if service:
                    item_code = service.service_code or f"S{service.id:06d}"

            if not item_code:
                continue
                
            line, extra_line = _sap_priced_lines(item, item_code, wh_code)
            # Note: We intentionally do NOT include the Sales BOM (ingredients) in the Credit Note.
            # A refunded salon service does not physically return the consumed shampoo to the bottle.
            document_lines.append(line)
            if extra_line is not None:
                document_lines.append(extra_line)

        if not document_lines:
            logger.info(f"SAP: No product/service lines in credit note for invoice {invoice.id} – skipping")
            invoice.sap_sync_status = True
            invoice.sap_sync_message = "Credit Note Skipped – No Products"
            invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        # As on the invoice: the bill discount is inside each line's price;
        # bills saved before Phase 6 send it as one header percentage.
        total_discount_percent = 0.0
        has_shares = invoice.items.filter(bill_discount_share__gt=0).exists()
        if not has_shares and Decimal(str(invoice.subtotal)) > 0 and Decimal(str(invoice.discount or 0)) > 0:
            total_discount_percent = float(
                (Decimal(str(invoice.discount)) / Decimal(str(invoice.subtotal)) * 100)
                .quantize(Decimal('0.000001')))

        import datetime
        bpl_id = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

        payload = {
            "CardCode": card_code,
            "DocDate": str(invoice.created_at.date() if invoice.created_at else datetime.date.today()),
            "DocDueDate": str(invoice.created_at.date() if invoice.created_at else datetime.date.today()),
            "Comments": f"Refund for POS Bill {invoice.display_number}",
            "BPL_IDAssignedToInvoice": bpl_id,
            "DocumentLines": document_lines,
            **_walk_in_address(card_code, invoice.center.sap_branch_name if invoice.center else None),
        }
        
        if total_discount_percent > 0:
            payload["DiscountPercent"] = round(total_discount_percent, 6)
            
        if hasattr(invoice, 'rounding') and invoice.rounding and float(invoice.rounding) != 0.0:
            payload["Rounding"] = "tYES"
            payload["RoundingDiffAmount"] = float(invoice.rounding)

        # Tax codes exactly as the invoice gets them (same codes, same 12% rule,
        # same state comparison), so the credit note reverses what was posted.
        # This used codes the invoices never use (CGST@2.5, IGST@5.0) and the
        # COMPANY_STATE_CODE setting rather than the centre's GSTIN.
        for line in payload["DocumentLines"]:
            _apply_invoice_tax_code(invoice, line, line.pop("_TaxPercent", 0))

        # Link each line to the invoice line it reverses (blueprint X7) when
        # SAP allows it (the invoice is still open), and credit the account
        # the sale went to either way. See _link_to_base_invoice.
        _link_to_base_invoice(invoice.sap_doc_entry, payload["DocumentLines"])

        from pos_backend.sap_ledger import post_once, odata_filter, odata_str

        def _post():
            response = _send_sap_request('POST', f"{SAP_URL_BASE}/CreditNotes", json=payload, timeout=_post_timeout())
            if response.status_code in (200, 201):
                return response.json().get('DocEntry')
            logger.error(f"SAP: Error creating CreditNote: {getattr(response, 'text', '')}")
            return None

        def _find():
            url = (f"{SAP_URL_BASE}/CreditNotes?$select=DocEntry&$filter="
                   + odata_filter(f"Comments eq {odata_str(payload['Comments'])}"))
            rows = (_send_sap_request('GET', url, timeout=10).json() or {}).get('value') or []
            return rows[0].get('DocEntry') if rows else None

        doc_entry = post_once(f"creditnote:{invoice.id}", _post, _find)
        if doc_entry:
            logger.info(f"SAP: Created CreditNote {doc_entry} for refunded invoice {invoice.id}")
            invoice.sap_sync_status = True
            invoice.sap_sync_message = "Refund Synced"
            invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        invoice.sap_sync_status = False
        invoice.sap_sync_message = "SAP did not accept the credit note"
        invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except Exception as e:
        logger.error(f"SAP CreditNote creation error: {e}")
        invoice.sap_sync_status = False
        invoice.sap_sync_message = str(e)[:300]
        invoice.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def _split_gst(gst_amount):
    """Split GST into CGST and SGST halves that add back to the exact total.

    Each half used to be rounded on its own: Rs 5.05 became 2.53 + 2.53 =
    5.06, the journal no longer balanced, and SAP rejected it. The second
    half now takes whatever the first leaves.
    """
    gst = round(float(gst_amount or 0), 2)
    cgst = round(gst / 2.0, 2)
    sgst = round(gst - cgst, 2)
    return cgst, sgst


def _find_je(payload, total, key_prefix):
    """DocEntry (JdtNum) of a journal SAP already holds for this payload, if any.

    Matches on the reference date, the memo and the total debit, skipping
    journals already recorded against another posting. Raises if SAP cannot
    be asked.
    """
    from pos_backend.sap_ledger import odata_filter, odata_str, known_doc_entries

    memo = str(payload.get('Memo') or '')
    flt = f"ReferenceDate eq {odata_str(payload.get('ReferenceDate'))}"
    if memo:
        flt += f" and startswith(Memo,{odata_str(memo[:40])})"
    url = f"{SAP_URL_BASE}/JournalEntries?$select=JdtNum,Memo,JournalEntryLines&$filter=" + odata_filter(flt)
    rows = (_send_sap_request('GET', url, timeout=15).json() or {}).get('value') or []
    claimed = known_doc_entries(key_prefix)
    for row in rows:
        entry = str(row.get('JdtNum') or '')
        if not entry or entry in claimed:
            continue
        debit = round(sum(float(l.get('Debit') or 0) for l in row.get('JournalEntryLines') or []), 2)
        if abs(debit - round(float(total), 2)) < 0.005:
            return entry
    return None


def _post_je_once(key, payload, total):
    """POST a journal entry unless this posting is already recorded or in SAP.

    Returns the JdtNum, or None if SAP did not accept it. Raises on transport
    and HTTP errors, as _send_sap_request does.
    """
    from pos_backend.sap_ledger import post_once

    def _post():
        response = _send_sap_request('POST', f"{SAP_URL_BASE}/JournalEntries", json=payload, timeout=_post_timeout())
        if response.status_code in (200, 201):
            data = response.json() if response.content else {}
            return data.get('JdtNum') or data.get('Number') or 'posted'
        logger.error(f"SAP JE {key}: unexpected HTTP {response.status_code} – {getattr(response, 'text', '')[:300]}")
        return None

    family = key.rsplit(':', 1)[0] + ':'
    return post_once(key, _post, lambda: _find_je(payload, total, family), amount=round(float(total), 2))


def create_sap_advance_je(advance):
    """
    Creates a Journal Entry for an AdvancePayment in SAP.
    Debits the bank account, credits Advance Liability and Output GST.
    """
    try:
        import datetime
        from django.conf import settings
        # GL Accounts (should ideally be configurable)
        advance_liability_account = _sap('sap.account.advance_liability')
        # Default to 5% sales tax accounts for advances (using the new advance-specific accounts)
        cgst_account = _sap('sap.account.cgst_advance_5')
        sgst_account = _sap('sap.account.sgst_advance_5')
        if hasattr(advance, 'tax_percent') and advance.tax_percent and float(advance.tax_percent) >= 12.0:
            cgst_account = _sap('sap.account.cgst_advance_18') # Update with 18% equivalent if provided later
            sgst_account = _sap('sap.account.sgst_advance_18')
        
        # Payment method to Bank Account mapping
        # Default to Paytm Virtual A/C Clearing
        bank_account = _sap('sap.account.upi')
        if advance.payment_method == 'Cash':
            bank_account = _sap('sap.account.cash')
        elif advance.payment_method in ['UPI', 'Google Pay', 'PhonePe', 'Paytm', 'BharatPe']:
            bank_account = _sap('sap.account.upi')

        client = None
        if advance.client_id:
            from clients.models import Client
            client = Client.objects.filter(pk=advance.client_id).first()
        card_code = _resolve_sap_card_code(client)

        bpl_id = _resolve_bpl_id(advance.center.sap_branch_name if advance.center else None)
        
        cgst_part, sgst_part = _split_gst(advance.gst_amount)
        if getattr(advance, 'gst_inclusive', False):
            # The amount is what the client paid, GST included: the bank gets
            # all of it, the liability only the part that is not GST.
            total_amount = round(float(advance.amount), 2)
            base_amount = round(total_amount - cgst_part - sgst_part, 2)
        else:
            base_amount = round(float(advance.amount), 2)
            total_amount = round(base_amount + cgst_part + sgst_part, 2)

        # Create a mock object with standard invoice attributes for the UDF helper
        class MockInvoice:
            pass
        mock_invoice = MockInvoice()
        mock_invoice.client = client
        mock_invoice.staff = None
        mock_invoice.id = advance.id
        mock_invoice.display_number = str(advance.id)

        payload = {
            "ReferenceDate": str(advance.created_at.date() if advance.created_at else datetime.date.today()),
            "Memo": f"Advance Payment POS {advance.id} - {card_code}",
            "JournalEntryLines": [
                {
                    "AccountCode": bank_account,
                    "Debit": total_amount,
                    "Credit": 0.0,
                    "BPLID": bpl_id
                },
                {
                    "AccountCode": advance_liability_account,
                    "Debit": 0.0,
                    "Credit": base_amount,
                    "BPLID": bpl_id
                }
            ]
        }
        if cgst_part + sgst_part > 0:
            payload["JournalEntryLines"].extend([
                {
                    "AccountCode": cgst_account,
                    "Debit": 0.0,
                    "Credit": cgst_part,
                    "BPLID": bpl_id
                },
                {
                    "AccountCode": sgst_account,
                    "Debit": 0.0,
                    "Credit": sgst_part,
                    "BPLID": bpl_id
                }
            ])

        je_udfs = _get_je_udf_fields(mock_invoice)
        for line in payload["JournalEntryLines"]:
            line.update(je_udfs)

        doc_entry = _post_je_once(f"je:advance:{advance.id}", payload, total_amount)
        if doc_entry:
            logger.info(f"SAP: Created Journal Entry for advance {advance.id}")
            advance.sap_sync_status = True
            advance.sap_sync_message = "Success"
            advance.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        logger.error(f"SAP Advance JE error: SAP did not accept the journal for advance {advance.id}")
        advance.sap_sync_status = False
        advance.sap_sync_message = "SAP did not accept the journal entry"
        advance.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
        
    except Exception as e:
        logger.error(f"SAP Advance JE exception: {e}")
        advance.sap_sync_status = False
        advance.sap_sync_message = str(e)[:300]
        advance.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False


def petty_cash_accounts():
    """[(code, name)] of the petty-cash expense accounts in the SAP chart of accounts.

    These are the active posting accounts whose name starts with "Petty-".
    Accounts that merely sit under a "Petty-" group, or mention petty cash
    elsewhere in the name, are not included. Raises when SAP cannot be read.
    """
    rows, url = [], (f"{SAP_URL_BASE}/ChartOfAccounts?$select=Code,Name,AccountLevel,ActiveAccount"
                     f"&$filter=startswith(Name,'Petty-')")
    while url:
        data = _send_sap_request('GET', url, timeout=30,
                                 headers={'Prefer': 'odata.maxpagesize=1000'}).json() or {}
        rows.extend(data.get('value') or [])
        nxt = data.get('odata.nextLink') or data.get('@odata.nextLink')
        url = (nxt if nxt.startswith('http') else f"{SAP_URL_BASE}/{nxt}") if nxt else None

    out = []
    for r in rows:
        name = (r.get('Name') or '').strip()
        if (r.get('ActiveAccount') == 'tYES' and r.get('AccountLevel') == 5
                and name.lower().startswith('petty-')):
            out.append((r['Code'], name))
    return sorted(out, key=lambda pair: pair[1].lower())


def sync_petty_cash_categories():
    """Make the POS petty-cash categories match SAP's petty-cash accounts.

    One category per account, named as SAP names it without the "Petty-"
    prefix. A category found by its account code is renamed and re-activated;
    categories typed in by hand (no account) are switched off, since an entry
    in one could not be posted. Returns (created, updated, switched_off).
    """
    import re
    from finance.models import PettyCashCategory

    created = updated = 0
    seen = set()
    accounts = petty_cash_accounts()
    if not accounts:
        # An empty answer is more likely a SAP hiccup than "every petty account
        # was removed": keep what the POS has rather than switching it all off.
        return 0, 0, 0
    for code, name in accounts:
        label = re.sub(r'^petty-\s*', '', name, flags=re.I)
        label = label.strip()[:80] or name[:80]
        seen.add(code)
        cat = PettyCashCategory.objects.filter(sap_account=code).first()
        if cat is None:
            cat = PettyCashCategory.objects.filter(name__iexact=label, sap_account='').first()
        if cat is None:
            clash = PettyCashCategory.objects.filter(name__iexact=label).exists()
            PettyCashCategory.objects.create(name=f"{label} ({code})" if clash else label,
                                             sap_account=code)
            created += 1
            continue
        changed = []
        if cat.sap_account != code:
            cat.sap_account = code; changed.append('sap_account')
        if cat.name != label and not PettyCashCategory.objects.filter(name__iexact=label).exclude(pk=cat.pk).exists():
            cat.name = label; changed.append('name')
        if not cat.is_active:
            cat.is_active = True; changed.append('is_active')
        if changed:
            cat.save(update_fields=changed)
            updated += 1
    # Accounts SAP no longer lists as petty-cash accounts, and hand-made categories.
    off = PettyCashCategory.objects.filter(is_active=True).exclude(sap_account__in=seen).update(is_active=False)
    return created, updated, off


def cost_centre_code(center):
    """The SAP cost centre (distribution rule) made for a centre, or None.

    SAP keeps one distribution rule per salon, described with the salon's
    name (rule CGPAO001 = "ADDRESS ONE"). The centre is matched on that name,
    ignoring case. A centre SAP has no rule for posts without one rather than
    failing. Kept ten minutes; None when SAP cannot be asked.
    """
    names = {str(getattr(center, f, '') or '').strip().lower()
             for f in ('display_name', 'center_name')} - {''}
    if not names:
        return None
    rules = cache.get('sap_cost_centres')
    if rules is None:
        try:
            rows = _get_all("DistributionRules?$select=FactorCode,FactorDescription,InWhichDimension,Active",
                            timeout=10)
        except Exception as e:
            logger.warning(f"SAP: could not read distribution rules: {e}")
            return None
        rules = {str(r.get('FactorDescription') or '').strip().lower(): r['FactorCode']
                 for r in rows if r.get('Active') == 'tYES' and r.get('InWhichDimension') == 1}
        cache.set('sap_cost_centres', rules, 10 * 60)
    return next((rules[n] for n in names if n in rules), None)


def create_sap_petty_cash_je(entry):
    """Journal entry for one petty-cash expense.

    Debit the SAP expense account of the entry's category, credit the petty
    cash account (Cash in Hand), both on the business place of the centre; the
    expense line also carries the centre's cost centre when SAP has one.
    One journal per entry; posted once.
    """
    try:
        category = entry.category
        if category is None or not category.sap_account:
            raise ValueError("This petty cash entry has no SAP category. Pick one of the categories "
                             "loaded from SAP; it cannot be posted without it.")
        amount = round(float(entry.amount), 2)
        center = entry.center
        branch = getattr(center, 'sap_branch_name', None)
        if not branch or branch == 'No Branch':
            name = getattr(center, 'display_name', None) or getattr(center, 'center_name', None) or 'This centre'
            raise ValueError(f"{name} has no SAP branch name. Set it on the centre, then retry.")
        bpl_id = _resolve_bpl_id(branch)
        cash_account = _sap('sap.account.petty_cash')
        cost_centre = cost_centre_code(center)
        day = _local_date(entry.date)
        # What SAP shows where: Ref1 = POS entry, Ref2 = voucher number,
        # Ref3 = description, Remarks = comments. The same on the journal and on
        # every line (JDT1), and the cost centre on every line. SAP holds the
        # references to 200 characters and the remarks to 254.
        ref = f"PC{entry.id}"
        voucher = (entry.voucher_number or '').strip()[:200]
        description = (entry.description or '').strip()[:200]
        remarks = (entry.comments or '').strip()[:254] or f"Petty Cash POS {entry.id} - {category.name}"[:254]

        def line(account, debit, credit):
            row = {"AccountCode": account, "Debit": debit, "Credit": credit, "BPLID": bpl_id,
                   "Reference1": ref, "LineMemo": remarks}
            if voucher:
                row["Reference2"] = voucher
            if description:
                row["AdditionalReference"] = description
            if cost_centre:
                row["CostingCode"] = cost_centre
            return row

        payload = {
            "ReferenceDate": str(day),
            "Memo": remarks,
            "Reference": ref,
            "JournalEntryLines": [line(category.sap_account, amount, 0.0),
                                  line(cash_account, 0.0, amount)],
        }
        if voucher:
            payload["Reference2"] = voucher
        if description:
            payload["Reference3"] = description
        doc_entry = _post_je_once(f"je:petty:{entry.id}", payload, amount)
        if doc_entry:
            entry.sap_sync_status = True
            entry.sap_sync_message = "Success"
            entry.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True
        entry.sap_sync_status = False
        entry.sap_sync_message = "SAP did not accept the journal entry"
        entry.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
    except Exception as e:
        logger.error(f"SAP petty cash JE exception: {e}")
        entry.sap_sync_status = False
        entry.sap_sync_message = str(e)[:300]
        entry.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False


def _resolve_card_code_for_invoice(invoice):
    """Return the SAP card_code string for the client on an invoice."""
    return _invoice_card_code(invoice)


def _resolve_bank_account_for_invoice(invoice):
    """Return the SAP bank/clearing GL account based on the primary cash payment."""
    from django.conf import settings
    # Default to Paytm Virtual A/C Clearing (1110100007) as requested
    bank_account = _sap('sap.account.upi')
    
    primary_payment = invoice.payments.exclude(
        payment_method__in=['Advance', 'Value Card', 'Cashback Wallet', 'Membership']
    ).first()
    
    if primary_payment:
        method = primary_payment.payment_method or ''
        if method == 'Cash':
            # Use cash account if explicitly cash
            bank_account = _sap('sap.account.cash')
        elif method in ['UPI', 'Google Pay', 'PhonePe', 'Paytm', 'BharatPe']:
            bank_account = _sap('sap.account.upi')
            
    return bank_account


def _create_marketing_ar_invoice(invoice, item, item_code, item_name, price, gst_percent, card_code, bpl_id):
    """
    Create an A/R Invoice in SAP for a marketing item (Card/Membership/Package).

    Structure (matching the screenshot):
      DR: B2C (client BP)   = base_amount + gst_amount
      CR: Sales Revenue     = base_amount
      CR: Output GST        = gst_amount
    """
    try:
        import datetime
        from django.conf import settings

        # Ensure the Item Master exists in SAP
        create_sap_marketing_item(item_code, item_name, price, gst_percent)

        base_amount = float(item.total_price or 0)
        gst_amount  = float(item.tax_amount or 0)

        ref_date = str(invoice.created_at.date() if invoice.created_at else datetime.date.today())

        # Determine tax code
        tax_code = _gst_tax_code(gst_percent)

        wh_code = _warehouse_code(invoice.center)

        payload = {
            "CardCode": card_code,
            "DocDate": ref_date,
            "DocDueDate": ref_date,
            "BPL_IDAssignedToInvoice": bpl_id,
            "DocumentLines": [
                {
                    "ItemCode": item_code,
                    "Quantity": str(float(item.quantity or 1)),
                    "UnitPrice": str(base_amount / float(item.quantity or 1)),
                    "WarehouseCode": wh_code,
                    "TaxCode": tax_code,
                }
            ],
        }

        response = _send_sap_request('POST', f"{SAP_URL_BASE}/Invoices", json=payload, timeout=_post_timeout())
        if response.status_code in (200, 201):
            doc_entry = response.json().get('DocEntry')
            logger.info(f"SAP: Created AR Invoice (DocEntry {doc_entry}) for marketing item {item_code}")
            return doc_entry
        body = getattr(response, 'text', '')
        logger.error(f"SAP marketing AR Invoice error: HTTP {response.status_code} – {body[:300]}")
        return None
    except Exception as e:
        logger.error(f"SAP marketing AR Invoice exception: {e}")
        return None


def _sale_debit_accounts(invoice, total_amount):
    """[(account, amount)] the sale journal debits: where the customer's money went.

    Cash goes to the cash account, UPI to the UPI parking account, other methods
    to their own account (payment_account), each in proportion to what the bill
    was actually paid with. A bill paid from a card / advance balance (no new
    money) or with nothing recorded falls back to the clearing account."""
    clearing = _sap('sap.account.virtual_clearing')
    paid = {}
    for p in invoice.payments.all().order_by('id'):
        if getattr(p, 'is_tip', False) or _is_virtual_payment(p.payment_method):
            continue
        account = payment_account(p.payment_method)
        paid[account] = paid.get(account, Decimal('0')) + Decimal(str(p.amount or 0))
    real = sum(paid.values(), Decimal('0'))
    if real <= 0:
        return [(clearing, total_amount)]
    shares, running = [], Decimal('0')
    accounts = list(paid.items())
    for n, (account, amount) in enumerate(accounts):
        if n == len(accounts) - 1:
            part = Decimal(str(total_amount)) - running      # the last takes the rounding
        else:
            part = (Decimal(str(total_amount)) * amount / real).quantize(Decimal('0.01'))
        running += part
        if part > 0:
            shares.append((account, round(float(part), 2)))
    return shares or [(clearing, total_amount)]


def _create_marketing_je(invoice, item, label, liability_account):
    """
    Step 1 – Journal Entry when a Value Card / Membership / Package is SOLD.

    Matches Excel 'Advance' JE exactly:
      DR: Paytm Virtual A/C Clearing (1110100007) = total_amount
      CR: Advance from Customer (liability_account) = base_amount
      CR: OUTPUT GST (CGST + SGST accounts)         = gst_amount
    """
    try:
        import datetime
        from django.conf import settings

        gst_percent = float(item.tax_percentage or 5.0)
        if gst_percent >= 12.0:
            cgst_account = _sap('sap.account.cgst_advance_18')
            sgst_account = _sap('sap.account.sgst_advance_18')
        else:
            cgst_account = _sap('sap.account.cgst_advance_5')
            sgst_account = _sap('sap.account.sgst_advance_5')

        # Paytm Virtual A/C Clearing account (collecting the cash from customer)
        virtual_clearing = _sap('sap.account.virtual_clearing')
        card_code = _resolve_card_code_for_invoice(invoice)
        bpl_id    = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

        base_amount  = round(float(item.total_price or 0), 2)
        cgst_part, sgst_part = _split_gst(item.tax_amount)
        gst_amount   = round(cgst_part + sgst_part, 2)
        total_amount = round(base_amount + gst_amount, 2)

        if total_amount <= 0:
            return True

        ref_date = str(invoice.created_at.date() if invoice.created_at else datetime.date.today())
        memo = f"{label} - Invoice #{invoice.id} - {card_code}"

        client_name = "Walk-in"
        client_phone = ""
        if hasattr(invoice, 'client') and invoice.client:
            first = getattr(invoice.client, 'first_name', '') or ''
            last = getattr(invoice.client, 'last_name', '') or ''
            client_name = f"{first} {last}".strip() or "Walk-in"
            client_phone = getattr(invoice.client, 'phone', '') or ''

        # JE lines matching Excel 'Advance' block:
        #   DR  Paytm Virtual A/C Clearing = total_amount
        #   CR  Advance from Customer      = base_amount
        #   CR  OUTPUT GST (CGST + SGST)   = gst_amount
        lines = [
            {
                "AccountCode": account,
                "Debit": amount,
                "Credit": 0.0,
                "BPLID": bpl_id
            }
            for account, amount in _sale_debit_accounts(invoice, total_amount)
        ] + [
            {
                "AccountCode": liability_account,
                "Debit": 0.0,
                "Credit": base_amount,
                "BPLID": bpl_id
            },
        ]
        if gst_amount > 0:
            lines.extend([
                {
                    "AccountCode": cgst_account,
                    "Debit": 0.0,
                    "Credit": cgst_part,
                    "BPLID": bpl_id
                },
                {
                    "AccountCode": sgst_account,
                    "Debit": 0.0,
                    "Credit": sgst_part,
                    "BPLID": bpl_id
                }
            ])

        je_udfs = _get_je_udf_fields(invoice)
        for line in lines:
            line.update(je_udfs)

        payload = {
            "ReferenceDate": ref_date,
            "Memo": memo,
            "Reference": str(card_code)[:11],
            "Reference2": str(client_phone)[:11],
            "Reference3": str(client_name)[:27],
            "JournalEntryLines": lines,
        }

        # One sale journal per sold line; a retry never posts it twice.
        if _post_je_once(f"je:sale:{item.id}", payload, total_amount):
            logger.info(f"SAP: Created purchase JE for {memo}")
            return True

        logger.error(f"SAP {label} JE error: SAP did not accept the journal for line {item.id}")
        return False

    except Exception as e:
        body = getattr(getattr(e, 'response', None), 'text', '')
        logger.error(f"SAP {label} JE exception: {e} - Body: {body[:300]}")
        return False


def _adjustment_parts(payment_amount, gst_percent, advance_account=None, gst_share=None):
    """What a spent card or advance releases, or None for a zero amount.

    `total` is what the client settled; `base` goes against the liability
    account and the GST part against the CGST / SGST advance accounts (the 5%
    pair below 12%, the 12%+ pair above). `gst_share` (0..1) overrides the
    rate when the caller knows the GST part of the total from the advance.
    """
    total = round(float(payment_amount), 2)
    if total <= 0:
        return None
    gst_percent = float(gst_percent if gst_percent is not None else 5.0)
    if gst_percent >= 12.0:
        cgst_account = _sap('sap.account.cgst_advance_18')
        sgst_account = _sap('sap.account.sgst_advance_18')
    else:
        cgst_account = _sap('sap.account.cgst_advance_5')
        sgst_account = _sap('sap.account.sgst_advance_5')
    if gst_share is not None:
        gst_amount = round(total * float(gst_share), 2)
        base = round(total - gst_amount, 2)
    else:
        gst_rate = (gst_percent or 5.0) / 100.0
        base = round(total / (1 + gst_rate), 2)
        gst_amount = round(total - base, 2)
    cgst, sgst = _split_gst(gst_amount)
    return {'total': total, 'base': base, 'gst': gst_amount, 'cgst': cgst, 'sgst': sgst,
            'advance_account': advance_account or _sap('sap.account.valuecard_liability'),
            'cgst_account': cgst_account, 'sgst_account': sgst_account}


def create_sap_valuecard_redemption_je(invoice, payment_amount, gst_percent=5.0, payment_id=None,
                                       advance_account=None, label='Value Card', gst_share=None,
                                       key_prefix='je:vcredeem'):
    """
    Step 3 – JE (Advance Adjustment) posted when a customer REDEEMS a Value Card
    or spends an Advance (create_sap_advance_adjustment_je).

    Matches Excel 'JE (Advance Adjustment)' block EXACTLY (Scenarios 01 to 03):
      CR: B2C Customer                         = total_amount  (closes AR Invoice)
      DR: Advance from customer (2320100001)   = base_amount
      DR: Output CGST 2.5% advance (2341100024)= half_gst
      DR: Output SGST 2.5% advance (2341100025)= half_gst

    GST is taken out of the total at `gst_percent`, or at `gst_share` (the GST
    part of the total, 0..1) when the caller knows it from the advance itself.
    """
    try:
        import datetime
        from django.conf import settings

        card_code = _resolve_card_code_for_invoice(invoice)
        bpl_id    = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

        parts = _adjustment_parts(payment_amount, gst_percent, advance_account, gst_share)
        if parts is None:
            return True
        total_amount, base_amount, gst_amount = parts['total'], parts['base'], parts['gst']
        cgst_part, sgst_part = parts['cgst'], parts['sgst']
        advance_account = parts['advance_account']
        cgst_account, sgst_account = parts['cgst_account'], parts['sgst_account']

        ref_date = str(invoice.created_at.date() if invoice.created_at else datetime.date.today())
        memo = f"Advance Adjustment ({label}) - Invoice #{invoice.id} - {card_code}"

        client_name = "Walk-in"
        client_phone = ""
        if hasattr(invoice, 'client') and invoice.client:
            first = getattr(invoice.client, 'first_name', '') or ''
            last = getattr(invoice.client, 'last_name', '') or ''
            client_name = f"{first} {last}".strip() or "Walk-in"
            client_phone = getattr(invoice.client, 'phone', '') or ''

        lines = [
            # CR: B2C Customer – closes the AR Invoice receivable (Excel: B2C Customer Credit)
            {
                "ShortName": card_code,
                "Debit": 0.0,
                "Credit": total_amount,
                "BPLID": bpl_id
            },
            # DR: Advance from customer / Customer Advances (Excel: Advance from customer Debit)
            {
                "AccountCode": advance_account,
                "Debit": base_amount,
                "Credit": 0.0,
                "BPLID": bpl_id
            },
        ]
        if gst_amount > 0:
            lines.extend([
                # DR: Output CGST 2.5% advance (Excel: 2341100024)
                {
                    "AccountCode": cgst_account,
                    "Debit": cgst_part,
                    "Credit": 0.0,
                    "BPLID": bpl_id
                },
                # DR: Output SGST 2.5% advance (Excel: 2341100025)
                {
                    "AccountCode": sgst_account,
                    "Debit": sgst_part,
                    "Credit": 0.0,
                    "BPLID": bpl_id
                }
            ])

        je_udfs = _get_je_udf_fields(invoice)
        for line in lines:
            line.update(je_udfs)

        payload = {
            "ReferenceDate": ref_date,
            "Memo": memo,
            "Reference": str(card_code)[:11],
            "Reference2": str(client_phone)[:11],
            "Reference3": str(client_name)[:27],
            "JournalEntryLines": lines,
        }

        key = (f"{key_prefix}:{payment_id}" if payment_id
               else f"{key_prefix}:inv{invoice.id}-{total_amount:.2f}")
        if _post_je_once(key, payload, total_amount):
            logger.info(f"SAP: Created Advance Adjustment JE for Invoice #{invoice.id}")
            return True

        logger.error(f"SAP Advance Adjustment JE error: SAP did not accept the journal for invoice {invoice.id}")
        return False

    except Exception as e:
        body = getattr(getattr(e, 'response', None), 'text', '')
        logger.error(f"SAP Advance Adjustment JE exception: {e} - Body: {body[:300]}")
        return False


def create_sap_advance_adjustment_je(invoice, payment_amount, payment_id=None):
    """JE (Advance Adjustment) posted when a client spends their ADVANCE on a bill.

    SAP scenario 01.1: an advance of 20,000 + 1,000 GST is a 21,000 balance.
    Spending it on a bill:
      CR  B2C customer                          = the amount spent (closes the invoice)
      DR  Advance from customer                 = its part without GST
      DR  Output CGST / SGST advance            = the GST part, released
    The GST part is the share of the client's GST-inclusive advances that was
    GST; advances taken before the balance included GST carry none, and a
    client with only those spends exactly as before (liability only).
    """
    rate, share = _advance_gst_args(invoice)
    return create_sap_valuecard_redemption_je(
        invoice, payment_amount, gst_percent=rate, payment_id=payment_id,
        advance_account=_sap('sap.account.advance_liability'), label='Advance',
        gst_share=share, key_prefix='je:advadj')


def _advance_gst_args(invoice):
    """(rate, share) of the GST inside the client's advances, for spending one."""
    from django.db.models import Sum
    from billing.models import AdvancePayment

    share = 0.0
    if invoice.client_id:
        rows = AdvancePayment.objects.filter(
            client_id=invoice.client_id, amount__gt=0, gst_inclusive=True,
            reverses__isnull=True).aggregate(gross=Sum('amount'), gst=Sum('gst_amount'))
        gross = float(rows['gross'] or 0)
        if gross > 0:
            share = min(max(float(rows['gst'] or 0) / gross, 0.0), 0.5)
    # The rate behind that share picks the 5% or the 12%+ advance-GST accounts.
    rate = (share / (1 - share) * 100.0) if share > 0 else 0.0
    return rate, share


def _value_card_gst_percent(payment):
    """The GST rate of the card a Value Card payment drew on (5% if unknown)."""
    gst_percent = 5.0
    try:
        from billing.payment_methods import extract_value_card_id
        card_id = extract_value_card_id(payment.payment_method, payment.value_card_id)
        if card_id:
            from clients.models import ClientValueCard
            cvc = ClientValueCard.objects.select_related('value_card').get(id=card_id)
            if hasattr(cvc.value_card, 'gst_percent'):
                gst_percent = float(cvc.value_card.gst_percent or 5.0)
    except Exception as e:
        logger.error(f"Error scaling Value Card redemption: {e}")
    return gst_percent


def create_sap_valuecard_forfeiture_je(card):
    """Journal for the unused balance of an expired value card (F7, D9).

    The sale credited the card's liability with its pre-tax value (and the
    GST to the advance GST accounts). What is forfeited is released from the
    liability to income at the same pre-tax value:

      DR  Value card liability          = unused face x pre-tax / face
      CR  Forfeited value-card income   = the same

    The GST on that part was paid when the card was sold and is left where the
    sale put it. Posted only once Settings › SAP mapping names the income
    account; until then the forfeiture waits (nothing is lost - the hourly
    forfeiture job queues every forfeited card not yet in SAP).
    """
    import datetime
    from decimal import ROUND_HALF_UP

    income = _sap('sap.account.valuecard_forfeiture_income')
    if not income:
        return True
    unused = Decimal(str(card.forfeited_amount or 0))
    if unused <= 0:
        return True
    face, pre, _rate = card.terms()
    if face <= 0:
        return True
    value = (unused * pre / face).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if value <= 0:
        return True

    center = card.center or getattr(card.source_invoice, 'center', None)
    bpl_id = _resolve_bpl_id(center.sap_branch_name if center else None)
    amount = float(value)
    lines = [
        {"AccountCode": _sap('sap.account.valuecard_liability'), "Debit": amount, "Credit": 0.0,
         "BPLID": bpl_id},
        {"AccountCode": income, "Debit": 0.0, "Credit": amount, "BPLID": bpl_id},
    ]
    client = card.client
    payload = {
        "ReferenceDate": str(card.forfeited_on or datetime.date.today()),
        "Memo": f"Value card {card.number} forfeited - unused Rs {unused} face value",
        "Reference": str(_resolve_sap_card_code(client))[:11],
        "Reference2": str(getattr(client, 'phone', '') or '')[:11],
        "Reference3": str(getattr(client, 'full_name', '') or '')[:27],
        "JournalEntryLines": lines,
    }
    if _post_je_once(f"je:forfeit:{card.id}", payload, amount):
        logger.info(f"SAP: posted the forfeiture of value card {card.number}")
        return True
    return False


def create_sap_valuecard_je(invoice, item):
    """
    Full SAP integration when a Value Card is SOLD on an invoice:
      Step 1 – A/R Invoice (revenue recognition: Dr B2C / Cr Sales + Cr GST)
      Step 2 – Journal Entry (cash settlement: Dr Bank / Cr Liability + Cr GST)

    Liability account: 2320100002 (Value Card Advance from Customer)
    """
    from django.conf import settings
    liability = _sap('sap.account.valuecard_liability')

    # Resolve item details from the invoice item
    item_code = f"VC{item.object_id:06d}" if item.object_id else "VC000000"
    item_name = item.description or 'Value Card'
    price = float(item.total_price or 0)
    gst_percent = float(item.tax_percentage or 0)
    card_code = _resolve_card_code_for_invoice(invoice)
    bpl_id    = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

    # Step 1: AR Invoice - explicitly disabled as per request (we only need the JE)
    # _create_marketing_ar_invoice(invoice, item, item_code, item_name, price, gst_percent, card_code, bpl_id)

    # Step 2: JE (liability + GST)
    return _create_marketing_je(invoice, item, "Value Card Sale", liability)


def create_sap_membership_je(invoice, item):
    """
    Full SAP integration when a Membership is SOLD on an invoice.
    Liability account: 2320100001 (Advance from customer B2C)
    """
    from django.conf import settings
    liability = _sap('sap.account.membership_liability')

    item_code   = f"MB{item.object_id:06d}" if item.object_id else "MB000000"
    item_name   = item.description or 'Membership'
    price       = float(item.total_price or 0)
    gst_percent = float(item.tax_percentage or 0)
    card_code   = _resolve_card_code_for_invoice(invoice)
    bpl_id      = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

    # _create_marketing_ar_invoice(invoice, item, item_code, item_name, price, gst_percent, card_code, bpl_id)
    return _create_marketing_je(invoice, item, "Membership Sale", liability)


def create_sap_package_je(invoice, item):
    """
    Full SAP integration when a Package is SOLD on an invoice.
    Liability account: 2320100001 (Advance from customer B2C)
    """
    from django.conf import settings
    liability = _sap('sap.account.package_liability')

    item_code   = f"PK{item.object_id:06d}" if item.object_id else "PK000000"
    item_name   = item.description or 'Package'
    price       = float(item.total_price or 0)
    gst_percent = float(item.tax_percentage or 18.0)
    card_code   = _resolve_card_code_for_invoice(invoice)
    bpl_id      = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)

    # _create_marketing_ar_invoice(invoice, item, item_code, item_name, price, gst_percent, card_code, bpl_id)
    return _create_marketing_je(invoice, item, "Package Sale", liability)



def create_sap_upi_settlements(now=None):
    """Move UPI money from the parking account to the bank, a day after it was taken.

    SAP scenario 04: a UPI payment is posted to the parking account
    (Paytm Virtual Parking A/c); when it shows in the bank statement - one day
    later, the CA says - one journal per branch per payment day clears it:

        Dr  UPI bank account (Paytm Bank A/c)
        Cr  UPI parking account

    Only UPI payments taken after the settlement started count (`upisettle:start`,
    written the first time this runs): older ones were posted to a different
    account and have nothing in the parking account to clear. Each payment is
    recorded as settled, and each journal is posted once (a retry never
    posts it twice). Returns how many journals were posted; raises at the end
    if SAP refused any, so the job shows the error.
    """
    import datetime
    import hashlib
    from django.utils import timezone
    from billing.models import AdvancePayment, Payment
    from pos_backend import app_settings, sap_ledger
    from pos_backend.models import SapPosting
    from salon_admin.models import Center

    days = int(app_settings.get('sap.upi_settlement_days'))
    minutes = int(app_settings.get('sap.upi_settlement_minutes'))
    if days <= 0 and minutes <= 0:
        return 0
    now = now or timezone.now()
    start_row = sap_ledger.row('upisettle:start')
    if start_row is None or start_row.status != SapPosting.DONE:
        sap_ledger.mark_done('upisettle:start', now.isoformat(), note='UPI settlement journals start here')
        return 0
    start = datetime.datetime.fromisoformat(start_row.doc_entry)
    def local_date(moment):
        # USE_TZ may be off: then datetimes are naive local time already.
        return (timezone.localtime(moment) if timezone.is_aware(moment) else moment).date()

    cutoff = local_date(now) - datetime.timedelta(days=days)
    # Demo mode (minutes > 0): a payment is due once it is that many minutes old.
    due_before = now - datetime.timedelta(minutes=minutes) if minutes > 0 else None

    def is_due(created_at, d):
        return created_at <= due_before if due_before is not None else d <= cutoff

    settled = {k for k in SapPosting.objects.filter(
        key__startswith='upisettle:', status=SapPosting.DONE).values_list('key', flat=True)}
    upi = sorted(_UPI_FAMILY)
    def who(client):
        """(name, phone) of the client as the other journals write them."""
        if client is None:
            return "Walk-in", ""
        name = f"{getattr(client, 'first_name', '') or ''} {getattr(client, 'last_name', '') or ''}".strip()
        return name or "Walk-in", _sap_phone(getattr(client, 'phone', ''))

    due = []   # (center_id, payment date, marker key, amount, client name, client phone)
    for p in (Payment.objects.filter(payment_method__in=upi, is_tip=False, created_at__gte=start,
                                     invoice__sap_sync_status=True)
              .select_related('invoice', 'invoice__client')):
        d = local_date(p.created_at)
        if is_due(p.created_at, d) and f"upisettle:pay:{p.id}" not in settled:
            due.append((p.invoice.center_id, d, f"upisettle:pay:{p.id}", Decimal(str(p.amount)),
                        *who(p.invoice.client)))
    for a in AdvancePayment.objects.filter(payment_method__in=upi, invoice__isnull=True, amount__gt=0,
                                           reverses__isnull=True, created_at__gte=start,
                                           sap_sync_status=True).select_related('client'):
        d = local_date(a.created_at)
        if is_due(a.created_at, d) and f"upisettle:adv:{a.id}" not in settled:
            gross = Decimal(str(a.amount)) if a.gst_inclusive else \
                Decimal(str(a.amount)) + Decimal(str(a.gst_amount or 0))
            due.append((a.center_id, d, f"upisettle:adv:{a.id}", gross, *who(a.client)))

    groups = {}
    for center_id, d, marker, amount, name, phone in due:
        groups.setdefault((center_id, d), []).append((marker, amount, name, phone))

    parking, bank = _sap('sap.account.upi'), _sap('sap.account.upi_bank')
    posted, failures = 0, []
    for (center_id, d), items in sorted(groups.items(), key=lambda kv: (kv[0][1], kv[0][0] or 0)):
        try:
            center = Center.all_objects.filter(pk=center_id).first() if center_id else None
            bpl_id = _resolve_bpl_id(center.sap_branch_name if center else None)
            total = float(round(sum((amount for _, amount, _, _ in items), Decimal('0')), 2))
            if total <= 0:
                continue
            where = (center.display_name or center.center_name) if center else 'all'
            digest = hashlib.sha1(','.join(sorted(m for m, _, _, _ in items)).encode()).hexdigest()[:12]
            # The header says the payment day and the branch; every UPI payment
            # is its own pair of lines (Dr bank, Cr parking) carrying that
            # client's name and phone, so the SAP statement shows who paid.
            bank_lines, parking_lines = [], []
            for _, amount, name, phone in items:
                amount = float(round(amount, 2))
                udfs = {"U_B2C_NAME": name, "U_B2C_PH": phone}
                bank_lines.append({"AccountCode": bank, "Debit": amount, "Credit": 0.0,
                                   "BPLID": bpl_id, **udfs})
                parking_lines.append({"AccountCode": parking, "Debit": 0.0, "Credit": amount,
                                      "BPLID": bpl_id, **udfs})
            payload = {
                "ReferenceDate": str(local_date(now) if due_before is not None
                                     else d + datetime.timedelta(days=days)),
                "Memo": f"UPI settlement {d} - {where} - {len(items)} pmt"[:50],
                "Reference": "UPI-SETTLE",
                "Reference2": str(d)[:11],
                "Reference3": str(where)[:27],
                "JournalEntryLines": bank_lines + parking_lines,
            }
            jdt = _post_je_once(f"je:upisettle:{center_id or 0}:{d}:{digest}", payload, total)
            if not jdt:
                failures.append(f"{where} {d}: SAP did not accept the journal")
                continue
            for marker, _, _, _ in items:
                sap_ledger.mark_done(marker, jdt, note='UPI settled to the bank')
            posted += 1
            logger.info(f"SAP: UPI settlement journal {jdt} for {where} {d}: {total}")
        except Exception as e:
            logger.error(f"SAP UPI settlement for centre {center_id} {d} failed: {e}")
            failures.append(f"{center_id} {d}: {e}")
    if failures:
        raise RuntimeError('UPI settlement: ' + '; '.join(failures)[:500])
    return posted


_VIRTUAL_EXACT = ['Advance', 'Value Card', 'Cashback Wallet', 'Membership']


def _is_virtual_payment(method):
    """Payments that settle from a balance, not new money (the original rule)."""
    method = method or ''
    return method in _VIRTUAL_EXACT or 'Value Card' in method or 'Advance' in method


def _legacy_payments_already_in_sap(invoice, payments):
    """True when this invoice's payments were handled before postings were recorded.

    Every invoice posted from now on is recorded under `invoice:<id>`, and its
    payments are recorded as they post. An invoice without that record was
    synced by the old code, and one recovered from SAP by a retry may or may
    not have had its payments posted. For those, SAP's PaidToDate decides: if
    SAP shows anything paid, the payments that exist now are treated as posted
    (re-posting them would pay the invoice twice) and nothing is sent. A later
    part-payment on such an old bill still has to be entered in SAP by hand,
    as before.
    """
    from pos_backend import sap_ledger

    if sap_ledger.done_rows(f"inpay:{invoice.id}:"):
        return False
    inv_row = sap_ledger.row(f"invoice:{invoice.id}")
    if inv_row is not None and inv_row.status == 'done' and inv_row.last_error != 'Recovered from SAP':
        return False  # posted under the new rules: nothing to infer

    # Synced by the old code (or recovered): ask SAP what is already paid.
    res = _send_sap_request('GET', f"{SAP_URL_BASE}/Invoices({int(invoice.sap_doc_entry)})?$select=PaidToDate", timeout=10)
    paid_to_date = float((res.json() or {}).get('PaidToDate') or 0)
    if paid_to_date <= 0:
        # Nothing applied to the invoice yet. The old code posted the cash
        # part first and stopped if it failed, so its value-card journals only
        # ran when there was no cash part; those may be in SAP already (a
        # journal does not show in PaidToDate), so they are not repeated.
        real_money = [p for p in payments if not _is_virtual_payment(p.payment_method)]
        if not real_money:
            for p in payments:
                if 'Value Card' in (p.payment_method or ''):
                    sap_ledger.mark_done(f"je:vcredeem:{p.id}", '', note='Before postings were recorded')
        return False
    ids = [p.id for p in payments]
    sap_ledger.mark_done(f"inpay:{invoice.id}:ar:legacy", '', amount=0,
                         detail={'payment_ids': ids, 'marketing_applied': 10 ** 9},
                         note='Paid in SAP before postings were recorded')
    for p in payments:
        if _is_virtual_payment(p.payment_method):
            sap_ledger.mark_done(f"inpay:{invoice.id}:virt:{p.id}", '', note='Before postings were recorded')
            sap_ledger.mark_done(f"je:vcredeem:{p.id}", '', note='Before postings were recorded')
    logger.warning(f"SAP Incoming Payment: invoice {invoice.id} was synced before postings were "
                   f"recorded; its payments are treated as already in SAP and nothing is re-posted.")
    return True


def _post_bill_journal(invoice, payments, card_code, bpl_id, doc_date, marketing_total):
    """One journal for a bill settled partly from a value card / advance.

    The SAP team's scenario 01.1 shows the whole settlement as ONE journal
    (JE Advance Adjustment): the card or advance releases its liability and GST,
    the cash or UPI paid on top is a debit line in the same entry, and B2C is
    credited with the full bill:

        Cr  B2C customer                 = card/advance part + cash/UPI part
        Dr  Advance / card liability     = its part without GST
        Dr  Output CGST / SGST advance   = its GST part
        Dr  Cash / UPI parking / bank    = what was paid on top

    The POS used to send the card part as a journal and the cash/UPI part as a
    separate SAP incoming payment. Each posting is recorded under the keys the
    separate postings use, so a retry, or the incoming-payment sync that runs
    afterwards, sees them as done and sends nothing twice.

    Returns None when this bill is not handled here (no card/advance payment, or
    part of it was posted the old way: the usual path then runs), True when it
    is posted or there is nothing to do, False when SAP did not accept it.
    """
    from pos_backend import sap_ledger

    spent = []   # (payment, ledger key, gst args) for each card / advance payment
    for p in payments:
        method = p.payment_method or ''
        if float(p.amount) <= 0:
            continue
        if 'Value Card' in method:
            spent.append((p, f"je:vcredeem:{p.id}", 'card'))
        elif 'Advance' in method:
            spent.append((p, f"je:advadj:{p.id}", 'advance'))
    if not spent:
        return None
    if any(sap_ledger.posted(key) for _, key, _ in spent):
        return None   # already posted the old way (or by an earlier run)
    if sap_ledger.done_rows(f"inpay:{invoice.id}:"):
        return None   # part of this bill was paid in SAP as an incoming payment

    real = [p for p in payments if not _is_virtual_payment(p.payment_method) and float(p.amount) > 0]
    real_total = sum((Decimal(str(p.amount)) for p in real), Decimal('0'))
    marketing_used = min(real_total, Decimal(str(marketing_total or 0)))
    ar_total = real_total - marketing_used    # the rest went to the card/membership/package sale lines

    # Everything the card and advance payments release, summed per account.
    credit_b2c = Decimal('0')
    debits = {}   # account -> Decimal
    for p, _, kind in spent:
        if kind == 'card':
            parts = _adjustment_parts(p.amount, _value_card_gst_percent(p))
        else:
            rate, share = _advance_gst_args(invoice)
            parts = _adjustment_parts(p.amount, rate, _sap('sap.account.advance_liability'), share)
        if parts is None:
            continue
        credit_b2c += Decimal(str(parts['total']))
        for account, amount in ((parts['advance_account'], parts['base']),
                                (parts['cgst_account'], parts['cgst']),
                                (parts['sgst_account'], parts['sgst'])):
            if amount:
                debits[account] = debits.get(account, Decimal('0')) + Decimal(str(amount))

    # The cash / UPI part: one debit line per account it goes to, in proportion,
    # the last one taking the paisa.
    by_account = {}
    for p in real:
        by_account.setdefault(payment_account(p.payment_method), []).append(p)
    given = Decimal('0')
    accounts = list(by_account)
    for n, account in enumerate(accounts):
        paid = sum((Decimal(str(p.amount)) for p in by_account[account]), Decimal('0'))
        part = (ar_total - given) if n == len(accounts) - 1 else \
            ((ar_total * paid / real_total).quantize(Decimal('0.01')) if real_total > 0 else Decimal('0'))
        given += part
        if part > 0:
            debits[account] = debits.get(account, Decimal('0')) + part
    credit_b2c += ar_total

    if credit_b2c <= 0:
        return True

    lines = [{"ShortName": card_code, "Debit": 0.0, "Credit": float(round(credit_b2c, 2)),
              "BPLID": bpl_id}]
    for account, amount in debits.items():
        lines.append({"AccountCode": account, "Debit": float(round(amount, 2)), "Credit": 0.0,
                      "BPLID": bpl_id})
    je_udfs = _get_je_udf_fields(invoice)
    for line in lines:
        line.update(je_udfs)

    client_name, client_phone = "Walk-in", ""
    if getattr(invoice, 'client', None):
        first = getattr(invoice.client, 'first_name', '') or ''
        last = getattr(invoice.client, 'last_name', '') or ''
        client_name = f"{first} {last}".strip() or "Walk-in"
        client_phone = getattr(invoice.client, 'phone', '') or ''

    ids = '-'.join(str(p.id) for p in [s[0] for s in spent] + real)
    payload = {
        "ReferenceDate": doc_date,
        "Memo": f"Advance Adjustment (Bill) - Invoice #{invoice.id} - {card_code}",
        "Reference": str(card_code)[:11],
        "Reference2": str(client_phone)[:11],
        "Reference3": str(client_name)[:27],
        "JournalEntryLines": lines,
    }
    total = float(round(credit_b2c, 2))
    jdt = _post_je_once(f"je:bill:{invoice.id}:{ids}", payload, total)
    if not jdt:
        logger.error(f"SAP bill journal error: SAP did not accept the journal for invoice {invoice.id}")
        return False

    for p, key, _ in spent:
        sap_ledger.mark_done(key, jdt, amount=float(p.amount), note='In the bill journal')
    if real:
        sap_ledger.mark_done(
            f"inpay:{invoice.id}:ar:j{'-'.join(str(p.id) for p in real)}", f"JE{jdt}",
            amount=float(ar_total),
            detail={'payment_ids': [p.id for p in real], 'marketing_applied': float(marketing_used),
                    'methods': sorted({p.payment_method for p in real}), 'journal': True},
            note='In the bill journal')
    logger.info(f"SAP: Created single journal for invoice {invoice.id} (card/advance + payments)")
    return True


def create_sap_incoming_payment(invoice):
    """
    Sync Payments for a finalized invoice to SAP as Incoming Payments.
    On failure the payment is enqueued for retry so it is never silently lost.

    Every SAP document posted here is recorded (sap_ledger), so running this
    again - from the retry queue, or after a later part-payment - posts only
    what SAP does not have yet:
      * the A/R payment records which POS payments it covered; a payment added
        later is posted as its own incoming payment (it used to never reach SAP);
      * each advance / cashback settlement and each value-card redemption
        journal is recorded per POS payment, and a failure now returns False so
        the queue retries it (it used to be logged and dropped).
    """
    try:
        if not invoice.sap_doc_entry:
            logger.warning(f"SAP Incoming Payment: invoice {invoice.id} has no sap_doc_entry – skipping")
            return False

        import datetime
        from django.conf import settings
        from pos_backend import sap_ledger

        doc_date = str(invoice.created_at.date() if invoice.created_at else datetime.date.today())

        # Calculate total of all marketing items (Advances, Memberships, Value Cards, Packages)
        # These are billed separately via _create_marketing_ar_invoice and _create_marketing_je,
        # so their cash must NOT be applied to the primary A/R Invoice.
        marketing_items_total = sum(
            float(i.total_price) for i in invoice.items.select_related('content_type').all()
            if i.content_type and i.content_type.app_label == 'marketing'
        )

        payments = [p for p in invoice.payments.all().order_by('id') if not getattr(p, 'is_tip', False)]
        if not payments:
            return True

        if _legacy_payments_already_in_sap(invoice, payments):
            return True

        # The same partner the invoice was posted to, or SAP cannot apply it.
        card_code = _invoice_card_code(invoice)

        bpl_id = _resolve_bpl_id(invoice.center.sap_branch_name if invoice.center else None)
        all_ok = True

        # ── 0. A bill part-paid from a card / advance is ONE journal ─────────
        # (the card part and the cash/UPI part together, as in the SAP sheet).
        # Anything else - or a bill already partly posted the old way - goes
        # through the steps below unchanged. If SAP refuses it, stop here: the
        # retry posts the whole journal, and the cash part must not also go as
        # an incoming payment meanwhile.
        if _post_bill_journal(invoice, payments, card_code, bpl_id, doc_date,
                              marketing_items_total) is False:
            return False

        # ── 1. Real money against the A/R invoice ────────────────────────────
        # One incoming payment per SAP account the money goes to: each way of
        # paying has its own account in Settings › SAP mapping (Q18), and an
        # incoming payment carries one cash and one transfer account. Amounts
        # are worked in Decimal; SAP gets them rounded to the paisa.
        from pos_backend.models import SapPosting
        ar_prefix = f"inpay:{invoice.id}:ar:"
        ar_done = sap_ledger.done_rows(ar_prefix)
        covered = set()
        marketing_applied = Decimal('0')
        for row in ar_done:
            covered.update((row.detail or {}).get('payment_ids') or [])
            marketing_applied += Decimal(str((row.detail or {}).get('marketing_applied') or 0))

        new_real = [p for p in payments if not _is_virtual_payment(p.payment_method) and p.id not in covered]
        if new_real:
            # An incoming payment carries one cash account and one transfer
            # account, so cash rides with the first transfer account, as it
            # always did; only a second transfer account needs its own document.
            cash = [p for p in new_real if p.payment_method == 'Cash']
            transfers = {}
            for p in new_real:
                if p.payment_method != 'Cash':
                    transfers.setdefault(payment_account(p.payment_method), []).append(p)
            groups = {}
            for n_group, (account, rows) in enumerate(transfers.items()):
                groups[(n_group == 0 and bool(cash), account)] = (cash if n_group == 0 else []) + rows
            if not transfers:
                groups[(True, None)] = cash

            total_paid = sum((Decimal(str(p.amount)) for p in new_real), Decimal('0'))
            marketing_left = max(Decimal('0'), Decimal(str(marketing_items_total)) - marketing_applied)
            marketing_used = min(total_paid, marketing_left)
            # Only what is left after the card / membership / package lines
            # (posted on their own) is applied to the A/R invoice.
            ar_total = total_paid - marketing_used

            ar_given = Decimal('0')
            keys = list(groups)
            for g, (is_cash, account) in enumerate(keys):
                group = groups[(is_cash, account)]
                group_paid = sum((Decimal(str(p.amount)) for p in group), Decimal('0'))
                if g == len(keys) - 1:
                    ar_part = ar_total - ar_given
                else:
                    ar_part = (ar_total * group_paid / total_paid).quantize(Decimal('0.01')) \
                        if total_paid > 0 else Decimal('0')
                ar_given += ar_part
                # Keyed by the payments it carries, so a retry of one group
                # never lands on another group's record. A payment re-posted
                # after a method change gets a fresh reference, so the lookup
                # cannot mistake the cancelled document for the new one.
                ids = '-'.join(str(p.id) for p in group)
                key = f"{ar_prefix}p{ids}"
                rev = SapPosting.objects.filter(key__startswith=f"inpayvoid:{invoice.id}:ar:p{ids}:").count()
                detail = {'payment_ids': [p.id for p in group],
                          'marketing_applied': float(group_paid - ar_part),
                          'methods': sorted({p.payment_method for p in group})}

                if ar_part <= 0:
                    # All of it went to the card/membership/package lines:
                    # nothing to post, but these payments are accounted for.
                    sap_ledger.mark_done(key, '', amount=0, detail=detail,
                                         note='Covered by marketing lines')
                    continue

                amount = float(ar_part.quantize(Decimal('0.01')))
                reference = f"POS_INV_{invoice.id}_AR{ids}" + (f"R{rev}" if rev else '')
                payload = {
                    "CardCode": card_code,
                    "DocDate": doc_date,
                    "BPLID": bpl_id,
                    "Remarks": reference,
                    "PaymentInvoices": [
                        {
                            "DocEntry": invoice.sap_doc_entry,
                            "SumApplied": str(amount),
                            "InvoiceType": "it_Invoice",
                        }
                    ],
                }
                # This document's money split between its cash and transfer
                # accounts in proportion, the transfer part taking the paisa.
                cash_paid = sum((Decimal(str(p.amount)) for p in group if p.payment_method == 'Cash'),
                                Decimal('0'))
                cash_part = ((ar_part * cash_paid / group_paid).quantize(Decimal('0.01'))
                             if group_paid > 0 else Decimal('0'))
                transfer_part = ar_part - cash_part
                if cash_part > 0:
                    payload["CashSum"] = float(cash_part)
                    payload["CashAccount"] = payment_account('Cash')
                if transfer_part > 0:
                    payload["TransferSum"] = float(transfer_part)
                    payload["TransferAccount"] = account
                    payload["TransferDate"] = doc_date
                    refs = ', '.join(p.reference for p in group if getattr(p, 'reference', ''))
                    payload["TransferReference"] = (refs or f"POS_INV_{invoice.id}")[:27]

                def _post(payload=payload):
                    response = _send_sap_request('POST', f"{SAP_URL_BASE}/IncomingPayments", json=payload, timeout=_post_timeout())
                    if response.status_code not in (200, 201):
                        body = getattr(response, 'text', '')
                        logger.error(f"SAP Incoming Payment error: HTTP {response.status_code} for invoice {invoice.id} – {body[:300]}")
                        return None
                    return response.json().get('DocEntry') or 'posted'

                if sap_ledger.post_once(key, _post, lambda r=reference: _find_incoming_payment(r),
                                        amount=amount, detail=detail):
                    logger.info(f"SAP: Created Incoming Payment for invoice {invoice.id} ({ids})")
                else:
                    all_ok = False

        # ── 2. Balances that settle the rest of the A/R invoice ──────────────
        for p in payments:
            method = p.payment_method or ''
            is_advance = method == 'Advance' or 'Advance' in method
            is_cashback = method == 'Cashback Wallet' or 'Cashback' in method
            is_value_card = method == 'Value Card' or 'Value Card' in method

            if not (is_advance or is_cashback or is_value_card):
                continue

            amount_to_clear = float(p.amount)
            if amount_to_clear <= 0:
                continue

            if is_value_card:
                # Value Card redemption -> Step 3: Advance Adjustment JE (NOT an Incoming Payment)
                gst_percent_for_je = _value_card_gst_percent(p)

                if not create_sap_valuecard_redemption_je(invoice, amount_to_clear, gst_percent_for_je, payment_id=p.id):
                    all_ok = False
                continue  # Advance Adjustment JE handles this - no Incoming Payment for value cards

            if is_advance:
                # Spending an advance is a journal, as in the SAP scenarios
                # (JE Advance Adjustment): it releases the advance liability
                # and its GST. Bills whose advance was already settled the old
                # way (an incoming payment to the liability account) are left.
                old_settlement = sap_ledger.row(f"inpay:{invoice.id}:virt:{p.id}")
                if old_settlement is not None and old_settlement.status == SapPosting.DONE:
                    continue
                if not create_sap_advance_adjustment_je(invoice, amount_to_clear, payment_id=p.id):
                    all_ok = False
                continue
            liability_account = _sap('sap.account.cashback_liability')

            rev = SapPosting.objects.filter(key__startswith=f"inpayvoid:{invoice.id}:virt:{p.id}:").count()
            reference = f"POS_INV_{invoice.id}_P{p.id}" + (f"R{rev}" if rev else '')
            virtual_payload = {
                "CardCode": card_code,
                "DocDate": doc_date,
                "BPLID": bpl_id,
                "Remarks": reference,
                "TransferSum": amount_to_clear,
                "TransferAccount": liability_account,
                "TransferDate": doc_date,
                "TransferReference": f"POS_INV_{invoice.id}_{method}",
                "PaymentInvoices": [
                    {
                        "DocEntry": invoice.sap_doc_entry,
                        "SumApplied": str(amount_to_clear),
                        "InvoiceType": "it_Invoice",
                    }
                ],
            }

            def _post_virtual(payload=virtual_payload, method=method):
                vp_res = _send_sap_request('POST', f"{SAP_URL_BASE}/IncomingPayments", json=payload, timeout=_post_timeout())
                if vp_res.status_code in (200, 201):
                    return vp_res.json().get('DocEntry') or 'posted'
                logger.error(f"SAP: Failed to settle {method} payment: {getattr(vp_res, 'text', '')}")
                return None

            try:
                if sap_ledger.post_once(f"inpay:{invoice.id}:virt:{p.id}", _post_virtual,
                                        lambda r=reference: _find_incoming_payment(r),
                                        amount=round(amount_to_clear, 2)):
                    logger.info(f"SAP: Settled {method} payment of {amount_to_clear} against invoice {invoice.id}")
                else:
                    all_ok = False
            except Exception as e:
                logger.error(f"SAP: Exception settling {method} payment: {e}")
                all_ok = False

        return all_ok

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        logger.error(f"SAP Incoming Payment creation error for invoice {invoice.id}: HTTP {e.response.status_code} - {body}")
        return False

    except Exception as e:
        logger.error(f"SAP Incoming Payment creation error for invoice {invoice.id}: {e}")
        return False


def request_payment_repost(invoice, payment_ids):
    """Note that these payments changed method after the bill, so their SAP
    incoming payments must be cancelled and posted again (#41). Called in
    the same transaction as the change."""
    from pos_backend import sap_ledger
    from pos_backend.models import SapPosting
    for pid in payment_ids:
        rec = sap_ledger._record(f"repost:{invoice.id}:{pid}")
        SapPosting.objects.filter(pk=rec.pk).update(status=SapPosting.PENDING, last_error='')


def repost_incoming_payments(invoice):
    """Cancel the SAP incoming payments of payments whose method changed, then
    post them again as they are now.

    An incoming payment that carried a changed payment is cancelled in SAP
    (all of it - it may carry other payments too, which are simply posted
    again) and its ledger record is set aside as `inpayvoid:...`. Then the
    normal payment sync posts every payment SAP no longer has. If the bill
    never reached SAP there is nothing to cancel.
    """
    from pos_backend import sap_ledger
    from pos_backend.models import SapPosting

    markers = list(SapPosting.objects.filter(key__startswith=f"repost:{invoice.id}:",
                                             status=SapPosting.PENDING))
    if not markers:
        return True
    changed = {int(m.key.rsplit(':', 1)[1]) for m in markers}

    rows = []
    for row in sap_ledger.done_rows(f"inpay:{invoice.id}:"):
        ids = set((row.detail or {}).get('payment_ids') or [])
        if row.key.startswith(f"inpay:{invoice.id}:virt:"):
            ids.add(int(row.key.rsplit(':', 1)[1]))
        if ids & changed:
            rows.append(row)

    for row in rows:
        if (row.detail or {}).get('journal'):
            # Posted inside the bill's single journal, not as an incoming
            # payment, so there is no payment to cancel and re-posting it would
            # count the money twice. It stays as it is in SAP.
            logger.warning(f"SAP: payment(s) {(row.detail or {}).get('payment_ids')} of invoice "
                           f"{invoice.id} are inside the bill journal; their method change is not "
                           f"re-posted - correct it in SAP.")
            continue
        doc = str(row.doc_entry or '')
        if doc.isdigit():
            res = _send_sap_request('POST', f"{SAP_URL_BASE}/IncomingPayments({doc})/Cancel",
                                    timeout=15)
            if not 200 <= res.status_code < 300:
                logger.error(f"SAP: could not cancel incoming payment {doc} for invoice "
                             f"{invoice.id}: {getattr(res, 'text', '')[:300]}")
                return False
        void_key = row.key.replace('inpay:', 'inpayvoid:', 1) + f":{row.pk}"
        SapPosting.objects.filter(pk=row.pk).update(key=void_key[:120])

    SapPosting.objects.filter(pk__in=[m.pk for m in markers]).update(status=SapPosting.DONE)
    if not invoice.sap_doc_entry:
        return True
    return create_sap_incoming_payment(invoice)


def _cancel_in_sap(entity, number, label):
    """Cancel one SAP document (`JournalEntries`, `IncomingPayments`) exactly once.

    SAP's own Cancel action writes the reversing document, so the books carry
    both the original and its reversal, dated and numbered by SAP. A cancel whose
    reply was lost and is then sent again is answered "already cancelled", which
    is the outcome wanted, so it counts as done.
    """
    from pos_backend.sap_ledger import post_once

    def _post():
        try:
            response = _send_sap_request('POST', f"{SAP_URL_BASE}/{entity}({number})/Cancel",
                                         timeout=_post_timeout())
        except requests.exceptions.HTTPError as exc:
            text = str(getattr(getattr(exc, 'response', None), 'text', '') or '').lower()
            if 'already' in text and 'cancel' in text:
                return 'already cancelled'
            raise
        return 'cancelled' if 200 <= response.status_code < 300 else None

    return post_once(f"cancel:{label}:{number}", _post)


def sap_documents_of_bill(invoice):
    """(journal numbers, incoming-payment numbers) the POS has put into SAP for this bill.

    Read from the posting ledger, so only what really reached SAP is listed:
    the sale journal of each card / membership / package line, the advance it
    sold, the single bill journal, each card / advance spend, and every
    incoming payment (cash, UPI, cashback wallet...). A journal that carried
    several of these appears once.
    """
    from django.db.models import Q
    from billing.models import AdvancePayment
    from pos_backend.models import SapPosting

    item_ids = list(invoice.items.values_list('id', flat=True))
    pay_ids = list(invoice.payments.values_list('id', flat=True))
    adv_ids = list(AdvancePayment.objects.filter(invoice=invoice).values_list('id', flat=True))
    exact = ([f'je:sale:{i}' for i in item_ids] + [f'je:vcredeem:{p}' for p in pay_ids]
             + [f'je:advadj:{p}' for p in pay_ids] + [f'je:advance:{a}' for a in adv_ids])
    wanted = Q(key__in=exact)
    for prefix in (f'je:bill:{invoice.id}:', f'je:vcredeem:inv{invoice.id}-', f'je:advadj:inv{invoice.id}-'):
        wanted |= Q(key__startswith=prefix)
    journals, payments = set(), set()
    for row in SapPosting.objects.filter(wanted, status=SapPosting.DONE):
        if str(row.doc_entry or '').isdigit():
            journals.add(int(row.doc_entry))
    for row in SapPosting.objects.filter(key__startswith=f'inpay:{invoice.id}:', status=SapPosting.DONE):
        doc = str(row.doc_entry or '')
        if doc.isdigit():
            payments.add(int(doc))
        elif doc[:2] == 'JE' and doc[2:].isdigit():
            journals.add(int(doc[2:]))        # paid inside the bill journal
    return sorted(journals), sorted(payments)


def create_sap_cancel_reversals(invoice):
    """Take back in SAP what a CANCELLED bill put there besides its invoice.

    The credit memo reverses the A/R invoice, but nothing reversed the rest: the
    sale journal of a membership, card, package or advance stayed in SAP (so SAP
    kept the liability and the cash while the POS had taken both back), a cash
    or UPI payment stayed applied to an invoice that no longer stood, and a card
    or cashback spend stayed charged to the customer's wallet account.

    Each journal and each incoming payment is cancelled with SAP's own Cancel
    action, once (the posting ledger remembers). The payments are cancelled
    BEFORE the credit memo is made (see sap_jobs), so the invoice is open again
    and the memo closes it instead of floating beside it.

    Returns True when everything is cancelled (or there is nothing to cancel).
    """
    try:
        if invoice.status not in ('cancelled', 'refunded'):
            return True
        journals, payments = sap_documents_of_bill(invoice)
        done = True
        # Payments first, then the journals that hold the liabilities.
        for number in payments:
            if not _cancel_in_sap('IncomingPayments', number, 'inpay'):
                done = False
        for number in journals:
            if not _cancel_in_sap('JournalEntries', number, 'je'):
                done = False
        if journals or payments:
            logger.info(f"SAP: cancelled {len(payments)} payment(s) and {len(journals)} journal(s) of invoice {invoice.id}")
        if not done:
            invoice.sap_sync_message = "SAP did not accept the cancellation of a payment or journal"
            invoice.save(update_fields=['sap_sync_message'])
        return done
    except Exception as e:
        logger.error(f"SAP cancel reversals error for invoice {invoice.id}: {e}")
        invoice.sap_sync_message = str(e)[:300]
        invoice.save(update_fields=['sap_sync_message'])
        return False


def _find_incoming_payment(remarks):
    """DocEntry of an incoming payment carrying this POS reference, if any."""
    from pos_backend.sap_ledger import odata_filter, odata_str
    url = (f"{SAP_URL_BASE}/IncomingPayments?$select=DocEntry&$filter="
           + odata_filter(f"Remarks eq {odata_str(remarks)}"))
    rows = (_send_sap_request('GET', url, timeout=10).json() or {}).get('value') or []
    return rows[0].get('DocEntry') if rows else None

def _find_by_comments(entity, comments):
    """DocEntry of a marketing document of `entity` whose Comments match, if any."""
    from pos_backend.sap_ledger import odata_filter, odata_str
    url = (f"{SAP_URL_BASE}/{entity}?$select=DocEntry&$filter="
           + odata_filter(f"Comments eq {odata_str(comments)}"))
    rows = (_send_sap_request('GET', url, timeout=10).json() or {}).get('value') or []
    return rows[0].get('DocEntry') if rows else None


def _apply_tax_fallback(body, lines, tax_for_line):
    """The original GST fallback: explicit IGST/CG+SG codes when SAP asks for them."""
    is_inter = "254000293" in body
    is_intra = "254000294" in body
    for line in lines:
        tax_pct = tax_for_line(line)
        suffix = str(int(tax_pct)) if tax_pct else "0"
        if is_inter:
            line['TaxCode'] = f"IGST@{suffix}"
        elif is_intra:
            line['TaxCode'] = f"CG+SG@{suffix}"


def create_sap_purchase_delivery_note(po):
    """
    Sync a POS Purchase Order GRPO to SAP as a Purchase Delivery Note (GRPO).

    Fixed: a GRPO that SAP accepted on the fallback path (no new receipt rows)
    was then marked as failed and queued, and the retry took the same
    fallback path and posted the whole PO again, doubling stock in SAP. Each
    PO and each receipt is now posted once (sap_ledger), and a PO that already
    has its GRPO is never posted again through the fallback.
    """
    try:
        if not po.vendor:
            return False

        from pos_backend.sap_ledger import post_once

        vendor_code = po.vendor.vendor_code or f"V{po.vendor.id:06d}"

        # Ensure vendor exists — call directly (same module, no import needed)
        if not check_sap_bp_exists(vendor_code):
            create_sap_vendor(po.vendor)

        wh_code = _warehouse_code(po.center)

        import datetime
        doc_date = str(po.created_at.date() if po.created_at else datetime.date.today())
        valid_items = [item for item in po.items.select_related('product').all() if item.quantity > 0]

        bpl_id = _resolve_bpl_id(po.center.sap_branch_name if po.center else None)

        # 1. CREATE SAP PURCHASE ORDER (IF NOT EXISTS)
        if not getattr(po, 'sap_po_doc_entry', None):
            po_lines = []
            for item in valid_items:
                item_code = item.product.product_code or f"P{item.product.id:06d}"
                po_lines.append({
                    "ItemCode": item_code,
                    "Quantity": plain_qty(item.quantity),
                    "UnitPrice": str(item.rate),
                    "WarehouseCode": wh_code,
                })

            if po_lines:
                po_payload = {
                    "CardCode": vendor_code,
                    "DocDate": doc_date,
                    "BPL_IDAssignedToInvoice": bpl_id,
                    "Comments": f"POS PO-{po.id}",
                    "DocumentLines": po_lines
                }

                def _post_po():
                    try:
                        po_response = _send_sap_request('POST', f"{SAP_URL_BASE}/PurchaseOrders", json=po_payload, timeout=_post_timeout())
                        return po_response.json().get('DocEntry')
                    except requests.exceptions.HTTPError as po_e:
                        body = getattr(po_e.response, 'text', '')
                        if "254000293" in body or "254000294" in body:
                            # Tax error, retry with explicit TaxCode
                            _apply_tax_fallback(body, po_payload['DocumentLines'],
                                                lambda line: getattr(valid_items[po_payload['DocumentLines'].index(line)], 'tax_percent', 0))
                            try:
                                po_response_retry = _send_sap_request('POST', f"{SAP_URL_BASE}/PurchaseOrders", json=po_payload, timeout=_post_timeout())
                                logger.info(f"SAP: Created PO for POS PO {po.id} on retry")
                                return po_response_retry.json().get('DocEntry')
                            except requests.exceptions.HTTPError as retry_e:
                                raise Exception(f"Failed to create PO even after tax fallback: {getattr(retry_e.response, 'text', '')}")
                        raise

                po_entry = post_once(f"po:{po.id}", _post_po,
                                     lambda: _find_by_comments('PurchaseOrders', po_payload['Comments']))
                if po_entry and po_entry != 'posted':
                    po.sap_po_doc_entry = int(po_entry)
                    po.save(update_fields=['sap_po_doc_entry'])
                    logger.info(f"SAP: Created PO {po.sap_po_doc_entry} for POS PO {po.id}")

        # 2. CREATE SAP GRPO
        from inventory.models import StockTransaction
        unsynced_txns = list(StockTransaction.objects.filter(
            center=po.center,
            transaction_type='PO_RECEIPT',
            notes=f'Receipt from PO-{po.id}',
            sap_sync_status=False
        ).select_related('product', 'lot', 'batch').order_by('id'))

        # The goods came in when they were received, not when the PO was written.
        receipt_moment = min((t.created_at for t in unsynced_txns), default=None)
        grpo_date = str(_local_date(receipt_moment))

        document_lines = []
        if unsynced_txns:
            for txn in unsynced_txns:
                item = po.items.filter(product=txn.product).first()
                if not item: continue

                try:
                    base_line = valid_items.index(item)
                except ValueError:
                    continue

                item_code = txn.product.product_code or f"P{txn.product.id:06d}"
                line = {
                    "ItemCode": item_code,
                    "Quantity": plain_qty(txn.quantity_change),
                    "UnitPrice": str(item.rate),
                    "WarehouseCode": wh_code,
                }

                # New receipts carry a StockBatch (`batch`); `lot` is the old record.
                batches = receipt_batches(item_code, txn.quantity_change, batch=txn.batch,
                                          lot=txn.lot, on=txn.created_at)
                if batches:
                    line["BatchNumbers"] = batches
                if getattr(po, 'sap_po_doc_entry', None):
                    line["BaseType"] = 22  # SAP Object Type for Purchase Orders
                    line["BaseEntry"] = po.sap_po_doc_entry
                    line["BaseLine"] = base_line
                document_lines.append(line)
            receipt_ref = 't' + '-'.join(str(t.id) for t in unsynced_txns)
        else:
            if po.sap_doc_entry:
                # Every receipt row is already in SAP and the PO has its GRPO.
                # Posting the whole PO again here is what doubled stock.
                return True
            # Fallback if no specific unsynced transactions found (e.g. legacy sync)
            for idx, item in enumerate(valid_items):
                qty = item.received_quantity if item.received_quantity > 0 else item.quantity
                if qty <= 0: continue

                item_code = item.product.product_code or f"P{item.product.id:06d}"
                line = {
                    "ItemCode": item_code,
                    "Quantity": plain_qty(qty),
                    "UnitPrice": str(item.rate),
                    "WarehouseCode": wh_code,
                }
                batches = receipt_batches(item_code, qty)
                if batches:
                    line["BatchNumbers"] = batches
                if getattr(po, 'sap_po_doc_entry', None):
                    line["BaseType"] = 22
                    line["BaseEntry"] = po.sap_po_doc_entry
                    line["BaseLine"] = idx
                document_lines.append(line)
            receipt_ref = 'all'

        if not document_lines:
            return True

        if len(receipt_ref) > 90:
            import hashlib
            receipt_ref = 'h' + hashlib.sha1(receipt_ref.encode()).hexdigest()[:20]

        payload = {
            "CardCode": vendor_code,
            "DocDate": grpo_date,
            "BPL_IDAssignedToInvoice": bpl_id,
            "Comments": f"POS PO-{po.id} GRPO {receipt_ref}",
            "DocumentLines": document_lines
        }

        def _tax_for_grpo_line(line):
            for item in valid_items:
                item_code = item.product.product_code or f"P{item.product.id:06d}"
                if line["ItemCode"] == item_code:
                    return item.tax_percent
            return 0

        def _post_grpo():
            try:
                response = _send_sap_request('POST', f"{SAP_URL_BASE}/PurchaseDeliveryNotes", json=payload, timeout=_post_timeout())
                logger.info(f"SAP: Created GRPO for PO {po.id}")
            except requests.exceptions.HTTPError as e:
                body = getattr(e.response, 'text', '')
                if "254000293" in body or "254000294" in body:
                    _apply_tax_fallback(body, payload['DocumentLines'], _tax_for_grpo_line)
                    try:
                        response = _send_sap_request('POST', f"{SAP_URL_BASE}/PurchaseDeliveryNotes", json=payload, timeout=_post_timeout())
                        logger.info(f"SAP: Created GRPO for PO {po.id} on tax retry")
                    except requests.exceptions.HTTPError as retry_e:
                        raise Exception(f"HTTP {retry_e.response.status_code} - {getattr(retry_e.response, 'text', '')}")
                else:
                    raise
            return response.json().get('DocEntry') or 'posted'

        grpo_entry = post_once(f"grpo:{po.id}:{receipt_ref}", _post_grpo,
                               lambda: _find_by_comments('PurchaseDeliveryNotes', payload['Comments']))
        if not grpo_entry:
            po.sap_sync_status = False
            po.sap_sync_message = "SAP did not accept the GRPO"
            po.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return False

        po.sap_sync_status = True
        po.sap_sync_message = "Success"
        if grpo_entry != 'posted':
            po.sap_doc_entry = int(grpo_entry)
        po.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_doc_entry'])
        if unsynced_txns:
            StockTransaction.objects.filter(pk__in=[t.pk for t in unsynced_txns]).update(
                sap_sync_status=True, sap_sync_message="Synced via GRPO")
        return True

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        logger.error(f"SAP GRPO creation error for PO {po.id}: HTTP {e.response.status_code} - {body[:300]}")
        po.sap_sync_status = False
        po.sap_sync_message = f"HTTP {e.response.status_code}: {body[:250]}"
        po.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
    except Exception as e:
        logger.error(f"SAP GRPO creation error: {e}")
        po.sap_sync_status = False
        po.sap_sync_message = str(e)[:300]
        po.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def create_sap_stock_transfer(transfer):
    """
    Sync a POS Stock Transfer to SAP as an Inventory Transfer.
    """
    try:
        if transfer.sap_doc_entry:
            # Already in SAP; a retry must not move the stock a second time.
            return True

        # Between two branches (two GSTINs) the move is a supply: SAP gets the tax invoice (raised when the goods
        # left) and, here, the purchase invoice for what arrived. Only stock inside one branch is a plain transfer.
        from inventory.places import supply_kind
        kind, why = supply_kind(transfer.from_center, transfer.to_center)
        if kind in ('igst', 'cgst_sgst'):
            from pos_backend.sap_branch_transfer import create_sap_branch_purchase
            return create_sap_branch_purchase(transfer)
        if kind == 'unknown':
            transfer.sap_sync_status = False
            transfer.sap_sync_message = f'Cannot tell how this transfer is taxed: {why}. Nothing was posted.'
            transfer.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return False

        from_wh = _warehouse_code(transfer.from_center)
        to_wh = _warehouse_code(transfer.to_center)
        
        wanted = []
        for item in transfer.items.select_related('product').all():
            # What the receiving branch accepted (the whole line unless it signed in less).
            quantity = item.quantity if item.received_quantity is None else item.received_quantity
            if quantity <= 0: continue
            wanted.append((item.product.product_code or f"P{item.product.id:06d}", quantity))

        # SAP keeps stock only for Inventory Items (OITM.InvntItem = Y); a transfer is posted for those only.
        # A line for an item that is not one is left out of the SAP document (the POS still moved its stock).
        # A code SAP does not know stays in, so SAP answers for it as it always did.
        from inventory.sap_items import stock_item_flags
        flags = stock_item_flags([code for code, _ in wanted]) if sap_enabled() else {}
        not_stock_items = [code for code, _ in wanted if flags.get(code) is False]

        document_lines = []
        for item_code, quantity in wanted:
            if flags.get(item_code) is False:
                continue
            line = {
                "ItemCode": item_code,
                "Quantity": plain_qty(quantity),
                "FromWarehouseCode": from_wh,
                "WarehouseCode": to_wh
            }

            batches = issue_batches(item_code, from_wh, quantity)
            if batches:
                line["BatchNumbers"] = batches
                
            document_lines.append(line)

        if not document_lines and not_stock_items:
            # Nothing in it that SAP keeps stock of: there is nothing to post, and it must not stay "unsent"
            # (the retry sweep would pick it up for ever).
            transfer.sap_sync_status = True
            transfer.sap_sync_message = ("Success (nothing sent: none of its items is an Inventory Item in SAP: "
                                         + ', '.join(not_stock_items)[:180] + ")")
            transfer.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        if not document_lines:
            return True

        import datetime

        bpl_id = _resolve_bpl_id(transfer.from_center.sap_branch_name if transfer.from_center else None)
                
        payload = {
            # Posted when the goods arrive, so dated the day they arrived.
            "DocDate": str(_local_date(transfer.received_at or transfer.dispatched_at or transfer.created_at)),
            "FromWarehouse": from_wh,
            "ToWarehouse": to_wh,
            "BPLID": bpl_id,
            "Comments": f"POS Stock Transfer #{transfer.id}",
            "StockTransferLines": document_lines
        }

        from pos_backend.sap_ledger import post_once

        def _post():
            response = _send_sap_request('POST', f"{SAP_URL_BASE}/StockTransfers", json=payload, timeout=_post_timeout())
            if response.status_code in (200, 201):
                return response.json().get('DocEntry') or 'posted'
            logger.error(f"SAP: Error creating Stock Transfer: {getattr(response, 'text', '')}")
            return None

        doc_entry = post_once(f"transfer:{transfer.id}", _post,
                              lambda: _find_by_comments('StockTransfers', payload['Comments']))
        if doc_entry:
            logger.info(f"SAP: Created Stock Transfer {transfer.id}")
            transfer.sap_sync_status = True
            transfer.sap_sync_message = "Success"
            if not_stock_items:
                transfer.sap_sync_message = ("Success (not sent, not Inventory Items in SAP: "
                                             + ', '.join(not_stock_items)[:180] + ")")
            if doc_entry != 'posted':
                transfer.sap_doc_entry = int(doc_entry)
            transfer.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_doc_entry'])
            return True

        transfer.sap_sync_status = False
        transfer.sap_sync_message = "SAP did not accept the stock transfer"
        transfer.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        logger.error(f"SAP Stock Transfer error: HTTP {e.response.status_code} - {body[:300]}")
        transfer.sap_sync_status = False
        transfer.sap_sync_message = f"HTTP {e.response.status_code}: {body[:250]}"
        transfer.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
    except Exception as e:
        logger.error(f"SAP Stock Transfer error: {e}")
        transfer.sap_sync_status = False
        transfer.sap_sync_message = str(e)[:300]
        transfer.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def create_sap_warehouse(center):
    """
    Sync a POS Center to SAP as a Warehouse.

    The previous version called /WarehouseLocations which is a different SAP entity
    (physical locations within a warehouse). The correct endpoint for warehouse master
    data in SAP Business One Service Layer is /Warehouses.
    """
    try:
        warehouse_code = center.sap_warehouse_code or f"WH{center.id:04d}"

        # Fetch locations from SAP to map the region automatically
        sap_location_id = None
        try:
            region_name = (center.region or "").strip()
            # Every page: a region past the first 20 used to look missing and
            # was created again as a duplicate location.
            locations = _get_all('WarehouseLocations?$select=Code,Name')
            region_lower = region_name.lower()
            for loc in locations:
                if (loc.get('Name') or '').lower().strip() == region_lower:
                    sap_location_id = loc.get('Code')
                    logger.info(f"Auto-mapped region '{region_name}' to SAP Location '{sap_location_id}'")
                    break
            
            # Auto-create the location in SAP if it doesn't exist
            if not sap_location_id and region_name:
                logger.info(f"Location '{region_name}' not found in SAP. Creating new WarehouseLocation...")
                create_loc_res = _send_sap_request('POST', f"{SAP_URL_BASE}/WarehouseLocations", json={"Name": region_name[:100]})
                if create_loc_res.status_code in (201, 200):
                    sap_location_id = create_loc_res.json().get('Code')
                    logger.info(f"Created new SAP Location '{region_name}' with Code '{sap_location_id}'")
        except Exception as e:
            logger.warning(f"Failed to fetch or create SAP location for auto-mapping: {e}")

        # Fallback to Punjab (2) if creation fails or region is empty
        if not sap_location_id:
            sap_location_id = _sap("sap.default_location")

        payload = {
            "WarehouseCode": warehouse_code,
            "WarehouseName": (center.display_name or center.center_name)[:100],
            "Street": (center.address or "")[:100],
            "City": (center.region or "")[:100],
            "FederalTaxID": (center.gst_number or center.cst_number or "")[:32],
            "Location": sap_location_id
        }
        
        # Map branch name to SAP BusinessPlaceID. Business place 1 is a real
        # branch too: only a centre with no branch name is left unassigned.
        branch = (center.sap_branch_name or '').strip()
        bpl_id = _resolve_bpl_id(branch)
        if branch and branch != 'No Branch':
            payload["BusinessPlaceID"] = bpl_id


        # First check if the warehouse already exists to avoid duplicate errors
        try:
            check_resp = _send_sap_request('GET', f"{SAP_URL_BASE}/Warehouses('{warehouse_code}')", timeout=10)
            if check_resp.status_code == 200:
                logger.info(f"SAP Warehouse '{warehouse_code}' already exists – patching")
                # SAP rejects Location/WarehouseCode on PATCH for existing warehouses.
                # Only send fields SAP accepts: name + branch assignment.
                patch_payload = {"WarehouseName": payload['WarehouseName']}
                if payload.get('BusinessPlaceID') is not None:
                    patch_payload['BusinessPlaceID'] = payload['BusinessPlaceID']
                _send_sap_request(
                    'PATCH', f"{SAP_URL_BASE}/Warehouses('{warehouse_code}')",
                    json=patch_payload, timeout=10
                )
                center.sap_sync_status = True
                center.sap_sync_message = "Already exists in SAP – patched"
                center.sap_warehouse_code = warehouse_code
                center.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_warehouse_code'])
                return True

        except requests.exceptions.HTTPError as check_err:
            if check_err.response.status_code != 404:
                raise

        response = _send_sap_request('POST', f"{SAP_URL_BASE}/Warehouses", json=payload, timeout=_post_timeout())

        if response.status_code in (200, 201, 204):
            logger.info(f"SAP: Created Warehouse '{warehouse_code}' for center {center.id}")
            center.sap_sync_status = True
            center.sap_sync_message = "Success"
            center.sap_warehouse_code = warehouse_code
            center.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_warehouse_code'])
            return True

        body = getattr(response, 'text', '')
        if response.status_code in (400, 409) and 'already exists' in body.lower():
            center.sap_sync_status = True
            center.sap_sync_message = "Already exists in SAP"
            center.sap_warehouse_code = warehouse_code
            center.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_warehouse_code'])
            return True

        logger.error(f"SAP: Error creating Warehouse: {body}")
        center.sap_sync_status = False
        center.sap_sync_message = f"HTTP {response.status_code}: {body[:250]}"
        center.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except Exception as e:
        logger.error(f"SAP Warehouse creation error: {e}")
        center.sap_sync_status = False
        center.sap_sync_message = str(e)[:300]
        center.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

def _stock_movement_covered_by(transaction):
    """The SAP document that already moves this stock, or None.

    These rows record, in the POS ledger, stock that another SAP document
    moves anyway. Posting them as a Goods Receipt / Goods Issue as well moved
    the stock in SAP twice - a refunded product went back into stock once via
    the A/R Credit Memo and again via a Goods Receipt.
    """
    kind = transaction.transaction_type
    notes = transaction.notes or ''
    if kind in ('SALE', 'BOM_CONSUMPTION', 'SERVICE_USE'):
        return 'A/R Invoice'
    if kind in ('TRANSFER_OUT', 'TRANSFER_IN'):
        return 'Inventory Transfer'
    if kind == 'REFUND':
        return 'A/R Credit Memo'
    if kind == 'PO_RECEIPT' and notes.startswith('Receipt from PO-'):
        return 'GRPO'
    if notes.startswith('Stock Transfer Out to') or notes.startswith('Stock Transfer In from'):
        return 'Inventory Transfer'
    return None


def create_sap_stock_transaction(transaction):
    """
    Sync a POS StockTransaction (Audit/Checkout/Sale/Refund) to SAP as
    Goods Issue or Goods Receipt entry.
      quantity_change > 0  →  /InventoryGenEntries  (stock in)
      quantity_change < 0  →  /InventoryGenExits    (stock out)
      quantity_change == 0 →  skip (e.g. oversell audit note with zero delta)

    The original version used item code PRD-{id:04d} which is inconsistent with
    the P{id:06d} / product_code format used by create_sap_item and create_sap_invoice.
    """
    try:
        if transaction.sap_doc_entry:
            # Already in SAP; a retry must not move the stock a second time.
            return True

        covered_by = _stock_movement_covered_by(transaction)
        if covered_by:
            transaction.sap_sync_status = True
            transaction.sap_sync_message = f"Synced via {covered_by}"
            transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        if not transaction.quantity_change or transaction.quantity_change == 0:
            # Zero-delta audit notes (oversell records) have no stock movement in SAP
            logger.info(f"SAP StockTransaction {transaction.id}: zero delta, skipping.")
            transaction.sap_sync_status = True
            transaction.sap_sync_message = "Skipped – zero quantity change"
            transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
            return True

        warehouse_code = _warehouse_code(transaction.center)
        item_code = transaction.product.product_code or f"P{transaction.product.id:06d}"

        # Goods Issue / Receipt moves SAP stock, and SAP keeps stock only for Inventory Items (InvntItem = Y):
        # for any other item there is nothing to move (the same rule as the Inventory Transfer).
        if sap_enabled():
            from inventory.sap_items import stock_item_flags
            if stock_item_flags([item_code]).get(item_code) is False:
                transaction.sap_sync_status = True
                transaction.sap_sync_message = "Skipped – not an Inventory Item in SAP"
                transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
                return True

        abs_qty = abs(transaction.quantity_change)
        endpoint = "/InventoryGenEntries" if transaction.quantity_change > 0 else "/InventoryGenExits"

        # Fix: For Audits, compute the true delta needed by SAP, since POS and SAP might have different starting baselines!
        if transaction.transaction_type == 'AUDIT':
            import re
            match = re.search(r'Physical count (\d+(?:\.\d+)?)', transaction.notes or '')
            if match:
                counted_qty = Decimal(match.group(1))
                try:
                    resp = _send_sap_request('GET', f"{SAP_URL_BASE}/Items('{item_code}')?$select=ItemWarehouseInfoCollection", timeout=10)
                    if resp.status_code == 200:
                        sap_data = resp.json()
                        wh_info = sap_data.get('ItemWarehouseInfoCollection', [])
                        sap_stock = 0
                        for wh in wh_info:
                            if wh.get('WarehouseCode') == warehouse_code:
                                sap_stock = wh.get('InStock', 0)
                                break
                        
                        sap_delta = counted_qty - Decimal(str(sap_stock))
                        if sap_delta == 0:
                            logger.info(f"SAP StockTransaction {transaction.id}: SAP already matches physical count.")
                            transaction.sap_sync_status = True
                            transaction.sap_sync_message = "SAP already matches physical count."
                            transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
                            return True
                            
                        abs_qty = abs(sap_delta)
                        endpoint = "/InventoryGenEntries" if sap_delta > 0 else "/InventoryGenExits"
                except Exception as e:
                    logger.warning(f"Failed to fetch SAP InStock for audit sync: {e}")

        bpl_id = _resolve_bpl_id(transaction.center.sap_branch_name if transaction.center else None)

        line = {
            "ItemCode": item_code,
            # A string: a Decimal cannot be written as JSON.
            "Quantity": plain_qty(abs_qty),
            "UnitPrice": float(transaction.product.price or 0.0),
            "WarehouseCode": warehouse_code,
        }
        
        # A batch-managed item must name its batches; a plain one must not.
        if endpoint == "/InventoryGenExits":
            batches = issue_batches(item_code, warehouse_code, abs_qty)
        else:
            batches = receipt_batches(item_code, abs_qty, batch=transaction.batch,
                                      lot=transaction.lot, on=transaction.created_at)
        if batches:
            line["BatchNumbers"] = batches

        payload = {
            "Comments": f"POS {transaction.transaction_type} TXN#{transaction.id}: {(transaction.notes or '')[:200]}",
            "BPL_IDAssignedToInvoice": bpl_id,
            "DocumentLines": [line],
        }

        from pos_backend.sap_ledger import post_once, odata_filter, odata_str

        def _post():
            response = _send_sap_request('POST', f"{SAP_URL_BASE}{endpoint}", json=payload, timeout=_post_timeout())
            if response.status_code in (200, 201):
                return response.json().get('DocEntry') or 'posted'
            body = getattr(response, 'text', '')
            logger.error(f"SAP: Error creating {endpoint} for TXN {transaction.id}: HTTP {response.status_code} – {body[:300]}")
            return None

        def _find():
            # The comment starts with "POS <type> TXN#<id>:", unique per row.
            prefix = f"POS {transaction.transaction_type} TXN#{transaction.id}:"
            url = (f"{SAP_URL_BASE}{endpoint}?$select=DocEntry&$filter="
                   + odata_filter(f"startswith(Comments,{odata_str(prefix)})"))
            rows = (_send_sap_request('GET', url, timeout=10).json() or {}).get('value') or []
            return rows[0].get('DocEntry') if rows else None

        doc_entry = post_once(f"stocktxn:{transaction.id}", _post, _find)
        if doc_entry:
            logger.info(f"SAP: Created {endpoint} {doc_entry} for StockTransaction {transaction.id}")
            transaction.sap_sync_status = True
            transaction.sap_sync_message = "Success"
            if doc_entry != 'posted':
                transaction.sap_doc_entry = int(doc_entry)
            transaction.save(update_fields=['sap_sync_status', 'sap_sync_message', 'sap_doc_entry'])
            return True

        transaction.sap_sync_status = False
        transaction.sap_sync_message = "SAP did not accept the stock entry"
        transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

    except requests.exceptions.HTTPError as e:
        body = getattr(e.response, 'text', '')
        logger.error(f"SAP Stock Transaction creation error for TXN {transaction.id}: HTTP {e.response.status_code} - {body[:300]}")
        transaction.sap_sync_status = False
        transaction.sap_sync_message = f"HTTP {e.response.status_code}: {body[:250]}"
        transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False
    except Exception as e:
        logger.error(f"SAP Stock Transaction creation error for TXN {transaction.id}: {e}")
        transaction.sap_sync_status = False
        transaction.sap_sync_message = str(e)[:300]
        transaction.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return False

