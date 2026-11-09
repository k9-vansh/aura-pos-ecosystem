"""Move the old SAP retry queue (SapSyncQueue) into the outbox (BackgroundJob).

Known rows become `sap.sync` jobs, due now.

Rows of type 'unknown' were written by old code that filed a failed journal
under args[0] - the INVOICE id - and then deleted them unprocessed. The six in
the real data (8-9 Oct 2026) are all value-card sale journals. Each is mapped
to the sold line it belongs to and held as "needs attention" for the SAP team
to check before pressing Retry; its posting key is marked as tried, so even
then SAP is asked whether the journal is already there before anything is
posted (blueprint X38 / B11-2). A row that cannot be mapped is held as it is.

The old table is left in place (nothing writes it any more) so the move can be
audited; reversing this migration deletes only the jobs it created.
"""

from django.db import migrations
from django.utils import timezone

KIND = 'sap.sync'
MARK = 'Moved from the old SAP queue'

PRIORITY = {
    'center': 10, 'client': 10, 'product': 10, 'vendor': 10,
    'invoice': 20, 'advance_je': 25, 'marketing_je': 30, 'incoming_payment': 30,
    'credit_note': 40, 'purchase_order': 40, 'stock_transfer': 40, 'stock_transaction': 50,
}


def forwards(apps, schema_editor):
    Queue = apps.get_model('pos_backend', 'SapSyncQueue')
    Job = apps.get_model('pos_backend', 'BackgroundJob')
    Posting = apps.get_model('pos_backend', 'SapPosting')
    InvoiceItem = apps.get_model('billing', 'InvoiceItem')
    now = timezone.now()

    def add(object_type, object_id, *, held, note, label='', center_id=None):
        key = f'sap:{object_type}:{object_id}'
        if Job.objects.filter(dedupe_key=key).exists():
            return
        Job.objects.create(
            kind=KIND, dedupe_key=key,
            payload={'object_type': object_type, 'object_id': object_id,
                     'center_id': center_id, 'label': label or f'{object_type} #{object_id}',
                     'migrated': True},
            status='dead' if held else 'queued', priority=PRIORITY.get(object_type, 50),
            max_attempts=10, run_after=now, finished_at=now if held else None,
            last_error=note[:4000])

    for row in Queue.objects.order_by('id'):
        when = row.created_at.strftime('%d %b %Y %H:%M') if row.created_at else 'an unknown date'
        if row.object_type != 'unknown':
            add(row.object_type, row.object_id, held=False,
                note=f'{MARK} ({when}): {row.last_error or ""}')
            continue

        items = list(InvoiceItem.objects.select_related('invoice', 'content_type').filter(
            invoice_id=row.object_id,
            content_type__app_label='marketing',
            content_type__model__in=['valuecard', 'membership', 'package']))
        if not items:
            add('unknown', row.id, held=True,
                note=(f'{MARK} ({when}): an old journal failure for record {row.object_id} that '
                      f'could not be matched to a document. Check it with the SAP team.'))
            continue
        for item in items:
            # Marked as tried: post_once asks SAP for the journal before posting.
            Posting.objects.get_or_create(key=f'je:sale:{item.id}',
                                          defaults={'status': 'pending', 'attempts': 1})
            add('marketing_je', item.id, held=True, center_id=item.invoice.center_id,
                label=f'Card / membership / package journal - bill {row.object_id}, line {item.id}',
                note=(f'{MARK} ({when}): the {item.content_type.model} sale journal for bill '
                      f'{row.object_id} never reached SAP (old code dropped it). Check SAP for it, '
                      f'then press Retry; SAP is asked again before anything is posted.'))


def backwards(apps, schema_editor):
    Job = apps.get_model('pos_backend', 'BackgroundJob')
    Job.objects.filter(kind=KIND, payload__migrated=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('pos_backend', '0004_app_settings'),
        ('billing', '0029_advancepayment_gst_amount_and_more'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
