"""
The billing engine: one computation for the quote and for the saved bill.

The till used to work out offers, membership discounts and the value-card
"discount" itself and send the result; the server could only check it, and the
two disagreed (the double coupon discount X3, the membership cap applied after
the payments were written X10, the float value-card scaling X11). Now the till
sends WHAT the client is using - a promotion id, a membership id, a value card
id - and this module works out what that is worth:

  1. every line is priced against the catalogue (unchanged rules: list price,
     price change kept and logged, products' GST inside the MRP, the advance
     line untaxed);
  2. ONE offer - a promotion or a membership - discounts the services, on their
     pre-tax value (#29, #30, D1);
  3. or a value card covers services: each rupee of face value buys
     pre-tax/face rupees of service, taxed at the card's rate (F5, F6), and
     the bill takes no other discount (D4, D4b);
  4. tax per line, CGST/SGST halves, rounding to the rupee (#50).

`InvoiceSerializer` and the `quote` endpoint both call `price_bill`, so what the
screen shows is what gets saved.
"""

import datetime

from rest_framework import serializers

from pos_backend.money import D, HUNDRED, money, rupees, split, ZERO
from billing import pricing

CT_ADVANCE = 'billing.advancepayment'

# Limits that keep a bill inside what its columns can hold (10 digits, 2 after the point).
MAX_LINE_QUANTITY = 100_000
MAX_BILL_AMOUNT = D('99999999.99')

# What a bill line may be.
SELLABLE_TYPES = frozenset({
    pricing.CT_SERVICE, pricing.CT_PRODUCT, pricing.CT_MEMBERSHIP,
    pricing.CT_PACKAGE, pricing.CT_VALUE_CARD, CT_ADVANCE,
})


def _fail(field, message):
    raise serializers.ValidationError({field: message})


def _is_redemption_payload(item_data):
    """Package redemption line? The explicit flag first; the legacy emoji
    marker in the description for clients not yet updated."""
    from billing.models import REDEEM_MARKER
    if item_data.get('is_redemption') is not None:
        return bool(item_data.get('is_redemption'))
    return REDEEM_MARKER in (item_data.get('description') or '')


class PricedLine:
    """One fully-resolved bill line, ready to be stored."""

    __slots__ = ('index', 'raw', 'content_type', 'object_id', 'description',
                 'unit_price', 'discount', 'manager_discount', 'quantity',
                 'tax_percentage', 'tax_amount', 'total_price', 'is_redemption',
                 'staff', 'staff_members', 'catalog', 'notes', 'ct_label', 'staff_allocations',
                 'item_code', 'hsn_sac', 'bill_discount_share', 'recipe_choices',
                 'value_card', 'card_face', 'card_benefit', 'card_taxable', 'card_tax',
                 'card_tax_percentage')

    def __init__(self, **kw):
        for slot in self.__slots__:
            setattr(self, slot, kw.get(slot))
        for slot in ('bill_discount_share', 'card_face', 'card_benefit', 'card_taxable',
                     'card_tax', 'card_tax_percentage'):
            if getattr(self, slot) is None:
                setattr(self, slot, ZERO)

    @property
    def gross(self):
        return money(D(self.unit_price) * D(self.quantity))

    @property
    def is_service(self):
        return self.ct_label == pricing.CT_SERVICE and not self.is_redemption

    def service_name(self):
        obj = getattr(self.catalog, 'obj', None)
        name = getattr(obj, 'name', None) or self.description or ''
        return str(name).strip().lower()


