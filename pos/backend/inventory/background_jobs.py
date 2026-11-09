"""Background jobs owned by inventory (discovered by pos_backend.jobs)."""

from pos_backend.jobs import periodic


@periodic('inventory.low_stock_digest', every=24 * 3600)
def low_stock_digest():
    """Once a day, tell the people the rule names which branches have products at or
    below their reorder level (Admin > Integrations > Stock is low)."""
    from django.db.models import F

    from inventory.models import Product
    from notifications.service import notify

    rows = (Product.objects.filter(current_stock__lte=F('reorder_level'), reorder_level__gt=0)
            .select_related('center').order_by('center_id', 'name'))
    by_center = {}
    for product in rows:
        if product.center_id:
            by_center.setdefault(product.center, []).append(product)
    for center, products in by_center.items():
        names = ', '.join(p.name for p in products[:8])
        extra = f' and {len(products) - 8} more' if len(products) > 8 else ''
        where = center.display_name or center.center_name
        notify('inventory.low_stock', title=f'Low stock at {where}',
               body=f'{len(products)} product(s) at or below the reorder level: {names}{extra}.',
               url='/inventory', center=center, data={'count': len(products)})


@periodic('inventory.sap_unit_sync', every=600)
def sap_unit_sync():
    """A unit of measure changed on an item in SAP (PCS -> ML) reaches the POS within minutes. Reads only
    item code and unit, so it is cheap; only when SAP is on."""
    from inventory.sap_unit_sync import sync_units_from_sap
    from pos_backend.sap_utils import sap_enabled

    if not sap_enabled():
        return 'off'
    return f'{sync_units_from_sap()} changed'


@periodic('inventory.sap_stock_sync', every=60)
def sap_stock_sync():
    """Stock added or removed in SAP shows up in the POS (inventory.sap_stock_sync).

    Runs every `sap.stock_sync_minutes`; only when SAP is on."""
    from django.core.cache import cache

    from inventory.sap_stock_sync import sync_stock_from_sap
    from pos_backend import app_settings
    from pos_backend.sap_utils import sap_enabled

    minutes = int(app_settings.get('sap.stock_sync_minutes'))
    if minutes <= 0 or not sap_enabled():
        return 'off'
    if cache.get('inventory:sap_stock_sync:last'):
        return 'recent'
    cache.set('inventory:sap_stock_sync:last', True, minutes * 60)
    return f'{sync_stock_from_sap()} changed'


@periodic('inventory.sap_master_sync', every=3600)
def sap_master_sync():
    """Scheduled SAP master sync (X27): the old `pull_from_sap` command, run by the
    workers instead of by hand. Off until `sap.master_sync_hours` is set; it only
    runs when SAP is on and the interval has passed since the last run."""
    from django.core.cache import cache
    from django.core.management import call_command

    from pos_backend import app_settings
    from pos_backend.sap_utils import sap_enabled

    hours = int(app_settings.get('sap.master_sync_hours'))
    if hours <= 0 or not sap_enabled():
        return 'off'
    if cache.get('inventory:sap_master_sync:last'):
        return 'recent'
    call_command('pull_from_sap')
    cache.set('inventory:sap_master_sync:last', True, hours * 3600)
    return 'ran'
