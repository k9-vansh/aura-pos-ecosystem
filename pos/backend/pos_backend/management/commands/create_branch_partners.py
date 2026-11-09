"""The customer and vendor partners that stand for each branch, for the tax invoices of stock sent between branches.

A transfer between two branches (two GSTINs) is an A/R invoice at the sender, to the receiving branch's CUSTOMER, and an
A/P invoice at the receiver, from the sending branch's VENDOR (pos_backend/sap_branch_transfer.py). SAP needs those two
partners per branch, each carrying the branch's GSTIN on its address so SAP applies IGST and the returns reconcile.

    manage.py create_branch_partners            checks every branch and changes nothing (the default)
    manage.py create_branch_partners --apply    creates the missing partners and puts the GSTIN on the address of
                                                partners that have none (the SAP team's "<state> Branch" ones)
    manage.py create_branch_partners --apply --only CUST000779    the same for one partner, to try it first

The partners the SAP team made are used as they are (found by their state, see sap_branch_transfer.branch_partner);
only a branch with none gets IBC-<business place id> (customer) / IBV-<business place id> (vendor). A business place
with no GSTIN in SAP is skipped and reported.
"""
import requests
from django.core.management.base import BaseCommand, CommandError

from inventory import places
from pos_backend import sap_branch_transfer, sap_utils


def _partner(code):
    """The partner as SAP holds it (its addresses and business place assignments), or None when there is none."""
    try:
        url = (f"{sap_utils.SAP_URL_BASE}/BusinessPartners('{code}')"
               '?$select=CardCode,CardName,BPAddresses,BPBranchAssignment')
        return sap_utils._send_sap_request('GET', url, timeout=15).json() or {}
    except requests.exceptions.HTTPError as exc:
        if getattr(exc.response, 'status_code', 0) == 404:
            return None
        raise


def _address_state(details):
    return details['state'] or sap_utils._BP_STATE_CODES.get(details['gstin'][:2], '')


def partner_payloads(name, details):
    """[(code, kind, rich payload, plain payload)] for one business place."""
    gstin = details['gstin']
    state = _address_state(details)
    assignments = sap_utils._bp_branch_assignments()
    out = []
    for kind, code, card_type, group_key, label in (
            ('customer', sap_branch_transfer.customer_code(details['id']), 'cCustomer', 'sap.b2b_customer_group', 'customer'),
            ('vendor', sap_branch_transfer.vendor_code(details['id']), 'cSupplier', 'sap.vendor_group', 'vendor')):
        plain = {
            'CardCode': code, 'CardName': f'{name} (own branch) - {label}'[:100], 'CardType': card_type,
            'FederalTaxID': gstin,
            'BPFiscalTaxIDCollection': [{'TaxId0': gstin[2:12], 'AddrType': 'bo_ShipTo'}],
        }
        rich = dict(plain, GroupCode=int(sap_utils._sap(group_key)), BPAddresses=sap_utils._bp_addresses(state, gstin))
        if assignments:
            rich['BPBranchAssignment'] = assignments
        out.append((code, kind, rich, plain))
    return out


def without_gstin(partner, gstin):
    """True when some address of the partner does not carry this GSTIN."""
    addresses = partner.get('BPAddresses') or []
    return not addresses or any((a.get('GSTIN') or '').strip().upper() != gstin for a in addresses)


def addresses_with_gstin(partner, gstin):
    """The partner's whole address list (SAP replaces the list on an update) with the GSTIN set on every address.
    The rows go back exactly as SAP returned them, nulls and stamps included: that is the only form its Service
    Layer accepts (a trimmed row, even one with nothing changed, is refused with -2035 "entry already exists", and
    a row without its State with -5002). Tried against the SAP test company on CUST000779."""
    rows = []
    for address in partner.get('BPAddresses') or []:
        rows.append(dict(address, GSTIN=gstin, GstType='gstRegularTDSISD'))
    return rows


def unassigned_places(partner, details, names):
    """Names of these business places (with a GSTIN) the partner is not available in, so an invoice there would be refused."""
    assigned = {a.get('BPLID') for a in partner.get('BPBranchAssignment') or [] if a.get('DisabledForBP') != 'tYES'}
    return sorted(n for n in names if details[n]['gstin'] and details[n]['id'] not in assigned)


