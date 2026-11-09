"""The SAP retry queue: what each queued row means, and one way to work it.

The scheduler thread and the `retry_sap_syncs` command used to carry two
copies of the same dispatch table, and the copies had drifted. Rows for
journal entries were queued as 'unknown' and then deleted unprocessed, so a
failed value-card, membership, package or advance journal never reached SAP.
Both runners now call `process_due()`.

Each row is claimed with a conditional UPDATE before it is worked, so two
runners can never pick up the same row at once.
"""

import logging
from datetime import timedelta

from django.db import close_old_connections
from django.utils import timezone

logger = logging.getLogger(__name__)

MAX_RETRY = 6            # caps the wait at 5 * 3^6 = 3645 min (~2.5 days)
CLAIM_MINUTES = 10       # a claimed row is hidden from other runners this long
BATCH = 50


def _backoff(retry_count):
    return timedelta(minutes=5 * (3 ** retry_count))


# ─── Function -> queue type ───────────────────────────────────────────────

# By name, so a new sync function cannot silently fall through to 'unknown'.
FUNCTION_TYPES = {
    'create_sap_business_partner': 'client',
    'create_sap_item': 'product',
    'update_sap_item': 'product_update',
    'create_sap_service': 'service',
    'create_sap_vendor': 'vendor',
    'create_sap_invoice': 'invoice',
    'create_sap_incoming_payment': 'incoming_payment',
    'repost_incoming_payments': 'payment_repost',
    'create_sap_valuecard_forfeiture_je': 'valuecard_forfeit',
    'create_sap_credit_note': 'credit_note',
    'create_sap_cancel_reversals': 'bill_cancel',
    'create_sap_purchase_delivery_note': 'purchase_order',
    'create_sap_stock_transfer': 'stock_transfer',
    'create_sap_branch_sale': 'branch_sale',
    'create_sap_stock_transaction': 'stock_transaction',
    'create_sap_warehouse': 'center',
    'create_sap_advance_je': 'advance_je',
    'create_sap_petty_cash_je': 'petty_cash_je',
    'create_sap_valuecard_je': 'marketing_je',
    'create_sap_membership_je': 'marketing_je',
    'create_sap_package_je': 'marketing_je',
}


def object_type_for(func):
    return FUNCTION_TYPES.get(getattr(func, '__name__', ''), 'unknown')


def queue_object(func, args):
    """The (object_type, object_id) to queue for a call, or None.

    The journal functions take (invoice, item); the row has to point at the
    item, because one invoice can sell several cards.
    """
    object_type = object_type_for(func)
    if object_type == 'unknown' or not args:
        return None
    obj = args[1] if object_type == 'marketing_je' and len(args) > 1 else args[0]
    obj_id = getattr(obj, 'pk', None)
    if obj_id is None:
        return None
    return object_type, obj_id


# ─── Queue type -> work ───────────────────────────────────────────────────

def _marketing_je(item):
    from pos_backend import sap_utils
    model = item.content_type.model if item.content_type else ''
    fn = {
        'valuecard': sap_utils.create_sap_valuecard_je,
        'membership': sap_utils.create_sap_membership_je,
        'package': sap_utils.create_sap_package_je,
    }.get(model)
    if fn is None:
        return True  # not a sold card/membership/package any more: nothing to post
    return fn(item.invoice, item)


def _loader(object_type):
    """(model manager, sync function) for a queue type."""
    from pos_backend import sap_branch_transfer, sap_utils
    from clients.models import Client
    from inventory.models import Product, Vendor, PurchaseOrder, StockTransfer, StockTransaction
    from billing.models import Invoice, InvoiceItem, AdvancePayment
    from clients.models import ClientValueCard
    from salon_admin.models import Center
    from services.models import ServiceMaster
    from finance.models import PettyCashEntry

    return {
        'client': (Client.objects, sap_utils.create_sap_business_partner),
        'product': (Product.objects, sap_utils.create_sap_item),
        # A product edited in the POS: the item's details in SAP are brought up to date (the earlier
        # 'product' queue type ran the create call for edits too, which never updated the item group).
        'product_update': (Product.objects, sap_utils.update_sap_item),
        'service': (ServiceMaster.objects, sap_utils.create_sap_service),
        'vendor': (Vendor.objects, sap_utils.create_sap_vendor),
        'invoice': (Invoice.objects, sap_utils.create_sap_invoice),
        'incoming_payment': (Invoice.objects, sap_utils.create_sap_incoming_payment),
        'payment_repost': (Invoice.objects, sap_utils.repost_incoming_payments),
        'valuecard_forfeit': (ClientValueCard.objects.select_related('value_card', 'client', 'center'),
                              sap_utils.create_sap_valuecard_forfeiture_je),
        'credit_note': (Invoice.objects, sap_utils.create_sap_credit_note),
        'bill_cancel': (Invoice.objects, sap_utils.create_sap_cancel_reversals),
        'purchase_order': (PurchaseOrder.objects, sap_utils.create_sap_purchase_delivery_note),
        'stock_transfer': (StockTransfer.objects, sap_utils.create_sap_stock_transfer),
        'branch_sale': (StockTransfer.objects, sap_branch_transfer.create_sap_branch_sale),
        # all_objects so soft-deleted centres are still retrievable
        'center': (Center.all_objects, sap_utils.create_sap_warehouse),
        'stock_transaction': (StockTransaction.objects, sap_utils.create_sap_stock_transaction),
        'advance_je': (AdvancePayment.objects, sap_utils.create_sap_advance_je),
        'petty_cash_je': (PettyCashEntry.objects.select_related('category', 'center'),
                          sap_utils.create_sap_petty_cash_je),
        'marketing_je': (InvoiceItem.objects.select_related('invoice', 'content_type'), _marketing_je),
    }.get(object_type)


