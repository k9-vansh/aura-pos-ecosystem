"""
Authoritative catalog pricing.

The previous implementation lived inline in InvoiceSerializer.create() and had
three defects:

  1. It gated the service branch on `ct.model == 'service'`, but the
     ContentType model name is 'servicemaster'. Every service therefore fell
     through to a price of Decimal('0') and was stored at zero - which zeroed
     service revenue reporting and every commission derived from it.
  2. The same typo disabled the CenterService per-center price override.
  3. It overwrote unit_price with the database price but never recomputed the
     invoice header, so the customer was still charged whatever the client sent.

The policy now:
  * The catalog price is the authoritative LIST price.
  * Selling below list is allowed, but the shortfall becomes a recorded
    `discount` so it is visible in the discount audit rather than hidden in a
    rewritten unit price.
  * Selling above list is rejected, except for packages, where building a
    custom bundle priced above the template is a real business case.
  * Tax percentage always comes from the database, never the client.
"""

import logging

from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType

from pos_backend.money import D, HUNDRED, money, ZERO

logger = logging.getLogger(__name__)

# Canonical ContentType labels, spelled the way Django actually stores them.
CT_SERVICE = 'services.servicemaster'
CT_PRODUCT = 'inventory.product'
CT_MEMBERSHIP = 'marketing.membership'
CT_PACKAGE = 'marketing.package'
CT_VALUE_CARD = 'marketing.valuecard'

# Item types where the sale price may legitimately exceed the catalog price.
PRICE_ABOVE_LIST_ALLOWED = frozenset({CT_PACKAGE, CT_SERVICE, CT_PRODUCT})

# Item types where the user can fully override the price (up or down).
PRICE_OVERRIDE_ALLOWED = frozenset({
    CT_SERVICE, 
    CT_PRODUCT,
    CT_VALUE_CARD,
    CT_MEMBERSHIP,
    CT_PACKAGE
})

# Item types that create a client entitlement when sold.
PERK_TYPES = frozenset({CT_MEMBERSHIP, CT_PACKAGE, CT_VALUE_CARD})


class CatalogPrice:
    """Resolved pricing for one catalog object."""

    __slots__ = ('obj', 'label', 'list_price', 'tax_percentage', 'incentive_percent', 'resolved',
                 'tax_inclusive', 'unavailable', 'no_mrp')

    def __init__(self, obj=None, label=None, list_price=ZERO, tax_percentage=ZERO,
                 incentive_percent=ZERO, resolved=False, tax_inclusive=False,
                 unavailable=None, no_mrp=False):
        self.obj = obj
        self.label = label
        self.list_price = money(list_price)
        self.tax_percentage = D(tax_percentage)
        self.incentive_percent = D(incentive_percent)
        self.resolved = resolved
        # True when list_price already CONTAINS the tax (a product's MRP).
        self.tax_inclusive = tax_inclusive
        # Why this catalogue record cannot be sold at this branch (switched
        # off, or belonging to another branch), or None. The till hides such
        # records; the server must refuse them too, because a stale screen or
        # a direct call can still send one.
        self.unavailable = unavailable
        # A product with no MRP, billed at its purchase price.
        self.no_mrp = no_mrp

    def __repr__(self):
        return (f'CatalogPrice({self.label}, list={self.list_price}, '
                f'tax={self.tax_percentage}%, resolved={self.resolved})')


def content_type_label(ct):
    """'app_label.model' for a ContentType instance, or None."""
    if not ct:
        return None
    return f'{ct.app_label}.{ct.model}'


#: Resolved content types, keyed by "app_label.model". These never change
#: while the process is running.
_CONTENT_TYPE_CACHE = {}


def get_content_type(label):
    """Resolve 'app_label.model' to a ContentType, or None if unknown.

    Cached per process, because this is called on the hot path.

    `ContentType.objects.get(app_label=..., model=...)` is NOT covered by
    Django's ContentType cache - only `get_for_model` and `get_for_id` are - so
    every call was a database round trip. Invoice creation called it twice for
    every line (once inside `resolve()`, once when building the PricedLine), so a
    12-line bill spent 24 queries re-reading a table of a dozen rows that never
    change. Measured: 46 queries for a 1-line bill, 146 for 12 lines, about 9 per
    line, and 8 of the 3-line bill's queries were this function.

    `get_for_model` IS cached, so resolving the model class first and asking for
    its content type costs nothing after the first call.
    """
    if not label or not isinstance(label, str) or '.' not in label:
        return None

    app_label, model = label.split('.', 1)
    key = f'{app_label.strip().lower()}.{model.strip().lower()}'

    if key in _CONTENT_TYPE_CACHE:
        return _CONTENT_TYPE_CACHE[key]

    try:
        model_class = django_apps.get_model(key)
    except (LookupError, ValueError):
        _CONTENT_TYPE_CACHE[key] = None
        return None

    if model_class is None:
        _CONTENT_TYPE_CACHE[key] = None
        return None

    try:
        # get_for_model reads Django's own ContentType cache.
        content_type = ContentType.objects.get_for_model(
            model_class, for_concrete_model=True)
    except Exception:
        return None

    _CONTENT_TYPE_CACHE[key] = content_type
    return content_type


