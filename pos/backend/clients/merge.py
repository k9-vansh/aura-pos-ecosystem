"""Merge two client records into one (blueprint B5.3: the duplicate-phone groups).

Everything that points at the duplicate - bills, appointments, advance and
cashback ledgers, value cards, memberships, packages, promotion usage - is
re-pointed at the client kept. The relations are found from the model, so a
table added later is not silently left behind. The duplicate is archived, not
deleted, with a note saying where it went.
"""

from django.db import transaction

# Details copied onto the kept client only where it has none.
FILL_IF_BLANK = ('last_name', 'email', 'birthday', 'gst_number', 'center', 'phone')


class MergeError(ValueError):
    pass


def merge_clients(keep_id, drop_id):
    """Merge client `drop_id` into `keep_id`. Returns {table: rows moved}."""
    from clients.models import Client

    if int(keep_id) == int(drop_id):
        raise MergeError('Choose two different clients.')

    with transaction.atomic():
        clients = {c.pk: c for c in Client.objects.select_for_update()
                   .filter(pk__in=[keep_id, drop_id])}
        keep, drop = clients.get(int(keep_id)), clients.get(int(drop_id))
        if keep is None or drop is None:
            raise MergeError('Client not found.')

        moved = {}
        for rel in Client._meta.related_objects:
            if not (rel.one_to_many or rel.one_to_one) or rel.related_model is Client:
                continue
            name = rel.field.name
            count = rel.related_model._base_manager.filter(**{name: drop}).update(**{name: keep})
            if count:
                moved[rel.related_model._meta.label] = count

        # The duplicate gives up its phone first: the phone is unique.
        drop_phone = drop.phone
        drop.is_active = False
        drop.phone = ''
        note = f'Merged into client #{keep.pk} ({keep.full_name}).'
        drop.notes = f'{drop.notes}\n{note}'.strip() if drop.notes else note
        drop.save()

        for field in FILL_IF_BLANK:
            value = drop_phone if field == 'phone' else getattr(drop, field)
            if value and not getattr(keep, field):
                setattr(keep, field, value)
        keep.save()
    return moved
