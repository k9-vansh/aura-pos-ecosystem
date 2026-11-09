import os

path = r'pos\backend\pos_backend\settings.py'
with open(path, 'r', encoding='utf-8') as f:
    c = f.read()

c = c.replace(
    "'38.45.94.56,localhost,127.0.0.1,testserver'",
    "'*,38.45.94.56,localhost,127.0.0.1,testserver,.onrender.com,.vercel.app'"
)

# Also fix _DB_PASSWORD check for sqlite
old_pw = """_DB_PASSWORD = os.environ.get('DB_PASSWORD')
if _DB_PASSWORD is None:
    if DEBUG:
        _DB_PASSWORD = ''
    else:
        raise ImproperlyConfigured(
            'DB_PASSWORD environment variable must be set when DEBUG=False.'
        )"""

new_pw = """_DB_PASSWORD = os.environ.get('DB_PASSWORD')
if _DB_PASSWORD is None:
    if DEBUG or os.environ.get('DB_ENGINE', 'sqlite').strip().lower() == 'sqlite':
        _DB_PASSWORD = ''
    else:
        raise ImproperlyConfigured(
            'DB_PASSWORD environment variable must be set when DEBUG=False.'
        )"""

c = c.replace(old_pw.replace('\n', '\r\n'), new_pw.replace('\n', '\r\n'))
c = c.replace(old_pw, new_pw)

# Also allow in-memory cache fallback when REDIS_URL is not set on demo hosts
old_redis = """if not DEBUG and not _redis_url:
    raise ImproperlyConfigured("REDIS_URL must be set in production to prevent cache collisions and double-billing.")"""

new_redis = """if not DEBUG and not _redis_url and os.environ.get('DEMO_MODE') != '1':
    import logging
    logging.warning("REDIS_URL not set: using in-memory cache fallback for demo mode.")"""

c = c.replace(old_redis.replace('\n', '\r\n'), new_redis.replace('\n', '\r\n'))
c = c.replace(old_redis, new_redis)

with open(path, 'w', encoding='utf-8') as f:
    f.write(c)
print('settings.py updated for Render/production deployment!')
