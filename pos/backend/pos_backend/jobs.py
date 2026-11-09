"""Background jobs on the database: an outbox plus worker processes.

Why the database and not a separate broker: a job must exist if and only if the
business change that caused it committed. Writing the job row in the same
transaction as the bill or stock move gives exactly that, with nothing extra to
run or to lose. Worker processes (`manage.py run_workers`) do the slow work - SAP,
e-mail, WhatsApp, scheduled tasks - so a till never waits on any of it.

Registering work::

    from pos_backend.jobs import job, periodic, enqueue

    @job('sap.invoice')
    def post_invoice(payload, job_row):
        ...                      # raise to fail; RetryLater(seconds) to postpone

    @periodic('billing.expire_drafts', every=300)
    def expire_drafts():
        ...

    enqueue('sap.invoice', {'invoice_id': 42}, dedupe_key='sap:invoice:42')

Handlers live in a `background_jobs.py` module in any app; they are discovered
automatically. Claiming is safe with any number of workers: MySQL uses
SELECT ... FOR UPDATE SKIP LOCKED, SQLite a conditional UPDATE.
"""

import logging
import os
import socket
import threading
from datetime import timedelta

from django.db import IntegrityError, close_old_connections, connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

LEASE_SECONDS = 300          # a claimed job is hidden from other workers this long
BATCH = 20
MAX_BACKOFF_SECONDS = 3600

_REGISTRY = {}               # kind -> handler(payload, job_row)
_PERIODIC = {}               # name -> (every_seconds, fn)
_loaded = False
_load_lock = threading.Lock()


class RetryLater(Exception):
    """Raise from a handler to run the job again after `seconds` without
    counting it as a failure (e.g. SAP is in maintenance)."""

    def __init__(self, seconds=60, reason=''):
        super().__init__(reason or f'retry in {seconds}s')
        self.seconds = int(seconds)


# ─── Registration ─────────────────────────────────────────────────────────────

def job(kind):
    def register(fn):
        if kind in _REGISTRY and _REGISTRY[kind] is not fn:
            raise ValueError(f'Background job kind {kind!r} registered twice.')
        _REGISTRY[kind] = fn
        return fn
    return register


_ON_DEAD = {}                # kind -> [fn(row)] called once a job gives up


def on_dead(kind):
    """Register `fn(job_row)` to run when a job of `kind` runs out of attempts
    (to tell someone). It must never break the worker; errors are logged."""
    def register(fn):
        _ON_DEAD.setdefault(kind, []).append(fn)
        return fn
    return register


def periodic(name, every):
    def register(fn):
        _PERIODIC[name] = (int(every), fn)
        return fn
    return register


def ensure_loaded():
    """Import every app's `background_jobs` module once."""
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if not _loaded:
            from django.utils.module_loading import autodiscover_modules
            autodiscover_modules('background_jobs')
            _loaded = True


def registered_kinds():
    ensure_loaded()
    return sorted(_REGISTRY)


# ─── Enqueue ──────────────────────────────────────────────────────────────────

def enqueue(kind, payload=None, *, dedupe_key=None, run_after=None,
            priority=100, max_attempts=8):
    """Create a job in the caller's transaction and return it.

    With `dedupe_key`, a live (not yet finished) job with the same key is
    returned instead of creating a second one.
    """
    from pos_backend.models import BackgroundJob

    ensure_loaded()
    if kind not in _REGISTRY:
        # A typo must fail loudly here, not sit in the queue forever.
        raise ValueError(f'No background job handler registered for {kind!r}.')

    fields = dict(kind=kind, payload=payload or {}, priority=priority,
                  max_attempts=max_attempts, run_after=run_after or timezone.now())
    if not dedupe_key:
        return BackgroundJob.objects.create(**fields)

    existing = BackgroundJob.objects.filter(dedupe_key=dedupe_key).first()
    if existing:
        return _coalesce(existing)
    try:
        with transaction.atomic():
            return BackgroundJob.objects.create(dedupe_key=dedupe_key, **fields)
    except IntegrityError:
        # Another request created it between our read and our insert.
        return BackgroundJob.objects.get(dedupe_key=dedupe_key)


RERUN_FLAG = '_rerun'


