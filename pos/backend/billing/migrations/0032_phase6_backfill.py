"""Phase 6 backfill: give existing records what the new rules read.

  * value cards: their terms (face value, pre-tax price paid, GST rate) and
    the branch that sold them;
  * memberships and packages: the branch that sold them;
  * cashback: each row's kind, and what is left of each credit - the balance
    is assumed spent oldest first, so the newest credits hold what remains;
    old credits never expire (they had no expiry);
  * completed bills: the customer / branch snapshot, the biller (from the
    bill's change log) and the item code and HSN/SAC of each line.
Nothing about money changes.
"""

from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations

CENT = Decimal('0.01')
STATES = {
    '01': 'Jammu and Kashmir', '02': 'Himachal Pradesh', '03': 'Punjab',
    '04': 'Chandigarh', '05': 'Uttarakhand', '06': 'Haryana', '07': 'Delhi',
    '08': 'Rajasthan', '09': 'Uttar Pradesh', '10': 'Bihar', '11': 'Sikkim',
    '12': 'Arunachal Pradesh', '13': 'Nagaland', '14': 'Manipur', '15': 'Mizoram',
    '16': 'Tripura', '17': 'Meghalaya', '18': 'Assam', '19': 'West Bengal',
    '20': 'Jharkhand', '21': 'Odisha', '22': 'Chhattisgarh', '23': 'Madhya Pradesh',
    '24': 'Gujarat', '26': 'Dadra and Nagar Haveli and Daman and Diu',
    '27': 'Maharashtra', '29': 'Karnataka', '30': 'Goa', '31': 'Lakshadweep',
    '32': 'Kerala', '33': 'Tamil Nadu', '34': 'Puducherry',
    '35': 'Andaman and Nicobar Islands', '36': 'Telangana', '37': 'Andhra Pradesh',
    '38': 'Ladakh', '97': 'Other Territory',
}


