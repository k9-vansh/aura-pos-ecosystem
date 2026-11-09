"""
Django settings for pos_backend project.
Split into dev/prod via DJANGO_ENV environment variable.
"""

from pathlib import Path
import os
import logging.handlers
# pyrefly: ignore [missing-import]
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env if present
load_dotenv(os.path.join(BASE_DIR, '.env'))

# ─── Core ──────────────────────────────────────────────────────────────────────
_INSECURE_KEY = 'django-insecure-9k^bakokugv8=6u^h4&e0br9#aj2yf^oo3#anie*o6v(v_!0g3'

SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', _INSECURE_KEY)

DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

# Guard: refuse to start in production with the insecure fallback key
if not DEBUG and SECRET_KEY == _INSECURE_KEY:
    raise ImproperlyConfigured(
        "DJANGO_SECRET_KEY environment variable must be set to a strong random value "
        "when DEBUG=False. Do not use the default insecure key in production."
    )

ALLOWED_HOSTS = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    '*,38.45.94.56,localhost,127.0.0.1,testserver,.onrender.com,.vercel.app'
).split(',')

# ─── Installed Apps ────────────────────────────────────────────────────────────
INSTALLED_APPS = [
    # The project package itself, so its deployment safety checks are loaded.
    # They run on every `manage.py check`, which means every runserver, migrate
    # and test run - because nobody remembers to run `check --deploy`.
    'pos_backend.apps.PosBackendConfig',
    'daphne',
    'channels',
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third party
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',

    # Local apps
    'accounts',
    'salon_admin',
    'inventory',
    'marketing',
    'staff',
    'appointments',
    'clients',
    'services',
    'billing',
    'finance',
    'audit_logs',
    'notifications',
    'approvals',
    'owner_api',
    'client_api',
    'staff_api',
]

# ─── REST Framework ────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        # ExpiringTokenAuthentication was written but never wired up, so
        # API_TOKEN_MAX_AGE_DAYS did nothing and tokens never expired.
        # CustomAuthToken now reissues the token on every login, so enabling
        # this does not lock users out after the max age.
        'accounts.authentication.ExpiringTokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS': 'pos_backend.pagination.OptionalPagination',
    # A query the database stopped (a report too big for its time limit) answers 503 with a readable message.
    'EXCEPTION_HANDLER': 'pos_backend.heavy_reports.api_exception_handler',
    # How many proxies sit in front of us.
    #
    # Without this, DRF derives the throttle bucket from the WHOLE
    # X-Forwarded-For header - which is caller-supplied. Anyone could change one
    # character of it per attempt and get a fresh rate-limit bucket every time,
    # which makes every throttle here decorative. With NUM_PROXIES=1 DRF reads
    # the address the LAST proxy wrote, which is the one nginx controls.
    #
    # Set to 0 if nothing is in front of Django, and keep it equal to the real
    # number of proxies: too high and the bucket is caller-controlled again, too
    # low and everyone shares one bucket.
    'NUM_PROXIES': int(os.environ.get('DRF_NUM_PROXIES', 1)),
    'DEFAULT_THROTTLE_CLASSES': [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'anon': '200/hour',       # bumped from 100/day — more realistic for walk-in usage
        'user': '10000/hour',     # 10K/hour per user (handles staff with many API calls)
        'login': '5/minute',
        # Staff mobile app PIN login. This endpoint was only covered by the
        # default anon rate, which is generous against a numeric PIN.
    },
    # Return proper 401 instead of 403 for unauthenticated requests
    'UNAUTHENTICATED_USER': None,
}

# ─── Cache (Redis for production, in-memory fallback for dev) ──────────────────
# Install for production: pip install django-redis redis
_redis_url = os.environ.get('REDIS_URL', '')

if not DEBUG and not _redis_url and os.environ.get('DEMO_MODE') != '1':
    import logging
    logging.warning("REDIS_URL not set: using in-memory cache fallback for demo mode.")

