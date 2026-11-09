import json
import re
import logging
import threading
import ipaddress
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from django.utils.deprecation import MiddlewareMixin
from .models import SystemLog

logger = logging.getLogger(__name__)

class BoundedExecutor:
    """Bounded background writer for audit log rows.

    Set AUDIT_LOG_ASYNC = False to write inline. Required under the test suite:
    a background thread opens its own database connection, which deadlocks
    against the test transaction on SQLite, and makes assertions racy on any
    backend.
    """

    def __init__(self, max_workers, max_queue_size):
        self.executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='audit_log')
        self.semaphore = threading.BoundedSemaphore(max_queue_size)

    @property
    def is_async(self):
        from django.conf import settings
        return getattr(settings, 'AUDIT_LOG_ASYNC', True)

    def submit(self, fn, *args, **kwargs):
        if not self.is_async:
            try:
                fn(*args, **kwargs)
            except Exception:
                logger.exception('Audit log write failed.')
            return None

        if not self.semaphore.acquire(blocking=False):
            logger.warning("Audit log queue full, dropping log.")
            return None

        def _wrapper(*a, **kw):
            from django.db import close_old_connections
            try:
                close_old_connections()
                fn(*a, **kw)
            except Exception:
                logger.exception('Audit log write failed.')
            finally:
                close_old_connections()
                self.semaphore.release()

        return self.executor.submit(_wrapper, *args, **kwargs)

_executor = BoundedExecutor(max_workers=4, max_queue_size=10000)

# Substrings, matched case-insensitively against every key at every depth.
# 'pin' covers pin / app_pin / new_pin; 'password' covers app_password too.
SENSITIVE_KEYS = {
    'password', 'passwd', 'pin', 'token', 'secret', 'otp',
    'aadhar', 'aadhaar', 'cvv', 'card_number', 'api_key', 'authorization',
}

EXCLUDED_PREFIXES = (
    '/audit_logs/',
    '/admin/',
    '/static/',
    '/media/',
)

MODULE_RULES = [
    (r'/accounts/api/login',                'USERS',        'Session'),
    (r'/accounts/api/logout',               'USERS',        'Session'),
    (r'/accounts/api/users',                'USERS',        'User'),
    (r'/salon_admin/api/centers',           'CENTRES',      'Centre'),
    (r'/salon_admin/api/roles',             'ROLES',        'Role'),
    (r'/salon_admin/api/dashboard',         'DASHBOARD',    'Dashboard'),
    (r'/salon_admin/api/bills',             'BILLING',      'Admin Bill'),
    (r'/salon_admin/api/changes',           'BILLING',      'Price Change'),
    (r'/salon_admin/api/manager.discounts', 'BILLING',      'Manager Discount'),
    (r'/staff/api/members',                 'STAFF',        'Staff Member'),
    (r'/staff/api/logs',                    'STAFF',        'Service Log'),
    (r'/staff/api/consumptions',            'STAFF',        'Consumption'),
    (r'/staff/api/payrolls',                'STAFF',        'Payroll'),
    (r'/staff/api/transfers',               'STAFF',        'Transfer'),
    (r'/staff/api/tools',                   'STAFF',        'Tool'),
    (r'/staff/api/designations',            'STAFF',        'Designation'),
    (r'/staff/api/reports',                 'STAFF',        'Staff Report'),
    (r'/clients/api/clients',               'CLIENTS',      'Client'),
    (r'/services/api/master',               'SERVICES',     'Service'),
    (r'/services/api/center',               'SERVICES',     'Centre Service Override'),
    (r'/inventory/api/products/checkout',   'INVENTORY',    'Stock Checkout'),
    (r'/inventory/api/products/audit',      'INVENTORY',    'Stock Audit'),
    (r'/inventory/api/products',            'INVENTORY',    'Product'),
    (r'/inventory/api/vendors',             'INVENTORY',    'Vendor'),
    (r'/inventory/api/purchase-orders',     'INVENTORY',    'Purchase Order'),
    (r'/inventory/api/lots',                'INVENTORY',    'Product Lot'),
    (r'/inventory/api/stock-transactions',  'INVENTORY',    'Stock Transaction'),
    (r'/billing/invoices',                  'BILLING',      'Invoice'),
    (r'/billing/advances',                  'BILLING',      'Advance'),
    (r'/billing/api',                       'BILLING',      'Billing'),
    (r'/appointments/api/appointments',     'APPOINTMENTS', 'Appointment'),
    (r'/marketing/api/promotions',          'MARKETING',    'Promotion'),
    (r'/marketing/api/cards',               'MARKETING',    'Value Card'),
    (r'/marketing/api/memberships',         'MARKETING',    'Membership'),
    (r'/marketing/api/packages',            'MARKETING',    'Package'),
    (r'/marketing/api/whatsapp',            'MARKETING',    'WhatsApp Campaign'),
    (r'/finance/api/daily-closing',         'FINANCE',      'Daily Closing'),
    (r'/finance/api/petty-cash',            'FINANCE',      'Petty Cash'),
    (r'/finance/api/shifts',                'FINANCE',      'Shift'),
    (r'/finance/api/incentives',            'FINANCE',      'Incentive'),
    (r'/finance/api/refunds',               'FINANCE',      'Refund'),
    (r'/finance/api/register',              'FINANCE',      'Register'),
    (r'/finance/api',                       'FINANCE',      'Finance'),
]