def run_job(object_type, object_id):
    """Run one queued sync. Returns (outcome, error) where outcome is
    'done', 'failed', 'missing' (the record is gone) or 'unknown' (bad type)."""
    from pos_backend import sap_utils

    entry = _loader(object_type)
    if entry is None:
        return 'unknown', f"Unknown object_type '{object_type}'"
    manager, fn = entry
    obj = manager.filter(pk=object_id).first()
    if obj is None:
        return 'missing', f'{object_type} {object_id} no longer exists'
    sap_utils.clear_last_sap_error()
    ok = fn(obj)
    if ok:
        return 'done', ''
    # SAP's own words when it refused something, else what the sync recorded.
    error = (sap_utils.last_sap_error()
             or getattr(obj, 'sap_sync_message', None)
             or 'SAP did not accept the document')
    return 'failed', error


# ─── The outbox (Phase 2) ─────────────────────────────────────────────────
#
# Postings used to run on a thread started after the request committed. A
# server restart between the commit and the thread finishing lost the
# posting with no trace, and a failure went to a second table worked by a
# thread inside every web process. Now a posting is a BackgroundJob row
# written in the SAME transaction as the bill or stock move, worked by
# `manage.py run_workers` (see pos_backend/background_jobs.py). Nothing is
# lost, nothing waits on SAP, and every failure is visible in Admin > SAP Sync.

JOB_KIND = 'sap.sync'
MAX_ATTEMPTS = 10        # ~3 hours of retries (30 s doubling, capped at 1 h), then "needs attention"

# Masters first, then the invoice, then what hangs off it.
PRIORITY = {
    'center': 10, 'client': 10, 'product': 10, 'product_update': 11, 'service': 10, 'vendor': 10,
    'invoice': 20, 'advance_je': 25, 'petty_cash_je': 30, 'marketing_je': 30, 'incoming_payment': 30,
    'payment_repost': 35, 'valuecard_forfeit': 45,
    'bill_cancel': 38, 'credit_note': 40, 'purchase_order': 40, 'branch_sale': 35, 'stock_transfer': 40, 'stock_transaction': 50,
}

LABELS = {
    'center': 'Warehouse', 'client': 'Business partner', 'product': 'Item', 'product_update': 'Item update', 'service': 'Service item',
    'vendor': 'Vendor', 'invoice': 'A/R invoice', 'incoming_payment': 'Incoming payment',
    'payment_repost': 'Incoming payment re-post (method changed)',
    'valuecard_forfeit': 'Value card forfeiture journal',
    'credit_note': 'Credit memo', 'bill_cancel': 'Cancel payments and journals', 'purchase_order': 'Purchase order / GRPO',
    'stock_transfer': 'Inventory transfer', 'branch_sale': 'Tax invoice for stock sent to another branch',
    'stock_transaction': 'Goods receipt / issue',
    'advance_je': 'Advance journal', 'petty_cash_je': 'Petty cash journal', 'marketing_je': 'Card / membership / package journal',
}


def describe(object_type, obj):
    """(center_id, label) for the monitor, read once when the job is queued."""
    if obj is None:
        return None, f'{LABELS.get(object_type, object_type)} #?'
    center_id = getattr(obj, 'center_id', None)
    if object_type == 'marketing_je':
        invoice = getattr(obj, 'invoice', None)
        center_id = getattr(invoice, 'center_id', None)
        ref = getattr(invoice, 'display_number', None) or getattr(invoice, 'pk', '')
        return center_id, f"{LABELS[object_type]} - bill {ref}, line {obj.pk}"
    if object_type in ('invoice', 'incoming_payment', 'credit_note', 'payment_repost', 'bill_cancel'):
        ref = getattr(obj, 'display_number', None) or obj.pk
        return center_id, f"{LABELS[object_type]} - bill {ref}"
    if object_type in ('stock_transfer', 'branch_sale'):
        center_id = getattr(obj, 'from_center_id', None)
    name = getattr(obj, 'name', None) or getattr(obj, 'center_name', None) or ''
    suffix = f" - {name}" if name else ''
    return center_id, f"{LABELS.get(object_type, object_type)} #{obj.pk}{suffix}"


