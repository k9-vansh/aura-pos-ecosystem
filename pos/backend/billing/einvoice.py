"""
E-invoicing for B2B bills (C5 Q4b): the IRN and signed QR from the GST
e-invoice portal (IRP).

Built ready, with no credentials (like WhatsApp, D5). A completed tax invoice
for a client with a GSTIN is queued here; the `einvoice.generate` job builds the
portal's JSON (schema 1.1) and, until the portal access is set up, records
"not configured" on the bill - nothing is sent anywhere. When your CA provides
the IRP/GSP access, the provider's client is plugged into `_submit` and the
queued bills go through. `problems()` lists what a bill still lacks (the
portal needs PIN codes and addresses the POS does not hold everywhere).
"""

import datetime
import logging
import re

from django.conf import settings

from pos_backend.jobs import RetryLater, enqueue, job
from pos_backend.money import D, money, ZERO

logger = logging.getLogger(__name__)


def configured():
    return bool(getattr(settings, 'EINVOICE_API_URL', '') and
                getattr(settings, 'EINVOICE_USERNAME', ''))


def needs_irn(invoice):
    from billing.models import Invoice
    return (bool(invoice.customer_gstin)
            and invoice.document_type == Invoice.DOC_TAX_INVOICE
            and invoice.status in Invoice.REVENUE_STATUSES)


def queue_for(invoice):
    """Mark a completed B2B tax invoice for its IRN and queue the job."""
    from billing.models import Invoice
    if not needs_irn(invoice) or invoice.irn:
        return False
    Invoice.objects.filter(pk=invoice.pk).update(einvoice_status=Invoice.EINV_PENDING)
    invoice.einvoice_status = Invoice.EINV_PENDING
    from django.db import transaction
    transaction.on_commit(lambda: enqueue('einvoice.generate', {'invoice_id': invoice.pk},
                                          dedupe_key=f'einvoice:{invoice.pk}'))
    return True


_PIN = re.compile(r'\b(\d{6})\b')


def _pin(address):
    match = _PIN.search(address or '')
    return match.group(1) if match else ''


def _lines(address):
    parts = [p.strip() for p in re.split(r'[\n,]+', address or '') if p.strip()]
    return parts


def build_payload(invoice):
    """The IRP JSON (e-invoice schema 1.1) for a saved, completed bill."""
    from pos_backend.gst import state_code

    seller_addr = _lines(invoice.branch_address)
    buyer_addr = _lines(invoice.customer_address)
    seller_state = state_code(invoice.branch_gstin)
    buyer_state = state_code(invoice.customer_gstin)
    when = invoice.finalized_at or invoice.created_at or datetime.datetime.now()

    items = []
    for n, item in enumerate(invoice.items.select_related('content_type').order_by('id'), 1):
        if item.is_redemption:
            continue
        ass = money(D(item.total_price) - D(item.bill_discount_share))
        tax = money(item.tax_amount)
        half = money(tax / 2)
        gross = money(D(item.unit_price) * D(item.quantity))
        is_service = not (item.content_type and item.content_type.app_label == 'inventory')
        items.append({
            'SlNo': str(n),
            'PrdDesc': (item.description or '')[:300],
            'IsServc': 'Y' if is_service else 'N',
            'HsnCd': item.hsn_sac or '',
            'Qty': float(item.quantity),
            'Unit': 'OTH' if is_service else 'NOS',
            'UnitPrice': float(money(item.unit_price)),
            'TotAmt': float(gross),
            'Discount': float(money(gross - ass)),
            'AssAmt': float(ass),
            'GstRt': float(money(item.card_tax_percentage if D(item.card_face) > 0
                                 else item.tax_percentage)),
            'IgstAmt': 0.0,
            'CgstAmt': float(half),
            'SgstAmt': float(money(tax - half)),
            'TotItemVal': float(money(ass + tax)),
        })

    ass_val = money(sum((D(i['AssAmt']) for i in items), ZERO))
    return {
        'Version': '1.1',
        'TranDtls': {'TaxSch': 'GST', 'SupTyp': 'B2B', 'RegRev': 'N', 'IgstOnIntra': 'N'},
        'DocDtls': {'Typ': 'INV', 'No': invoice.invoice_number or '',
                    'Dt': when.strftime('%d/%m/%Y')},
        'SellerDtls': {
            'Gstin': invoice.branch_gstin, 'LglNm': invoice.branch_legal_name or invoice.branch_name,
            'TrdNm': invoice.branch_name,
            'Addr1': (seller_addr[0] if seller_addr else '')[:100],
            'Loc': (seller_addr[-1] if seller_addr else '')[:50],
            'Pin': int(_pin(invoice.branch_address) or 0), 'Stcd': seller_state,
        },
        'BuyerDtls': {
            'Gstin': invoice.customer_gstin, 'LglNm': invoice.customer_name,
            'Pos': seller_state,
            'Addr1': (buyer_addr[0] if buyer_addr else '')[:100],
            'Loc': (buyer_addr[-1] if buyer_addr else '')[:50],
            'Pin': int(_pin(invoice.customer_address) or 0), 'Stcd': buyer_state,
        },
        'ItemList': items,
        'ValDtls': {
            'AssVal': float(ass_val),
            'CgstVal': float(money(invoice.cgst)), 'SgstVal': float(money(invoice.sgst)),
            'IgstVal': float(money(invoice.igst)),
            'RndOffAmt': float(money(invoice.rounding)),
            'TotInvVal': float(money(invoice.total_amount)),
        },
    }


def problems(payload):
    """What the portal would reject this payload for, in plain words."""
    out = []
    seller, buyer = payload['SellerDtls'], payload['BuyerDtls']
    if not seller['Gstin']:
        out.append('The branch has no GSTIN.')
    if not seller['Pin']:
        out.append("The branch address has no 6-digit PIN code.")
    if not buyer['Addr1'] or not buyer['Pin']:
        out.append("The client's address with its PIN code is missing.")
    if any(not i['HsnCd'] for i in payload['ItemList']):
        out.append('A line has no HSN/SAC code.')
    return out


def _submit(payload):
    """Send the payload to the IRP through the provider your CA chooses.

    Returns {'Irn', 'AckNo', 'AckDt', 'SignedQRCode'}. The provider's API
    (NIC direct or a GSP) is plugged in here once access exists."""
    raise RetryLater(seconds=6 * 3600, reason='E-invoice provider not set up yet.')


@job('einvoice.generate')
def generate(payload, job_row=None):
    from billing.models import Invoice

    invoice = Invoice.objects.filter(pk=(payload or {}).get('invoice_id')).first()
    if invoice is None or invoice.irn or not needs_irn(invoice):
        return
    body = build_payload(invoice)
    missing = problems(body)
    if not configured():
        Invoice.objects.filter(pk=invoice.pk).update(
            einvoice_status=Invoice.EINV_NOT_CONFIGURED,
            einvoice_error=' '.join(['E-invoice access is not set up yet.'] + missing)[:2000])
        return
    if missing:
        Invoice.objects.filter(pk=invoice.pk).update(
            einvoice_status=Invoice.EINV_FAILED, einvoice_error=' '.join(missing)[:2000])
        return
    result = _submit(body)
    ack = result.get('AckDt')
    Invoice.objects.filter(pk=invoice.pk).update(
        einvoice_status=Invoice.EINV_DONE, einvoice_error='',
        irn=result.get('Irn', ''), irn_ack_no=str(result.get('AckNo', '')),
        irn_ack_date=(datetime.datetime.fromisoformat(ack) if ack else None),
        irn_signed_qr=result.get('SignedQRCode', ''))
