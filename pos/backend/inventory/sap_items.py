"""SAP's item master, read from HANA in the shape the Service Layer's /Items returns it.

The three background jobs that pull items from SAP (the unit sync, the stock sync and the master
sync) used to page through /Items 100 at a time: with the stock of every warehouse inside each
page that is about 22 requests of 16 MB and a few minutes. These functions answer the same
question with one to three SELECTs, in a fraction of a second, and hand back dicts with the same
keys the Service Layer uses, so the code that reads them does not change.

Each function raises `hana_reads.HanaUnavailable` when HANA cannot answer; the caller then falls
back to the Service Layer.
"""
from pos_backend import hana_reads

_ITEM_COLUMNS = ('"ItemCode","ItemName","InvntItem","SellItem","CodeBars","ItmsGrpCod","U_BRAND","U_SUB_GR","U_TAXRT",'
                 '"ChapterID","SACEntry","InvntryUom"')
_CHUNK = 500                                   # warehouse codes per IN (...)


def _yes_no(flag):
    return 'tYES' if flag == 'Y' else 'tNO'


def _placeholders(count):
    return ','.join('?' * count)


def _warehouse_stock(warehouses, inventory_only=True):
    """{item code: [{'WarehouseCode', 'InStock'}]} for the given warehouses (one query per 500 codes)."""
    queries = []
    codes = sorted({str(w).strip() for w in warehouses if str(w).strip()})
    for start in range(0, len(codes), _CHUNK):
        chunk = codes[start:start + _CHUNK]
        sql = ('SELECT w."ItemCode", w."WhsCode", w."OnHand" FROM "OITW" w '
               'JOIN "OITM" i ON i."ItemCode" = w."ItemCode" '
               + ('WHERE i."InvntItem" = \'Y\' AND ' if inventory_only else 'WHERE ')
               + f'w."WhsCode" IN ({_placeholders(len(chunk))})')
        queries.append((sql, chunk))
    stock = {}
    for result in (hana_reads.many(queries) if queries else []):
        for code, warehouse, on_hand in result:
            stock.setdefault(code, []).append({'WarehouseCode': warehouse, 'InStock': on_hand})
    return stock


def unit_items():
    """[{'ItemCode', 'InventoryItem', 'InventoryUOM'}] for the whole item master."""
    result = hana_reads.rows('SELECT "ItemCode","InvntItem","InvntryUom" FROM "OITM" ORDER BY "ItemCode"')
    return [{'ItemCode': code, 'InventoryItem': _yes_no(inventory), 'InventoryUOM': unit}
            for code, inventory, unit in result]


def stock_items(warehouses):
    """Inventory items with their stock in `warehouses`:
    [{'ItemCode', 'InventoryItem': 'tYES', 'ItemWarehouseInfoCollection': [{'WarehouseCode', 'InStock'}]}]."""
    stock = _warehouse_stock(warehouses)
    return [{'ItemCode': code, 'InventoryItem': 'tYES', 'ItemWarehouseInfoCollection': stock[code]}
            for code in sorted(stock)]


def master_items(warehouses, price_list=None):
    """Every item with the fields the master sync reads, prices and the stock of `warehouses`."""
    price_sql = 'SELECT "ItemCode","PriceList","Price" FROM "ITM1"'
    price_params = ()
    if price_list is not None:
        price_sql += ' WHERE "PriceList" = ?'
        price_params = (int(price_list),)
    items, prices = hana_reads.many([
        (f'SELECT {_ITEM_COLUMNS} FROM "OITM" ORDER BY "ItemCode"', ()),
        (price_sql, price_params),
    ])
    stock = _warehouse_stock(warehouses, inventory_only=False)
    price_of = {}
    for code, price_list_no, price in prices:
        price_of.setdefault(code, []).append({'PriceList': price_list_no, 'Price': price})
    out = []
    for (code, name, inventory, selling, barcode, group, brand, sub_group, tax_rate,
         chapter, sac, unit) in items:
        out.append({
            'ItemCode': code, 'ItemName': name, 'InventoryItem': _yes_no(inventory), 'SalesItem': _yes_no(selling),
            'BarCode': barcode, 'ItemsGroupCode': group, 'U_BRAND': brand, 'U_SUB_GR': sub_group,
            'U_TAXRT': tax_rate, 'ChapterID': chapter, 'SACEntry': sac, 'InventoryUOM': unit,
            'ItemPrices': price_of.get(code, []), 'ItemWarehouseInfoCollection': stock.get(code, []),
        })
    return out