def clear_content_type_cache():
    """For tests, and after a migration that adds or renames a model."""
    _CONTENT_TYPE_CACHE.clear()


def _derive_tax_from_price_pair(pre_tax, post_tax):
    """Infer a tax rate from a configured pre/post tax price pair.

    DEPRECATED for pricing. Memberships and value cards carry an explicit
    `gst_percent` now; inferring the rate from the price pair let the data-entry
    form decide the tax rate as a side effect. Kept because the backfill
    migration needs the old meaning to preserve existing rows.

    Memberships, packages and value cards have no tax_percentage column, so
    they were billed at 0% GST. Where the operator has filled in both
    pre_tax_price and post_tax_price, the difference between them IS their
    intended tax rate, so we use it rather than inventing a default.
    """
    pre = D(pre_tax)
    post = D(post_tax)
    if pre <= 0 or post <= 0 or post <= pre:
        return ZERO
    return ((post - pre) / pre * D('100')).quantize(D('0.01'))


def resolve(ct_label, object_id, center=None):
    """Look up authoritative price and tax for a catalog item.

    Returns a CatalogPrice. `resolved` is False when the item cannot be found,
    in which case the caller must fall back to the client-supplied values (this
    is the legitimate path for custom packages and ad-hoc lines).
    """
    if not ct_label or not object_id:
        return CatalogPrice(label=ct_label, resolved=False)

    ct = get_content_type(ct_label)
    if not ct:
        logger.warning('[Pricing] Unknown content type label %r', ct_label)
        return CatalogPrice(label=ct_label, resolved=False)

    label = content_type_label(ct)
    model_class = ct.model_class()
    if model_class is None:
        return CatalogPrice(label=label, resolved=False)

    try:
        obj = model_class.objects.get(pk=object_id)
    except Exception:
        logger.warning('[Pricing] %s id=%s not found', label, object_id)
        return CatalogPrice(label=label, resolved=False)

    if label == CT_SERVICE:
        price = D(obj.default_price)
        tax = D(obj.tax_percentage)
        incentive = D(getattr(obj, 'incentive', 0))
        unavailable = None
        # Per-center price and incentive override.
        if center is not None:
            from services.models import CenterService
            override = CenterService.objects.filter(center=center, service=obj).first()
            if override is not None and not override.is_active:
                # Switched off at this branch (decision #16): not sold here,
                # and certainly not at the central price.
                unavailable = f'"{obj.name}" is switched off at this branch.'
            elif override is not None:
                if override.price is not None:
                    price = D(override.price)
                if override.incentive is not None:
                    incentive = D(override.incentive)
            if (unavailable is None and obj.level == 'Center'
                    and not obj.centers.filter(pk=center.pk).exists()):
                unavailable = f'"{obj.name}" is not offered at this branch.'
        return CatalogPrice(obj, label, price, tax, incentive, resolved=True,
                            unavailable=unavailable)

    if label == CT_PRODUCT:
        # A product's price is its MRP, and the MRP INCLUDES GST (owner
        # decisions #27, F22): the customer pays the MRP and the GST is shown
        # inside it. Products used to be billed at 0% GST, so no GST was ever
        # charged or reported on product sales (finding X6). The invoice
        # serializer works the taxable value out of the MRP; nothing is added
        # on top.
        product_price = obj.mrp if obj.mrp is not None else obj.price
        tax = D(getattr(obj, 'gst_percent', None))
        if tax < 0 or tax > 100:
            tax = ZERO
        unavailable = None
        if not obj.is_active:
            unavailable = f'"{obj.name}" is switched off.'
        elif center is not None and obj.center_id and obj.center_id != center.id:
            unavailable = f'"{obj.name}" belongs to another branch.'
        return CatalogPrice(obj, label, D(product_price), tax,
                            D(getattr(obj, 'incentive', 0)), resolved=True, tax_inclusive=True,
                            unavailable=unavailable, no_mrp=obj.mrp is None)

    if label in (CT_MEMBERSHIP, CT_VALUE_CARD):
        pre = D(obj.pre_tax_price)
        post = D(obj.post_tax_price)

        # The rate is STATED on the row now, not inferred from the gap between
        # the two prices. Inferring it meant the create/edit forms decided the
        # tax rate as a side effect of how they filled the pair - and they wrote
        # both with a hardcoded 1.05, so every membership and value card was
        # invoiced at 5% and 18% could not be entered at all.
        tax = D(getattr(obj, 'gst_percent', None))
        if tax < 0 or tax > 100:
            tax = ZERO

        if pre > 0:
            price = pre
        elif post > 0 and tax > 0:
            # Only a tax-inclusive price was entered: work the base out of it,
            # rather than billing the inclusive figure AND adding tax on top.
            price = money(post / (D('1') + tax / HUNDRED))
        else:
            price = post

        return CatalogPrice(obj, label, price, tax,
                            D(getattr(obj, 'incentive', 0)), resolved=True,
                            unavailable=_perk_unavailable(
                                obj, getattr(obj, 'name', None) or getattr(obj, 'title', ''), center))

    if label == CT_PACKAGE:
        # A package sale carried a hardcoded 0% GST, and a redemption line is
        # zero-value and zero-tax, so GST was never charged on a package at any
        # point. Memberships and value cards can express a rate through their
        # pre/post tax price pair; Package has only a single price column, so
        # there was no way to say otherwise.
        #
        # Whether a prepaid package should be taxed at the point of sale is a
        # question for the operator's accountant. Set in Admin > Settings
        # ("GST on package sales"); default 0, exactly the old behaviour. An
        # invalid value falls back to 0.
        from pos_backend import app_settings

        own_rate = getattr(obj, 'gst_percent', None)
        package_tax = D(own_rate) if own_rate is not None else D(
            app_settings.get('billing.package_gst_percent'))
        return CatalogPrice(obj, label, D(obj.price), package_tax, ZERO,
                            resolved=True,
                            unavailable=_perk_unavailable(obj, obj.name, center))

    # Unknown but valid content type - do not guess at a price.
    return CatalogPrice(obj, label, ZERO, ZERO, ZERO, resolved=False)


