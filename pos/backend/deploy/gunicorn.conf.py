"""
Gunicorn configuration for the POS backend.

Replaces `manage.py runserver`, which is Daphne's DEVELOPMENT server: one
process on one GIL, no worker pool, no request timeout, and nothing to restart
it when it dies.

Uvicorn workers, not sync workers, because `asgi.py` routes ALL http through
Django (not just /ws/) and the websocket chat needs ASGI. The worker count is
deliberately fixed: every view is synchronous ORM code, so under ASGI Django
creates a thread per request and a Django database connection is thread-local.
Bounding workers is what puts a ceiling on MySQL connections.

    gunicorn -c deploy/gunicorn.conf.py pos_backend.asgi:application
"""

import multiprocessing
import os

# ── Where to listen ──────────────────────────────────────────────────────────
# Loopback only: nginx should be the only thing that can reach this. The old
# setup bound 0.0.0.0:4091 straight to the internet.
bind = os.environ.get('GUNICORN_BIND', '127.0.0.1:8000')

# ── Workers ──────────────────────────────────────────────────────────────────
worker_class = 'uvicorn.workers.UvicornWorker'

# (2 x cores) + 1 is the usual starting point. Capped, because each worker can
# hold several MySQL connections and `max_connections` defaults to 151.
_suggested = multiprocessing.cpu_count() * 2 + 1
workers = int(os.environ.get('GUNICORN_WORKERS', min(_suggested, 8)))

# A checkout that has not finished in two minutes is not going to.
timeout = int(os.environ.get('GUNICORN_TIMEOUT', 120))
graceful_timeout = 30

# Recycle workers to bound the effect of any slow leak. The jitter stops every
# worker restarting at the same moment mid-service.
max_requests = 2000
max_requests_jitter = 200

keepalive = 5

# ── Logging ──────────────────────────────────────────────────────────────────
accesslog = os.environ.get('GUNICORN_ACCESS_LOG', '-')
errorlog = os.environ.get('GUNICORN_ERROR_LOG', '-')
loglevel = os.environ.get('GUNICORN_LOG_LEVEL', 'warning')

# Log how long each request took, so a slow endpoint shows up without a
# profiler. %(D)s is microseconds.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sus'

proc_name = 'pos_backend'


# ── Safety net ───────────────────────────────────────────────────────────────
def on_starting(server):
    """Refuse to serve production traffic with a configuration that must not.

    The live box was running with DEBUG=True and the committed SECRET_KEY. Both
    are one line of .env away from happening again, and neither is obvious from
    the outside once the server is up.
    """
    import django

    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pos_backend.settings')
    django.setup()
    from django.conf import settings

    if settings.DEBUG:
        raise RuntimeError(
            'DEBUG is True. Refusing to serve production traffic: it renders '
            'tracebacks with settings and local variables to whoever triggered '
            'the error, and disables secure cookies, HSTS and the CORS '
            'allowlist. Set DJANGO_DEBUG=False in backend/.env.')

    if str(settings.SECRET_KEY).startswith('django-insecure'):
        raise RuntimeError(
            'SECRET_KEY is the value committed to the repository. Session '
            'cookies and the staff/client app tokens are signed with it, so '
            'anyone holding the repo could forge a login for any user. '
            'Generate one with get_random_secret_key() and set '
            'DJANGO_SECRET_KEY.')
