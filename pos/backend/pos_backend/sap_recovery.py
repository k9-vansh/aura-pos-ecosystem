"""Re-send what SAP missed, by itself (Settings > SAP mapping > "Re-send what SAP missed, by itself").

Three gaps a person would otherwise have to find by comparing the POS with SAP:

  * a bill is in SAP but its payment never reached SAP (nothing queued it, or the job was lost);
  * a bill was never queued at all;
  * a posting gave up after its retries only because SAP was slow or unreachable (a timeout, a failed login),
    not because SAP refused the document.

Each of these only QUEUES the posting again. The posting functions are idempotent: they ask SAP (or the posting
ledger) what it already has first, so a document is never posted twice. What SAP refused for a business reason
(stock it does not hold, a business place it does not know) is NOT retried here: that needs a person.
"""
import logging
from datetime import timedelta

from django.db.models import CharField, Exists, OuterRef, Value
from django.db.models.functions import Cast, Concat
from django.utils import timezone

logger = logging.getLogger(__name__)

# How long a bill is left alone: a bill raised a moment ago is still on its way to SAP by the normal route.
SETTLE = timedelta(minutes=10)
# Only the recent past is swept each time; older gaps are left to Admin > SAP Sync.
WINDOW = timedelta(days=14)
MAX_REVIVALS = 3
REVIVE_AFTER = timedelta(minutes=30)

# What a transient failure looks like in a job's last_error (SAP slow, down or logging in failed).
TRANSIENT = ('timed out', 'read timeout', 'connection', 'max retries exceeded', 'could not obtain sap session',
             'sap login failed', 'service unavailable', 'bad gateway', 'gateway timeout', 'http 502', 'http 503',
             'http 504', 'temporarily')


def is_transient(error):
    text = (error or '').lower()
    return any(marker in text for marker in TRANSIENT)


def _requeue(object_type, obj):
    """Queue the posting; a finished (done) job for the same object is reopened, because it did not do its work."""
    from pos_backend import sap_jobs
    from pos_backend.models import BackgroundJob

    job = sap_jobs.enqueue(object_type, obj.pk, obj=obj)
    if job.status == BackgroundJob.DONE:
        BackgroundJob.objects.filter(pk=job.pk, status=BackgroundJob.DONE).update(
            status=BackgroundJob.QUEUED, attempts=0, run_after=timezone.now(), finished_at=None, locked_until=None)
    return job


def _has_unfinished_job(key):
    from pos_backend.models import BackgroundJob
    return BackgroundJob.objects.filter(dedupe_key=key).exclude(status=BackgroundJob.DONE).exists()


def missing_payment_invoices(now=None, limit=50):
    """Bills posted to SAP that paid real money but have no incoming payment recorded for them."""
    from billing.models import Invoice
    from pos_backend.models import SapPosting
    from pos_backend.sap_utils import _is_virtual_payment

    now = now or timezone.now()
    recorded = SapPosting.objects.filter(
        status=SapPosting.DONE,
        key__startswith=Concat(Value('inpay:'), Cast(OuterRef('pk'), CharField()), Value(':')))
    candidates = (Invoice.objects
                  .filter(status__in=['paid', 'partial'], sap_doc_entry__isnull=False, paid_amount__gt=0,
                          created_at__lt=now - SETTLE, created_at__gte=now - WINDOW)
                  .annotate(has_payment_posting=Exists(recorded)).filter(has_payment_posting=False)
                  .order_by('id')[:limit * 4])
    found = []
    for invoice in candidates:
        real = [p for p in invoice.payments.all()
                if not getattr(p, 'is_tip', False) and not _is_virtual_payment(p.payment_method)]
        if not real or _has_unfinished_job(f'sap:incoming_payment:{invoice.pk}'):
            continue
        found.append(invoice)
        if len(found) >= limit:
            break
    return found


def unqueued_invoices(now=None, limit=50):
    """Bills that are not in SAP and have no posting job at all (the queue never got them)."""
    from billing.models import Invoice
    from pos_backend.models import BackgroundJob

    now = now or timezone.now()
    queued = BackgroundJob.objects.filter(dedupe_key=Concat(Value('sap:invoice:'), Cast(OuterRef('pk'), CharField())))
    return list(Invoice.objects
                .filter(status__in=['paid', 'partial'], sap_sync_status=False, sap_doc_entry__isnull=True,
                        created_at__lt=now - SETTLE, created_at__gte=now - WINDOW)
                .annotate(has_job=Exists(queued)).filter(has_job=False).order_by('id')[:limit])


def recover(now=None):
    """Queue what SAP is missing. Returns {'payments': n, 'invoices': n}."""
    from django.db import transaction

    counts = {'payments': 0, 'invoices': 0}
    for invoice in missing_payment_invoices(now):
        with transaction.atomic():
            _requeue('incoming_payment', invoice)
        counts['payments'] += 1
        logger.warning('SAP recovery: bill %s is in SAP but its payment is not; payment queued.',
                       invoice.display_number or invoice.pk)
    for invoice in unqueued_invoices(now):
        with transaction.atomic():
            _requeue('invoice', invoice)
        counts['invoices'] += 1
        logger.warning('SAP recovery: bill %s was never queued for SAP; queued.', invoice.display_number or invoice.pk)
    return counts


def revive_transient_failures(now=None, limit=50):
    """Give a posting that ran out of attempts only because SAP was slow or down another go (at most 3 times)."""
    from pos_backend.models import BackgroundJob

    now = now or timezone.now()
    revived = 0
    for job in (BackgroundJob.objects
                .filter(kind='sap.sync', status=BackgroundJob.DEAD, finished_at__lt=now - REVIVE_AFTER)
                .order_by('finished_at')[:limit * 4]):
        payload = dict(job.payload or {})
        if payload.get('revived', 0) >= MAX_REVIVALS or not is_transient(job.last_error):
            continue
        payload['revived'] = payload.get('revived', 0) + 1
        BackgroundJob.objects.filter(pk=job.pk, status=BackgroundJob.DEAD).update(
            payload=payload, status=BackgroundJob.QUEUED, attempts=0, run_after=now, finished_at=None,
            locked_until=None)
        revived += 1
        if revived >= limit:
            break
    return revived