def _perk_unavailable(obj, name, center):
    """Why a membership, package or value card cannot be sold at `center`.

    The same rule the Marketing lists use to decide what a branch is shown:
    active, and either organisation-wide or this branch's own."""
    if not getattr(obj, 'is_active', True):
        return f'"{name}" is switched off.'
    if (center is not None and getattr(obj, 'level', 'Organisation') != 'Organisation'
            and getattr(obj, 'center_id', None) and obj.center_id != center.id):
        return f'"{name}" belongs to another branch.'
    return None


def reconcile_line_price(catalog, requested_unit_price, requested_discount, ct_label,
                         quantity=1):
    """Reconcile a client-supplied line price against the catalog.

    Returns (unit_price, discount, note) where `note` records any adjustment
    for the audit log.

    Policy: the unit price is ALWAYS the catalog list price. Price reductions
    travel in `discount`, which is exactly what the POS client already sends -
    it puts the catalog price in unit_price and the reduction in discount.

    That makes the rule strict and unambiguous: an understated unit_price is
    simply replaced, so it cannot understate the bill, while a genuine
    negotiated price still works because it arrives as a discount and stays
    visible in the discount audit.

    Two exceptions:
      * an unresolvable catalog object (a custom line or a deleted master) -
        there is no list price to enforce, so the client price is used
      * packages - building a bundle priced above the template is a real case
    """
    requested_price = D(requested_unit_price)
    discount = money(max(D(requested_discount), ZERO))
    qty = max(int(quantity or 1), 1)

    if not catalog.resolved:
        # No catalog price to enforce. Never allow a negative unit price.
        price = money(requested_price) if requested_price > 0 else ZERO
        return price, discount, None

    list_price = catalog.list_price

    if ct_label in PRICE_ABOVE_LIST_ALLOWED and requested_price > list_price:
        # A custom bundle priced above its template. Owner decision #25: every
        # price change is traceable, so this is logged like a reduction is.
        return money(requested_price), discount, (
            f'Unit price Rs {list_price} overridden to Rs {money(requested_price)}.')

    if ct_label in PRICE_OVERRIDE_ALLOWED:
        note = None
        if money(requested_price) != list_price:
            note = (f'Unit price Rs {list_price} overridden to '
                    f'Rs {money(requested_price)}.')
        return money(requested_price), discount, note

    note = None
    if money(requested_price) != list_price:
        note = (f'Unit price Rs {money(requested_price)} replaced with catalog price '
                f'Rs {list_price}.')
    return list_price, discount, note


def discount_percent(unit_price, quantity, discount):
    """The discount on a line as a percentage of its gross value."""
    gross = D(unit_price) * D(quantity or 1)
    if gross <= 0:
        return ZERO
    return (D(discount) / gross * D('100')).quantize(D('0.01'))
