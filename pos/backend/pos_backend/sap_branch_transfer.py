"""Stock moving between two branches (two GSTINs) is a supply, so SAP gets invoices, not a plain transfer.

The SAP team's rule: when stock moves between business places that have different GSTINs (in practice: a
different state), the sending branch SELLS it and the receiving branch BUYS it. IGST is worked out by SAP, from
the tax code on each line; the POS never calculates it.

    goods leave (Draft -> In Transit)   an A/R invoice at the SENDING business place, to the receiving branch's
                                        customer partner, for the quantity sent, from the sender's warehouse
    goods arrive (-> Completed)         an A/P invoice at the RECEIVING business place, from the sending branch's
                                        vendor partner, for the quantity accepted, into the receiver's warehouse,
                                        the same batches, price and tax code as the A/R invoice

Stock between two centres of the SAME business place (same GSTIN) stays a plain Inventory Transfer
(sap_utils.create_sap_stock_transfer), which hands over to `create_sap_branch_purchase` only when the two
branches differ. Where the branches are told apart is `inventory.places.supply_kind`.

Both posts are idempotent (the posting ledger + a look-up in SAP before a retry), and a transfer whose tax
treatment cannot be told (a branch without a GSTIN in SAP ...) posts nothing and says why.

Partners: each branch has one customer and one vendor that stand for it, carrying the branch's GSTIN on the address.
The ones the SAP team made ("<state> Branch", CUST0007xx / VEND0012xx) are found by their state; the ones
`manage.py create_branch_partners --apply` makes are `IBC-<business place id>` / `IBV-<business place id>`.
Shortage: the A/P invoice is for what was accepted; the difference needs a credit memo in SAP (the transfer says so).
"""
import logging
import re
from decimal import Decimal

import requests
from django.core.cache import cache

from inventory.quantities import plain as plain_qty

logger = logging.getLogger(__name__)

IGST_RATES = (0, 5, 12, 18, 28)


def customer_code(bpl_id):
    return f'IBC-{bpl_id}'


def vendor_code(bpl_id):
    return f'IBV-{bpl_id}'


_PARTNERS_KEY = 'sap_branch_partner_candidates'
_OWN_CODES = ('IBC-', 'IBV-')


def partner_candidates():
    """[(code, name, 'C'|'S', ship-to state, ship-to GSTIN)] of the active SAP partners that look like a branch (their
    name ends in "Branch", or they carry one of the codes this POS creates); None when HANA cannot be read.

    Read from HANA, kept two minutes, so a partner SAP's team has just made is found without a restart."""
    found = cache.get(_PARTNERS_KEY)
    if found is not None:
        return found
    from pos_backend import hana_reads
    try:
        rows = hana_reads.rows(
            'SELECT T0."CardCode", T0."CardName", T0."CardType", T1."State", T1."GSTRegnNo" FROM "OCRD" T0 '
            'JOIN "CRD1" T1 ON T1."CardCode" = T0."CardCode" AND T1."AdresType" = \'S\' '
            'WHERE T0."CardType" IN (\'C\', \'S\') AND T0."validFor" = \'Y\' AND T0."frozenFor" = \'N\' '
            'AND (UPPER(RTRIM(T0."CardName")) LIKE \'%BRANCH\' OR T0."CardCode" LIKE \'IBC-%\' OR T0."CardCode" LIKE \'IBV-%\')')
    except hana_reads.HanaUnavailable:
        return None
    found = [(code, name, kind, (state or '').strip(), (gstin or '').strip().upper())
             for code, name, kind, state, gstin in rows]
    cache.set(_PARTNERS_KEY, found, 120)
    return found


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())


def branch_partner(kind, branch, details):
    """The SAP code of the customer (`kind` 'customer') or vendor ('vendor') that stands for the business place
    `branch`. A partner this POS made (IBC-/IBV-) wins; then one whose address carries the branch's GSTIN; then the
    one "... Branch" partner in the branch's state (told apart by name when two business places share a state).
    With no match the POS's own code comes back, and the posting says SAP does not have it."""
    place = details[branch]
    own = (customer_code if kind == 'customer' else vendor_code)(place['id'])
    wanted = 'C' if kind == 'customer' else 'S'
    rows = [r for r in (partner_candidates() or []) if r[2] == wanted]
    if any(r[0] == own for r in rows):
        return own
    others = [r for r in rows if not r[0].startswith(_OWN_CODES)]
    by_gstin = sorted({r[0] for r in others if place['gstin'] and r[4] == place['gstin']})
    if len(by_gstin) == 1:
        return by_gstin[0]
    in_state = [r for r in others if place['state'] and r[3] == place['state']
                and r[4] in ('', place['gstin'])]                    # a GSTIN of another branch rules it out
    if sum(1 for d in details.values() if place['state'] and d['state'] == place['state']) > 1:
        in_state = [r for r in in_state if _norm(r[1]).startswith(_norm(branch))]
    codes = sorted({r[0] for r in in_state})
    if len(codes) > 1:
        raise ValueError(f'SAP has {len(codes)} active {kind}s that could stand for the branch "{branch}" '
                         f'({", ".join(codes)}). Keep one (block the others) so the POS knows which to use.')
    return codes[0] if codes else own