class Quote:
    """The priced bill."""

    def __init__(self):
        self.lines = []
        self.subtotal = ZERO
        self.discount = ZERO
        self.cgst = ZERO
        self.sgst = ZERO
        self.igst = ZERO
        self.tax_total = ZERO
        self.rounding = ZERO
        self.total_amount = ZERO
        self.audit_notes = []
        self.promotion = None
        self.membership = None
        self.offer_discount = ZERO
        self.value_card = None
        self.card_face = ZERO
        self.card_settled = ZERO

    @property
    def payable(self):
        """What is still to be paid after the value card."""
        return money(self.total_amount - self.card_settled)

    def tax_breakdown(self):
        """Taxable value and tax per GST rate, for the receipt."""
        rows = {}

        def add(rate, taxable, tax):
            rate = D(rate)
            if taxable <= 0 and tax <= 0:
                return
            row = rows.setdefault(rate, {'taxable': ZERO, 'tax': ZERO})
            row['taxable'] += taxable
            row['tax'] += tax

        for line in self.lines:
            if line.is_redemption:
                continue
            if D(line.card_face) > 0:
                add(line.card_tax_percentage, D(line.card_taxable), D(line.card_tax))
            rest = money(D(line.total_price) - D(line.card_taxable) - D(line.bill_discount_share))
            rest_tax = money(D(line.tax_amount) - D(line.card_tax))
            add(line.tax_percentage if rest_tax > 0 else ZERO, rest, rest_tax)
        return breakdown_rows(rows)

    def as_dict(self):
        card = self.value_card
        return {
            'subtotal': str(self.subtotal),
            'discount': str(self.discount),
            'cgst': str(self.cgst),
            'sgst': str(self.sgst),
            'igst': str(self.igst),
            'tax_total': str(self.tax_total),
            'rounding': str(self.rounding),
            'total_amount': str(self.total_amount),
            'payable': str(self.payable),
            'offer': ({
                'kind': 'membership' if self.membership is not None else 'promotion',
                'id': (self.membership or self.promotion).id,
                'name': (self.membership.membership.name if self.membership is not None
                         else self.promotion.name),
                'discount': str(self.offer_discount),
            } if (self.membership is not None or self.promotion is not None) else None),
            'value_card': ({
                'id': card.id,
                'number': card.number,
                'title': getattr(card.value_card, 'title', 'Value Card'),
                'face_used': str(self.card_face),
                'balance_before': str(money(card.balance)),
                'balance_after': str(money(D(card.balance) - self.card_face)),
                'settled': str(self.card_settled),
            } if card is not None else None),
            'tax_breakdown': self.tax_breakdown(),
            'lines': [{
                'index': l.index,
                'description': l.description,
                'item_code': l.item_code,
                'hsn_sac': l.hsn_sac,
                'quantity': l.quantity,
                'unit_price': str(l.unit_price),
                'discount': str(l.discount),
                'bill_discount_share': str(l.bill_discount_share),
                'taxable': str(money(D(l.total_price) - D(l.bill_discount_share))),
                'tax_percentage': str(l.tax_percentage),
                'tax_amount': str(l.tax_amount),
                'total_price': str(l.total_price),
                'card_face': str(l.card_face),
                'card_benefit': str(l.card_benefit),
                'card_taxable': str(l.card_taxable),
                'card_tax': str(l.card_tax),
                'card_tax_percentage': str(l.card_tax_percentage),
            } for l in self.lines],
        }


def breakdown_rows(rows):
    out = []
    for rate in sorted(rows):
        taxable = money(rows[rate]['taxable'])
        tax = money(rows[rate]['tax'])
        halves = split(tax, 2) if tax > 0 else [ZERO, ZERO]
        out.append({'rate': str(money(rate)), 'taxable': str(taxable), 'tax': str(tax),
                    'cgst': str(halves[0]), 'sgst': str(halves[1])})
    return out


