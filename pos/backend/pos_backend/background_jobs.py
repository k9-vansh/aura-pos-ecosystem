"""Background job handlers owned by pos_backend (discovered by jobs.ensure_loaded).

`sap.sync` posts one POS document to SAP. The document functions themselves
live in sap_utils and are unchanged in what they post; this decides when they
run and what a failure means:

  * SAP switched off             -> wait 10 minutes, no attempt used;
  * a payment or credit memo
    whose invoice is not in SAP  -> wait for it (the invoice is queued if it
                                    was not), no attempt used;
  * the document was deleted     -> nothing to post, done;
  * SAP refused it               -> retried with backoff; after MAX_ATTEMPTS
                                    it is "needs attention" in Admin > SAP Sync
                                    with SAP's own message.
"""

import logging

from pos_backend.jobs import RetryLater, job, on_dead, periodic

logger = logging.getLogger(__name__)


@periodic('sap.upi_settlement', every=60)
def upi_settlement():
    """Post the next-day UPI settlement journals (parking account -> bank).

    Off when SAP is off or both `sap.upi_settlement_days` and
    `sap.upi_settlement_minutes` are 0. Every minute, so the demo setting
    (minutes) is picked up promptly; with only the days setting it does
    nothing until a day's UPI is due, then settles it on the next run."""
    from pos_backend.sap_utils import create_sap_upi_settlements, sap_enabled

    if not sap_enabled():
        return 'off'
    return create_sap_upi_settlements()


@periodic('sap.auto_recovery', every=600)
def sap_auto_recovery():
    """Queue again what SAP missed: payments of bills that are in SAP, bills never queued, and postings that
    gave up only because SAP was slow or unreachable (see pos_backend/sap_recovery.py). Off with SAP, or with
    Settings > SAP mapping > "Re-send what SAP missed, by itself" turned off."""
    from pos_backend import app_settings, sap_recovery
    from pos_backend.sap_utils import sap_enabled

    if not sap_enabled() or not app_settings.get('sap.auto_recovery'):
        return 'off'
    counts = sap_recovery.recover()
    revived = sap_recovery.revive_transient_failures()
    return f"{counts['payments']} payment(s), {counts['invoices']} bill(s) queued; {revived} posting(s) retried"


class SapPostingFailed(Exception):
    """SAP did not take the document; the message is SAP's (or the sync's)."""


def _record_reference(job_row, reference):
    """Keep SAP's document number on the job, so the monitor shows where it went."""
    if not reference:
        return
    from pos_backend.models import BackgroundJob

    current = BackgroundJob.objects.filter(pk=job_row.pk).values_list('payload', flat=True).first() or {}
    BackgroundJob.objects.filter(pk=job_row.pk).update(payload={**current, 'sap_ref': reference})


@job('sap.sync')
def sap_sync(payload, job_row):
    from pos_backend import sap_jobs
    from pos_backend.sap_utils import sap_enabled

    if not sap_enabled():
        raise RetryLater(600, 'SAP is switched off; the posting waits until it is back on')

    object_type = payload.get('object_type')
    object_id = payload.get('object_id')

    try:
        if sap_jobs.invoice_dependency(object_type, object_id) == 'skip':
            logger.info('SAP: %s %s has nothing to post against (invoice posts nothing).',
                        object_type, object_id)
            return
    except sap_jobs.WaitForInvoice as wait:
        raise RetryLater(120, str(wait))

    outcome, error = sap_jobs.run_job(object_type, object_id)
    if outcome == 'done':
        _record_reference(job_row, sap_jobs.sap_reference(object_type, object_id))
        return
    if outcome == 'missing':
        logger.warning('SAP: %s %s no longer exists; nothing to post.', object_type, object_id)
        return
    raise SapPostingFailed(error)


@on_dead('sap.sync')
def tell_sap_posting_failed(job_row):
    """A posting ran out of attempts: tell the people the rule names (Admin > Integrations)."""
    from notifications.service import notify
    from salon_admin.models import Center

    payload = job_row.payload or {}
    center = Center.all_objects.filter(pk=payload.get('center_id')).first() if payload.get('center_id') else None
    what = f"{payload.get('object_type', 'document')} #{payload.get('object_id', '')}".strip()
    error = (job_row.last_error or '').splitlines()[0][:200] if job_row.last_error else ''
    notify('sap.posting_failed', title='SAP posting needs attention',
           body=f'{what} could not be posted to SAP after all retries. {error}',
           url='/admin/sap-sync', center=center, data={'job_id': job_row.pk})