def stock_item_flags(item_codes):
    """{item code: True/False}: is the item an Inventory Item in SAP (OITM.InvntItem = Y)?

    SAP keeps stock only for inventory items, so only they can be moved by an Inventory Transfer. A code SAP
    does not know is left out of the answer. HANA answers in one query; the Service Layer (20 codes per
    request) is the fallback. Raises RuntimeError when neither can be read, so the posting waits and retries
    instead of sending a transfer SAP may refuse."""
    codes = sorted({str(c) for c in item_codes if c})
    if not codes:
        return {}
    try:
        flags = {}
        for start in range(0, len(codes), _CHUNK):
            chunk = codes[start:start + _CHUNK]
            for code, flag in hana_reads.rows(
                    f'SELECT "ItemCode","InvntItem" FROM "OITM" WHERE "ItemCode" IN ({_placeholders(len(chunk))})',
                    chunk):
                flags[code] = flag == 'Y'
        return flags
    except hana_reads.HanaUnavailable:
        pass

    import urllib.parse

    from pos_backend.sap_utils import SAP_URL_BASE, _send_sap_request
    flags = {}
    try:
        for start in range(0, len(codes), 20):
            wanted = ' or '.join("ItemCode eq '%s'" % c.replace("'", "''") for c in codes[start:start + 20])
            url = f"{SAP_URL_BASE}/Items?$select=ItemCode,InventoryItem&$filter={urllib.parse.quote(wanted, safe='')}"
            data = _send_sap_request('GET', url, timeout=15, headers={'Prefer': 'odata.maxpagesize=100'}).json() or {}
            for row in data.get('value') or []:
                if row.get('ItemCode') and row.get('InventoryItem') in ('tYES', 'tNO'):
                    flags[row['ItemCode']] = row['InventoryItem'] == 'tYES'
    except Exception as exc:                                        # noqa: BLE001
        raise RuntimeError(f"Could not check in SAP which items are inventory items: {str(exc)[:160]}") from exc
    return flags


BATCH_FLAGS_TTL = 600                      # the same ten minutes sap_utils.batch_managed() always kept them


def batch_key(item_code):
    """The cache entry that says whether SAP manages an item by batch (shared with sap_utils.batch_managed)."""
    import hashlib
    return 'sap_batch_managed:' + hashlib.md5(str(item_code).encode()).hexdigest()


def remember_batch_flags():
    """Read "managed by batch" (OITM.ManBtchNum) for the WHOLE item master in one query and remember every item.

    Posting a document asked the Service Layer about each item on its own (and logged in first when the session
    was cold). One HANA query answers for all 2,000-odd items, so after it no posting waits on SAP for this.
    Returns True when HANA answered; False (and says so for a minute, so a down HANA is not retried on every
    item) when it did not, and the Service Layer is asked as before."""
    from django.core.cache import cache

    if cache.get('sap_batch_flags:hana_down'):
        return False
    try:
        rows = hana_reads.rows('SELECT "ItemCode","ManBtchNum" FROM "OITM"')
    except hana_reads.HanaUnavailable:
        cache.set('sap_batch_flags:hana_down', True, 60)
        return False
    cache.set_many({batch_key(code): flag == 'Y' for code, flag in rows}, BATCH_FLAGS_TTL)
    return True


def item_groups():
    """{group number: group name}, e.g. {101: 'RETAIL'}."""
    return {number: name for number, name in hana_reads.rows('SELECT "ItmsGrpCod","ItmsGrpNam" FROM "OITB"')}