def stored_tax_breakdown(invoice):
    """The receipt's per-rate table for a saved bill."""
    rows = {}
    for item in invoice.items.all():
        if item.is_redemption:
            continue
        if D(item.card_face) > 0:
            row = rows.setdefault(D(item.card_tax_percentage), {'taxable': ZERO, 'tax': ZERO})
            row['taxable'] += D(item.card_taxable)
            row['tax'] += D(item.card_tax)
        rest = money(D(item.total_price) - D(item.card_taxable) - D(item.bill_discount_share))
        rest_tax = money(D(item.tax_amount) - D(item.card_tax))
        if rest <= 0 and rest_tax <= 0:
            continue
        rate = D(item.tax_percentage) if rest_tax > 0 else ZERO
        row = rows.setdefault(rate, {'taxable': ZERO, 'tax': ZERO})
        row['taxable'] += rest
        row['tax'] += rest_tax
    return breakdown_rows(rows)


# ─── Lines ──────────────────────────────────────────────────────────────────

def _snapshot_codes(catalog, label):
    """Item code and HSN/SAC printed on the bill."""
    from pos_backend.gst import DEFAULT_SERVICE_SAC
    obj = getattr(catalog, 'obj', None)
    if obj is None:
        return '', ''
    if label == pricing.CT_SERVICE:
        code = obj.service_code or f'S{obj.id:06d}'
        sac = (obj.sac_code or obj.hsn_code or DEFAULT_SERVICE_SAC or '').strip()
        return code[:100], sac[:20]
    if label == pricing.CT_PRODUCT:
        code = obj.product_code or f'P{obj.id:06d}'
        hsn = (getattr(obj, 'hsn_code', None) or getattr(obj, 'sac_code', None) or '').strip()
        return code[:100], hsn[:20]
    return '', ''