def _money(value):
    return None if value is None else Decimal(str(value)).quantize(Decimal('0.01'))


def _save(transfer, **fields):
    """Write only these columns, so a posting never overwrites what the receive or dispatch just saved."""
    type(transfer).objects.filter(pk=transfer.pk).update(**fields)
    for name, value in fields.items():
        setattr(transfer, name, value)


def _fail(transfer, message):
    """The posting stops here: the reason is kept on the transfer (and shown in Admin > SAP Sync), and the job
    retries later. Nothing is half-posted: a failure before the POST leaves SAP untouched."""
    logger.error('SAP branch transfer ST-%s: %s', transfer.pk, message)
    text = str(message)[:500]
    _save(transfer, sap_branch_message=text, sap_sync_status=False, sap_sync_message=text)
    return False


def _tax_code(kind, rate):
    pct = int(round(float(rate or 0)))
    if pct not in IGST_RATES:
        raise ValueError(f'GST {rate}% has no SAP tax code (IGST@ / CG+SG@ exist for {", ".join(map(str, IGST_RATES))}).')
    return f"{'IGST' if kind == 'igst' else 'CG+SG'}@{pct}"


def _require_partner(code, what, branch):
    from pos_backend.sap_utils import SAP_URL_BASE, _send_sap_request
    try:
        _send_sap_request('GET', f"{SAP_URL_BASE}/BusinessPartners('{code}')?$select=CardCode", timeout=10)
    except requests.exceptions.HTTPError as exc:
        if getattr(exc.response, 'status_code', 0) == 404:
            raise ValueError(f'SAP has no {what} "{code}" for the branch "{branch}". Create a {what} for it in SAP '
                             f'(or manage.py create_branch_partners --apply), then retry.')
        raise


def unit_price(item_code, warehouse, product):
    """What one unit is invoiced at: SAP's cost of the item in the sending warehouse (Settings > SAP mapping >
    "Value of a stock transfer between branches"), or the POS product price. A value of zero cannot be invoiced."""
    from pos_backend import hana_reads
    from pos_backend.sap_utils import _sap

    if _sap('sap.transfer_price_basis') == 'product_price':
        price, source = Decimal(str(product.price or 0)), f'the POS price of {product.name}'
        fix = 'give the product a price in the POS, or switch the value to the SAP cost in Settings > SAP mapping.'
    else:
        rows = hana_reads.rows('SELECT "AvgPrice" FROM "OITW" WHERE "ItemCode" = ? AND "WhsCode" = ?',
                               (item_code, warehouse))
        price, source = Decimal(str((rows[0][0] if rows else 0) or 0)), f'the cost of {item_code} in {warehouse}'
        fix = ('SAP has no cost for it (often a zero-cost stock layer): fix the cost in SAP, '
               'or switch the value to the POS price in Settings > SAP mapping.')
    if price <= 0:
        raise ValueError(f'There is no value to invoice {item_code} at: {source} is zero. ({fix})')
    return price.quantize(Decimal('0.0001'))


def _doc_totals(data):
    total, tax = _money(data.get('DocTotal') or 0), _money(data.get('VatSum') or 0)
    return total - tax, tax, total


