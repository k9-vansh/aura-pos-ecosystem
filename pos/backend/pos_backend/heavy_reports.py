"""Keeping the big reports from starving the tills.

One owner opening the all-centre dashboard holds a web worker thread, and the database, for tens of seconds. In the
324-lakh-bill test, owner + finance opening their dashboards while 40 centres billed dropped the group from 301 to 30
bills a minute (a bill took 26 s instead of 2.7 s). Three small guards:

  * `HeavyReportGate` (middleware): only a few reports (HEAVY_REPORT_SLOTS per web process) are built at the same time;
    the others wait a little (HEAVY_REPORT_WAIT_SECONDS) and are then told "many reports are being built, try again" -
    the threads that remain stay free for billing;
  * while a report runs, the database stops any single SELECT after HEAVY_REPORT_QUERY_SECONDS (MySQL
    MAX_EXECUTION_TIME, set on the connection only for that request), so a runaway report cannot pin it for ever;
  * `api_exception_handler`: a query stopped that way (or any "query interrupted" error) answers with a readable
    503 - "choose a shorter period or fewer centres" - instead of a raw 500 page.

Nothing here changes what any report computes. Settings (environment): HEAVY_REPORT_SLOTS (default 4, 0 = no gate),
HEAVY_REPORT_QUEUE (4: how many may wait for a turn; more are told to retry at once, because a waiting thread is a
thread no till can use), HEAVY_REPORT_WAIT_SECONDS (25), HEAVY_REPORT_QUERY_SECONDS (120, 0 = no limit).
"""

import logging
import re
import threading

from django.conf import settings
from django.db import connection, connections
from django.db.utils import OperationalError
from django.http import JsonResponse
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

log = logging.getLogger(__name__)

# The screens that add up thousands to millions of bill lines or payments (measured slow in the full-size test).
HEAVY_PATHS = re.compile(
    r'^/(?:'
    r'salon_admin/api/(?:dashboard|analytics|usage-report)/'
    r'|finance/api/(?:reports|monthly_sales|register_summary|export)/'
    r'|billing/invoices/summary/'
    r'|staff/api/members/(?:incentive_report|commission_report)/'
    r'|staff/api/reports/'
    r'|marketing/api/promotions/usage_report/'
    r')')

TOO_BIG = ('This report is too big to build in one go. Choose a shorter period or fewer centres and try again.')
BUSY = ('Many big reports are being built right now. Please try again in a moment.')

# MySQL: 3024 = maximum statement execution time exceeded, 1317 = query execution was interrupted.
_INTERRUPTED = {3024, 1317}

_lock = threading.Lock()
_slots = None
_waiting = 0


def _gate():
    """The semaphore, made on first use from the settings (None when the gate is switched off)."""
    global _slots
    with _lock:
        if _slots is None:
            n = int(getattr(settings, 'HEAVY_REPORT_SLOTS', 4))
            _slots = threading.BoundedSemaphore(n) if n > 0 else False
    return _slots or None


def reset_gate():
    """For tests: forget the semaphore so the next request builds it from the current settings."""
    global _slots, _waiting
    with _lock:
        _slots = None
        _waiting = 0


def _take(slots):
    """A turn to build a report, waiting a little; False when the queue is full or the wait ran out.

    The queue is short on purpose: a thread that is only waiting is a thread no till can use.
    """
    global _waiting
    if slots.acquire(blocking=False):
        return True
    with _lock:
        if _waiting >= int(getattr(settings, 'HEAVY_REPORT_QUEUE', 4)):
            return False
        _waiting += 1
    try:
        return slots.acquire(timeout=float(getattr(settings, 'HEAVY_REPORT_WAIT_SECONDS', 25)))
    finally:
        with _lock:
            _waiting -= 1


def is_heavy(path):
    return bool(HEAVY_PATHS.match(path))


def _report_connection():
    """The connection a report reads through: the replica when one is configured (db_router.py), else the default."""
    from pos_backend import db_router
    alias = db_router.replica_alias()
    return connections[alias] if alias else connection


def _set_query_limit(seconds):
    """MySQL only: stop any single SELECT of this connection after `seconds` (0 = no limit)."""
    target = _report_connection()
    if target.vendor != 'mysql':
        return False
    try:
        with target.cursor() as cursor:
            cursor.execute('SET SESSION MAX_EXECUTION_TIME = %s', [int(seconds * 1000)])
        return True
    except Exception:                                    # a limit that cannot be set must not stop the report
        log.warning('could not set the report time limit', exc_info=True)
        return False


class HeavyReportGate:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        slots = _gate()
        if slots is None or request.method != 'GET' or not is_heavy(request.path):
            return self.get_response(request)
        if not _take(slots):
            response = JsonResponse({'detail': BUSY, 'code': 'reports_busy'}, status=503)
            response['Retry-After'] = '15'
            return response
        from pos_backend import db_router
        limit = float(getattr(settings, 'HEAVY_REPORT_QUERY_SECONDS', 120))
        limited = limit > 0 and _set_query_limit(limit)
        db_router.use_replica(True)                     # no-op unless DB_REPLICA_HOST is set
        try:
            return self.get_response(request)
        finally:
            db_router.use_replica(False)
            if limited:
                _set_query_limit(0)
            slots.release()


def interrupted(exc):
    return isinstance(exc, OperationalError) and bool(exc.args) and exc.args[0] in _INTERRUPTED


def api_exception_handler(exc, context):
    response = drf_exception_handler(exc, context)
    if response is None and interrupted(exc):
        return Response({'detail': TOO_BIG, 'code': 'report_too_big'}, status=503)
    return response