if _redis_url:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': _redis_url,
            'OPTIONS': {
                'CLIENT_CLASS': 'django_redis.client.DefaultClient',
                'SOCKET_CONNECT_TIMEOUT': 5,
                'SOCKET_TIMEOUT': 5,
                'RETRY_ON_TIMEOUT': True,
                'MAX_CONNECTIONS': 1000,
            },
        }
    }
    # Use Redis for DRF throttling so counters persist across Gunicorn workers/restarts
    REST_FRAMEWORK['DEFAULT_THROTTLE_CLASSES'] = [
        'rest_framework.throttling.AnonRateThrottle',
        'rest_framework.throttling.UserRateThrottle',
    ]
else:
    # Dev fallback: in-memory cache (NOT shared across workers)
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'pos-backend-cache',
        }
    }

# ─── Middleware ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',      # must be first
    'django.middleware.security.SecurityMiddleware',
    'django.middleware.gzip.GZipMiddleware',       # lists of products/clients shrink 5-10x on the wire
    'whitenoise.middleware.WhiteNoiseMiddleware',  # serves static files efficiently
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'audit_logs.middleware.AuditLogMiddleware',
    'pos_backend.scoping.CenterSelectionMiddleware',
    # Only a few big reports at a time, so they cannot starve the tills (pos_backend/heavy_reports.py).
    'pos_backend.heavy_reports.HeavyReportGate',
]

# Big reports: how many are built at once per web process, how long one waits for a turn, and how long the database
# lets any single query of a report run (0 = no limit). See pos_backend/heavy_reports.py.
HEAVY_REPORT_SLOTS = int(os.environ.get('HEAVY_REPORT_SLOTS', 4))
HEAVY_REPORT_QUEUE = int(os.environ.get('HEAVY_REPORT_QUEUE', 4))
HEAVY_REPORT_WAIT_SECONDS = float(os.environ.get('HEAVY_REPORT_WAIT_SECONDS', 25))
HEAVY_REPORT_QUERY_SECONDS = float(os.environ.get('HEAVY_REPORT_QUERY_SECONDS', 120))

TEST_RUNNER = 'pos_backend.test_runner.PosTestRunner'

# ─── CORS ──────────────────────────────────────────────────────────────────────
# In production: lock down to explicit origins via CORS_ALLOWED_ORIGINS env var.
# In development: allow all for convenience.
_cors_origins_env = os.environ.get('CORS_ALLOWED_ORIGINS', '')
# While the app is in testing every origin is allowed (owner decision, 19 Oct
# 2026). To lock it down for a live launch, set CORS_LOCKDOWN=1 and list the real
# site address(es) in CORS_ALLOWED_ORIGINS; nothing else changes.
_cors_listed = [o.strip() for o in _cors_origins_env.split(',') if o.strip()]
CORS_ALLOW_ALL_ORIGINS = os.environ.get('CORS_LOCKDOWN', '0') != '1'
CORS_ALLOWED_ORIGINS = _cors_listed

CORS_ALLOW_METHODS = ['DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT']
CORS_ALLOW_HEADERS = [
    'accept', 'accept-encoding', 'authorization', 'content-type',
    'dnt', 'origin', 'user-agent', 'x-csrftoken', 'x-requested-with',
    'cache-control', 'pragma', 'expires', 'x-staff-token', 'x-client-token',
    'x-background-request', 'idempotency-key',
]

CSRF_TRUSTED_ORIGINS = os.environ.get(
    'CSRF_TRUSTED_ORIGINS',
    'http://38.45.94.56:4091,http://38.45.94.56:8091,http://38.45.94.56:3093,http://38.45.94.56:4093,http://localhost:4200,http://localhost:8000'
).split(',')

# ─── Security Headers (only enforce in production) ────────────────────────────
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = 'DENY'
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

API_TOKEN_MAX_AGE_DAYS = int(os.environ.get('API_TOKEN_MAX_AGE_DAYS', 7))

# ─── URL / Templates / WSGI ───────────────────────────────────────────────────
ROOT_URLCONF = 'pos_backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'pos_backend.wsgi.application'
ASGI_APPLICATION = 'pos_backend.asgi.application'