def _coalesce(existing):
    """Fold a new request into the live job that has the same key.

    * queued / failed: it has not run (or will run again) - nothing to do;
    * running: it may already have read the data this request is about (a
      payment added while the bill's payment job runs), so it is flagged to
      run once more when it finishes;
    * dead: new work arrived for it, so it is brought back with fresh
      attempts. Otherwise the dead row would swallow every later request for
      the same object.
    """
    from pos_backend.models import BackgroundJob

    if existing.status == BackgroundJob.RUNNING:
        payload = dict(existing.payload or {})
        payload[RERUN_FLAG] = True
        BackgroundJob.objects.filter(pk=existing.pk).update(payload=payload)
    elif existing.status == BackgroundJob.DEAD:
        BackgroundJob.objects.filter(pk=existing.pk).update(
            status=BackgroundJob.QUEUED, attempts=0, run_after=timezone.now(),
            finished_at=None, locked_until=None)
    return existing


def revive(job_id):
    """Run a failed or dead job again now, with fresh attempts (monitor: Retry)."""
    from pos_backend.models import BackgroundJob

    return bool(BackgroundJob.objects.filter(
        pk=job_id, status__in=[BackgroundJob.FAILED, BackgroundJob.DEAD],
    ).update(status=BackgroundJob.QUEUED, attempts=0, run_after=timezone.now(),
             finished_at=None, locked_until=None))


# ─── Claim ────────────────────────────────────────────────────────────────────

def _due_queryset(now):
    from django.db.models import Q
    from pos_backend.models import BackgroundJob

    return BackgroundJob.objects.filter(
        Q(status__in=[BackgroundJob.QUEUED, BackgroundJob.FAILED], run_after__lte=now)
        # A worker that died mid-job: its lease ran out, so the job is due again.
        | Q(status=BackgroundJob.RUNNING, locked_until__lt=now)
    ).order_by('priority', 'run_after', 'id')


def claim(worker, limit=BATCH):
    """Claim up to `limit` due jobs for `worker`. Each job goes to one worker."""
    from django.db.models import F
    from pos_backend.models import BackgroundJob

    now = timezone.now()
    lease = now + timedelta(seconds=LEASE_SECONDS)
    claimed_ids = []

    if connection.features.has_select_for_update_skip_locked:
        with transaction.atomic():
            ids = list(_due_queryset(now).select_for_update(skip_locked=True)
                       .values_list('id', flat=True)[:limit])
            if ids:
                BackgroundJob.objects.filter(id__in=ids).update(
                    status=BackgroundJob.RUNNING, locked_until=lease,
                    locked_by=worker, attempts=F('attempts') + 1)
                claimed_ids = ids
    else:
        for row in _due_queryset(now).values('id', 'status', 'run_after', 'locked_until')[:limit]:
            won = BackgroundJob.objects.filter(
                id=row['id'], status=row['status'], run_after=row['run_after'],
                locked_until=row['locked_until'],
            ).update(status=BackgroundJob.RUNNING, locked_until=lease,
                     locked_by=worker, attempts=F('attempts') + 1)
            if won:
                claimed_ids.append(row['id'])

    return list(BackgroundJob.objects.filter(id__in=claimed_ids).order_by('priority', 'id'))


# ─── Run ──────────────────────────────────────────────────────────────────────

def _backoff_seconds(attempts):
    return min(30 * (2 ** max(attempts - 1, 0)), MAX_BACKOFF_SECONDS)


