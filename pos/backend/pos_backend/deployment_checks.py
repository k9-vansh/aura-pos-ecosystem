"""
Startup checks for the things that make a deployment unsafe.

These exist because of what was found on the live box: `backend/.env` contained
exactly one line, `DJANGO_DEBUG=True`, and every safety guard in settings.py is
written as `if not DEBUG:`. So that one line switched off, in a single stroke:

  * the refusal to boot with the committed SECRET_KEY,
  * the refusal to boot without DB_PASSWORD,
  * secure cookies, the SSL redirect and HSTS,
  * the CORS allowlist (CORS_ALLOW_ALL_ORIGINS became True).

The SECRET_KEY is in git history. Django signs session cookies, password-reset
tokens and the staff/client app tokens with it, so anyone holding the repository
can mint a valid session for any user. That is the finding, and it is not
theoretical: the running process was verified as
`Starting ASGI/Daphne version 4.2.3 development server`.

`manage.py check --deploy` already covers some of this, but nobody runs it. These
run on EVERY `manage.py check`, which means every `runserver`, every `migrate`
and every test run.

They are warnings, not errors, and deliberately so: making them fatal would stop
the live system from restarting, which is a worse failure than the one being
reported. `python manage.py check --fail-level WARNING` turns them fatal for CI.
"""

from django.conf import settings
from django.core.checks import Warning as CheckWarning, register

# The value committed to settings.py as a fallback. Anything starting with this
# prefix is public knowledge.
INSECURE_KEY_PREFIX = 'django-insecure'


@register('deployment')
def check_deployment_safety(app_configs, **kwargs):
    """Flag settings that are unsafe to face the internet with."""
    issues = []
    debug = getattr(settings, 'DEBUG', False)

    # ── The signing key ───────────────────────────────────────────────────
    secret = getattr(settings, 'SECRET_KEY', '') or ''
    if secret.startswith(INSECURE_KEY_PREFIX):
        issues.append(CheckWarning(
            'SECRET_KEY is the insecure value committed to the repository.',
            hint=('Django signs session cookies and the staff/client app tokens '
                  'with this key, and it is in git history - anyone with the '
                  'repository can forge a session for any user, including an '
                  'owner. Generate one and put it in backend/.env:\n'
                  '    python -c "from django.core.management.utils import '
                  'get_random_secret_key; print(get_random_secret_key())"\n'
                  'Then set DJANGO_SECRET_KEY=<that value>. Everyone is signed '
                  'out when it changes, which is the point.'),
            id='deployment.W001',
        ))

    # ── DEBUG ─────────────────────────────────────────────────────────────
    if debug:
        issues.append(CheckWarning(
            'DEBUG is True. Every other safety guard is switched off with it.',
            hint=('backend/.env sets DJANGO_DEBUG=True. Because the guards in '
                  'settings.py are written `if not DEBUG:`, that one line also '
                  'disables secure cookies, the SSL redirect, HSTS and the CORS '
                  'allowlist, and it makes Django render a full traceback - '
                  'settings and local variables included - to whoever triggered '
                  'the error. It also switches the database to a debug cursor '
                  'that retains up to 9,000 SQL strings per connection.\n'
                  'Set DJANGO_DEBUG=False. The app will then refuse to start '
                  'until SECRET_KEY and DB_PASSWORD are set: that refusal is the '
                  'guard working, not a bug.'),
            id='deployment.W002',
        ))

    # ── CORS ──────────────────────────────────────────────────────────────
    if getattr(settings, 'CORS_ALLOW_ALL_ORIGINS', False):
        issues.append(CheckWarning(
            'CORS_ALLOW_ALL_ORIGINS is True: any website may call this API.',
            hint=('Any page a signed-in member of staff visits can read from and '
                  'write to this API as them. Set CORS_ALLOWED_ORIGINS to your '
                  'own front-end origins.'),
            id='deployment.W003',
        ))

    # ── Transport ─────────────────────────────────────────────────────────
    origins = list(getattr(settings, 'CORS_ALLOWED_ORIGINS', []) or [])
    plain_http = [o for o in origins if str(o).startswith('http://')
                  and 'localhost' not in str(o) and '127.0.0.1' not in str(o)]
    if plain_http:
        issues.append(CheckWarning(
            f'{len(plain_http)} allowed origin(s) use plain http, so there is no TLS.',
            hint=(f'For example {plain_http[0]}. Staff passwords, customer phone '
                  f'numbers and auth tokens cross the network in the clear, and '
                  f'anyone on the same network can read or change them. '
                  f'Terminate TLS at nginx and move these to https://.'),
            id='deployment.W004',
        ))

    if not debug and not getattr(settings, 'SESSION_COOKIE_SECURE', False):
        issues.append(CheckWarning(
            'SESSION_COOKIE_SECURE is False, so the session cookie is sent over http.',
            id='deployment.W005',
        ))

    # ── A shared cache is required once there is more than one worker ─────
    #
    # LocMemCache is per PROCESS. Anything using it for coordination silently
    # stops coordinating the moment a second worker exists: the checkout
    # duplicate-submit lock stops catching duplicates, and every throttle allows
    # N times its configured rate with N workers.
    caches = getattr(settings, 'CACHES', {}) or {}
    backend = str(caches.get('default', {}).get('BACKEND', ''))
    if 'locmem' in backend.lower():
        issues.append(CheckWarning(
            'The cache is per-process (LocMemCache), which breaks anything that '
            'coordinates between requests.',
            hint=('The checkout duplicate-submit guard and every rate limit are '
                  'stored here. With more than one worker process each has its '
                  'own copy, so a double-clicked bill can be accepted twice - two '
                  'invoices, two GST serial numbers, stock deducted twice and the '
                  'wallet debited twice - and the login throttle allows N times '
                  'its rate with N workers. LocMemCache also culls a third of its '
                  'entries once it holds 300, so the guard can be evicted even '
                  'with a single worker.\n'
                  'Set REDIS_URL and let settings.py switch to the Redis cache '
                  'before running more than one worker.'),
            id='deployment.W006',
        ))

    return issues