# InMemoryChannelLayer only works in a single process: with more than one
# worker, group_send does not cross processes and chat messages silently fail
# to arrive for some users. Use Redis whenever it is configured.
if _redis_url:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {'hosts': [_redis_url]},
        },
    }
else:
    if not DEBUG:
        import warnings
        warnings.warn(
            'REDIS_URL is not set. Chat is using the in-memory channel layer, '
            'which only works with a single worker process, and DRF throttling '
            'and the checkout idempotency guard are per-process.',
            RuntimeWarning,
        )
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels.layers.InMemoryChannelLayer'
        },
    }

# ─── Database ─────────────────────────────────────────────────────────────────
# The password must come from the environment. It used to have a hardcoded
# fallback that was committed to git; a missing env var silently fell back to
# it rather than failing, so the credential could never actually be retired.
_DB_PASSWORD = os.environ.get('DB_PASSWORD')
if _DB_PASSWORD is None:
    if DEBUG or os.environ.get('DB_ENGINE', 'sqlite').strip().lower() == 'sqlite':
        _DB_PASSWORD = ''
    else:
        raise ImproperlyConfigured(
            'DB_PASSWORD environment variable must be set when DEBUG=False.'
        )

# DB_ENGINE selects the database. SQLite stays the default so an unchanged
# .env behaves exactly as before; set DB_ENGINE=mysql (plus DB_NAME, DB_USER,
# DB_PASSWORD, DB_HOST, DB_PORT) to run on MySQL 8.
DB_ENGINE = os.environ.get('DB_ENGINE', 'sqlite').strip().lower()

if DB_ENGINE == 'mysql':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.mysql',
            'NAME': os.environ.get('DB_NAME', 'pos'),
            'USER': os.environ.get('DB_USER', 'pos'),
            'PASSWORD': _DB_PASSWORD,
            'HOST': os.environ.get('DB_HOST', '127.0.0.1'),
            'PORT': os.environ.get('DB_PORT', '3306'),
            # Persistent connections; 0 disables them.
            'CONN_MAX_AGE': int(os.environ.get('DB_CONN_MAX_AGE', 60)),
            'CONN_HEALTH_CHECKS': True,
            'OPTIONS': {
                'charset': 'utf8mb4',
                # Strict mode: MySQL rejects bad data instead of silently
                # truncating it (a truncated amount is a wrong bill).
                'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            'TEST': {
                'CHARSET': 'utf8mb4',
                'COLLATION': 'utf8mb4_0900_ai_ci',
            },
        }
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': BASE_DIR / 'db.sqlite3',
            'OPTIONS': {
                'timeout': 20,
                # SQLite only: a transaction takes the write lock when it starts and waits its
                # turn (up to `timeout`). Without this, a transaction that has read and then
                # wants to write fails at once with "database is locked" whenever the audit
                # writer or a job worker writes in between (a 500 on Receive goods or Stock
                # count). MySQL, the live database, is not affected.
                'transaction_mode': 'IMMEDIATE',
            }
        }
    }

# A replica for the big reports (pos_backend/db_router.py): set DB_REPLICA_HOST to a MySQL server that replicates the
# primary, and the all-centre dashboards and reports read from it instead of the server the tills write to.
if DB_ENGINE == 'mysql' and os.environ.get('DB_REPLICA_HOST'):
    DATABASES['replica'] = {
        **DATABASES['default'],
        'HOST': os.environ['DB_REPLICA_HOST'],
        'PORT': os.environ.get('DB_REPLICA_PORT', DATABASES['default']['PORT']),
        'NAME': os.environ.get('DB_REPLICA_NAME', DATABASES['default']['NAME']),
        'USER': os.environ.get('DB_REPLICA_USER', DATABASES['default']['USER']),
        'PASSWORD': os.environ.get('DB_REPLICA_PASSWORD', DATABASES['default']['PASSWORD']),
        'TEST': {'MIRROR': 'default'},
    }
    DATABASE_ROUTERS = ['pos_backend.db_router.ReportsReadFromReplica']

# ─── Password Validation ──────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ─── Internationalisation ─────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Kolkata'    # IST — important for Indian salon timestamps
USE_I18N = True
USE_TZ = False