def enqueue(object_type, object_id, error='', obj=None):
    """Queue one posting in the caller's transaction. One live job per object."""
    from pos_backend import jobs

    center_id, label = describe(object_type, obj)
    row = jobs.enqueue(
        JOB_KIND,
        {'object_type': object_type, 'object_id': object_id,
         'center_id': center_id, 'label': label},
        dedupe_key=f'sap:{object_type}:{object_id}',
        priority=PRIORITY.get(object_type, 50),
        max_attempts=MAX_ATTEMPTS,
    )
    if error and not row.last_error:
        type(row).objects.filter(pk=row.pk).update(last_error=str(error)[:4000])
    return row


def sap_reference(object_type, object_id):
    """The SAP document number(s) a finished posting produced, for the monitor
    ('47', 'payments 20, 21'), or '' when SAP has nothing for it."""
    from pos_backend.models import SapPosting

    def entries(prefix):
        return [r.doc_entry for r in SapPosting.objects.filter(
            key__startswith=prefix, status=SapPosting.DONE).order_by('id') if r.doc_entry]

    if object_type == 'invoice':
        from billing.models import Invoice
        entry = Invoice.objects.filter(pk=object_id).values_list('sap_doc_entry', flat=True).first()
        return str(entry or '')
    keyed = {
        'incoming_payment': f'inpay:{object_id}:',
        'credit_note': f'creditnote:{object_id}',
        'marketing_je': f'je:sale:{object_id}',
        'advance_je': f'je:advance:{object_id}',
        'petty_cash_je': f'je:petty:{object_id}',
        'valuecard_forfeit': f'je:forfeit:{object_id}',
        'stock_transfer': f'transfer:{object_id}',
        'branch_sale': f'branchsale:{object_id}',
        'stock_transaction': f'stocktxn:{object_id}',
        'purchase_order': f'grpo:{object_id}:',
    }
    if object_type == 'stock_transfer':
        return ', '.join(entries(f'transfer:{object_id}') + entries(f'branchpurchase:{object_id}'))
    if object_type in keyed:
        return ', '.join(entries(keyed[object_type]))
    return ''


class WaitForInvoice(Exception):
    """The posting needs its invoice in SAP first (carries the RetryLater delay)."""


def _branch_sale_dependency(transfer_id):
    """A transfer between branches is received into SAP as a purchase invoice, which copies the tax invoice
    raised when the goods left: wait for that one (it is queued if it never was)."""
    from inventory.models import StockTransfer
    from inventory.places import supply_kind
    from pos_backend.models import BackgroundJob
    from pos_backend.sap_recovery import _requeue

    transfer = StockTransfer.objects.select_related('from_center', 'to_center').filter(pk=transfer_id).first()
    if transfer is None or transfer.sap_ar_doc_entry or transfer.sap_doc_entry:
        return 'ready'
    if supply_kind(transfer.from_center, transfer.to_center)[0] not in ('igst', 'cgst_sgst'):
        return 'ready'
    blocking = BackgroundJob.objects.filter(dedupe_key=f'sap:branch_sale:{transfer.pk}').first()
    if blocking is not None and blocking.status == BackgroundJob.DEAD:
        raise RuntimeError('Its tax invoice could not be posted; retry the tax invoice first.')
    if blocking is None or blocking.status == BackgroundJob.DONE:
        _requeue('branch_sale', transfer)
    raise WaitForInvoice('Waiting for the tax invoice of this transfer to reach SAP')


def _cancel_dependency(invoice_id):
    """A cancelled bill's payments and journals can only be cancelled once they are in SAP.

    Waits while any posting of the bill (invoice, payments, card / membership /
    package / advance journals) is still queued or retrying; a posting that gave
    up never reached SAP, so there is nothing of it to cancel."""
    from billing.models import AdvancePayment, Invoice
    from pos_backend.models import BackgroundJob

    invoice = Invoice.objects.filter(pk=invoice_id).first()
    if invoice is None:
        return 'ready'
    keys = [f'sap:invoice:{invoice.pk}', f'sap:incoming_payment:{invoice.pk}', f'sap:payment_repost:{invoice.pk}']
    keys += [f'sap:marketing_je:{i}' for i in invoice.items.values_list('id', flat=True)]
    keys += [f'sap:advance_je:{a}' for a in AdvancePayment.objects.filter(invoice=invoice).values_list('id', flat=True)]
    if BackgroundJob.objects.filter(
            dedupe_key__in=keys,
            status__in=[BackgroundJob.QUEUED, BackgroundJob.RUNNING, BackgroundJob.FAILED]).exists():
        raise WaitForInvoice('Waiting for this bill\'s postings to reach SAP before they are cancelled')
    return 'ready'


