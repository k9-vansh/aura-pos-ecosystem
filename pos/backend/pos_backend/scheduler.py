"""In-process background worker, started from PosBackendConfig.ready().

Production runs workers as their own service (`manage.py run_workers`,
deploy/pos-worker.service) and sets SAP_SCHEDULER=0 on the web servers. For a
single machine, or a developer's runserver, SAP_SCHEDULER=1 (the default) runs
one worker thread inside the web process instead, so postings still go out.

History: this used to start a thread that worked the old SapSyncQueue table.
Since Phase 2 every SAP posting is a BackgroundJob (the outbox), and the
worker is the same `jobs.run_worker_forever` the service runs. Claiming is
safe with any number of workers (SKIP LOCKED / conditional UPDATE), so a web
process and the service can both run without posting anything twice.

It starts only in a serving process - never in migrate, shell, test or the
autoreloader's parent - so a management command cannot post to SAP.
"""

import logging
import os
import socket
import sys
import threading

logger = logging.getLogger(__name__)

_started = False
_lock = threading.Lock()


def _is_server_process() -> bool:
    argv = sys.argv or ['']
    if os.path.basename(argv[0]) in ('manage.py', 'django-admin', 'django-admin.py'):
        command = argv[1] if len(argv) > 1 else ''
        if command != 'runserver':
            return False  # migrate, shell, test, collectstatic, run_workers ...
        # With the autoreloader only the child process (RUN_MAIN=true) serves.
        if '--noreload' not in argv and os.environ.get('RUN_MAIN') != 'true':
            return False
    return True  # daphne / gunicorn / uvicorn / runserver child


def start_scheduler():
    global _started
    from django.conf import settings

    if not getattr(settings, 'SAP_SCHEDULER', True):
        logger.info('In-process background worker not started: SAP_SCHEDULER is off '
                    '(run `manage.py run_workers`).')
        return
    if not _is_server_process():
        return

    with _lock:
        if _started:
            return
        _started = True

    from pos_backend.jobs import run_worker_forever

    worker = f"web:{socket.gethostname()}:{os.getpid()}"[:120]
    thread = threading.Thread(target=run_worker_forever, kwargs={'worker': worker, 'interval': 5},
                              name='pos-background-worker', daemon=True)
    thread.start()
    logger.info('In-process background worker %s started.', worker)
