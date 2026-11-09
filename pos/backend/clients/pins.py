"""Client PINs and the daily master PIN.

A client has one 4-digit PIN. It is sent to them when they buy something they can spend later (a value card, a
membership, a package, an advance), they see it in the client app, and the till asks for it before that money or
those services are used - like the code of a gift card, so only the right person can claim them.

If a client has forgotten it, the manager asks the admin for the branch's MASTER PIN: a different random 4-digit
number for every branch and every day, seen only by people whose role ticks "Master PINs".

Both are kept sealed (encrypted) so the client app can show a client their own PIN and nobody reading the database can.
Set PIN_ENCRYPTION_KEY in .env on a real server (it falls back to a key derived from SECRET_KEY).

A wrong PIN is counted: after MAX_FAILS misses in LOCK_MINUTES the client (or the branch's master PIN) is locked
for the rest of that window, so a PIN cannot be guessed at the till.
"""
import base64
import datetime
import hashlib
import hmac
import os
import re
import secrets

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core import signing
from django.utils import timezone

from .models import MasterPin, PinAttempt

MAX_FAILS = 5
MASTER_MAX_FAILS = 10
LOCK_MINUTES = 15
TOKEN_SECONDS = 600
TOKEN_SALT = 'client-pin-check'
PIN_RE = re.compile(r'^\d{4}$')


class PinLocked(Exception):
    """Too many wrong tries: wait, or use the master PIN."""


def _fernet():
    raw = getattr(settings, 'PIN_ENCRYPTION_KEY', '') or settings.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest()))


def seal(pin):
    return _fernet().encrypt(str(pin).encode()).decode()


def unseal(text):
    if not text:
        return ''
    try:
        return _fernet().decrypt(text.encode()).decode()
    except (InvalidToken, ValueError):
        return ''


def new_pin():
    return '%04d' % (1000 + secrets.randbelow(9000))


def clean(pin):
    return re.sub(r'\D', '', str(pin or ''))[:4]


# ─── a client's own PIN ─────────────────────────────────────────────────────

def client_pin(client):
    return unseal(client.pin_sealed)


def has_pin(client):
    return bool(client_pin(client))


def set_pin(client, pin=None):
    """Give the client this PIN (a new random one when none is given). Returns it."""
    pin = clean(pin) if pin is not None else new_pin()
    if not PIN_RE.match(pin):
        raise ValueError('A PIN is exactly 4 digits.')
    client.pin_sealed = seal(pin)
    client.pin_set_at = timezone.now()
    client.save(update_fields=['pin_sealed', 'pin_set_at'])
    return pin


def ensure_pin(client):
    """(pin, created): the client's PIN, made now if they had none."""
    existing = client_pin(client)
    if existing:
        return existing, False
    return set_pin(client), True


# ─── counting wrong tries ───────────────────────────────────────────────────

def _fails(kind, **who):
    since = timezone.now() - datetime.timedelta(minutes=LOCK_MINUTES)
    return PinAttempt.objects.filter(kind=kind, ok=False, at__gte=since, **who).count()


def _record(kind, ok, client=None, center=None, user=None, via=''):
    PinAttempt.objects.create(kind=kind, ok=ok, client=client, center=center, via=via[:30],
                              user_id=getattr(user, 'pk', None) if getattr(user, 'is_authenticated', False) else None)


def attempts_left(client):
    return max(MAX_FAILS - _fails('client', client=client), 0)


def verify_client(client, pin, center=None, user=None, via='till'):
    """True if this is the client's PIN. Raises PinLocked after too many wrong tries."""
    if _fails('client', client=client) >= MAX_FAILS:
        raise PinLocked(f'Too many wrong PINs. Wait {LOCK_MINUTES} minutes, or ask for the master PIN.')
    expected = client_pin(client)
    ok = bool(expected) and PIN_RE.match(clean(pin) or '') is not None and hmac.compare_digest(expected, clean(pin))
    _record('client', ok, client=client, center=center, user=user, via=via)
    return ok


# ─── the daily master PIN of a branch ───────────────────────────────────────

def master_pin(center, day=None):
    """Today's master PIN of this branch (made the first time it is asked for)."""
    day = day or datetime.date.today()
    row = MasterPin.objects.filter(center=center, date=day).first()
    if row is None:
        row, _ = MasterPin.objects.get_or_create(center=center, date=day, defaults={'sealed': seal(new_pin())})
    return unseal(row.sealed)


