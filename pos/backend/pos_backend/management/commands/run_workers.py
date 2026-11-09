"""Run the background job worker (see pos_backend/jobs.py).

    python manage.py run_workers              # loop forever (production: systemd)
    python manage.py run_workers --once       # one batch + periodic tasks, then exit
    python manage.py run_workers --list       # show registered job kinds

Run as many worker processes as needed; each job is claimed by exactly one.
"""

from django.core.management.base import BaseCommand

from pos_backend import jobs


class Command(BaseCommand):
    help = 'Run background jobs (SAP postings, notifications, scheduled tasks).'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true', help='Process due work once and exit.')
        parser.add_argument('--interval', type=int, default=5, help='Seconds between polls (default 5).')
        parser.add_argument('--name', default=None, help='Worker name (default host:pid).')
        parser.add_argument('--list', action='store_true', help='List registered job kinds and exit.')

    def handle(self, *args, **opts):
        jobs.ensure_loaded()
        if opts['list']:
            for kind in jobs.registered_kinds():
                self.stdout.write(kind)
            for name, (every, _fn) in sorted(jobs._PERIODIC.items()):
                self.stdout.write(f'{name}  (every {every}s)')
            return

        worker = opts['name'] or jobs.default_worker_name()
        if opts['once']:
            total = {}
            while True:
                counts = jobs.process_due(worker)
                if not counts:
                    break
                for k, v in counts.items():
                    total[k] = total.get(k, 0) + v
            ran = jobs.run_periodic()
            self.stdout.write(f'jobs: {total or "none due"}; periodic run: {ran or "none due"}')
            return

        jobs.run_worker_forever(worker, interval=opts['interval'])
