from django.core.management.base import BaseCommand

from pos_backend import jobs, sap_jobs
from pos_backend.models import BackgroundJob
from pos_backend.sap_utils import sap_enabled


class Command(BaseCommand):
    help = ('Retry SAP postings now: failed and "needs attention" ones are run again '
            '(use --loop to keep working the queue as a standalone worker)')

    def add_arguments(self, parser):
        parser.add_argument('--loop', action='store_true',
                            help='Keep running as a background worker (same as run_workers)')

    def handle(self, *args, **options):
        if not sap_enabled():
            self.stdout.write(self.style.WARNING(
                'SAP is switched off (SAP_ENABLED=0 or not configured); nothing to do.'))
            return

        # Rows written to the old table before the outbox existed.
        legacy = sap_jobs.process_due(out=self.stdout.write)
        if any(legacy.values()):
            self.stdout.write(f"Old queue: {legacy}")

        if options.get('loop'):
            self.stdout.write("Starting the background worker...")
            jobs.run_worker_forever()
            return

        stuck = BackgroundJob.objects.filter(
            kind=sap_jobs.JOB_KIND, status__in=[BackgroundJob.FAILED, BackgroundJob.DEAD])
        revived = sum(jobs.revive(pk) for pk in stuck.values_list('pk', flat=True))
        counts = {}
        while True:
            batch = jobs.process_due()
            if not batch:
                break
            for status, n in batch.items():
                counts[status] = counts.get(status, 0) + n
            if set(batch) <= {BackgroundJob.FAILED, BackgroundJob.DEAD}:
                break  # what is left is waiting on its backoff
        self.stdout.write(self.style.SUCCESS(f"Retried {revived}; results: {counts or 'nothing due'}."))