# ─── Device & Browser parsing (using user-agents library) ─────────────────────
def _parse_ua(ua_string: str) -> dict:
    """Returns dict with device_type, browser, os_info."""
    if not ua_string:
        return {'device_type': 'Unknown', 'browser': 'Unknown', 'os_info': 'Unknown'}
    try:
        import user_agents
        ua = user_agents.parse(ua_string)

        # Device type
        if ua.is_mobile:
            dtype = 'Mobile'
        elif ua.is_tablet:
            dtype = 'Tablet'
        elif ua.is_pc:
            dtype = 'Desktop / Laptop'
        elif ua.is_bot:
            dtype = 'Bot'
        else:
            dtype = 'Unknown'

        # Browser: name + major version
        browser_name = ua.browser.family or ''
        browser_ver  = str(ua.browser.version[0]) if ua.browser.version and ua.browser.version[0] else ''
        browser = f'{browser_name} {browser_ver}'.strip() if browser_name else 'Unknown'

        # OS: e.g. "Windows 10", "macOS 14", "iOS 17", "Android 14"
        os_family  = ua.os.family or ''
        os_ver     = ua.os.version_string or ''
        os_info    = f'{os_family} {os_ver}'.strip() if os_family else 'Unknown'

        return {'device_type': dtype, 'browser': browser, 'os_info': os_info}
    except Exception:
        return {'device_type': 'Unknown', 'browser': 'Unknown', 'os_info': 'Unknown'}


def _is_private_ip(ip: str) -> bool:
    """Check if an IP is private/loopback (no geo lookup possible)."""
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return True