def create_sap_branch_sale(transfer):
    """The A/R invoice of a transfer between branches, raised when the goods leave. True when nothing is needed
    (the same business place) or it is in SAP; False with the reason on the transfer otherwise."""
    from inventory.models import StockTransfer
    from inventory.places import business_place_details, supply_kind
    from pos_backend import sap_ledger
    from pos_backend.sap_utils import (
        SAP_URL_BASE, _local_date, _post_timeout, _resolve_bpl_id, _send_sap_request, _warehouse_code, issue_batches,
    )
    try:
        if transfer.sap_ar_doc_entry:
            return True
        kind, why = supply_kind(transfer.from_center, transfer.to_center)
        if kind == 'plain':
            _save(transfer, supply_kind='plain')
            return True
        if kind == 'unknown':
            return _fail(transfer, f'Cannot tell how this transfer is taxed: {why}. Nothing was posted.')

        details = business_place_details()
        from_branch, to_branch = transfer.from_center.sap_branch_name.strip(), transfer.to_center.sap_branch_name.strip()
        customer = branch_partner('customer', to_branch, details)
        _require_partner(customer, 'customer', to_branch)
        from_wh = _warehouse_code(transfer.from_center)

        lines = []
        for item in transfer.items.select_related('product').order_by('id'):
            quantity = item.quantity
            if quantity <= 0:
                continue
            product = item.product
            code = product.product_code or f'P{product.id:06d}'
            line = {
                'ItemCode': code, 'Quantity': plain_qty(quantity), 'WarehouseCode': from_wh,
                'UnitPrice': str(unit_price(code, from_wh, product)),
                'TaxCode': _tax_code(kind, product.gst_percent),
            }
            batches = issue_batches(code, from_wh, quantity)
            if batches:
                line['BatchNumbers'] = batches
            lines.append(line)
        if not lines:
            return True

        label = f'ST-{transfer.pk}'
        day = str(_local_date(transfer.dispatched_at or transfer.created_at))
        payload = {
            'CardCode': customer, 'DocDate': day, 'DocDueDate': day,
            'BPL_IDAssignedToInvoice': _resolve_bpl_id(from_branch), 'NumAtCard': label, 'U_POSINNO': label,
            'Comments': f'POS Stock Transfer #{transfer.pk} - tax invoice for stock sent to {to_branch}',
            'DocumentLines': lines,
        }
        reply = {}

        def _post():
            response = _send_sap_request('POST', f'{SAP_URL_BASE}/Invoices', json=payload, timeout=_post_timeout())
            if response.status_code in (200, 201):
                reply.update(response.json() or {})
                return reply.get('DocEntry') or 'posted'
            return None

        def _find():
            from pos_backend.sap_ledger import odata_filter, odata_str
            url = f"{SAP_URL_BASE}/Invoices?$select=DocEntry&$filter=" + odata_filter(f'U_POSINNO eq {odata_str(label)}')
            rows = (_send_sap_request('GET', url, timeout=10).json() or {}).get('value') or []
            return rows[0].get('DocEntry') if rows else None

        entry = sap_ledger.post_once(f'branchsale:{transfer.pk}', _post, _find)
        if not entry:
            return _fail(transfer, 'SAP did not accept the tax invoice.')
        if not reply.get('DocNum'):          # found in SAP after a lost reply: read its numbers
            reply = _send_sap_request(
                'GET', f'{SAP_URL_BASE}/Invoices({int(entry)})?$select=DocNum,DocTotal,VatSum', timeout=10).json() or {}
        taxable, tax, total = _doc_totals(reply)
        _save(transfer, supply_kind=kind, sap_ar_doc_entry=int(entry), sap_ar_doc_num=reply.get('DocNum'),
              sap_taxable_value=taxable, sap_tax_amount=tax, sap_invoice_total=total,
              sap_branch_message=f'Tax invoice {reply.get("DocNum")} posted in SAP: taxable {taxable}, '
                                 f'{"IGST" if kind == "igst" else "CGST+SGST"} {tax}, total {total}.')
        return True
    except requests.exceptions.HTTPError as exc:
        from pos_backend.sap_utils import _sap_error_text
        return _fail(transfer, f'HTTP {exc.response.status_code}: {_sap_error_text(getattr(exc.response, "text", ""))}')
    except Exception as exc:                                        # noqa: BLE001
        return _fail(transfer, exc)


def _take_batches(batches, accepted):
    """The invoiced batches cut down to the quantity accepted (a shortage comes off the last batches)."""
    left = Decimal(str(accepted))
    out = []
    for batch in batches or []:
        take = min(Decimal(str(batch.get('Quantity') or 0)), left)
        if take > 0 and batch.get('BatchNumber'):
            out.append({'BatchNumber': batch['BatchNumber'], 'Quantity': float(take)})
            left -= take
        if left <= 0:
            break
    return out


