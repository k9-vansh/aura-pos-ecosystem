"""Background tasks owned by billing (discovered by pos_backend.jobs)."""

import logging

from django.utils import timezone

from pos_backend.jobs import periodic

logger = logging.getLogger(__name__)


@periodic('billing.expire_drafts', every=3600)
def expire_drafts():
    """Delete held bills left over from earlier days (owner decisions #42, #43).

    A draft took no money, moved no stock and has no number, so nothing is
    lost. Until branch hours exist (Phase 8) "end of day" is midnight; the
    lists already hid yesterday's drafts, which then sat in the table forever.
    """
    from billing.models import Invoice

    now = timezone.now()
    if timezone.is_aware(now):
        now = timezone.localtime(now)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    stale = Invoice.objects.filter(
        status='draft', finalized_at__isnull=True, invoice_number__isnull=True,
        created_at__lt=midnight,
    )
    count = stale.count()
    if count:
        stale.delete()
        logger.info('[Billing] Expired %s held bill(s) from earlier days.', count)
    return count


@periodic('billing.expire_cashback', every=3600)
def expire_cashback():
    """Write off cashback past its campaign's expiry (#35). Hourly, so a
    credit lapses within the hour after its last valid day; idempotent."""
    from billing import wallets

    count = wallets.expire_cashback()
    if count:
        logger.info('[Billing] Expired %s cashback credit(s).', count)
    return count


@periodic('billing.forfeit_value_cards', every=3600)
def forfeit_value_cards():
    """Forfeit the unused balance of expired value cards (F7)."""
    from billing import wallets

    count = wallets.forfeit_expired_cards()
    if count:
        logger.info('[Billing] Forfeited %s expired value card balance(s).', count)
    queue_forfeiture_postings()
    return count


def queue_forfeiture_postings():
    """Queue the SAP journal of every forfeited card not yet in SAP - once
    Settings › SAP mapping names the forfeiture income account (D9)."""
    from clients.models import ClientValueCard
    from pos_backend import app_settings
    from pos_backend.models import SapPosting
    from pos_backend.sap_utils import create_sap_valuecard_forfeiture_je, run_sap_sync_async

    if not app_settings.get('sap.account.valuecard_forfeiture_income'):
        return 0
    posted = {int(k.rsplit(':', 1)[1]) for k in SapPosting.objects.filter(
        key__startswith='je:forfeit:', status=SapPosting.DONE).values_list('key', flat=True)}
    queued = 0
    for card in ClientValueCard.objects.filter(forfeited_on__isnull=False, forfeited_amount__gt=0):
        if card.id not in posted:
            run_sap_sync_async(create_sap_valuecard_forfeiture_je, card)
            queued += 1
    return queued


# Registers the `einvoice.generate` job kind.
from billing import einvoice  # noqa: E402,F401