def price_lines(center, items_data, raw_items, *, allocations, strict_recipe=False):
    """Price every line against the catalogue. `allocations(idx, item)`
    returns the cleaned staff split for a line."""
    from pos_backend import app_settings

    lines = []
    audit_notes = []
    audit_threshold = D(app_settings.get('billing.discount_audit_percent', center=center))
    gst_on = center is None or getattr(center, 'gst_enabled', True)

    for idx, item_data in enumerate(items_data):
        raw = raw_items[idx] if idx < len(raw_items) and isinstance(raw_items[idx], dict) else {}

        raw_qty = item_data.get('quantity', None)
        # Decimal('0') is falsy, so `D(x) or 1` silently turned 0 into 1.
        quantity = 1 if raw_qty in (None, '') else int(D(raw_qty))
        if quantity <= 0:
            _fail('items', f'Line {idx + 1}: quantity must be greater than zero.')
        if quantity > MAX_LINE_QUANTITY:
            _fail('items', f'Line {idx + 1}: quantity cannot be more than {MAX_LINE_QUANTITY:,}.')

        ct_value = item_data.get('content_type')
        ct_label = ct_value if isinstance(ct_value, str) else pricing.content_type_label(ct_value)
        object_id = item_data.get('object_id')
        is_redemption = _is_redemption_payload({**raw, **item_data})

        catalog = pricing.resolve(ct_label, object_id, center=center)

        # A line must be something the till sells, and a catalogue line must
        # point at a record that exists. A made-up object id or another table
        # (a user, say) used to be saved and completed as PAID with no tax.
        known_label = pricing.content_type_label(pricing.get_content_type(ct_label)) if ct_label else None
        if known_label not in SELLABLE_TYPES:
            _fail('items', f'Line {idx + 1}: this is not something that can be sold.')
        if (known_label != CT_ADVANCE and not catalog.resolved
                and (known_label != pricing.CT_PACKAGE or object_id)):
            _fail('items', f'Line {idx + 1}: that item no longer exists. Remove it and add it again.')

        # Only what this branch sells. A package redemption is exempt: the
        # client already paid for that service when they bought the package.
        if catalog.resolved and not is_redemption:
            if catalog.unavailable:
                _fail('items', f'Line {idx + 1}: {catalog.unavailable} Remove it from the bill.')
            if catalog.no_mrp and not app_settings.get('billing.sell_products_without_mrp'):
                _fail('items', f'Line {idx + 1}: "{getattr(catalog.obj, "name", "This product")}" has '
                               'no MRP, so it cannot be billed. Set its MRP first.')

        requested_price = D(item_data.get('unit_price', 0))
        requested_discount = D(item_data.get('discount', 0))
        if requested_discount < 0:
            _fail('items', 'Item discount cannot be negative.')

        unit_price, discount, note = pricing.reconcile_line_price(
            catalog, requested_price, requested_discount,
            catalog.label or ct_label, quantity=quantity,
        )
        if note:
            audit_notes.append(f'Line {idx + 1}: {note}')

        # manager_discount is a BREAKDOWN of discount, never additive.
        manager_discount = money(max(D(item_data.get('manager_discount', 0)), ZERO))
        if manager_discount > discount:
            manager_discount = discount

        gross = unit_price * D(quantity)
        if discount > gross:
            _fail('items', f'Line {idx + 1}: discount cannot exceed the line value.')

        line_pct = pricing.discount_percent(unit_price, quantity, discount)
        if discount > 0 and line_pct >= audit_threshold:
            audit_notes.append(f'Line {idx + 1}: {line_pct}% discount '
                               f'(Rs {discount} off Rs {money(gross)}).')

        label = catalog.label or ct_label
        if is_redemption:
            # Redeemed from a package: zero value, zero tax; the quantity
            # still depletes the client's entitlement.
            taxable = ZERO
            tax_pct = ZERO
        else:
            taxable = money(gross - discount)
            tax_pct = (catalog.tax_percentage if catalog.resolved
                       else D(raw.get('tax_percentage', 0)))
            if label == CT_ADVANCE and not app_settings.get('billing.gst_on_advance'):
                # An advance is money held for the client: no GST when it is
                # paid in, the normal GST when it is spent (#34, F8). The SAP
                # team's scenarios take the GST at receipt instead; that is
                # the 'Charge GST when an advance is taken' setting.
                tax_pct = ZERO
            if not gst_on:
                tax_pct = ZERO
            # A product's MRP already contains its GST (F22): the line is
            # stored at its taxable value so tax is added back exactly once.
            if catalog.tax_inclusive and tax_pct > 0:
                taxable = money(taxable * HUNDRED / (HUNDRED + tax_pct))

        item_code, hsn_sac = _snapshot_codes(catalog, label)
        # A service with a recipe the POS owns: check what the till chose (an option that
        # is not on the recipe, a quantity that is not a number). When the bill is being
        # completed a required choice left empty is refused too.
        recipe_choices = {}
        if label == pricing.CT_SERVICE and catalog.resolved:
            from services import recipes
            if recipes.uses_recipe(catalog.obj):
                recipe_choices = recipes.clean_selection(raw.get('recipe_choices'))
                recipes.resolve(catalog.obj, center, quantity, recipe_choices, strict=strict_recipe)
        lines.append(PricedLine(
            index=idx, raw=raw, ct_label=label,
            content_type=pricing.get_content_type(label),
            object_id=object_id,
            description=item_data.get('description') or (catalog.obj and str(catalog.obj)) or 'Item',
            unit_price=unit_price, discount=discount, manager_discount=manager_discount,
            quantity=quantity, tax_percentage=tax_pct, tax_amount=ZERO,
            total_price=taxable, is_redemption=is_redemption,
            # The line's main person: the one named, else the first of those listed, so
            # reports that group by `staff` (Staff Performance) are not empty for a
            # client that only sends `staff_members`.
            staff=item_data.get('staff') or (list(item_data.get('staff_members') or [None])[0]),
            staff_members=item_data.get('staff_members') or [],
            staff_allocations=allocations(idx, item_data),
            catalog=catalog, notes=note,
            item_code=item_code, hsn_sac=hsn_sac, recipe_choices=recipe_choices,
        ))
    return lines, audit_notes


# ─── Offers ─────────────────────────────────────────────────────────────────

