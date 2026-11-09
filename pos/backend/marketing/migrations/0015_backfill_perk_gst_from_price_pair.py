"""
Backfill gst_percent from each row's existing price pair.

`gst_percent` defaults to 18, which is the right default for a beauty service
under SAC 9997. But applying 18 to rows that already exist would change what
customers are charged for a membership they may already hold, and would make
historical invoices disagree with the catalogue they were priced from.

So every existing row keeps the rate its own price pair already implied - which
in practice is the 5% the UI forced. Nothing is repriced by this migration. New
rows get 18, and the operator can set the rate explicitly from now on.

Rows whose pair implies nothing usable (no pre-tax price, or post <= pre) are
left at 0 rather than silently taxed: they were being sold at 0% before, and
changing that here would raise a price behind the operator's back. The Marketing
screen now shows the rate, so those rows are visible and fixable.
"""

from decimal import Decimal, InvalidOperation

from django.db import migrations

# The rates Indian GST actually uses. A pair that implies something close to one
# of these is snapped to it, because an operator who typed a rounded post-tax
# price produced 17.99% or 5.01% rather than a real rate - and a tax invoice must
# not carry a rate that does not exist.
VALID_RATES = [Decimal('0'), Decimal('5'), Decimal('12'), Decimal('18'), Decimal('28')]
SNAP_TOLERANCE = Decimal('0.75')


def implied_rate(pre, post):
    """The rate implied by a pre/post pair, snapped to a real GST rate."""
    try:
        pre = Decimal(pre or 0)
        post = Decimal(post or 0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal('0.00')

    if pre <= 0 or post <= 0 or post <= pre:
        return Decimal('0.00')

    raw = ((post - pre) / pre * Decimal('100')).quantize(Decimal('0.01'))

    for rate in VALID_RATES:
        if abs(raw - rate) <= SNAP_TOLERANCE:
            return rate.quantize(Decimal('0.01'))
    return raw


def backfill(apps, schema_editor):
    for model_name in ('Membership', 'ValueCard'):
        model = apps.get_model('marketing', model_name)
        for row in model.objects.all().iterator():
            row.gst_percent = implied_rate(row.pre_tax_price, row.post_tax_price)
            row.save(update_fields=['gst_percent'])


def noop(apps, schema_editor):
    """Nothing to undo: the column goes away with the schema migration."""


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0014_perk_gst_percent'),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
