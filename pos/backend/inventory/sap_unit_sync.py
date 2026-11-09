"""Keep each product's unit of measure the one SAP holds.

SAP's item master is where a product's unit lives (PCS, ML, ...). A unit changed there must show up in
the POS without anyone pulling the whole catalogue: this reads only item code and unit (a few pages) and
corrects the POS products that differ. Only reads from SAP.
"""
import logging

from inventory.models import Product

logger = logging.getLogger(__name__)


def _sap_items():
    """Every item's code and unit: from HANA in one query, else from the Service Layer's pages."""
    from inventory import sap_items
    from pos_backend import hana_reads
    from pos_backend.management.commands.pull_from_sap import Command
    from pos_backend.sap_utils import SAP_URL_BASE

    if hana_reads.available():
        try:
            return iter(sap_items.unit_items())
        except hana_reads.HanaUnavailable as exc:
            logger.warning('SAP unit sync: HANA could not be read (%s); using the Service Layer.', exc)
    return Command()._paginate(f"{SAP_URL_BASE}/Items?$select=ItemCode,InventoryItem,InventoryUOM", 'ItemCode')


def sync_units_from_sap():
    """Returns how many product rows changed unit."""
    from pos_backend.management.commands.pull_from_sap import MARKETING_ITEM
    from services import units

    wanted = {}
    for item in _sap_items():
        code = item.get('ItemCode') or ''
        if not code or MARKETING_ITEM.match(code) or item.get('InventoryItem', 'tYES') != 'tYES':
            continue
        wanted[code] = units.norm(item.get('InventoryUOM'))[:20] or 'pcs'

    changed = 0
    by_unit = {}
    for code, unit in wanted.items():
        by_unit.setdefault(unit, []).append(code)
    for unit, codes in by_unit.items():
        for start in range(0, len(codes), 900):
            # A "1 pcs holds 250 ml" note means nothing once the unit itself changes, so it goes with it.
            changed += (Product.objects.filter(product_code__in=codes[start:start + 900]).exclude(unit=unit)
                        .update(unit=unit, pack_size=None, pack_unit=''))
    if changed:
        logger.info('SAP unit sync: %d product row(s) changed unit', changed)
    return changed
