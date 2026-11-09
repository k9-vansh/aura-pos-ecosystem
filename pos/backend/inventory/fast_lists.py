"""The product list, built without the serializer.

A branch's product list is thousands of rows, and the till and the stock screens load all of it. The
serializer spends about half a millisecond a row (27 fields, each looked up and converted one by one),
which was most of the time a page waited. This builds the same JSON from plain database rows.

`tests_fast_lists.py` compares it with ProductSerializer field by field, so the two cannot drift apart.
"""
from decimal import Decimal

from .models import Product, ProductLot
from .quantities import qty as qty_

_TWO = Decimal('0.01')

PRODUCT_COLUMNS = (
    'id', 'product_id_str', 'product_code', 'name', 'brand', 'category', 'sub_category', 'vendor_name',
    'price', 'mrp', 'gst_percent', 'barcode', 'sac_code', 'is_active', 'reorder_level', 'reorder_quantity',
    'current_stock', 'unit', 'pack_size', 'pack_unit', 'center_id', 'incentive', 'created_at', 'updated_at',
    'sap_sync_status', 'sap_sync_message',
)
LOT_COLUMNS = ('id', 'product_id', 'lot_number', 'net_price', 'mrp', 'expiry_date', 'manufacturing_date',
               'created_at', 'updated_at')


def _money(value, places=_TWO):
    """A decimal as DRF writes it by default: a string with the field's decimal places."""
    return None if value is None else format(Decimal(value).quantize(places), 'f')


def _iso(value):
    return None if value is None else value.isoformat()


def _lot(row):
    return {
        'id': row['id'], 'lot_number': row['lot_number'], 'net_price': _money(row['net_price']),
        'mrp': _money(row['mrp']), 'expiry_date': _iso(row['expiry_date']),
        'manufacturing_date': _iso(row['manufacturing_date']),
        'created_at': _iso(row['created_at']), 'updated_at': _iso(row['updated_at']),
        'product': row['product_id'],
    }


def aggregate_rows(queryset, limit):
    """(rows, truncated): ONE row per product across all the branches in `queryset`, with their stock added up.

    This is the "All locations" view of the Inventory screen. A product is the same product in every branch
    when it has the same code (else the same ID, else the same name); the row is the first branch's row, as
    `product_rows` gives it, with `current_stock` the total over the branches. The screen used to download
    every branch's copy of every product (hundreds of thousands of rows, cut at 10,000 so the totals were
    wrong) and add them up in the browser."""
    from django.db.models import Min, Sum, Value
    from django.db.models.functions import Coalesce, Lower, NullIf

    key = Lower(Coalesce(NullIf('product_code', Value('')), NullIf('product_id_str', Value('')), 'name'))
    groups = (queryset.order_by().annotate(_key=key).values('_key')
              .annotate(first=Min('id'), total=Sum('current_stock')))
    totals = {g['first']: g['total'] for g in groups.values('first', 'total')}
    firsts = groups.values('first')
    rows, truncated = product_rows(Product.objects.filter(pk__in=firsts).order_by('name', 'id'), limit)
    for row in rows:
        total = totals.get(row['id'])
        if total is not None:
            row['current_stock'] = float(total)
            level = int(row['reorder_level'] or 0)
            row['is_low_stock'] = level > 0 and qty_(total) <= level
    return rows, truncated


def product_rows(queryset, limit):
    """(rows, truncated): the products of `queryset` as the API sends them, at most `limit` of them."""
    products = list(queryset.prefetch_related(None).select_related(None)
                    .values(*PRODUCT_COLUMNS)[: limit + 1])
    truncated = len(products) > limit
    products = products[:limit]

    lots = {}
    ids = [p['id'] for p in products]
    # In chunks: SQLite takes at most 999 values in one "IN (...)".
    for start in range(0, len(ids), 900):
        for lot in (ProductLot.objects.filter(product_id__in=ids[start:start + 900])
                    .order_by('id').values(*LOT_COLUMNS)):
            lots.setdefault(lot['product_id'], []).append(_lot(lot))

    out = []
    for p in products:
        level = int(p['reorder_level'] or 0)
        stock = p['current_stock']
        out.append({
            'id': p['id'], 'lots': lots.get(p['id'], []),
            'current_stock': float(stock) if stock is not None else None,
            'is_low_stock': level > 0 and qty_(stock) <= level,
            'product_id_str': p['product_id_str'], 'product_code': p['product_code'], 'name': p['name'],
            'brand': p['brand'], 'category': p['category'], 'sub_category': p['sub_category'],
            'vendor_name': p['vendor_name'],
            'price': _money(p['price']), 'mrp': _money(p['mrp']), 'gst_percent': _money(p['gst_percent']),
            'barcode': p['barcode'], 'sac_code': p['sac_code'], 'is_active': p['is_active'],
            'reorder_level': p['reorder_level'], 'reorder_quantity': p['reorder_quantity'],
            'unit': p['unit'], 'pack_size': _money(p['pack_size'], Decimal('0.001')), 'pack_unit': p['pack_unit'],
            'center': p['center_id'], 'incentive': _money(p['incentive']),
            'created_at': _iso(p['created_at']), 'updated_at': _iso(p['updated_at']),
            'sap_sync_status': p['sap_sync_status'], 'sap_sync_message': p['sap_sync_message'],
        })
    return out, truncated
