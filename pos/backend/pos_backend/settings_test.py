"""
Test settings: in-memory SQLite, no external services.

Lets the invariant test-suite run on any machine and in CI without a MySQL or
Redis instance. Everything else is inherited from the real settings so the test
run exercises the same middleware, apps and DRF configuration.
"""

import os

os.environ.setdefault('DJANGO_SECRET_KEY', 'test-only-key-not-used-in-production')
os.environ.setdefault('DJANGO_DEBUG', 'True')

# A test run must never reach SAP or HANA, whatever backend/.env holds.
# Set before settings load .env (load_dotenv does not override these).
for _key, _value in {
    'SAP_ENABLED': '0',
    'SAP_SCHEDULER': '0',
    'SAP_SERVICE_LAYER_URL': 'http://127.0.0.1:9/b1s/v1',
    'SAP_COMPANY_DB': 'TEST',
    'SAP_USERNAME': 'test',
    'SAP_PASSWORD': '',
    'HANA_ADDRESS': '127.0.0.1',
    'HANA_PORT': '9',
    'HANA_PASSWORD': '',
}.items():
    os.environ[_key] = _value

from pos_backend.settings import *  # noqa: F401,F403

SAP_ENABLED = False
SAP_SCHEDULER = False

# Nor may it call any outside service: the audit log's IP geolocation is an
# HTTP request to a third party. Tests that need it turn it on explicitly.
AUDIT_LOG_GEO_LOOKUP = False

if os.environ.get('TEST_DB_ENGINE', '').strip().lower() == 'mysql':
    # The same suite on MySQL 8: row locks (select_for_update) and constraints
    # only behave as in production on a real server - on SQLite every
    # concurrency test passes and proves nothing. Needs DB_ENGINE=mysql and the
    # DB_* keys; Django creates and drops the `test_pos` database.
    if DATABASES['default']['ENGINE'] != 'django.db.backends.mysql':  # noqa: F405
        raise RuntimeError('TEST_DB_ENGINE=mysql needs DB_ENGINE=mysql and the DB_* settings.')
    DATABASES['default']['TEST'] = {  # noqa: F405
        'NAME': 'test_pos', 'CHARSET': 'utf8mb4', 'COLLATION': 'utf8mb4_0900_ai_ci',
    }
    DATABASES['default']['CONN_MAX_AGE'] = 0  # noqa: F405
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }

# Fast, deterministic password hashing for tests.
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

# Never send real mail from a test run.
EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'

# Throttling off: tests exercise business logic, not rate limits.
REST_FRAMEWORK = {**REST_FRAMEWORK, 'DEFAULT_THROTTLE_CLASSES': []}  # noqa: F405

CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'pos-test-cache',
    }
}

CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}

# The Usage Report keeps slow answers for a couple of minutes; a test must always see its own data.
USAGE_REPORT_CACHE_SECONDS = 0

# Plain storage: the manifest static storage requires collectstatic to have run.
STORAGES = {
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
}

# Audit log rows are written inline. A background thread would open its own
# database connection and deadlock against the test transaction.
AUDIT_LOG_ASYNC = False

LOGGING_CONFIG = None  # keep test output readable

# The deployment safety checks describe a live server, not a test run: the test
# settings deliberately use LocMemCache and a throwaway key, so they would fire
# on every single test invocation and train everyone to ignore them.
# `pos_backend/tests_deployment_checks.py` asserts they fire when they should.
SILENCED_SYSTEM_CHECKS = [
    'deployment.W001', 'deployment.W002', 'deployment.W003',
    'deployment.W004', 'deployment.W005', 'deployment.W006',
    'deployment.W007', 'deployment.W008',
    'deployment.W009', 'deployment.W010',  # SAP is off on purpose in tests
]
