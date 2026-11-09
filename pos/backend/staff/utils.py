import threading
import time
from datetime import date
from django.db import transaction
from .models import StaffMember, StaffTransfer, StaffToolTracker

_last_sync = 0.0
_last_sync_day = None
_sync_lock = threading.Lock()


def sync_if_due(max_age_seconds=300):
    """Move people for transfers that have started and return them from ones
    that have ended, without needing a cron job.

    The daily management command was the only thing that ended a temporary
    transfer, so a deployment that never scheduled it left people at the other
    branch for good. Staff and transfer lists call this; it runs at most every
    few minutes per process - and at once on the first call of a new day, so a
    transfer starting or ending today takes effect when the branch opens - and
    never breaks the request it rides on.
    """
    global _last_sync, _last_sync_day
    now = time.monotonic()
    today = date.today()
    fresh = (now - _last_sync < max_age_seconds) and _last_sync_day == today
    if fresh or not _sync_lock.acquire(blocking=False):
        return
    try:
        _last_sync = now
        _last_sync_day = today
        sync_staff_transfers_and_tools()
    except Exception:
        import logging
        logging.getLogger(__name__).exception('staff transfer sync failed')
    finally:
        _sync_lock.release()

def sync_staff_transfers_and_tools():
    """
    Applies staff transfers by their dates, and expires tool tracking periods.

      * A transfer whose start date has come moves the person to the new
        centre (a transfer made for a future date only schedules the move).
      * A temporary transfer ends AFTER its end date: on the end date itself
        the person is still at the other centre, and the next day they are
        back at the original centre.
      * Tools past their expected return date are marked returned.
    """
    today = date.today()

    with transaction.atomic():
        # 1. End temporary transfers whose end date has passed.
        expired_transfers = StaffTransfer.objects.filter(
            status='Active',
            end_date__lt=today,
            transfer_type='Temporary'
        )

        for transfer in expired_transfers:
            transfer.status = 'Completed'
            transfer.save(update_fields=['status'])

            # Revert staff center back to the original center, if they are
            # still where this transfer put them.
            staff = StaffMember.objects.select_for_update().get(pk=transfer.staff_id)
            if transfer.applied and staff.center_id == transfer.to_center_id:
                staff.center = transfer.from_center
                staff.save()

        # 2. Start scheduled transfers whose start date has come (and that
        #    have not already run their course).
        due = (StaffTransfer.objects
               .filter(status='Active', applied=False, start_date__lte=today)
               .order_by('start_date', 'id'))
        for transfer in due:
            staff = StaffMember.objects.select_for_update().get(pk=transfer.staff_id)
            if staff.center_id == transfer.from_center_id:
                staff.center = transfer.to_center
                staff.save()
                transfer.applied = True
                transfer.save(update_fields=['applied'])

        # 3. Resolve expired tools
        expired_tools = StaffToolTracker.objects.filter(
            status='Taken',
            expected_return_date__lte=today
        )

        for tool in expired_tools:
            tool.status = 'Returned'
            tool.actual_return_date = today
            tool.save()