def run_one(row):
    """Run a claimed job and record the outcome. Returns the new status."""
    from pos_backend.models import BackgroundJob

    # Load the handlers here too: a caller that claims and runs jobs itself
    # (a command, a script) would otherwise kill every job as "no handler".
    ensure_loaded()
    handler = _REGISTRY.get(row.kind)
    now = timezone.now()
    if handler is None:
        BackgroundJob.objects.filter(pk=row.pk).update(
            status=BackgroundJob.DEAD, locked_until=None, finished_at=now,
            last_error=f'No handler registered for {row.kind!r}.')
        return BackgroundJob.DEAD

    try:
        handler(row.payload, row)
    except RetryLater as later:
        BackgroundJob.objects.filter(pk=row.pk).update(
            status=BackgroundJob.FAILED, locked_until=None,
            # A postponement is not a failure: give the attempt back.
            attempts=max(row.attempts - 1, 0),
            run_after=now + timedelta(seconds=later.seconds), last_error=str(later)[:4000])
        return BackgroundJob.FAILED
    except Exception as exc:  # one broken job must never stop the worker
        logger.exception('Background job %s #%s failed', row.kind, row.pk)
        dead = row.attempts >= row.max_attempts
        BackgroundJob.objects.filter(pk=row.pk).update(
            status=BackgroundJob.DEAD if dead else BackgroundJob.FAILED,
            locked_until=None,
            run_after=now + timedelta(seconds=_backoff_seconds(row.attempts)),
            finished_at=now if dead else None,
            last_error=f'{type(exc).__name__}: {exc}'[:4000])
        if dead:
            row.last_error = f'{type(exc).__name__}: {exc}'[:4000]
            for callback in _ON_DEAD.get(row.kind, ()):
                try:
                    callback(row)
                except Exception:                                  # noqa: BLE001
                    logger.exception('on_dead callback for %s failed', row.kind)
        return BackgroundJob.DEAD if dead else BackgroundJob.FAILED

    # Asked to run again while it ran (see _coalesce): queue it once more.
    current = BackgroundJob.objects.filter(pk=row.pk).values_list('payload', flat=True).first() or {}
    if current.get(RERUN_FLAG):
        payload = {k: v for k, v in current.items() if k != RERUN_FLAG}
        BackgroundJob.objects.filter(pk=row.pk).update(
            status=BackgroundJob.QUEUED, locked_until=None, payload=payload,
            run_after=timezone.now(), attempts=0, last_error='')
        return BackgroundJob.QUEUED

    BackgroundJob.objects.filter(pk=row.pk).update(
        status=BackgroundJob.DONE, locked_until=None, finished_at=timezone.now(),
        last_error='',
        # Free the key so the same work can be queued again later.
        dedupe_key=None)
    return BackgroundJob.DONE


def process_due(worker=None, limit=BATCH):
    """Claim and run one batch. Returns a dict of counts by outcome."""
    ensure_loaded()
    worker = worker or default_worker_name()
    counts = {}
    for row in claim(worker, limit):
        status = run_one(row)
        counts[status] = counts.get(status, 0) + 1
    return counts


def run_periodic(now=None):
    """Run every periodic task whose period has elapsed, once across all workers."""
    from django.db.models import Q
    from pos_backend.models import JobSchedule

    ensure_loaded()
    now = now or timezone.now()
    ran = []
    for name, (every, fn) in sorted(_PERIODIC.items()):
        JobSchedule.objects.get_or_create(name=name)
        cutoff = now - timedelta(seconds=every)
        won = JobSchedule.objects.filter(
            Q(last_run_at__isnull=True) | Q(last_run_at__lte=cutoff), name=name,
        ).update(last_run_at=now)
        if not won:
            continue
        try:
            fn()
            JobSchedule.objects.filter(name=name).update(last_error='')
        except Exception as exc:
            logger.exception('Periodic task %s failed', name)
            JobSchedule.objects.filter(name=name).update(last_error=f'{type(exc).__name__}: {exc}'[:4000])
        ran.append(name)
    return ran


def default_worker_name():
    return f'{socket.gethostname()}:{os.getpid()}'


def run_worker_forever(worker=None, interval=5, stop=None):
    """Worker loop: drain due jobs, run periodic tasks, sleep `interval`."""
    import time

    import threading

    worker = worker or default_worker_name()
    ensure_loaded()
    logger.info('Background worker %s started (%d job kinds, %d periodic tasks).',
                worker, len(_REGISTRY), len(_PERIODIC))

    # The periodic tasks (the stock pull from SAP walks every item and warehouse and takes minutes) run
    # on their own thread: run inline, they held up every posting - a bill, a new service, a receipt -
    # until they finished.
    def periodic_loop():
        while not (stop and stop.is_set()):
            try:
                close_old_connections()
                run_periodic()
            except Exception:
                logger.exception('Background periodic loop error')
            finally:
                close_old_connections()
            if stop:
                stop.wait(interval)
            else:
                time.sleep(interval)

    threading.Thread(target=periodic_loop, name='periodic-tasks', daemon=True).start()
    while not (stop and stop.is_set()):
        try:
            close_old_connections()
            while process_due(worker):
                close_old_connections()
        except Exception:
            logger.exception('Background worker loop error')
        finally:
            close_old_connections()
        if stop:
            stop.wait(interval)
        else:
            time.sleep(interval)


# ─── Built-in housekeeping ────────────────────────────────────────────────────

@periodic('jobs.prune_finished', every=24 * 3600)
def prune_finished(days=30):
    """Delete finished jobs older than `days`. Dead jobs are kept for review."""
    from pos_backend.models import BackgroundJob

    BackgroundJob.objects.filter(
        status=BackgroundJob.DONE, finished_at__lt=timezone.now() - timedelta(days=days),
    ).delete()