def _ip_scope(ip: str) -> str:
    """'loopback', 'private' (the salon's own network) or 'public'."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return 'private'
    if addr.is_loopback:
        return 'loopback'
    if addr.is_private or addr.is_link_local:
        return 'private'
    return 'public'


@lru_cache(maxsize=1024)
def _cached_geo(ip: str) -> dict:
    """Where an address is.

    * this computer (127.0.0.1) and the salon's own network (192.168.x.x and so
      on) are named as such - no outside lookup is possible or needed;
    * a public address is looked up on freeipapi.com ONLY when Admin > Settings
      "Show the location of IP addresses" is on. It is off by default because it
      sends the caller's address to a third party. Answers are kept for the life
      of the process, and a failed lookup is kept too, so an offline counter
      never stalls the audit queue retrying.
    """
    from pos_backend import app_settings

    empty = {'city': '', 'region': '', 'country': '', 'country_code': ''}
    if not ip:
        return empty

    scope = _ip_scope(ip)
    if scope == 'loopback':
        return {'city': 'This computer', 'region': '', 'country': 'Local', 'country_code': ''}
    if scope == 'private':
        return {'city': 'Local network', 'region': '', 'country': 'Local', 'country_code': ''}

    if not app_settings.get('privacy.log_ip_geolocation'):
        return empty

    try:
        import requests as req
        resp = req.get(f'https://freeipapi.com/api/json/{ip}', timeout=1)
        data = resp.json()
        if data.get('cityName') or data.get('countryName'):
            return {
                'city':         data.get('cityName', ''),
                'region':       data.get('regionName', ''),
                'country':      data.get('countryName', ''),
                'country_code': data.get('countryCode', ''),
            }
    except Exception:
        import logging; logging.getLogger(__name__).error('Handled exception', exc_info=True)
    return empty


# ─── Token & Centre lookups (LRU-cached) ──────────────────────────────────────
# `lru_cache` here meant the FIRST answer for a token was reused for the life of
# the process. Rename a branch, move someone between branches or change their
# role, and every later audit row still recorded the old values - and a token
# that had since been revoked kept resolving to its user. A short TTL keeps the
# saving (one query per token per minute, not per request) without freezing the
# record of who did what.
_LOOKUP_TTL_SECONDS = 60


def _cached_user_from_token(token_key: str):
    """User info for a token, cached briefly. Returns a tuple or None."""
    from django.core.cache import cache

    if not token_key:
        return None

    key = f'auditlog:token:{token_key}'
    cached = cache.get(key)
    if cached is not None:
        # `False` is the cached form of "no such token", so a bad token is not
        # re-queried on every request.
        return None if cached is False else cached

    result = _resolve_user_from_token(token_key)
    cache.set(key, result if result is not None else False, _LOOKUP_TTL_SECONDS)
    return result


def _resolve_user_from_token(token_key: str):
    try:
        from rest_framework.authtoken.models import Token
        try:
            t = Token.objects.select_related('user', 'user__role', 'user__center').get(key=token_key)
        except Token.DoesNotExist:
            # The owner app signs in with its own token and accounts (owner_api): its changes are logged to that person.
            from owner_api.models import OwnerToken
            o = OwnerToken.objects.select_related('account', 'account__role').get(key=token_key).account
            return (o.pk, o.name or '', o.email or o.phone or '', str(o.role) if o.role else 'Owner app', None, '')
        u = t.user
        role_str = ''
        try:
            role_str = str(u.role) if u.role else ('Super Admin' if u.is_superuser else '')
        except Exception:
            role_str = 'Super Admin' if u.is_superuser else ''
        c_id   = u.center_id if hasattr(u, 'center_id') else None
        c_name = u.center.center_name if (hasattr(u, 'center') and u.center) else ''
        return (u.pk, getattr(u, 'full_name', '') or '', u.email or '',
                role_str, c_id, c_name)
    except Exception as e:
        # Not an error: the request may carry no token at all, or one that has since been replaced.
        logger.debug(f"Could not resolve a user from the token: {e}")
        return None


def _cached_centre_name(center_id: int) -> str:
    """Branch name, cached briefly. A permanent cache kept a renamed branch's
    old name in every subsequent audit row."""
    from django.core.cache import cache

    if not center_id:
        return ''

    key = f'auditlog:centre:{center_id}'
    cached = cache.get(key)
    if cached is not None:
        return cached

    try:
        from salon_admin.models import Center
        row = Center.all_objects.filter(pk=center_id).values('center_name').first()
        name = row['center_name'] if row else ''
    except Exception:
        return ''

    cache.set(key, name, _LOOKUP_TTL_SECONDS)
    return name


def _get_action(method: str, path: str, status: int = 200) -> str:
    p = path.lower()
    if 'login' in p:
        return 'LOGIN' if (status or 200) < 400 else 'LOGIN_FAILED'
    if 'logout'         in p: return 'LOGOUT'
    if 'refund'         in p: return 'REFUND'
    if 'cancel'         in p: return 'CANCEL'
    if 'close_shift'    in p: return 'CLOSE_SHIFT'
    if 'lock'           in p: return 'LOCK'
    if 'mark_paid'      in p: return 'MARK_PAID'
    if '/pay/'          in p: return 'PAYMENT'
    if 'override'       in p: return 'OVERRIDE'
    if 'carry-over'     in p: return 'CARRY_OVER'
    if 'checkout'       in p: return 'CHECKOUT'
    if 'audit'          in p: return 'AUDIT'
    if 'send_campaign'  in p: return 'CAMPAIGN'
    if method == 'POST':      return 'CREATE'
    if method in ('PUT', 'PATCH'): return 'UPDATE'
    if method == 'DELETE':    return 'DELETE'
    return method


def _get_module_entity(path: str):
    for pattern, module, entity in MODULE_RULES:
        if re.search(pattern, path):
            return module, entity
    return 'SYSTEM', 'Request'


def _extract_entity_id(path: str) -> str:
    for part in reversed(path.rstrip('/').split('/')):
        if part.isdigit():
            return part
    return ''


def _is_sensitive_key(key) -> bool:
    """True if a payload key holds a secret.

    Matches on substring, case-insensitively. The old check was
    `k in SENSITIVE_KEYS` - an exact match on a small set - so `app_password`
    and `app_pin` were not covered and staff/client PINs were written to the
    audit log in cleartext.
    """
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def _redact(value, depth=0):
    """Recursively redact secrets in a decoded request body.

    The old implementation only walked top-level dict keys, so a secret nested
    inside a list or a sub-object passed through untouched.
    """
    if depth > 6:
        return '...'
    if isinstance(value, dict):
        return {
            k: ('***' if _is_sensitive_key(k) else _redact(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(v, depth + 1) for v in value[:50]]
    return value


def _sanitise(body_bytes: bytes) -> dict:
    if not body_bytes:
        return {}
    try:
        data = json.loads(body_bytes)
    except Exception:
        return {}
    redacted = _redact(data)
    if isinstance(redacted, dict):
        return redacted
    # A top-level list still deserves logging, wrapped so the caller gets a dict.
    if isinstance(redacted, list):
        return {'items': redacted}
    return {}


_FAILURE_REASON = {
    400: 'the request was invalid', 401: 'not signed in', 403: 'not allowed',
    404: 'not found', 405: 'not allowed', 409: 'it conflicted with existing data',
    413: 'too large', 422: 'the data was invalid', 429: 'too many attempts, blocked',
}


def _take_device_position(body: dict):
    """Pull the browser-reported position out of a sign-in body.

    Returns (lat, lng, accuracy_metres) or None. The value is the caller's own
    claim, so it is range-checked and rounded, and removed from the body so it
    is not stored twice as raw text.
    """
    raw = body.pop('device_location', None)
    if not isinstance(raw, dict):
        return None
    try:
        lat = round(float(raw.get('lat')), 6)
        lng = round(float(raw.get('lng')), 6)
        acc = raw.get('accuracy')
        acc = round(float(acc), 1) if acc is not None else None
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng, acc


def _failure_reason(status) -> str:
    if not status or status < 400:
        return ''
    if status in _FAILURE_REASON:
        return _FAILURE_REASON[status]
    return 'a server error' if status >= 500 else f'HTTP {status}'


def _human_description(action, entity, entity_id, who, body, centre_name, status=200):
    loc = f' at {centre_name}' if centre_name else ''
    if action == 'LOGIN_FAILED':
        target = body.get('username') or body.get('email') or who
        if status == 429:
            return f'Sign-in blocked for {target}: too many attempts{loc}.'
        return f'Failed sign-in attempt for {target}{loc}.'

    verb = {
        'LOGIN':       'logged in',
        'LOGOUT':      'logged out',
        'CREATE':      f'created a new {entity}',
        'UPDATE':      f'updated {entity}',
        'DELETE':      f'deleted {entity}',
        'CANCEL':      f'cancelled {entity}',
        'REFUND':      f'processed a Refund on {entity}',
        'CLOSE_SHIFT': 'closed a Shift',
        'LOCK':        f'locked {entity}',
        'MARK_PAID':   f'marked {entity} as Paid',
        'PAYMENT':     f'recorded a Payment on {entity}',
        'OVERRIDE':    f'overrode price for {entity}',
        'CARRY_OVER':  'carried over client perks',
        'CHECKOUT':    'performed Stock Checkout',
        'AUDIT':       'performed Stock Audit',
        'CAMPAIGN':    'sent a WhatsApp Campaign',
    }.get(action, f'performed {action} on {entity}')

    name_hints = ['name', 'full_name', 'first_name', 'phone', 'email', 'title', 'code']
    hint = ''
    for h in name_hints:
        v = body.get(h, '')
        if v and str(v) not in ('***', '', 'None'):
            hint = f'"{v}"'
            break
    if not hint and entity_id:
        hint = f'(ID {entity_id})'

    if action in ('LOGIN', 'LOGOUT', 'CAMPAIGN', 'CHECKOUT', 'AUDIT', 'CARRY_OVER'):
        text = f'{who} {verb}{loc}.'
    else:
        text = (f'{who} {verb} {hint}{loc}.' if hint else f'{who} {verb}{loc}.').strip()

    if status and status >= 400:
        text = text.rstrip('.') + f' - it did not go through ({_failure_reason(status)}).'
    return text


# ─── Background log writer ────────────────────────────────────────────────────
def _write_log(token_key, session_user_pk, path, method, body_bytes, ip, ua_string, staff_token=None, client_token=None, status=200):
    try:
        # ── User resolution ───────────────────────────────────────────────────
        user_obj = user_pk = user_name = user_email = user_role = None
        center_id = center_name = ''

        if token_key:
            cached = _cached_user_from_token(token_key)
            if cached:
                user_pk, user_name, user_email, user_role, center_id, center_name = cached
                # Get a fresh DB ref for the FK (never store ORM objects in lru_cache)
                try:
                    from django.contrib.auth import get_user_model
                    user_obj = get_user_model().objects.get(pk=user_pk)
                except Exception:
                    user_obj = None
        elif session_user_pk:
            try:
                from django.contrib.auth import get_user_model
                u = get_user_model().objects.select_related('role', 'center').get(pk=session_user_pk)
                user_obj   = u
                user_pk    = u.pk
                user_name  = getattr(u, 'full_name', '') or ''
                user_email = u.email or ''
                user_role  = str(u.role) if (hasattr(u, 'role') and u.role) else (
                    'Super Admin' if u.is_superuser else '')
                center_id   = u.center_id if hasattr(u, 'center_id') else None
                center_name = getattr(u.center, 'center_name', '') if (hasattr(u, 'center') and u.center) else ''
            except Exception:
                import logging; logging.getLogger(__name__).error('Handled exception', exc_info=True)

        # ── Body & action ─────────────────────────────────────────────────────
        body       = _sanitise(body_bytes)
        action     = _get_action(method, path, status)
        module, entity = _get_module_entity(path)
        entity_id  = _extract_entity_id(path)

        # Centre from body if not already known
        if not center_id:
            bc = body.get('center_id') or body.get('center')
            if bc:
                try:
                    center_id   = int(bc)
                    center_name = _cached_centre_name(center_id)
                except (TypeError, ValueError):
                    pass

        position = _take_device_position(body) if action in ('LOGIN', 'LOGIN_FAILED') else None

        # If it's a login, we can also extract the user email from the body as a fallback
        if action in ('LOGIN', 'LOGIN_FAILED') and not user_email:
            user_email = body.get('username') or body.get('email') or ''

        who   = user_name or user_email or 'Someone not signed in'
        human = _human_description(action, entity, entity_id, who, body, center_name, status)

        # ── Device info ───────────────────────────────────────────────────────
        device_info = _parse_ua(ua_string)

        # ── Geolocation from IP ───────────────────────────────────────────────
        geo = _cached_geo(ip) if ip else {}

        # ── Save to DB ────────────────────────────────────────────────────────
        SystemLog.objects.create(
            user_id=user_pk if user_obj is not None else None,
            user_name=user_name or '',
            user_email=user_email or '',
            user_role=user_role or '',
            user_id_snapshot=user_pk,
            center_id=center_id or None,
            center_name=center_name or '',
            action=action,
            module=module,
            entity_type=entity,
            entity_id=entity_id,
            human_description=human,
            path=path,
            description=(json.dumps(body)[:4000] if body else ''),
            status_code=status,
            ip_address=ip or None,
            device_info=ua_string,
            device_type=device_info.get('device_type', ''),
            browser=device_info.get('browser', ''),
            os_info=device_info.get('os_info', ''),
            geo_city=geo.get('city', ''),
            geo_region=geo.get('region', ''),
            geo_country=geo.get('country', ''),
            geo_country_code=geo.get('country_code', ''),
            geo_lat=position[0] if position else None,
            geo_lng=position[1] if position else None,
            geo_accuracy=position[2] if position else None,
        )
    except Exception as e:
        logger.error(f'audit_log _write_log error: {e}', exc_info=True)


def _client_ip(request) -> str:
    """The caller's address, preferring the socket over a forgeable header.

    Behind nginx the socket is nginx (127.0.0.1) for every request, so the log
    would show one address for everybody. Set AUDIT_LOG_TRUST_FORWARDED_FOR = 1
    on the server (it is in deploy/GO-LIVE.md) and the address nginx recorded is
    used. That is X-Real-IP, or failing that the LAST entry of X-Forwarded-For:
    nginx appends the real peer to whatever the caller sent, so the first entry
    is caller-controlled and the last is not.
    """
    from django.conf import settings

    remote = (request.META.get('REMOTE_ADDR') or '').strip()

    if getattr(settings, 'AUDIT_LOG_TRUST_FORWARDED_FOR', False):
        real = (request.META.get('HTTP_X_REAL_IP') or '').strip()
        if real:
            return real[:45]
        forwarded = (request.META.get('HTTP_X_FORWARDED_FOR') or '').strip()
        if forwarded:
            candidate = forwarded.split(',')[-1].strip()
            if candidate:
                return candidate[:45]

    return remote[:45]


class AuditLogMiddleware(MiddlewareMixin):
    """Zero-latency audit middleware — DB work runs in background thread."""

    def process_request(self, request):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            # Only a small JSON body is worth keeping for the log. Reading a
            # spreadsheet upload (up to 25 MB) into memory for it was waste.
            try:
                kind = (request.META.get('CONTENT_TYPE') or '').lower()
                size = int(request.META.get('CONTENT_LENGTH') or 0)
                if 'json' in kind and size <= 200_000:
                    _ = request.body
            except Exception:
                import logging; logging.getLogger(__name__).error('Handled exception', exc_info=True)

    def process_response(self, request, response):
        method = request.method
        if method not in ('POST', 'PUT', 'PATCH', 'DELETE'):
            return response

        path = getattr(request, 'path', '')
        if any(path.startswith(p) for p in EXCLUDED_PREFIXES):
            return response

        try:
            # Try to get token from Authorization header
            token_key = ''
            auth = request.META.get('HTTP_AUTHORIZATION', '')
            if auth.startswith('Token '):
                token_key = auth[6:].strip()

            # If it's a login, the token is returned in the response
            if not token_key and hasattr(response, 'data') and isinstance(response.data, dict):
                token_key = response.data.get('token', '')

            # Check for Staff and Client app tokens
            staff_token = request.headers.get('X-Staff-Token') or request.META.get('HTTP_X_STAFF_TOKEN', '')
            client_token = request.headers.get('X-Client-Token') or request.META.get('HTTP_X_CLIENT_TOKEN', '')

            # If it's a login response that returned auth_token for apps
            if hasattr(response, 'data') and isinstance(response.data, dict) and 'auth_token' in response.data:
                if 'staff' in path.lower():
                    staff_token = response.data.get('auth_token', '')
                elif 'client' in path.lower():
                    client_token = response.data.get('auth_token', '')

            session_user_pk = None
            if not token_key and not staff_token and not client_token and hasattr(request, 'user') and request.user and request.user.is_authenticated:
                session_user_pk = request.user.pk

            body_bytes = bytes(getattr(request, '_body', None) or b'')

            # X-Forwarded-For is a request header, so it is written by whoever
            # is calling. Trusting it unconditionally meant anyone could choose
            # the IP address recorded against their actions in the audit log -
            # which is most of what an audit log is for. It is now only believed
            # when the deployment says it sits behind a proxy that sets it.
            ip = _client_ip(request)
            ua  = request.META.get('HTTP_USER_AGENT', '')[:600]

            # Fire and forget — response returns IMMEDIATELY, logging happens in background
            _executor.submit(_write_log, token_key, session_user_pk,
                             path, method, body_bytes, ip, ua, staff_token, client_token,
                             response.status_code)

        except Exception as e:
            logger.error(f'AuditLogMiddleware submit error: {e}')

        return response
