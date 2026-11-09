"""
Backfill InvoiceItem.is_redemption from the legacy description marker.

Package redemption used to be detected by looking for the emoji marker
'🎁 [Redeem]' inside the item description, in three separate files. That is now
an explicit boolean column, so historical rows need the flag set from the
marker they were written with.

Mechanical and reversible, so it belongs in a migration. The monetary repair
(service prices zeroed by the content-type typo, and the tax-inclusive ->
tax-exclusive conversion) is deliberately NOT here - it is in the
`repair_invoice_math` management command so it can be dry-run and reviewed
before it touches financial records.
"""

from django.db import migrations

REDEEM_MARKER = '\U0001F381 [Redeem]'  # "🎁 [Redeem]"


def set_is_redemption(apps, schema_editor):
    InvoiceItem = apps.get_model('billing', 'InvoiceItem')
    updated = (InvoiceItem.objects
               .filter(description__contains=REDEEM_MARKER)
               .update(is_redemption=True, is_complimentary=True))
    if updated:
        print(f'  marked {updated} historical invoice item(s) as package redemptions')


def unset_is_redemption(apps, schema_editor):
    InvoiceItem = apps.get_model('billing', 'InvoiceItem')
    InvoiceItem.objects.filter(is_redemption=True).update(is_redemption=False)


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0022_invoicenumbersequence_advancepayment_center_and_more'),
    ]

    operations = [
        migrations.RunPython(set_is_redemption, unset_is_redemption),
    ]