def _membership_for(client, membership_id, center, today):
    from clients.models import ClientMembership
    if client is None:
        _fail('membership_id', 'A membership needs the client on the bill.')
    cm = (ClientMembership.objects.select_related('membership', 'source_invoice')
          .filter(id=membership_id, client=client, is_active=True).first())
    if cm is None:
        _fail('membership_id', 'This membership is not active for this client.')
    if cm.expiry_date and cm.expiry_date < today:
        _fail('membership_id', f'This membership expired on {cm.expiry_date:%d %b %Y}.')
    if cm.start_date and cm.start_date > today:
        _fail('membership_id', f'This membership starts on {cm.start_date:%d %b %Y}.')
    _check_selling_branch(cm, center, 'membership_id', 'membership')
    return cm


def perk_branch_id(perk):
    """The branch that sold a membership, package or value card."""
    if getattr(perk, 'center_id', None):
        return perk.center_id
    source = getattr(perk, 'source_invoice', None)
    return getattr(source, 'center_id', None)


def _check_selling_branch(perk, center, field, noun):
    from pos_backend import app_settings
    if not app_settings.get('perks.selling_branch_only'):
        return
    sold_at = perk_branch_id(perk)
    if sold_at and center is not None and sold_at != center.id:
        from salon_admin.models import Center
        where = Center.all_objects.filter(pk=sold_at).first()
        _fail(field, f'This {noun} can be used only at {where or "the branch that sold it"}.')


def _apportion(discount, lines):
    """Spread a bill discount over `lines` by their value, a share per line.

    Each share is rounded to the paisa on its own - the till does the same, so
    the screen and the bill agree to the rupee (tests_till_conformance)."""
    base = money(sum((D(l.total_price) for l in lines), ZERO))
    for l in lines:
        if base > 0 and discount > 0 and D(l.total_price) > 0:
            l.bill_discount_share = money(discount * D(l.total_price) / base)
        else:
            l.bill_discount_share = ZERO


# ─── Value card ─────────────────────────────────────────────────────────────

def _cover_with_card(quote, client, center, card_id, today, gst_on):
    from clients.models import ClientValueCard

    if client is None:
        _fail('value_card_id', 'A value card can only be used on a bill that has a customer.')
    card = (ClientValueCard.objects.select_related('value_card', 'source_invoice')
            .filter(id=card_id).first())
    if card is None or card.client_id != client.id:
        _fail('value_card_id', 'This value card does not belong to the client on the bill.')
    if card.expiry_date and card.expiry_date < today:
        _fail('value_card_id', f'This value card expired on {card.expiry_date:%d %b %Y}; '
                               'its balance is forfeited.')
    if not card.is_active or money(card.balance) <= 0:
        _fail('value_card_id', 'This value card has no balance left.')
    _check_selling_branch(card, center, 'value_card_id', 'value card')

    face, pre, rate = card.terms()
    if face <= 0 or pre <= 0:
        _fail('value_card_id', 'This value card has no face value or price on record.')
    if not gst_on:
        rate = ZERO

    available = money(card.balance)
    covered = []
    for line in quote.lines:
        if available <= 0:
            break
        if not line.is_service:
            continue
        gross = line.gross
        if gross <= 0:
            continue
        face_used = gross if gross <= available else available
        taxable = money(face_used * pre / face)
        benefit = money(face_used - taxable)
        line.value_card = card
        line.card_face = money(face_used)
        line.card_taxable = taxable
        line.card_benefit = benefit
        line.card_tax_percentage = money(rate)
        line.card_tax = money(taxable * rate / HUNDRED)
        line.discount = benefit
        line.total_price = money(gross - benefit)
        available = money(available - face_used)
        covered.append(line)

    if not covered:
        _fail('value_card_id', 'A value card pays for services only, and this bill has none.')

    quote.value_card = card
    quote.card_face = money(sum((l.card_face for l in covered), ZERO))
    quote.card_settled = money(sum((l.card_taxable + l.card_tax for l in covered), ZERO))