@register('deployment')
def check_serving_stack(app_configs, **kwargs):
    """Flag serving arrangements that will not survive real traffic."""
    issues = []

    if getattr(settings, 'SERVE_MEDIA_FILES', False) and not getattr(settings, 'DEBUG', False):
        issues.append(CheckWarning(
            'Uploaded media is served by Django rather than by the web server.',
            hint=('Every customer photo and chat image occupies a worker thread '
                  'for the length of the download. Serve MEDIA_ROOT from nginx.'),
            id='deployment.W007',
        ))

    # CONN_HEALTH_CHECKS is a per-database option (DATABASES['default']), not a
    # top-level setting; reading the top level always reported it as off.
    default_db = getattr(settings, 'DATABASES', {}).get('default', {})
    conn_max_age = default_db.get('CONN_MAX_AGE', 0)
    if conn_max_age and not default_db.get('CONN_HEALTH_CHECKS', False):
        issues.append(CheckWarning(
            f'CONN_MAX_AGE is {conn_max_age} but CONN_HEALTH_CHECKS is off.',
            hint=('A persistent connection that the database has already closed '
                  'is handed to a request and fails it. Set '
                  'CONN_HEALTH_CHECKS=True, or CONN_MAX_AGE=0 if you are not '
                  'pooling.'),
            id='deployment.W008',
        ))

    return issues


@register('deployment')
def check_sap_configuration(app_configs, **kwargs):
    """Say so when SAP postings are being queued instead of sent."""
    issues = []
    if not getattr(settings, 'SAP_CONFIGURED', True):
        issues.append(CheckWarning(
            'SAP is not configured: SAP_SERVICE_LAYER_URL, SAP_COMPANY_DB or SAP_USERNAME is missing.',
            hint=('The SAP and HANA connection details now come from backend/.env '
                  '(they used to be written into the code). Until they are set, '
                  'nothing is sent to SAP: every invoice, payment and stock '
                  'document is put on the retry queue and posted once they are. '
                  'Copy the SAP_* and HANA_* lines from .env.example.'),
            id='deployment.W009',
        ))
    elif not getattr(settings, 'SAP_ENABLED', True):
        issues.append(CheckWarning(
            'SAP_ENABLED is off: SAP postings are being queued, not sent.',
            hint='Set SAP_ENABLED=1 in backend/.env and restart to post the queue.',
            id='deployment.W010',
        ))
    elif not getattr(settings, 'SAP_VERIFY_TLS', False):
        issues.append(CheckWarning(
            'SAP_VERIFY_TLS is off: the SAP server\'s certificate is not checked.',
            hint=('Anyone between this server and SAP could read the SAP password and '
                  'every posting. Give the Service Layer a certificate this server trusts '
                  '(or add the company CA), then set SAP_VERIFY_TLS=1 in backend/.env.'),
            id='deployment.W011',
        ))
    return issues