def verify_master(center, pin, user=None, client=None):
    """True if this is the branch's master PIN today. Every use, right or wrong, is written down."""
    if _fails('master', center=center) >= MASTER_MAX_FAILS:
        raise PinLocked('Too many wrong master PINs for this branch. Try again later.')
    expected = master_pin(center)
    ok = PIN_RE.match(clean(pin) or '') is not None and hmac.compare_digest(expected, clean(pin))
    _record('master', ok, client=client, center=center, user=user, via='till')
    return ok


# ─── the pass a good PIN earns ──────────────────────────────────────────────

def issue_token(client, center, user, via):
    """A short-lived pass for this client at this branch: what the till sends with the payment."""
    return signing.dumps({'c': client.pk, 'k': center.pk if center else None, 'u': getattr(user, 'pk', None), 'v': via},
                         salt=TOKEN_SALT)


def token_via(token, client, center):
    """'client' or 'master' when the pass is good for this client here, else None."""
    if not token:
        return None
    try:
        data = signing.loads(str(token), salt=TOKEN_SALT, max_age=TOKEN_SECONDS)
    except signing.BadSignature:
        return None
    if data.get('c') != getattr(client, 'pk', None):
        return None
    if data.get('k') not in (None, getattr(center, 'pk', None)):
        return None
    return data.get('v')


# ─── telling the client ─────────────────────────────────────────────────────

def send_pin(client, reason, key):
    """Message the client their PIN (WhatsApp or e-mail, whichever is set up and allowed). Returns the channels used."""
    pin = client_pin(client)
    if not pin:
        return []
    from notifications.service import send_to_client
    salon = client.center.display_name or client.center.center_name if client.center_id else 'the salon'
    url = app_url('client')
    body = (f'Here is your PIN at {salon}. {reason}\n\nYour PIN: {pin}\n\n' + (f'Open your app: {url}\n\n' if url else '')
            + 'The salon will ask for this PIN before your card, advance or membership is used. Please do not share it with anyone.')
    sent = send_to_client('client.pin', client, subject='Your PIN', body=body, key=key, template_kind='reminder')
    _show_in_terminal(client, pin)
    return sent


def _show_in_terminal(client, pin):
    """While WhatsApp is not linked there is no way to reach the client, so on a development machine (DEBUG, or
    PIN_TO_TERMINAL=1) the PIN is written to the server's terminal. Never on a live server with WhatsApp set up."""
    from notifications import integrations
    if integrations.is_ready('whatsapp'):
        return
    if not (settings.DEBUG or str(os.environ.get('PIN_TO_TERMINAL', '')).lower() in ('1', 'true', 'yes')):
        return
    print(f'[PIN] WhatsApp is not linked: the PIN of {client.full_name} ({client.phone}) is {pin}', flush=True)


def app_url(kind):
    """The address people sign in at ('client' or 'staff'): the business's own setting (Admin > Settings), else, on a
    development machine only, the local one. Empty when it is not known yet, and then no link is put in the message."""
    from pos_backend import app_settings
    value = str(app_settings.get(f'apps.{kind}_url') or '').strip()
    if value:
        return value
    return {'client': 'http://localhost:5200', 'staff': 'http://localhost:5210'}[kind] if settings.DEBUG else ''


def on_create(client):
    """A new client is at the counter: they already have a PIN (made when the client was saved). One welcome goes to
    them (e-mail with the salon's logo, and WhatsApp) with their PIN and the address of the app."""
    if client is None or not client.phone:
        return []
    pin, _ = ensure_pin(client)
    salon = (client.center.display_name or client.center.center_name) if client.center_id else 'our salon'
    url = app_url('client')
    body = (f'Thank you for visiting {salon}. Your client profile is ready, so your visits, cards and memberships are now '
            f'in one place.\n\nMobile number: {client.phone}\nYour PIN: {pin}\n\n'
            + (f'Open your app: {url}\n\n' if url else '')
            + 'The salon will ask for this PIN before your card, advance or membership is used. Please do not share it with anyone.')
    from notifications.service import send_to_client
    from pos_backend import app_settings
    # Switching "E-mail new clients a welcome" off (Admin > Settings) stops the e-mail; WhatsApp is not affected.
    channels = ('whatsapp', 'email') if app_settings.get('clients.welcome_email') else ('whatsapp',)
    sent = send_to_client('client.welcome', client, subject=f'Welcome to {salon}: your PIN and app login', body=body,
                          key=f'pin-new:{client.pk}', template_kind='reminder', channels=channels)
    _show_in_terminal(client, pin)
    return sent


def on_sale(client, what, invoice_id):
    """A client just bought something they can spend later: they now have a PIN, and it is sent to them."""
    if client is None:
        return []
    ensure_pin(client)
    return send_pin(client, f'You need it to use your {what}.', f'pin-sale:{invoice_id}')