# ─── The bill ───────────────────────────────────────────────────────────────

def price_bill(*, center, client, items_data, raw_items, header_discount=ZERO,
               promo_id=None, membership_id=None, value_card_id=None,
               allocations=None, today=None, exclude_invoice_id=None, strict_recipe=False):
    """Price a whole bill. Raises DRF ValidationError on anything not allowed."""
    from marketing import promotions

    today = today or datetime.date.today()
    allocations = allocations or (lambda idx, item: {})
    gst_on = center is None or getattr(center, 'gst_enabled', True)

    quote = Quote()
    quote.lines, quote.audit_notes = price_lines(center, items_data, raw_items,
                                                 allocations=allocations, strict_recipe=strict_recipe)

    # A walk-in with no client record buys services and products only: a
    # membership, package, value card or advance is held FOR a client, so it
    # would be paid for and issued to nobody.
    if client is None:
        for line in quote.lines:
            if line.ct_label in pricing.PERK_TYPES or line.ct_label == CT_ADVANCE:
                _fail('items', f'Line {line.index + 1}: a membership, package, value card or '
                               'advance needs a client on the bill. A walk-in can buy services '
                               'and products only.')

    if promo_id and membership_id:
        _fail('promo_id', 'One offer per bill: choose the promotion or the membership, not both.')
    if value_card_id and (promo_id or membership_id):
        _fail('value_card_id', 'A bill paid with a value card takes no promotion or membership.')

    subtotal = money(sum((l.total_price for l in quote.lines), ZERO))
    services = [l for l in quote.lines if l.is_service]

    if value_card_id:
        if money(D(header_discount)) > 0 or any(D(l.discount) > 0 for l in quote.lines):
            _fail('value_card_id', 'A bill paid with a value card takes no other discount. '
                                   'Remove the discount or pay without the card.')
        _cover_with_card(quote, client, center, value_card_id, today, gst_on)
        # The card's benefit is now inside the covered lines' discount.
        subtotal = money(sum((l.total_price for l in quote.lines), ZERO))
        discount = ZERO
    elif promo_id or membership_id:
        base = money(sum((l.gross for l in services), ZERO))
        if membership_id:
            cm = _membership_for(client, membership_id, center, today)
            quote.membership = cm
            offer = money(base * D(cm.membership.discount_percent) / HUNDRED)
        else:
            from marketing.models import Promotion
            promo = Promotion.objects.filter(id=promo_id).first()
            if promo is None:
                _fail('promo_id', 'Promotion not found.')
            try:
                offer = promotions.bill_discount(
                    promo, services, subtotal=subtotal, client=client, center=center,
                    today=today, exclude_invoice_id=exclude_invoice_id)
            except promotions.PromotionError as exc:
                _fail('promo_id', str(exc))
            quote.promotion = promo
        cap = money(sum((l.total_price for l in services), ZERO))
        discount = offer if offer <= cap else cap
        quote.offer_discount = discount
        _apportion(discount, services)
    else:
        # A bill discount typed at the till, spread over every line - as
        # before. It needs approval above the role's limit once Phase 5 ships.
        discount = money(max(D(header_discount), ZERO))
        if discount > subtotal:
            discount = subtotal
        _apportion(discount, quote.lines)

    # Tax, line by line: the card's part at the card's rate, the rest less the
    # line's share of the bill discount at the line's own rate.
    tax_total = ZERO
    for line in quote.lines:
        rest = money(D(line.total_price) - D(line.card_taxable) - D(line.bill_discount_share))
        if rest < 0:
            rest = ZERO
        rest_tax = (money(rest * D(line.tax_percentage) / HUNDRED)
                    if D(line.tax_percentage) > 0 else ZERO)
        line.tax_amount = money(D(line.card_tax) + rest_tax)
        tax_total += line.tax_amount

    tax_total = money(tax_total)
    halves = split(tax_total, 2) if tax_total > 0 else [ZERO, ZERO]

    quote.subtotal = subtotal
    quote.discount = discount
    quote.cgst, quote.sgst = halves[0], halves[1]
    quote.tax_total = tax_total

    total_raw = money(subtotal - discount + tax_total)
    if quote.value_card is not None:
        # The card settles its part to the paisa; only what is paid now is
        # rounded to the rupee.
        payable_raw = money(total_raw - quote.card_settled)
        payable = rupees(payable_raw)
        quote.rounding = money(payable - payable_raw)
        quote.total_amount = money(quote.card_settled + payable)
    else:
        total = rupees(total_raw)
        quote.rounding = money(total - total_raw)
        quote.total_amount = money(total)
    # The bill's money columns hold up to 99,999,999.99; a bigger bill used to be saved as it
    # was, and the row then crashed every read of it.
    if quote.total_amount > MAX_BILL_AMOUNT or quote.subtotal > MAX_BILL_AMOUNT:
        _fail('items', f'This bill comes to more than Rs {MAX_BILL_AMOUNT:,.2f}, which is too large to record. '
                       'Check the quantities and prices.')
    return quote


