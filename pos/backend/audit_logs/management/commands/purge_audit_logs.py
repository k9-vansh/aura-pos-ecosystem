"""
Trim the activity log.

SystemLog gains one row for every POST/PUT/PATCH/DELETE and nothing ever removes
them. At the traffic this business expects that is on the order of a million rows
a week, growing without limit, and the Logs screen gets slower every day it runs:
measured at 300,000 rows, the facets endpoint took 1.4s, the summary 2.4s and the
user list 1.2s. Those costs are linear in table size.

Deleting audit history is not something to do casually, so this command is
deliberately awkward in the right ways:

  * it defaults to a DRY RUN and prints what it would remove;
  * `--apply` is required to delete anything;
  * it deletes in batches, so it never holds one enormous transaction that
    blocks the tills;
  * it refuses to go below a floor of 90 days without `--force`, because in
    India the tax authorities can ask about a period long past;
  * and it can write what it removes to a JSONL file first, so "trimmed" does
    not have to mean "gone".

Suggested use, once a week, off-peak:

    python manage.py purge_audit_logs --older-than 365 --archive-to /backups/audit
    python manage.py purge_audit_logs --older-than 365 --archive-to /backups/audit --apply
"""

import datetime
import gzip
import json
import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from audit_logs.models import SystemLog

#: Below this, the command refuses without --force. Long enough to answer a
#: question about last quarter.
MINIMUM_RETENTION_DAYS = 90

#: Rows deleted per transaction. Small enough not to hold a long lock.
BATCH_SIZE = 2000


class Command(BaseCommand):
    help = 'Delete activity-log rows older than N days, optionally archiving them first.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--older-than', type=int, default=365, metavar='DAYS',
            help='Delete rows older than this many days (default: 365).')
        parser.add_argument(
            '--apply', action='store_true',
            help='Actually delete. Without this the command only reports.')
        parser.add_argument(
            '--archive-to', metavar='DIR',
            help='Write the rows to a gzipped JSONL file here before deleting.')
        parser.add_argument(
            '--force', action='store_true',
            help=f'Allow a retention window under {MINIMUM_RETENTION_DAYS} days.')
        parser.add_argument(
            '--batch-size', type=int, default=BATCH_SIZE,
            help=f'Rows per transaction (default: {BATCH_SIZE}).')

    def handle(self, *args, **options):
        days = options['older_than']
        apply_changes = options['apply']
        archive_dir = options['archive_to']
        batch_size = max(1, options['batch_size'])

        if days < MINIMUM_RETENTION_DAYS and not options['force']:
            raise CommandError(
                f'Refusing to keep less than {MINIMUM_RETENTION_DAYS} days of '
                f'activity log. An audit trail is often needed long after the '
                f'fact - a GST query can arrive months later. Pass --force if '
                f'you are certain.')

        cutoff = timezone.now() - datetime.timedelta(days=days)
        doomed = SystemLog.objects.filter(timestamp__lt=cutoff)

        total = doomed.count()
        remaining = SystemLog.objects.count() - total

        self.stdout.write(f'Cutoff        : {cutoff:%Y-%m-%d %H:%M} ({days} days)')
        self.stdout.write(f'Rows to remove: {total:,}')
        self.stdout.write(f'Rows to keep  : {remaining:,}')

        if not total:
            self.stdout.write(self.style.SUCCESS('Nothing to do.'))
            return

        oldest = doomed.order_by('timestamp').values_list('timestamp', flat=True).first()
        newest = doomed.order_by('-timestamp').values_list('timestamp', flat=True).first()
        self.stdout.write(f'Covering      : {oldest:%Y-%m-%d} to {newest:%Y-%m-%d}')

        if not apply_changes:
            self.stdout.write(self.style.WARNING(
                '\nDRY RUN - nothing was deleted. Re-run with --apply.'))
            if not archive_dir:
                self.stdout.write(
                    'Consider --archive-to <dir> so the rows are kept in a file.')
            return

        archive_path = None
        if archive_dir:
            archive_path = self._archive(doomed, archive_dir, cutoff)
            self.stdout.write(self.style.SUCCESS(f'Archived to {archive_path}'))
        else:
            self.stdout.write(self.style.WARNING(
                'No --archive-to given: these rows will be gone for good.'))

        deleted = self._delete_in_batches(doomed, batch_size)

        self.stdout.write(self.style.SUCCESS(f'\nDeleted {deleted:,} rows.'))
        self.stdout.write(f'Remaining: {SystemLog.objects.count():,}')

    # ── helpers ──────────────────────────────────────────────────────────

    def _archive(self, queryset, directory, cutoff):
        """Stream the rows to a gzipped JSONL file.

        `.iterator()` so a multi-million row archive does not have to fit in
        memory.
        """
        os.makedirs(directory, exist_ok=True)
        name = f'audit_log_before_{cutoff:%Y%m%d}.jsonl.gz'
        path = os.path.join(directory, name)

        fields = [f.name for f in SystemLog._meta.fields]
        written = 0
        with gzip.open(path, 'wt', encoding='utf-8') as handle:
            for row in queryset.order_by('id').iterator(chunk_size=1000):
                record = {}
                for field in fields:
                    value = getattr(row, field, None)
                    if isinstance(value, (datetime.datetime, datetime.date)):
                        value = value.isoformat()
                    elif value is not None and not isinstance(
                            value, (str, int, float, bool)):
                        value = str(value)
                    record[field] = value
                handle.write(json.dumps(record, ensure_ascii=False) + '\n')
                written += 1

        self.stdout.write(f'Wrote {written:,} rows to the archive.')
        return path

    def _delete_in_batches(self, queryset, batch_size):
        """Delete in chunks so no single transaction blocks the tills."""
        deleted = 0
        while True:
            batch_ids = list(
                queryset.order_by('id').values_list('id', flat=True)[:batch_size])
            if not batch_ids:
                break
            with transaction.atomic():
                count, _ = SystemLog.objects.filter(id__in=batch_ids).delete()
            deleted += count
            self.stdout.write(f'  ... {deleted:,}', ending='\r')
            self.stdout.flush()
        return deleted
