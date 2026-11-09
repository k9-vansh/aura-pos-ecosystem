"""Bring stock changed in SAP into the POS.

The POS posts every stock movement it makes to SAP, so SAP's stock for an item
and warehouse should equal the POS stock of that branch, except for movements
the POS has not posted yet. Whatever else differs was changed in SAP (a goods
receipt or issue, a count, a transfer made there), and that difference is applied
to the branch here, as a ledger row marked as already in SAP so it is never
posted back.

Only reads from SAP. A product is left alone while it has a movement from the
last few minutes, because that movement may be on its way to SAP.

SAP is read straight from its database (HANA): one query answers for every item and warehouse in about a
second, where the Service Layer needs some 22 requests of 16 MB (a few minutes). When HANA cannot be read
the Service Layer is used, as before.
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from inventory.models import Product, StockTransaction
from inventory.quantities import ZERO, qty

logger = logging.getLogger(__name__)

SETTLE = timedelta(minutes=3)     # a movement newer than this may still be posting to SAP
PAGE_FIELDS = 'ItemCode,InventoryItem,ItemWarehouseInfoCollection'


def _apply(product, diff, center):
    """Move the branch's stock by `diff` and write the ledger rows (already in SAP)."""
    from inventory.stock import take_fefo

    product.current_stock = qty(product.current_stock) + diff
    product.save(update_fields=['current_stock'])
    note = 'Stock changed in SAP (synced into the POS)'
    if diff > 0:
        StockTransaction.objects.create(
            product=product, center=center, transaction_type='SAP_SYNC', quantity_change=diff,
            notes=note, sap_sync_status=True, sap_sync_message='Came from SAP')
        return
    for batch, taken in take_fefo(product, -diff):
        StockTransaction.objects.create(
            product=product, center=center, transaction_type='SAP_SYNC', quantity_change=-taken,
            batch=batch, notes=note, sap_sync_status=True, sap_sync_message='Came from SAP')


def _sap_items(centers):
    """The items (with their stock in the branches' warehouses) as SAP's Items list gives them.

    From HANA in one query; from the Service Layer's pages when HANA cannot be read."""
    from inventory import sap_items
    from pos_backend import hana_reads
    from pos_backend.management.commands.pull_from_sap import Command
    from pos_backend.sap_utils import SAP_URL_BASE

    if hana_reads.available():
        try:
            return iter(sap_items.stock_items(list(centers)))
        except hana_reads.HanaUnavailable as exc:
            logger.warning('SAP stock sync: HANA could not be read (%s); using the Service Layer.', exc)
    return Command()._paginate(f"{SAP_URL_BASE}/Items?$select={PAGE_FIELDS}", 'ItemCode')


def sync_stock_from_sap(now=None):
    """Returns how many product rows were changed."""
    from pos_backend.management.commands.pull_from_sap import MARKETING_ITEM
    from salon_admin.models import Center

    now = now or timezone.now()
    centers = {c.sap_warehouse_code.strip(): c for c in Center.objects.filter(is_active=True)
               if (c.sap_warehouse_code or '').strip()}
    if not centers:
        return 0

    changed = 0
    page = []

    def flush():
        nonlocal changed
        if page:
            changed += _sync_page(page, centers, now)
            page.clear()

    for item in _sap_items(centers):
        code = item.get('ItemCode') or ''
        if not code or MARKETING_ITEM.match(code) or item.get('InventoryItem', 'tYES') != 'tYES':
            continue
        page.append(item)
        if len(page) >= 100:
            flush()
    flush()
    if changed:
        logger.info(f"SAP stock sync: {changed} product row(s) changed from SAP")
    return changed


def _sync_page(items, centers, now):
    codes = [i['ItemCode'] for i in items]
    products = {(p.product_code, p.center_id): p
                for p in Product.objects.filter(product_code__in=codes, center__in=centers.values())}
    if not products:
        return 0
    ids = [p.id for p in products.values()]
    pending, busy = {}, set()
    # A page of 100 items is 100 products per branch: with a hundred branches that is far more than the
    # 999 values SQLite takes in one "IN (...)" (and a crash once there are a few hundred branches).
    for start in range(0, len(ids), 900):
        part = ids[start:start + 900]
        for row in (StockTransaction.objects.filter(product_id__in=part, sap_sync_status=False)
                    .values('product_id').annotate(t=Sum('quantity_change'))):
            pending[row['product_id']] = qty(row['t'])
        busy.update(StockTransaction.objects.filter(product_id__in=part, created_at__gte=now - SETTLE)
                    .values_list('product_id', flat=True))

    changed = 0
    for item in items:
        for wh in item.get('ItemWarehouseInfoCollection') or []:
            center = centers.get((wh.get('WarehouseCode') or '').strip())
            product = products.get((item['ItemCode'], center.id)) if center else None
            if product is None or product.id in busy:
                continue
            target = max(ZERO, qty(wh.get('InStock') or 0) + pending.get(product.id, ZERO))
            if target == qty(product.current_stock):
                continue
            with transaction.atomic():
                locked = Product.objects.select_for_update().get(pk=product.pk)
                # Re-check under the lock: a sale may have landed since the page was read.
                if StockTransaction.objects.filter(product=locked, created_at__gte=now - SETTLE).exists():
                    continue
                diff = target - qty(locked.current_stock)
                if diff != 0:
                    _apply(locked, diff, center)
                    changed += 1
    return changed
