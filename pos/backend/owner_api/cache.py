"""A short memory for the heavy screens, so a phone refreshed ten times does not ask the database ten times.

Today and the current month are remembered for seconds, days and months that are over for minutes. The key holds the
salons the answer is about, so one person's answer is never given to someone who sees other salons."""
import hashlib
import json

from django.conf import settings
from django.core.cache import cache


def remember(kind, cids, params, seconds, build):
    if not getattr(settings, 'OWNER_CACHE', True) or seconds <= 0:
        return build()
    raw = json.dumps([sorted(cids), params], sort_keys=True, default=str)
    key = f'owner:{kind}:{hashlib.md5(raw.encode()).hexdigest()}'
    hit = cache.get(key)
    if hit is not None:
        return hit
    value = build()
    cache.set(key, value, seconds)
    return value
