"""Units of measure for recipes.

A product is stocked in one unit (`Product.unit`, from SAP's inventory UoM: usually PCS), and a
recipe may say how much a service uses in another: 10 ml of a shampoo that is stocked as bottles.
Stock, the ledger and SAP only ever see the product's own unit, so the conversion happens once,
when the recipe is resolved (services.recipes).

Two kinds of conversion:

  * inside one family: ml <-> l, g <-> kg (a fixed factor);
  * through the pack: a product stocked in pieces that holds `pack_size` `pack_unit`
    ("1 pcs = 100 ml") can be used in ml.

A unit that cannot be reached either way is refused, never guessed.
"""
from decimal import Decimal

D = Decimal

# family -> {unit: how many of the family's base unit one of it is}
FAMILIES = {
    'volume': {'ml': D('1'), 'cl': D('10'), 'l': D('1000')},
    'weight': {'mg': D('0.001'), 'g': D('1'), 'kg': D('1000')},
    'count': {'pcs': D('1'), 'dozen': D('12')},
}
ALIASES = {
    'pc': 'pcs', 'pcs': 'pcs', 'piece': 'pcs', 'pieces': 'pcs', 'nos': 'pcs', 'no': 'pcs', 'unit': 'pcs',
    'units': 'pcs', 'each': 'pcs', 'ea': 'pcs',
    'ml': 'ml', 'millilitre': 'ml', 'milliliter': 'ml', 'cl': 'cl', 'l': 'l', 'ltr': 'l', 'lt': 'l',
    'litre': 'l', 'liter': 'l',
    'mg': 'mg', 'g': 'g', 'gm': 'g', 'gms': 'g', 'gram': 'g', 'grams': 'g', 'kg': 'kg', 'kgs': 'kg',
    'dozen': 'dozen', 'doz': 'dozen',
}


def norm(unit):
    """The unit as the POS names it: 'PCS' -> 'pcs', 'Ltr' -> 'l'; an unknown unit (PACK, BOX) just lower-cased."""
    text = (unit or '').strip().lower()
    return ALIASES.get(text, text)


def family(unit):
    unit = norm(unit)
    for name, members in FAMILIES.items():
        if unit in members:
            return name
    return ''


def _factor(unit):
    return FAMILIES[family(unit)][norm(unit)]


def pack(product):
    """(size, unit) when the product says what one stock unit holds, else (None, '')."""
    size = getattr(product, 'pack_size', None)
    unit = norm(getattr(product, 'pack_unit', ''))
    if size and D(size) > 0 and family(unit) in ('volume', 'weight'):
        return D(size), unit
    return None, ''


def options(product):
    """The units a recipe line for `product` can be written in, its own first."""
    own = norm(getattr(product, 'unit', '') or 'pcs')
    found = [own]
    families = [f for f in dict.fromkeys([family(own), family(pack(product)[1])]) if f]
    for fam in families:
        for unit in FAMILIES[fam]:
            if unit not in found:
                found.append(unit)
    return found


def to_stock_unit(amount, unit, product):
    """`amount` of `unit` expressed in the unit `product` is stocked in. ValueError when unreachable."""
    amount = D(str(amount))
    own, wanted = norm(getattr(product, 'unit', '') or 'pcs'), norm(unit or getattr(product, 'unit', '') or 'pcs')
    if wanted == own:
        return amount
    if family(wanted) and family(wanted) == family(own):
        return amount * _factor(wanted) / _factor(own)
    size, pack_unit = pack(product)
    if size is not None and family(wanted) == family(pack_unit):
        # amount in the pack's own unit, then how many stock units (packs) that is
        return amount * _factor(wanted) / _factor(pack_unit) / size
    raise ValueError(
        f'{getattr(product, "name", "This product")} is stocked in {own}; it cannot be used in {wanted}'
        + ('' if size is not None else f'. Set what one {own} holds on the product first (for example 1 {own} = 100 ml).'))