def m(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def forwards(apps, schema_editor):
    ClientValueCard = apps.get_model('clients', 'ClientValueCard')
    ClientMembership = apps.get_model('clients', 'ClientMembership')
    ClientPackage = apps.get_model('clients', 'ClientPackage')
    CashbackTransaction = apps.get_model('billing', 'CashbackTransaction')
    Invoice = apps.get_model('billing', 'Invoice')
    InvoiceItem = apps.get_model('billing', 'InvoiceItem')
    BillChangeLog = apps.get_model('billing', 'BillChangeLog')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    ServiceMaster = apps.get_model('services', 'ServiceMaster')
    Product = apps.get_model('inventory', 'Product')

    card_ct = ContentType.objects.filter(app_label='marketing', model='valuecard').first()

    # ─── Value cards ──────────────────────────────────────────────────────
    for card in ClientValueCard.objects.select_related('value_card', 'source_invoice'):
        tpl = card.value_card
        rate = m(tpl.gst_percent) if tpl.gst_percent is not None else Decimal('0')
        pre = None
        if card.source_invoice_id and card_ct is not None:
            line = InvoiceItem.objects.filter(invoice_id=card.source_invoice_id,
                                              content_type=card_ct,
                                              object_id=card.value_card_id).first()
            if line is not None and m(line.total_price) > 0:
                pre = m(m(line.total_price) / Decimal(max(int(line.quantity or 1), 1)))
        if pre is None:
            if m(tpl.pre_tax_price) > 0:
                pre = m(tpl.pre_tax_price)
            elif m(tpl.post_tax_price) > 0:
                pre = m(m(tpl.post_tax_price) * 100 / (100 + rate))
        card.face_value = m(tpl.value) if tpl.value is not None else None
        card.pre_tax_price = pre
        card.gst_percent = rate
        if card.source_invoice_id and card.center_id is None:
            card.center_id = card.source_invoice.center_id
        card.save(update_fields=['face_value', 'pre_tax_price', 'gst_percent', 'center'])

    for model in (ClientMembership, ClientPackage):
        for perk in model.objects.filter(center__isnull=True, source_invoice__isnull=False) \
                .select_related('source_invoice'):
            perk.center_id = perk.source_invoice.center_id
            perk.save(update_fields=['center'])

    # ─── Cashback ─────────────────────────────────────────────────────────
    client_ids = set(CashbackTransaction.objects.values_list('client_id', flat=True))
    for client_id in client_ids:
        rows = list(CashbackTransaction.objects.filter(client_id=client_id).order_by('-created_at', '-id'))
        balance = sum((m(r.amount) for r in rows), Decimal('0'))
        left = balance if balance > 0 else Decimal('0')
        for r in rows:
            amount = m(r.amount)
            if amount < 0:
                r.kind = 'reversal' if r.reverses_id else 'spent'
                r.remaining = Decimal('0')
            elif r.reverses_id:
                r.kind = 'reversal'
                r.remaining = Decimal('0')
            else:
                r.kind = 'earned'
                r.remaining = amount if amount <= left else left
                left -= r.remaining
            r.save(update_fields=['kind', 'remaining'])

    # ─── Completed bills ──────────────────────────────────────────────────
    for inv in Invoice.objects.exclude(status='draft').select_related('client', 'center'):
        client, center = inv.client, inv.center
        if client is not None:
            inv.customer_name = f"{client.first_name or ''} {client.last_name or ''}".strip()[:255]
            inv.customer_phone = (client.phone or '')[:30]
            inv.customer_gstin = (client.gst_number or '').strip().upper()[:15]
        if center is not None:
            inv.branch_name = (center.display_name or center.center_name or '')[:255]
            inv.branch_legal_name = (center.center_name or '')[:255]
            inv.branch_gstin = (center.gst_number or '').strip().upper()[:15]
            inv.branch_address = center.address or ''
            inv.branch_phone = (center.phone or '')[:30]
            code = inv.branch_gstin[:2]
            inv.place_of_supply = f'{code}-{STATES[code]}' if code in STATES else ''
        inv.document_type = ('bill_of_supply' if center is not None and not center.gst_enabled
                             else 'tax_invoice')
        if inv.created_by_id is None:
            log = (BillChangeLog.objects.filter(invoice=inv, user__isnull=False)
                   .order_by('created_at').first())
            if log is not None:
                inv.created_by_id = log.user_id
        inv.save(update_fields=['customer_name', 'customer_phone', 'customer_gstin', 'branch_name',
                                'branch_legal_name', 'branch_gstin', 'branch_address',
                                'branch_phone', 'place_of_supply', 'document_type', 'created_by'])

    svc_ct = ContentType.objects.filter(app_label='services', model='servicemaster').first()
    prod_ct = ContentType.objects.filter(app_label='inventory', model='product').first()
    if svc_ct is not None:
        services = {s.id: s for s in ServiceMaster.objects.all()}
        for item in InvoiceItem.objects.filter(content_type=svc_ct, item_code=''):
            svc = services.get(item.object_id)
            if svc is None:
                continue
            item.item_code = (svc.service_code or f'S{svc.id:06d}')[:100]
            item.hsn_sac = (svc.sac_code or svc.hsn_code or '999721').strip()[:20]
            item.save(update_fields=['item_code', 'hsn_sac'])
    if prod_ct is not None:
        for item in InvoiceItem.objects.filter(content_type=prod_ct, item_code=''):
            prod = Product.objects.filter(pk=item.object_id).first()
            if prod is None:
                continue
            item.item_code = (prod.product_code or f'P{prod.id:06d}')[:100]
            item.hsn_sac = (getattr(prod, 'hsn_code', None) or prod.sac_code or '').strip()[:20]
            item.save(update_fields=['item_code', 'hsn_sac'])


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0031_phase6_billing_engine'),
        ('clients', '0019_phase6_billing_engine'),
        ('services', '0009_servicebom'),
        ('inventory', '0022_purchaseorderitem_batch_number_and_more'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