def create_sap_branch_purchase(transfer):
    """The A/P invoice of a transfer between branches, raised when the goods arrive, for what was accepted.
    It copies price, tax code and batches from the A/R invoice SAP holds, so the two always agree."""
    from inventory.places import business_place_details
    from pos_backend import sap_ledger
    from pos_backend.sap_utils import (
        SAP_URL_BASE, _find_by_comments, _local_date, _post_timeout, _resolve_bpl_id, _send_sap_request, _warehouse_code,
    )
    try:
        if transfer.sap_ap_doc_entry:
            return True
        if not transfer.sap_ar_doc_entry:
            return _fail(transfer, 'Waiting for the tax invoice (A/R) of this transfer to reach SAP.')
        details = business_place_details()
        from_branch, to_branch = transfer.from_center.sap_branch_name.strip(), transfer.to_center.sap_branch_name.strip()
        vendor = branch_partner('vendor', from_branch, details)
        _require_partner(vendor, 'vendor', from_branch)
        to_wh = _warehouse_code(transfer.to_center)

        invoice = _send_sap_request(
            'GET', f'{SAP_URL_BASE}/Invoices({int(transfer.sap_ar_doc_entry)})?$select=DocNum,DocumentLines',
            timeout=15).json() or {}
        invoiced = {line['ItemCode']: line for line in invoice.get('DocumentLines') or []}
        lines, short = [], []
        for item in transfer.items.select_related('product').order_by('id'):
            code = item.product.product_code or f'P{item.product.id:06d}'
            accepted = item.quantity if item.received_quantity is None else item.received_quantity
            sent_line = invoiced.get(code)
            if sent_line is None:
                if accepted > 0:
                    raise ValueError(f'{code} is not on the tax invoice {invoice.get("DocNum")} in SAP.')
                continue
            if accepted < item.quantity:
                short.append(f'{code} {plain_qty(item.quantity - accepted)}')
            if accepted <= 0:
                continue
            line = {'ItemCode': code, 'Quantity': plain_qty(accepted), 'WarehouseCode': to_wh,
                    'UnitPrice': sent_line.get('UnitPrice'), 'TaxCode': sent_line.get('TaxCode')}
            batches = _take_batches(sent_line.get('BatchNumbers'), accepted)
            if batches:
                line['BatchNumbers'] = batches
            lines.append(line)
        if not lines:
            _save(transfer, sap_branch_message='Nothing was accepted, so no purchase invoice was needed.')
            return True

        comment = f'POS Stock Transfer #{transfer.pk} - purchase invoice for stock received from {from_branch}'
        day = str(_local_date(transfer.received_at or transfer.created_at))
        payload = {
            'CardCode': vendor, 'DocDate': day, 'DocDueDate': day,
            'BPL_IDAssignedToInvoice': _resolve_bpl_id(to_branch), 'NumAtCard': str(invoice.get('DocNum')),
            'Comments': comment, 'DocumentLines': lines,
        }
        reply = {}

        def _post():
            response = _send_sap_request('POST', f'{SAP_URL_BASE}/PurchaseInvoices', json=payload, timeout=_post_timeout())
            if response.status_code in (200, 201):
                reply.update(response.json() or {})
                return reply.get('DocEntry') or 'posted'
            return None

        entry = sap_ledger.post_once(f'branchpurchase:{transfer.pk}', _post,
                                     lambda: _find_by_comments('PurchaseInvoices', comment))
        if not entry:
            return _fail(transfer, 'SAP did not accept the purchase invoice.')
        if not reply.get('DocNum'):
            reply = _send_sap_request('GET', f'{SAP_URL_BASE}/PurchaseInvoices({int(entry)})?$select=DocNum',
                                      timeout=10).json() or {}
        message = f'Tax invoice {invoice.get("DocNum")} (sent) and purchase invoice {reply.get("DocNum")} (received) are in SAP.'
        if short:
            message += (f' Less arrived than was invoiced ({", ".join(short)}): raise a credit memo in SAP for the '
                        f'difference.')
        _save(transfer, sap_ap_doc_entry=int(entry), sap_ap_doc_num=reply.get('DocNum'), sap_branch_message=message,
              sap_sync_status=True, sap_sync_message='Success (tax invoice + purchase invoice)')
        return True
    except requests.exceptions.HTTPError as exc:
        from pos_backend.sap_utils import _sap_error_text
        return _fail(transfer, f'HTTP {exc.response.status_code}: {_sap_error_text(getattr(exc.response, "text", ""))}')
    except Exception as exc:                                        # noqa: BLE001
        return _fail(transfer, exc)
