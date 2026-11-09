"""The two ends of a stock transfer: the sender's count is checked before the goods go, and the receiving
branch signs them in (counts, a note when something differs, the bill that came with them).

Counts are scanned + typed, per line. What the receiving branch accepts becomes the line's
`received_quantity`: it can be less than was sent (the difference is recorded and told to the people who
sent and approved it) but never more."""
from rest_framework.exceptions import PermissionDenied, ValidationError

BILL_TYPES = ('.pdf', '.jpg', '.jpeg', '.png', '.webp')
BILL_MAX_BYTES = 8 * 1024 * 1024


def _qty(value):
    from inventory.serializers import qty_
    return qty_(value)


def _blank(value):
    return value is None or value == ''


def counted(scanned, manual):
    """Scanned + typed, or None when nothing was counted."""
    if _blank(scanned) and _blank(manual):
        return None
    return _qty(0 if _blank(scanned) else scanned) + _qty(0 if _blank(manual) else manual)


def check_sent_counts(transfer):
    """A line that has been counted must add up to its quantity before the goods leave."""
    for item in transfer.items.select_related('product'):
        total = counted(item.scanned_quantity, item.manual_quantity)
        if total is not None and total != _qty(item.quantity):
            raise ValidationError({'detail': f'{item.product.name}: {total.normalize():f} counted but '
                                             f'{_qty(item.quantity).normalize():f} to send. Fix the count first.'})


def check_bill(upload):
    name = (getattr(upload, 'name', '') or '').lower()
    if not name.endswith(BILL_TYPES):
        raise ValidationError({'detail': 'The bill must be a PDF or a picture (jpg, png, webp).'})
    if upload.size > BILL_MAX_BYTES:
        raise ValidationError({'detail': 'The bill is too big; keep it under 8 MB.'})


def accepted_quantities(transfer, rows):
    """{item id: (quantity accepted, the row typed)} from the counts given at receiving, else those saved
    earlier; a line with no count at all is taken as arrived in full."""
    given = {}
    for row in rows or []:
        if not isinstance(row, dict) or 'id' not in row:
            raise ValidationError({'detail': 'A received line is not valid.'})
        given[row['id']] = row
    on_transfer = {item.pk: item for item in transfer.items.all()}
    if any(pk not in on_transfer for pk in given):
        raise ValidationError({'detail': 'A received line is not on this transfer.'})
    out = {}
    for pk, item in on_transfer.items():
        row = given.get(pk)
        try:
            if row:
                total = counted(row.get('scanned'), row.get('manual'))
            else:
                total = counted(item.received_scanned_quantity, item.received_manual_quantity)
        except ValueError:
            raise ValidationError({'detail': 'A count must be a number.'})
        sent = _qty(item.quantity)
        total = sent if total is None else total
        if total < 0 or total >= 1_000_000:
            raise ValidationError({'detail': 'A count must be zero or more, and under a million.'})
        if total > sent:
            raise ValidationError({'detail': f'{item.product.name}: {total.normalize():f} counted but only '
                                             f'{sent.normalize():f} was sent.'})
        out[pk] = (total, row)
    return out


def receive(transfer, user, rows, note, bill):
    """Sign a dispatched transfer in at the receiving branch. Returns the transfer."""
    from django.db import transaction
    from inventory.models import StockTransfer
    from inventory.serializers import StockTransferSerializer
    from pos_backend.scoping import can_use_center

    if not can_use_center(user, transfer.to_center_id):
        raise PermissionDenied('Only someone at the receiving branch can receive this transfer.')
    if transfer.dispatched_by_id and transfer.dispatched_by_id == user.pk and not getattr(user, 'is_superuser', False):
        raise PermissionDenied('You sent this transfer, so someone at the receiving branch has to receive it.')
    note = (note or '').strip()[:300]
    if bill is None:
        raise ValidationError({'detail': 'Attach the bill (a PDF or a picture) that came with the goods.'})
    check_bill(bill)
    with transaction.atomic():
        transfer = StockTransfer.objects.select_for_update().get(pk=transfer.pk)
        if transfer.status != 'In Transit':
            raise ValidationError({'detail': 'Only a transfer that is on its way can be received.'})
        accepted = accepted_quantities(transfer, rows)
        items = list(transfer.items.all())
        different = any(accepted[item.pk][0] != _qty(item.quantity) for item in items)
        if different and not note:
            raise ValidationError({'detail': 'Less arrived than was sent: write a note saying what is missing.'})
        for item in items:
            total, row = accepted[item.pk]
            item.received_quantity = total
            if row is not None:
                item.received_scanned_quantity = None if _blank(row.get('scanned')) else _qty(row['scanned'])
                item.received_manual_quantity = None if _blank(row.get('manual')) else _qty(row['manual'])
            item.save(update_fields=['received_quantity', 'received_scanned_quantity', 'received_manual_quantity'])
        transfer.received_note = note
        transfer.has_difference = different
        transfer.bill_file = bill
        transfer.save(update_fields=['received_note', 'has_difference', 'bill_file'])
        # The same move the status change makes: the accepted stock arrives and the request is fulfilled.
        context = {'request': type('Req', (), {'user': user})()}
        StockTransferSerializer(transfer, context=context).update(transfer, {'status': 'Completed'})
    tell_received(transfer, user)
    return StockTransfer.objects.get(pk=transfer.pk)


def tell_sent(transfer, user):
    """The receiving branch hears that goods are on the way."""
    from inventory.stock_request_flow import holders
    from notifications.service import notify

    to = transfer.to_center.display_name or transfer.to_center.center_name
    frm = transfer.from_center.display_name or transfer.from_center.center_name
    people = [u for u in holders('stock_transfers', transfer.to_center_id) if u.pk != getattr(user, 'pk', None)]
    notify('stock.transfer_sent', title=f'Stock is on its way to {to}',
           body=f'ST-{transfer.pk} left {frm} for {to}. When it arrives, open Stock Transfers, count it and sign it '
                f'in (attach the bill that came with it).',
           url='/inventory', center=transfer.to_center, users=people, data={'stock_transfer_id': transfer.pk})


def tell_received(transfer, user):
    """Whoever asked for the stock, approved it and sent it hears that it arrived."""
    from notifications.service import notify

    to = transfer.to_center.display_name or transfer.to_center.center_name
    who = (user.full_name or user.email) if user else 'The branch'
    people = [transfer.dispatched_by, transfer.created_by]
    if transfer.stock_request is not None:
        people = [transfer.stock_request.created_by, transfer.stock_request.decided_by] + people
    seen, told = set(), []
    for u in people:
        if u is not None and u.pk not in seen and u.pk != getattr(user, 'pk', None):
            seen.add(u.pk)
            told.append(u)
    body = f'{who} received ST-{transfer.pk} at {to}.'
    if transfer.has_difference:
        body += f' Less arrived than was sent: {transfer.received_note}'
    title = f'Stock received at {to}' + (' - with a difference' if transfer.has_difference else '')
    notify('stock.transfer_received', title=title, body=body, url='/inventory', center=transfer.to_center,
           users=told, data={'stock_transfer_id': transfer.pk})
