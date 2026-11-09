"""Moving stock: the one place that knows about batches (F18, #68).

Callers already hold a row lock on the product and have updated
`product.current_stock`; these helpers keep the batch breakdown and the ledger
in step with it.
"""

from django.db.models import Sum

from inventory.models import StockBatch, StockTransaction
from inventory.quantities import ZERO, qty


def take_fefo(product, amount):
    """Reduce batches, earliest expiry first. Returns [(batch or None, taken)].

    `None` is the unbatched remainder (stock with no batch). Whatever the
    batches cannot cover comes from that remainder."""
    amount = qty(amount)
    parts = []
    if amount <= 0:
        return parts
    batches = sorted(
        StockBatch.objects.select_for_update().filter(product=product, quantity__gt=0),
        key=lambda b: (b.expiry_date is None, b.expiry_date, b.received_on, b.id))
    left = amount
    for batch in batches:
        if left <= 0:
            break
        taken = min(batch.quantity, left)
        batch.quantity = qty(batch.quantity - taken)
        batch.save(update_fields=['quantity'])
        parts.append((batch, qty(taken)))
        left = qty(left - taken)
    if left > 0:
        parts.append((None, left))
    return parts


def add_batch(product, quantity, batch_number='', expiry_date=None, source='receipt'):
    """Record a received batch. Goods with neither number nor expiry join the unbatched remainder."""
    quantity = qty(quantity)
    if quantity <= 0 or (not batch_number and not expiry_date):
        return None
    batch = StockBatch.objects.filter(product=product, batch_number=batch_number or '',
                                      expiry_date=expiry_date).first()
    if batch:
        batch.quantity = qty(batch.quantity + quantity)
        batch.save(update_fields=['quantity'])
        return batch
    return StockBatch.objects.create(product=product, batch_number=batch_number or '',
                                     expiry_date=expiry_date, quantity=quantity, source=source)


def record_out(product, center, ttype, amount, notes, user=None, invoice=None):
    """Write the ledger rows for stock leaving, one per batch used. Returns them.

    `invoice` ties a sale's rows to its bill, so cancelling the bill can give
    every batch back what it took."""
    rows = []
    for batch, taken in take_fefo(product, amount):
        label = f' [batch {batch.batch_number or batch.pk}' + (
            f', exp {batch.expiry_date:%d-%b-%Y}' if batch and batch.expiry_date else '') + ']' if batch else ''
        rows.append(StockTransaction.objects.create(
            product=product, center=center, transaction_type=ttype, quantity_change=-taken,
            batch=batch, invoice=invoice,
            created_by=user if getattr(user, 'is_authenticated', False) else None,
            notes=(notes + label)[:2000]))
    return rows


def record_back(product, center, ttype, amount, notes, invoice, out_types):
    """Return stock a cancelled bill took, batch by batch. Returns the new rows.

    The bill's own outgoing ledger rows say which batch each piece came from;
    every batch gets back exactly that (unless the batch row is gone, then the
    quantity joins the unbatched remainder). Whatever the ledger does not
    cover - a bill made before the ledger carried the invoice - comes back
    unbatched, as every cancel used to. The caller has locked the product and
    raises `current_stock` itself, like `record_out`'s callers."""
    amount = qty(amount)
    rows = []
    if amount <= 0:
        return rows
    left = amount
    # What the bill took from each batch, less what an earlier line of the same
    # cancel has already given back (a bill may carry the product twice).
    took = {}
    for row in (StockTransaction.objects
                .filter(invoice=invoice, product=product, transaction_type__in=out_types,
                        quantity_change__lt=0).order_by('id')):
        took[row.batch_id] = qty(took.get(row.batch_id, ZERO) + (-row.quantity_change))
    for row in StockTransaction.objects.filter(invoice=invoice, product=product,
                                               transaction_type=ttype, quantity_change__gt=0):
        if row.batch_id in took:
            took[row.batch_id] = qty(took[row.batch_id] - row.quantity_change)
    for batch_id, available in took.items():
        if left <= 0:
            break
        give = min(available, left)
        if give <= 0:
            continue
        batch = None
        if batch_id:
            batch = StockBatch.objects.select_for_update().filter(pk=batch_id).first()
            if batch is not None:
                batch.quantity = qty(batch.quantity + give)
                batch.save(update_fields=['quantity'])
        rows.append(StockTransaction.objects.create(
            product=product, center=center, transaction_type=ttype, quantity_change=give,
            batch=batch, invoice=invoice, notes=notes[:2000]))
        left = qty(left - give)
    if left > 0:
        rows.append(StockTransaction.objects.create(
            product=product, center=center, transaction_type=ttype, quantity_change=left,
            invoice=invoice, notes=notes[:2000]))
    return rows


def unbatched(product):
    """Stock of the product that belongs to no batch."""
    in_batches = StockBatch.objects.filter(product=product).aggregate(t=Sum('quantity'))['t'] or ZERO
    return qty(qty(product.current_stock) - in_batches)
