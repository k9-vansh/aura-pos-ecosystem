"""
Management command: sync_staff_transfers

Automatically resolves expired temporary staff transfers and overdue tool
tracking records. This should be run daily via a scheduled task or cron job.

Usage:
    python manage.py sync_staff_transfers

Scheduling (Windows Task Scheduler / Linux cron):
    # Linux cron — run every day at 00:05
    5 0 * * * /path/to/venv/bin/python /path/to/manage.py sync_staff_transfers

    # Windows Task Scheduler — daily at 12:05 AM
    Action: python manage.py sync_staff_transfers
    Start in: <backend directory>
"""

import logging
from django.core.management.base import BaseCommand
from staff.utils import sync_staff_transfers_and_tools

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Resolves expired temporary staff transfers (reverts center) and "
        "marks overdue tool tracker records as returned. "
        "Intended to be run as a daily scheduled task."
    )

    def handle(self, *args, **options):
        self.stdout.write(self.style.NOTICE("Running staff transfer & tool sync..."))

        try:
            sync_staff_transfers_and_tools()
            self.stdout.write(
                self.style.SUCCESS("[OK] sync_staff_transfers_and_tools completed successfully.")
            )
            logger.info("[sync_staff_transfers] Completed successfully via management command.")
        except Exception as e:
            error_msg = f"[FAIL] sync_staff_transfers_and_tools failed: {e}"
            self.stdout.write(self.style.ERROR(error_msg))
            logger.error(f"[sync_staff_transfers] {error_msg}", exc_info=True)
            raise
