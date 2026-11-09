"""
Find (and optionally merge) duplicate client records.

Client phone uniqueness was enforced in the view only, with no database
constraint, so a race could create two rows for the same person at the same
center. Worse, uniqueness is scoped PER CENTER by design, so the same customer
visiting two branches is two rows with two SEPARATE advance and cashback
balances - they cannot spend at branch B what they topped up at branch A.

This command reports both situations so you can decide what to do, and can
merge exact same-center duplicates for you.

    python manage.py find_duplicate_clients
    python manage.py find_duplicate_clients --cross-center
    python manage.py find_duplicate_clients --merge-same-center --dry-run
    python manage.py find_duplicate_clients --merge-same-center --apply

Run this to completion BEFORE adding a database-level unique constraint on
(phone, center); the migration will fail while duplicates exist.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count

from pos_backend.money import D, money, ZERO


class Command(BaseCommand):
    help = 'Report duplicate client records, and optionally merge same-center ones.'

    def add_arguments(self, parser):
        parser.add_argument('--cross-center', action='store_true',
                            help='Also list the same phone number across different centers.')
        parser.add_argument('--merge-same-center', action='store_true',
                            help='Merge duplicates that share a phone AND a center.')
        parser.add_argument('--dry-run', action='store_true',
                            help='With --merge-same-center: report without writing.')
        parser.add_argument('--apply', action='store_true',
                            help='With --merge-same-center: perform the merge.')

    def handle(self, *args, **options):
        from clients.models import Client

        if options['merge_same_center'] and not (options['dry_run'] or options['apply']):
            raise CommandError('--merge-same-center requires either --dry-run or --apply.')

        self.stdout.write(self.style.MIGRATE_HEADING('\nSame phone, same center'))
        same_center = self._same_center_duplicates()
        if not same_center:
            self.stdout.write('  none found - safe to add a unique constraint on (phone, center)\n')
        else:
            for (phone, center_id), rows in same_center.items():
                self._print_group(f'phone {phone} at center {center_id}', rows)

            if options['merge_same_center']:
                self._merge(same_center, apply=options['apply'])

        if options['cross_center']:
            self.stdout.write(self.style.MIGRATE_HEADING('\nSame phone, different centers'))
            cross = self._cross_center_duplicates()
            if not cross:
                self.stdout.write('  none found\n')
            for phone, rows in cross.items():
                self._print_group(f'phone {phone}', rows)
            if cross:
                self.stdout.write(
                    '\n  These are NOT merged automatically: whether one person visiting two\n'
                    '  branches should share a single wallet is a business decision. Note that\n'
                    '  today they cannot spend a balance topped up at another branch.\n'
                )

    # ─── discovery ────────────────────────────────────────────────────────

    def _same_center_duplicates(self):
        from clients.models import Client

        keys = (
            Client.objects.values('phone', 'center_id')
            .annotate(n=Count('id')).filter(n__gt=1)
        )
        groups = {}
        for key in keys:
            rows = list(
                Client.objects
                .filter(phone=key['phone'], center_id=key['center_id'])
                .order_by('-is_active', 'id')
            )
            groups[(key['phone'], key['center_id'])] = rows
        return groups

    def _cross_center_duplicates(self):
        from clients.models import Client

        phones = (
            Client.objects.values('phone')
            .annotate(n=Count('center_id', distinct=True)).filter(n__gt=1)
        )
        groups = {}
        for entry in phones:
            rows = list(Client.objects.filter(phone=entry['phone']).order_by('id'))
            if len({r.center_id for r in rows}) > 1:
                groups[entry['phone']] = rows
        return groups

    # ─── reporting ────────────────────────────────────────────────────────

    def _print_group(self, label, rows):
        self.stdout.write(f'\n  {label}  ({len(rows)} records)')
        for row in rows:
            stats = self._activity(row)
            flag = '' if row.is_active else '  [archived]'
            self.stdout.write(
                f'    #{row.id:<7} {row.full_name[:28]:<28} center={row.center_id or "-":<5} '
                f'invoices={stats["invoices"]:<4} advance={stats["advance"]:>10} '
                f'cashback={stats["cashback"]:>9}{flag}'
            )

    @staticmethod
    def _activity(client):
        from billing.models import AdvancePayment, CashbackTransaction, Invoice
        from django.db.models import Sum

        return {
            'invoices': Invoice.objects.filter(client=client).count(),
            'advance': money(
                AdvancePayment.objects.filter(client=client)
                .aggregate(t=Sum('amount'))['t'] or 0
            ),
            'cashback': money(
                CashbackTransaction.objects.filter(client=client)
                .aggregate(t=Sum('amount'))['t'] or 0
            ),
        }

    # ─── merging ──────────────────────────────────────────────────────────

    def _merge(self, groups, apply):
        """Fold each group into its most-established record."""
        from billing.models import AdvancePayment, CashbackTransaction, Invoice
        from clients.models import (
            ClientMembership, ClientPackage, ClientValueCard,
        )
        from appointments.models import Appointment

        mode = 'APPLY' if apply else 'DRY RUN'
        self.stdout.write(self.style.MIGRATE_HEADING(f'\nMerge [{mode}]'))

        merged = 0
        for (phone, center_id), rows in groups.items():
            # Keep the record with the most invoice history; on a tie, the
            # active one, then the lowest id.
            survivor = max(
                rows,
                key=lambda r: (
                    Invoice.objects.filter(client=r).count(),
                    1 if r.is_active else 0,
                    -r.id,
                ),
            )
            losers = [r for r in rows if r.pk != survivor.pk]
            if not losers:
                continue

            self.stdout.write(
                f'  phone {phone} @ center {center_id}: '
                f'keeping #{survivor.id}, folding in {[r.id for r in losers]}'
            )

            if not apply:
                merged += len(losers)
                continue

            with transaction.atomic():
                for loser in losers:
                    Invoice.objects.filter(client=loser).update(client=survivor)
                    AdvancePayment.objects.filter(client=loser).update(client=survivor)
                    CashbackTransaction.objects.filter(client=loser).update(client=survivor)
                    ClientMembership.objects.filter(client=loser).update(client=survivor)
                    ClientPackage.objects.filter(client=loser).update(client=survivor)
                    ClientValueCard.objects.filter(client=loser).update(client=survivor)
                    Appointment.objects.filter(client=loser).update(client=survivor)

                    # Keep any detail the survivor is missing.
                    for field in ('email', 'birthday', 'gst_number', 'last_name',
                                  'notes', 'gender'):
                        if not getattr(survivor, field, None) and getattr(loser, field, None):
                            setattr(survivor, field, getattr(loser, field))
                    survivor.save()

                    # Hard-delete only after every reference has been moved.
                    loser.__class__.objects.filter(pk=loser.pk).delete()
                    merged += 1

        self.stdout.write(f'\n  {merged} record(s) {"merged" if apply else "would be merged"}')
        if not apply:
            self.stdout.write(self.style.WARNING(
                '  Nothing was written. Re-run with --apply once the plan looks right.\n'
            ))
