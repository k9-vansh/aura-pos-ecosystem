"""Post each SAP document at most once.

The sync functions used to POST and hope. When the reply was lost, or the
retry queue re-ran a function that had already posted half its documents, SAP
got the same goods receipt, payment or journal entry twice.

`post_once(key, post, find)` wraps a single POST:

  * a posting already recorded as done is never sent again;
  * a lease on the row stops two threads or processes posting it at once;
  * on a retry, `find()` first asks SAP whether the document is already there
    (it carries a reference the POS wrote into it), so a document whose reply
    was lost is recovered instead of duplicated;
  * if SAP cannot be asked, nothing is posted and the caller retries later.

The posting payloads themselves are unchanged; this module only decides
whether to send them.
"""

import logging
import urllib.parse
from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

LEASE_SECONDS = 180


class PostingBusy(Exception):
    """Another thread or process is posting this document right now."""


def odata_str(value) -> str:
    """Quote a value as an OData string literal: O'Brien -> 'O''Brien'."""
    return "'" + str(value).replace("'", "''") + "'"


def odata_filter(expression: str) -> str:
    """Percent-encode a $filter expression. A raw '#' (TXN#12) would otherwise
    be read as the start of a URL fragment and cut the filter short."""
    return urllib.parse.quote(expression, safe="()',=")


def _record(key):
    from pos_backend.models import SapPosting
    try:
        with transaction.atomic():
            rec, _ = SapPosting.objects.get_or_create(key=key)
    except IntegrityError:
        rec = SapPosting.objects.get(key=key)
    return rec


def row(key):
    """The posting record for `key`, or None."""
    from pos_backend.models import SapPosting
    return SapPosting.objects.filter(key=key).first()


def posted(key):
    """The doc entry recorded for `key`, or None if it has not been posted."""
    from pos_backend.models import SapPosting
    rec = SapPosting.objects.filter(key=key, status=SapPosting.DONE).first()
    return (rec.doc_entry or 'posted') if rec else None


def done_rows(prefix):
    """Done postings whose key starts with `prefix`, oldest first."""
    from pos_backend.models import SapPosting
    return list(SapPosting.objects.filter(key__startswith=prefix, status=SapPosting.DONE).order_by('id'))


def known_doc_entries(prefix):
    """Doc entries already claimed under `prefix`, so a lookup that matches
    several identical documents can pick one nobody has claimed yet."""
    return {r.doc_entry for r in done_rows(prefix) if r.doc_entry}


def mark_done(key, doc_entry='', amount=None, detail=None, note=''):
    """Record a posting as made."""
    from pos_backend.models import SapPosting
    rec = _record(key)
    fields = dict(status=SapPosting.DONE, doc_entry=str(doc_entry or '')[:40],
                  locked_until=None, last_error=note[:500], updated_at=timezone.now())
    if amount is not None:
        fields['amount'] = amount
    if detail is not None:
        fields['detail'] = detail
    SapPosting.objects.filter(pk=rec.pk).update(**fields)


def post_once(key, post, find=None, amount=None, detail=None):
    """Send one document to SAP unless it is already there.

    post()  -> the new DocEntry (truthy) on success, a falsy value when SAP
               refused it. May raise.
    find()  -> the DocEntry if SAP already holds this document, else None.
               Raises if SAP cannot be asked. Only consulted on a retry, i.e.
               when an earlier attempt may have reached SAP.

    Returns the DocEntry as a string, or None when the document was not
    posted. Raises PostingBusy if another worker holds the lease, and lets
    exceptions from post()/find() through after releasing the lease.
    """
    from pos_backend.models import SapPosting

    rec = _record(key)
    if rec.status == SapPosting.DONE:
        return rec.doc_entry or 'posted'

    now = timezone.now()
    claimed = (SapPosting.objects
               .filter(pk=rec.pk, status=SapPosting.PENDING)
               .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now))
               .update(locked_until=now + timedelta(seconds=LEASE_SECONDS),
                       attempts=F('attempts') + 1, updated_at=now))
    if not claimed:
        rec.refresh_from_db()
        if rec.status == SapPosting.DONE:
            return rec.doc_entry or 'posted'
        raise PostingBusy(key)
    rec.refresh_from_db()

    def release(error):
        SapPosting.objects.filter(pk=rec.pk).update(
            locked_until=None, last_error=str(error)[:500], updated_at=timezone.now())

    try:
        if find is not None and rec.attempts > 1:
            existing = find()
            if existing:
                logger.info('SAP: %s already in SAP as %s; not posting it again.', key, existing)
                mark_done(key, existing, amount, detail, note='Recovered from SAP')
                return str(existing)
        doc_entry = post()
    except Exception as exc:
        release(exc)
        raise

    if doc_entry:
        mark_done(key, doc_entry, amount, detail)
        return str(doc_entry)

    release('SAP did not accept the document')
    return None


# ─── Worker lease ─────────────────────────────────────────────────────────

WORKER_LEASE_KEY = 'lease:sap-retry-worker'


def hold_worker_lease(owner: str, seconds: int = 180) -> bool:
    """Take or renew the single retry-worker lease. True if `owner` holds it.

    Every web process may start the scheduler thread; only the one holding
    this lease works the queue. If that process dies the lease runs out and
    another takes over.
    """
    from pos_backend.models import SapPosting

    rec = _record(WORKER_LEASE_KEY)
    now = timezone.now()
    taken = (SapPosting.objects
             .filter(pk=rec.pk)
             .filter(Q(locked_until__isnull=True) | Q(locked_until__lt=now) | Q(doc_entry=owner[:40]))
             .update(doc_entry=owner[:40], locked_until=now + timedelta(seconds=seconds), updated_at=now))
    return bool(taken)
