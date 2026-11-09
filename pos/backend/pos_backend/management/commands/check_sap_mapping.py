"""Does every POS centre point at things SAP really has? Read-only: nothing is written anywhere.

For each active centre: its SAP warehouse exists, its SAP branch is a business place SAP has, and the warehouse
belongs to that business place. A centre that fails here has its bills, transfers or goods receipts refused by SAP
(or posted to the wrong branch), so run this after adding or renaming a centre.

    manage.py check_sap_mapping          (exit code 1 when something is wrong)
"""
from django.core.management.base import BaseCommand, CommandError

from pos_backend import hana_reads
from salon_admin.models import Center


def problems():
    """[(centre, what is wrong)] for every active centre; raises hana_reads.HanaUnavailable if SAP cannot be read."""
    warehouses = {code: bpl for code, bpl in hana_reads.rows('SELECT "WhsCode","BPLid" FROM "OWHS"')}
    places = {pid: name for pid, name in hana_reads.rows('SELECT "BPLId","BPLName" FROM "OBPL"')}
    found = []
    for centre in Center.objects.filter(is_active=True).order_by('id'):
        code = (centre.sap_warehouse_code or '').strip()
        branch = (centre.sap_branch_name or '').strip()
        if not code:
            found.append((centre, 'has no SAP warehouse code'))
            continue
        if code not in warehouses:
            found.append((centre, f'its SAP warehouse "{code}" does not exist in SAP'))
            continue
        if not branch:
            found.append((centre, 'has no SAP branch (its documents would go to business place 1)'))
            continue
        if branch not in places.values():
            found.append((centre, f'its SAP branch "{branch}" is not a business place in SAP'))
            continue
        owner = places.get(warehouses[code])
        if owner is None:
            found.append((centre, f'warehouse {code} is not assigned to any business place in SAP (it should be in "{branch}")'))
        elif owner != branch:
            found.append((centre, f'warehouse {code} is in business place "{owner}" in SAP, but the centre says "{branch}"'))
    return found


class Command(BaseCommand):
    help = 'List POS centres whose SAP warehouse / branch does not match SAP (read-only).'

    def handle(self, *args, **options):
        try:
            found = problems()
        except hana_reads.HanaUnavailable as exc:
            raise CommandError(f'SAP could not be read: {exc}')
        for centre, what in found:
            self.stdout.write(f'  [{centre.id}] {centre.display_name or centre.center_name}: {what}')
        total = Center.objects.filter(is_active=True).count()
        self.stdout.write(f'{len(found)} of {total} active centres have a mapping problem.')
        if found:
            raise CommandError('SAP mapping problems found.')
        self.stdout.write(self.style.SUCCESS('Every centre matches SAP.'))