class Command(BaseCommand):
    help = 'Check (or with --apply complete) the branch customer and vendor partners used for interstate stock transfers.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Create the missing partners and put the GSTIN on the addresses that have none.')
        parser.add_argument('--only', default='', help='With --apply: only this partner code.')

    def handle(self, *args, apply=False, only='', **options):
        details = places.business_place_details()
        if not details:
            raise CommandError('The SAP business places could not be read (is SAP switched on and HANA reachable?).')
        from salon_admin.models import Center
        in_use = {n.strip() for n in Center.objects.values_list('sap_branch_name', flat=True) if n and n.strip()}
        create, complete, skipped, problems = [], [], [], 0
        for name, d in sorted(details.items(), key=lambda kv: kv[1]['id']):
            if name not in in_use:                                  # e.g. ISD: no centre sends or receives stock there
                continue
            if not d['gstin'] or d['gstin'][:2] not in sap_utils._BP_STATE_CODES:
                skipped.append(name)
                continue
            payloads = {kind: (rich, plain) for _, kind, rich, plain in partner_payloads(name, d)}
            for kind in ('customer', 'vendor'):
                try:
                    code = sap_branch_transfer.branch_partner(kind, name, details)
                except ValueError as exc:
                    problems += 1
                    self.stdout.write(self.style.ERROR(f'  AMBIGUOUS {kind:9} {name}: {exc}'))
                    continue
                partner = _partner(code)
                if partner is None:
                    create.append((name, code, kind) + payloads[kind])
                    self.stdout.write(f'  MISSING  {code:11} {kind:9} {name}  (GSTIN {d["gstin"]})')
                    continue
                notes = []
                if without_gstin(partner, d['gstin']):
                    complete.append((name, code, kind, partner, d['gstin']))
                    notes.append('no GSTIN on its address')
                left = unassigned_places(partner, details, in_use & set(details))
                if left:
                    notes.append(f'not available in business place {", ".join(left)}')
                self.stdout.write(f'  have     {code:11} {kind:9} {name}' + (f'  <- {"; ".join(notes)}' if notes else ''))
        for name in skipped:
            self.stdout.write(self.style.WARNING(f'  skipped  {name}: no GSTIN in SAP (fill it in SAP first)'))
        if only:
            create = [c for c in create if c[1] == only]
            complete = [c for c in complete if c[1] == only]
        if not create and not complete:
            self.stdout.write(self.style.SUCCESS('Nothing to create or complete.') if not problems else
                              self.style.WARNING('Nothing the POS can fix; see the lines above.'))
            return
        if not apply:
            self.stdout.write(f'{len(create)} partner(s) missing, {len(complete)} without a GSTIN on the address. '
                              'Run with --apply to fix them in SAP.')
            return
        for name, code, kind, rich, plain in create:
            try:
                response, note = sap_utils._post_business_partner(rich, plain)
            except requests.exceptions.HTTPError as exc:
                self.stdout.write(self.style.ERROR(
                    f'  FAILED   {code}: {sap_utils._sap_error_text(getattr(exc.response, "text", ""))}'))
                continue
            ok = response.status_code in (200, 201)
            self.stdout.write((self.style.SUCCESS if ok else self.style.ERROR)(
                f'  {"created " if ok else "FAILED  "} {code} {kind} {name}{note}'))
        for name, code, kind, partner, gstin in complete:
            try:
                sap_utils._send_sap_request(
                    'PATCH', f"{sap_utils.SAP_URL_BASE}/BusinessPartners('{code}')",
                    json={'BPAddresses': addresses_with_gstin(partner, gstin)}, timeout=sap_utils._post_timeout())
                after = _partner(code) or {}
            except requests.exceptions.HTTPError as exc:
                self.stdout.write(self.style.ERROR(
                    f'  FAILED   {code}: {sap_utils._sap_error_text(getattr(exc.response, "text", ""))}'))
                continue
            if without_gstin(after, gstin):                         # SAP answered OK but the GSTIN is not there
                self.stdout.write(self.style.ERROR(f'  FAILED   {code} {kind} {name}: SAP accepted it but the address has no GSTIN'))
            else:
                self.stdout.write(self.style.SUCCESS(f'  updated  {code} {kind} {name}: GSTIN {gstin} on {len(after["BPAddresses"])} address(es)'))