def recompute_stored(invoice, save=True):
    """Re-derive a saved bill's tax and header from its stored lines.

    For the few paths that change a saved bill's discount (apply_promo on a
    held bill). The bill discount is spread over the lines it applies to - the
    services when the bill carries a promotion or membership, otherwise every
    line - and taxed the same way as `price_bill`.
    """
    items = list(invoice.items.select_related('content_type').all())
    subtotal = money(sum((D(i.total_price) for i in items), ZERO))
    discount = money(max(D(invoice.discount), ZERO))
    offer = invoice.promotion_id or invoice.membership_used_id
    targets = [i for i in items
               if not offer or (pricing.content_type_label(i.content_type) == pricing.CT_SERVICE
                                and not i.is_redemption)]
    cap = money(sum((D(i.total_price) for i in targets), ZERO))
    if discount > cap:
        discount = cap

    tax_total = ZERO
    for item in items:
        share = (money(discount * D(item.total_price) / cap)
                 if item in targets and cap > 0 and discount > 0 and D(item.total_price) > 0
                 else ZERO)
        rest = money(D(item.total_price) - D(item.card_taxable) - share)
        if rest < 0:
            rest = ZERO
        rest_tax = (money(rest * D(item.tax_percentage) / HUNDRED)
                    if D(item.tax_percentage) > 0 else ZERO)
        tax = money(D(item.card_tax) + rest_tax)
        if share != D(item.bill_discount_share) or tax != D(item.tax_amount):
            item.bill_discount_share = share
            item.tax_amount = tax
            if save:
                item.save(update_fields=['bill_discount_share', 'tax_amount'])
        tax_total += tax

    tax_total = money(tax_total)
    halves = split(tax_total, 2) if tax_total > 0 else [ZERO, ZERO]
    card_settled = money(sum((D(i.card_taxable) + D(i.card_tax) for i in items), ZERO))

    total_raw = money(subtotal - discount + tax_total)
    if card_settled > 0:
        payable_raw = money(total_raw - card_settled)
        payable = rupees(payable_raw)
        rounding = money(payable - payable_raw)
        total = money(card_settled + payable)
    else:
        total = money(rupees(total_raw))
        rounding = money(total - total_raw)

    invoice.subtotal = subtotal
    invoice.discount = discount
    invoice.cgst, invoice.sgst = halves[0], halves[1]
    invoice.rounding = rounding
    invoice.total_amount = total
    if save:
        invoice.save(update_fields=['subtotal', 'discount', 'cgst', 'sgst', 'rounding',
                                    'total_amount'])
    return invoice