def invoice_dependency(object_type, object_id):
    """For a payment or credit memo: 'ready', 'skip', or raise.

    'skip'  - the invoice posts nothing to SAP (it sold only cards,
              memberships or packages, which go as journals), so there is
              nothing to pay or credit against. This used to fail forever.
    raises  - WaitForInvoice while the invoice is queued or retrying (it is
              queued if it was not); RuntimeError once the invoice's own
              posting has given up, so this job does not spin behind it.
    """
    if object_type == 'stock_transfer':
        return _branch_sale_dependency(object_id)
    if object_type == 'bill_cancel':
        return _cancel_dependency(object_id)
    if object_type not in ('incoming_payment', 'credit_note'):
        return 'ready'
    from billing.models import Invoice
    from pos_backend.models import BackgroundJob

    if object_type == 'credit_note':
        # Payments and journals are cancelled first, so the credit memo closes the
        # reopened invoice instead of floating beside it.
        if BackgroundJob.objects.filter(
                dedupe_key=f'sap:bill_cancel:{object_id}',
                status__in=[BackgroundJob.QUEUED, BackgroundJob.RUNNING, BackgroundJob.FAILED]).exists():
            raise WaitForInvoice('Waiting for the bill\'s payments and journals to be cancelled in SAP')

    invoice = Invoice.objects.filter(pk=object_id).first()
    if invoice is None or invoice.sap_doc_entry:
        return 'ready'
    if invoice.sap_sync_status:
        return 'skip'
    blocking = BackgroundJob.objects.filter(dedupe_key=f'sap:invoice:{invoice.pk}').first()
    if blocking is not None and blocking.status == BackgroundJob.DEAD:
        raise RuntimeError('Its A/R invoice could not be posted; retry the invoice first.')
    if blocking is None:
        enqueue('invoice', invoice.pk, obj=invoice)
    raise WaitForInvoice('Waiting for the A/R invoice to reach SAP')


def process_due(limit=BATCH, out=None):
    """LEGACY: work the old SapSyncQueue table once. Returns a dict of counts.

    Kept only so rows written before the outbox can still be drained by the
    `retry_sap_syncs` command; migration 0005 moves existing rows into the
    outbox and nothing writes this table any more.
    """
    from pos_backend.models import SapSyncQueue
    from pos_backend.sap_utils import sap_enabled

    counts = {'done': 0, 'failed': 0, 'dropped': 0, 'skipped': 0}
    if not sap_enabled():
        return counts

    now = timezone.now()
    due = list(SapSyncQueue.objects.filter(next_retry_at__lte=now).order_by('next_retry_at', 'created_at')[:limit])

    for row in due:
        # Claim: only the runner whose UPDATE matches the row as read wins.
        claimed = SapSyncQueue.objects.filter(pk=row.pk, next_retry_at=row.next_retry_at).update(
            next_retry_at=timezone.now() + timedelta(minutes=CLAIM_MINUTES))
        if not claimed:
            counts['skipped'] += 1
            continue

        try:
            outcome, error = run_job(row.object_type, row.object_id)
        except Exception as exc:  # a bug in one sync must not stop the queue
            logger.exception('SAP retry of %s %s raised', row.object_type, row.object_id)
            outcome, error = 'failed', str(exc)

        if outcome == 'done':
            SapSyncQueue.objects.filter(pk=row.pk).delete()
            counts['done'] += 1
            if out:
                out(f'Synced {row.object_type} {row.object_id}')
        elif outcome in ('missing', 'unknown'):
            SapSyncQueue.objects.filter(pk=row.pk).delete()
            counts['dropped'] += 1
            logger.warning('SAP queue: dropping %s %s: %s', row.object_type, row.object_id, error)
            if out:
                out(f'Dropped {row.object_type} {row.object_id}: {error}')
        else:
            retry = min(row.retry_count + 1, MAX_RETRY)
            SapSyncQueue.objects.filter(pk=row.pk).update(
                retry_count=retry,
                next_retry_at=timezone.now() + _backoff(retry),
                last_error=str(error)[:2000],
            )
            counts['failed'] += 1
            if out:
                out(f'Retry #{retry} scheduled for {row.object_type} {row.object_id}')

    return counts


