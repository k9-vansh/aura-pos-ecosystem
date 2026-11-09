"""GST facts the bill needs: states, place of supply, the invoice snapshot.

Place of supply. Every POS bill is intra-state:
  * beauty, grooming and fitness services are supplied where they are actually
    performed (IGST Act s.12(4) / s.13(4)) - the branch;
  * goods sold over the counter are supplied where they are handed over - the
    branch.
So a bill carries CGST + SGST of the branch's state, never IGST, whatever the
customer's GSTIN says. Inter-state tax arises only on stock transfers (Phase 7).
"""

STATE_NAMES = {
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

# Salon services (SAC 9997 "other services": 999721 hairdressing and barbers,
# 999722 cosmetic treatment, manicure, pedicure). A service with no SAC of its
# own prints this one; it is a display default, set the real one per service.
DEFAULT_SERVICE_SAC = '999721'


def state_code(gstin):
    code = (gstin or '').strip()[:2]
    return code if code in STATE_NAMES else ''


def place_of_supply(center):
    """'07-Delhi' for a branch, from its GSTIN; '' when it has none."""
    code = state_code(getattr(center, 'gst_number', ''))
    return f'{code}-{STATE_NAMES[code]}' if code else ''


def document_type(center):
    """A branch that charges GST issues tax invoices; one that does not issues
    bills of supply (#10)."""
    from billing.models import Invoice
    if center is not None and not getattr(center, 'gst_enabled', True):
        return Invoice.DOC_BILL_OF_SUPPLY
    return Invoice.DOC_TAX_INVOICE


def snapshot_invoice(invoice):
    """Copy the customer's and the branch's details onto the bill.

    Taken when the bill is completed, so a later edit to the client or the
    branch never changes a tax invoice already issued.
    """
    client = invoice.client
    center = invoice.center
    if client is not None:
        invoice.customer_name = (client.full_name or '').strip()[:255]
        invoice.customer_phone = (client.phone or '')[:30]
        invoice.customer_gstin = (client.gst_number or '').strip().upper()[:15]
        address = (client.address or '').strip()
        if client.pin_code and client.pin_code not in address:
            address = f'{address} {client.pin_code}'.strip()
        invoice.customer_address = address
    if center is not None:
        invoice.branch_name = (center.display_name or center.center_name or '')[:255]
        invoice.branch_legal_name = (center.center_name or '')[:255]
        invoice.branch_gstin = (center.gst_number or '').strip().upper()[:15]
        address = (center.address or '').strip()
        if getattr(center, 'pin_code', '') and center.pin_code not in address:
            address = f'{address} {center.pin_code}'.strip()
        invoice.branch_address = address
        invoice.branch_phone = (center.phone or '')[:30]
        invoice.place_of_supply = place_of_supply(center)
    invoice.document_type = document_type(center)
    return ['customer_name', 'customer_phone', 'customer_gstin', 'customer_address', 'branch_name',
            'branch_legal_name', 'branch_gstin', 'branch_address', 'branch_phone',
            'place_of_supply', 'document_type']