# ─── Static & Media Files ─────────────────────────────────────────────────────
STATIC_URL = '/static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')   # collectstatic output dir

# FIXED: STATICFILES_STORAGE was deprecated in Django 4.2 — use STORAGES dict
STORAGES = {
    'staticfiles': {
        'BACKEND': 'whitenoise.storage.CompressedManifestStaticFilesStorage',
    },
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
}

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# ─── Logging ──────────────────────────────────────────────────────────────────
# FIXED: Use RotatingFileHandler so django.log never grows unbounded (was 517 KB already)
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {thread:d} {message}',
            'style': '{',
        },
        'simple': {
            'format': '{levelname} {asctime} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'level': 'WARNING',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(BASE_DIR, 'django.log'),
            'maxBytes': 5 * 1024 * 1024,  # 5 MB per file
            'backupCount': 5,              # keep up to 5 rotated files
            'formatter': 'verbose',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'simple',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'WARNING',
    },
    'loggers': {
        'asyncio': {
            'handlers': ['console', 'file'],
            'level': 'CRITICAL',
            'propagate': False,
        },
        'django.server': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'daphne': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'daphne.server': {
            'handlers': ['console', 'file'],
            'level': 'ERROR',
            'propagate': False,
        },
        'django': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
        'billing': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        # Spreadsheet imports. These modules previously had no logging at all,
        # so every failed import returned 400 to the operator and recorded
        # nothing on the server - which is why the repeated 400s in django.log
        # carry no reason.
        'bulk_upload': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        # SAP B1 Service Layer integration — log at INFO so successful syncs
        # and retry-queue events appear in the log without needing DEBUG.
        'pos_backend.sap_utils': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
        'pos_backend.scheduler': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# ─── Misc ─────────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'accounts.CustomUser'

# ─── Business policy ─────────────────────────────────────────────────────────
# Line discounts at or above this percentage are written to the bill change log
# so they surface in the manager-discount audit. There is deliberately no hard
# cap here: the ceiling is a business decision, not a correctness one. Set this
# to whatever your approval policy is.
DISCOUNT_AUDIT_PERCENT = int(os.environ.get('DISCOUNT_AUDIT_PERCENT', 50))

# Write audit log rows on a background thread. Set to False to write inline
# (required under the test suite, useful when debugging a missing log entry).
# Look up city/country for each caller IP via freeipapi.com. OFF because it
# sends staff and customer IP addresses to a third party on every write, and
# its 1 second timeout stalls the audit writers on a counter with no internet.
AUDIT_LOG_GEO_LOOKUP = os.environ.get('AUDIT_LOG_GEO_LOOKUP', '') == '1'

# Believe X-Forwarded-For when recording the caller's IP. Turn this ON only
# behind a reverse proxy that overwrites the header - it is caller-supplied,
# so trusting it lets anyone choose the address written to the audit log.
AUDIT_LOG_TRUST_FORWARDED_FOR = (
    os.environ.get('AUDIT_LOG_TRUST_FORWARDED_FOR', '') == '1')

AUDIT_LOG_ASYNC = os.environ.get('AUDIT_LOG_ASYNC', 'True') == 'True'

# Serve uploaded media through Django. Off in production: chat images and staff
# photographs were publicly retrievable by anyone who could guess the path,
# because the media route was added outside the DEBUG guard. In production,
# serve /media/ from nginx behind auth, or move it to object storage.
SERVE_MEDIA_FILES = os.environ.get('SERVE_MEDIA_FILES', 'True' if DEBUG else 'False') == 'True'

# ─── Email Configuration ──────────────────────────────────────────────────────
EMAIL_BACKEND = os.environ.get('EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend')
EMAIL_HOST = os.environ.get('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'True').lower() == 'true'
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.environ.get('DEFAULT_FROM_EMAIL', 'Chilli Potato <noreply@chillipotato.com>')

# The address people open the POS at; approval links in e-mail / WhatsApp point here.
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'http://localhost:4200')
# The name printed at the top of every e-mail the POS sends.
BRAND_NAME = os.environ.get('BRAND_NAME', 'Aura Salon & Spa')

# ─── Business policy switches ────────────────────────────────────────────
#
# These two were never decisions anyone made: the behaviour simply fell out
# of how the code was written. They are declared here so the choice is
# visible and reversible. Both default to what the system already did, so
# naming them changes nothing until you change them.

# Are customer advance and cashback balances shared across branches?
#   'global' - one balance per customer, spendable at any branch (current
#              behaviour). One branch then gives away service that another
#              was paid for, so the branches have to settle up. Every
#              ledger row records its center, so that report is possible.
#   'center' - a balance per customer per branch. Cleaner accounting, but
#              brief the counter staff first: a customer who topped up at
#              one branch will be told they have nothing at another.
WALLET_SCOPE = os.environ.get('WALLET_SCOPE', 'global')

# GST rate applied when a prepaid PACKAGE is sold.
#
# Packages carry no tax anywhere today: the sale is hardcoded to 0% and a
# redemption line is zero-value and zero-tax, so GST is never charged on a
# package at all. Memberships and value cards are different - they have
# pre_tax_price and post_tax_price, and the gap between them is taken as
# their rate - but Package has only a single price field, so there was no
# way to express a rate for it.
#
# Whether a prepaid package should carry GST at the point of sale is a
# question for your accountant, not for this code. 0 keeps today's
# behaviour; set it to 18 (or whatever they advise) to tax package sales.
PACKAGE_GST_PERCENT = os.environ.get('PACKAGE_GST_PERCENT', '0')

# Does the stylist who performs a service redeemed from a package earn
# commission on it?
#
# A redemption line is worth Rs 0 (the customer paid when they bought the
# package), so today the performer earns nothing and sees a Rs 0 row against
# real work. The package sale already paid commission to whoever sold it -
# but that is often a different person.
#
# False keeps today's behaviour. True pays the performer on the service's
# catalog value, which means a package can pay commission twice: once on the
# sale and once per redemption. Price your packages accordingly.
PACKAGE_REDEMPTION_EARNS_COMMISSION = (
    os.environ.get('PACKAGE_REDEMPTION_EARNS_COMMISSION', '') == '1')

# ─── Production Security Hardening ─────────────────────────────────────────────
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000  # 1 year
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True

# ─── SAP Business One / HANA ─────────────────────────────────────────────────
# Connection details come from backend/.env. They used to be written into the
# source code (settings.py, sap_utils.py and accounts/views.py), so anyone with
# the repository had the SAP and HANA passwords.
#
# SAP_ENABLED=0 is the kill switch: nothing is sent to SAP or HANA, and every
# posting waits in the outbox (Admin > SAP Sync) until it is switched back
# on. SAP is also treated as off when the connection details are missing, so a
# server deployed without them queues its postings rather than failing them.
# SAP_SCHEDULER=0 stops this process from running the background worker (set it
# on web servers when the worker runs as its own service:
# `manage.py run_workers`, deploy/pos-worker.service).
def _env_flag(name, default):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


SAP_SERVICE_LAYER_URL = os.environ.get('SAP_SERVICE_LAYER_URL', '').strip().rstrip('/')
SAP_COMPANY_DB = os.environ.get('SAP_COMPANY_DB', '')
SAP_USERNAME = os.environ.get('SAP_USERNAME', '')
SAP_PASSWORD = os.environ.get('SAP_PASSWORD', '')
SAP_VERIFY_TLS = _env_flag('SAP_VERIFY_TLS', False)
HANA_ADDRESS = os.environ.get('HANA_ADDRESS', '')
HANA_PORT = int(os.environ.get('HANA_PORT') or 30015)
HANA_USER = os.environ.get('HANA_USER', '')
HANA_PASSWORD = os.environ.get('HANA_PASSWORD', '')
HANA_SCHEMA = os.environ.get('HANA_SCHEMA', '')

SAP_CONFIGURED = bool(SAP_SERVICE_LAYER_URL and SAP_COMPANY_DB and SAP_USERNAME)
SAP_ENABLED = _env_flag('SAP_ENABLED', True) and SAP_CONFIGURED
SAP_SCHEDULER = _env_flag('SAP_SCHEDULER', True)
