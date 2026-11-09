"""A service's recipe: what it uses, and what one bill actually used.

The POS owns the recipe (Admin > Services > Recipe):

  fixed items     used every time (scissors, a towel)
  pick-one groups the person billing chooses one option per client: Colour A, B
                  or C. An option is a product, or "any product of a category",
                  so a new colour added to the category is offered with no edit.

What a bill used is resolved here, once, and stored on the bill line as
`consumables`. Stock, the ledger and the SAP A/R Invoice (which lists those items
under the service at no price) all read that stored list, so they can never disagree
with each other or with the screen.

Rules, in one place:

  * Retail products are for selling over the counter: never part of a recipe.
  * Technical and Consumable products (and no category) are used up: their stock is deducted.
  * The same choice applies to every unit on one line; a different colour is a
    second line.
  * The person billing may type the quantity really used, or add an extra
    product the recipe did not list.
"""

from decimal import Decimal
from types import SimpleNamespace

from rest_framework.exceptions import ValidationError

from inventory.quantities import ZERO, plain, qty, sap_decimals, to_sap
from services import units
from services.models import ServiceBOM, ServiceRecipeGroup, bom_role

D = Decimal


def uses_recipe(service):
    """True when the POS owns this service's recipe (SAP-owned ones keep the old path)."""
    return getattr(service, 'recipe_source', 'sap') == 'pos'


def _fail(message):
    raise ValidationError({'items': message})


def _products_at(center):
    from inventory.models import Product
    return Product.objects.filter(center=center, is_active=True) if center is not None else Product.objects.none()


def _by_code(center):
    return {p.product_code: p for p in _products_at(center) if p.product_code}


def group_options(group, center, products=None):
    """The options of a group at `center`: [{product_code, name, category, quantity, stock,
    unit, is_default}]. A category option offers every product of that category;
    an explicit product wins over the same product reached through a category."""
    by_code = products if products is not None else _by_code(center)
    options, seen = [], set()
    rows = list(group.options.all().order_by('id'))
    explicit = [r for r in rows if r.product_code]
    by_category = [r for r in rows if not r.product_code and r.category]

    def add(row, code, product):
        if code in seen:
            return
        category = (product.category if product else '') or ''
        if product is not None and bom_role(category) == 'retail':
            return                                   # sold, never used in a service
        seen.add(code)
        options.append({
            'product_code': code,
            'name': product.name if product else code,
            'category': category,
            'quantity': row.quantity,
            'stock': product.current_stock if product else ZERO,
            'unit': row.unit or (product.unit if product else 'pcs'),
            'stock_unit': product.unit if product else 'pcs',
            'is_default': bool(row.is_default),
        })

    for row in explicit:
        add(row, row.product_code, by_code.get(row.product_code))
    for row in by_category:
        wanted = row.category.strip().lower()
        for code, product in sorted(by_code.items(), key=lambda kv: kv[1].name.lower()):
            if (product.category or '').strip().lower() == wanted:
                add(row, code, product)
    return options


def clean_selection(raw):
    """The till's selection as {'choices': {group_id: {'product_code', 'quantity'?}}, 'extras': [...]}."""
    if raw in (None, '', {}, []):
        return {'choices': {}, 'extras': []}
    if not isinstance(raw, dict):
        _fail('The recipe choices are not in the expected form.')
    choices = {}
    given = raw.get('choices') or {}
    if not isinstance(given, dict):
        _fail('The recipe choices are not in the expected form.')
    for group_id, value in given.items():
        if isinstance(value, str):
            value = {'product_code': value}
        if not isinstance(value, dict) or not (value.get('product_code') or '').strip():
            continue
        entry = {'product_code': value['product_code'].strip()}
        if value.get('quantity') not in (None, ''):
            try:
                override = qty(value['quantity'])
            except ValueError:
                _fail('A quantity used must be a number.')
            if override <= 0:
                _fail('A quantity used must be more than zero.')
            entry['quantity'] = str(override)
        choices[str(group_id)] = entry
    extras = []
    given_extras = raw.get('extras') or []
    if not isinstance(given_extras, list):
        _fail('The extra products are not in the expected form.')
    for value in given_extras:
        if not isinstance(value, dict) or not (value.get('product_code') or '').strip():
            continue
        try:
            amount = qty(value.get('quantity'))
        except ValueError:
            _fail('An extra quantity must be a number.')
        if amount <= 0:
            _fail('An extra quantity must be more than zero.')
        extras.append({'product_code': value['product_code'].strip(), 'quantity': str(amount)})
    return {'choices': choices, 'extras': extras}


def _entry(product_code, product, amount, group, source, unit=''):
    """One line of what a bill used. `amount` is in `unit` (the recipe's); the line itself is
    always in the product's stock unit, which is what stock, the ledger and SAP count."""
    category = (product.category if product else '') or ''
    stock_unit = product.unit if product else 'pcs'
    entry = {
        'product_code': product_code,
        'name': product.name if product else product_code,
        'unit': stock_unit,
        'group': group,
        'source': source,
        'deduct': bom_role(category) == 'consumable',
    }
    if product is not None and unit and units.norm(unit) != units.norm(stock_unit):
        try:
            wanted = units.to_stock_unit(amount, unit, product)
        except ValueError as exc:
            _fail(str(exc))
        entry['used'] = f'{plain(amount)} {unit}'
    else:
        wanted = amount
    # SAP keeps two decimals in a quantity: what is taken out of stock here is what SAP will take out.
    counted = to_sap(wanted)
    if D(str(wanted)) > 0 and counted <= 0:
        _fail(f'{entry["name"]}: {plain(amount)} {unit or stock_unit} is less than SAP can count '
              f'(it keeps {sap_decimals()} decimals of a {stock_unit}). Use a larger quantity, or stock '
              f'the product in a smaller unit in SAP.')
    entry['quantity'] = plain(counted)
    return entry


def resolve(service, center, quantity, selection=None, strict=True):
    """What `quantity` units of `service` use at `center`, as a list of entries
    {product_code, name, unit, quantity, group, source, deduct}.

    `strict` is for completing a bill: a required group with nothing chosen (and
    no default) is refused. A held bill or a quote is not strict, so the till can
    show totals before every choice is made."""
    if not uses_recipe(service):
        return []
    quantity = max(int(quantity or 0), 0)
    if quantity == 0:
        return []
    selection = clean_selection(selection)
    products = _by_code(center)
    out = []

    for row in ServiceBOM.objects.filter(service=service, group__isnull=True).order_by('id'):
        product = products.get(row.product_code)
        if product is not None and bom_role(product.category) == 'retail':
            continue
        out.append(_entry(row.product_code, product, qty(row.quantity * quantity), '', 'fixed', row.unit))

    groups = list(ServiceRecipeGroup.objects.filter(service=service).prefetch_related('options'))
    valid_ids = {str(g.pk) for g in groups}
    for stray in selection['choices']:
        if stray not in valid_ids:
            _fail(f'{service.name}: that choice does not belong to this service.')

    for group in groups:
        options = group_options(group, center, products)
        by_code = {o['product_code']: o for o in options}
        chosen = selection['choices'].get(str(group.pk))
        if chosen is None:
            default = next((o for o in options if o['is_default']), None)
            if default is not None:
                chosen = {'product_code': default['product_code']}
            elif group.required:
                if strict:
                    _fail(f'Choose the {group.name} for {service.name}.')
                continue
            else:
                continue
        option = by_code.get(chosen['product_code'])
        if option is None:
            _fail(f'{chosen["product_code"]} is not an option for {group.name} on {service.name}.')
        amount = qty(chosen['quantity']) if chosen.get('quantity') else qty(option['quantity'] * quantity)
        out.append(_entry(option['product_code'], products.get(option['product_code']), amount,
                          group.name, 'choice', option['unit']))

    for extra in selection['extras']:
        product = products.get(extra['product_code'])
        if product is None:
            _fail(f'{extra["product_code"]} is not stocked at this branch.')
        if bom_role(product.category) == 'retail':
            _fail(f'{product.name} is a retail product: it is sold, not used in a service.')
        out.append(_entry(extra['product_code'], product, qty(extra['quantity']), '', 'extra'))
    return out


def deductions(entries):
    """{product_code: total quantity} that must come out of stock."""
    totals = {}
    for entry in entries:
        if entry['deduct']:
            totals[entry['product_code']] = qty(totals.get(entry['product_code'], ZERO) + D(entry['quantity']))
    return totals


def resolve_item(item, strict=True):
    """The entries for a saved bill line (a service line of a bill)."""
    from services.models import ServiceMaster
    service = item.content_object if isinstance(item.content_object, ServiceMaster) else None
    if service is None:
        return []
    center = item.invoice.center or (item.invoice.client.center if item.invoice.client else None)
    return resolve(service, center, item.quantity, item.recipe_choices, strict=strict)


# ─── The recipe form (Admin > Services) ─────────────────────────────────────

def recipe_payload(service):
    """The recipe as the admin form edits it."""
    from inventory.models import Product
    codes = {r.product_code for r in ServiceBOM.objects.filter(service=service) if r.product_code}
    names, categories, stocked = {}, {}, {}
    for p in Product.objects.filter(product_code__in=codes).order_by('id'):
        names.setdefault(p.product_code, p.name)
        categories.setdefault(p.product_code, p.category or '')
        stocked.setdefault(p.product_code, p)

    def unit_of(row):
        product = stocked.get(row.product_code)
        return row.unit or (product.unit if product else '') or ''

    def choices_for(row):
        product = stocked.get(row.product_code)
        return units.options(product) if product else []

    fixed = [{'id': r.pk, 'product_code': r.product_code, 'product_name': names.get(r.product_code, r.product_code),
              'category': categories.get(r.product_code, ''), 'unit': unit_of(r), 'unit_options': choices_for(r),
              'quantity': plain(r.quantity)}
             for r in ServiceBOM.objects.filter(service=service, group__isnull=True).order_by('id')]
    groups = []
    for g in ServiceRecipeGroup.objects.filter(service=service).prefetch_related('options'):
        groups.append({
            'id': g.pk, 'name': g.name, 'required': g.required,
            'options': [{'id': o.pk, 'product_code': o.product_code,
                         'product_name': names.get(o.product_code, o.product_code) if o.product_code else '',
                         'category': o.category or categories.get(o.product_code, ''),
                         'unit': unit_of(o), 'unit_options': choices_for(o),
                         'quantity': plain(o.quantity), 'is_default': o.is_default,
                         'by_category': bool(o.category and not o.product_code)}
                        for o in g.options.all().order_by('id')],
        })
    return {'source': service.recipe_source, 'fixed': fixed, 'groups': groups}


def _positive(value, what):
    try:
        amount = D(str(value))
    except Exception:
        raise ValidationError({'recipe': f'{what}: the quantity must be a number.'})
    if not amount.is_finite() or amount <= 0:
        raise ValidationError({'recipe': f'{what}: the quantity must be more than zero.'})
    return amount.quantize(D('0.0001'))


def _check_product_allowed(code, what):
    """A recipe never lists a retail product."""
    from inventory.models import Product
    rows = list(Product.objects.filter(product_code=code).values_list('category', flat=True))
    if rows and all(bom_role(c) == 'retail' for c in rows):
        raise ValidationError({'recipe': f'{what}: {code} is a retail product. Retail is sold, not used in a service.'})


def _check_unit(code, unit, what, row=None, pack_updates=None):
    """The unit a line is written in, or '' for the product's own. Refused when it cannot be
    turned into the product's stock unit (a bottle stocked in pcs, used in ml, with no pack size).

    The line may carry `pack_size` / `pack_unit` ("1 pcs holds 100 ml"): the editor asks for it
    when the product has none. It is checked here and written (to every branch's copy of the
    product) with the recipe, in `pack_updates`."""
    from inventory.models import Product
    unit = (unit or '').strip()
    if not code or not unit:
        return ''
    if len(unit) > 20:
        raise ValidationError({'recipe': f'{what}: the unit is too long.'})
    products = list(Product.objects.filter(product_code=code).order_by('id')[:1])
    if not products:
        return units.norm(unit)
    product = products[0]
    if units.norm(unit) == units.norm(product.unit or 'pcs'):
        return ''
    if row and row.get('pack_size') not in (None, ''):
        pack_unit = units.norm(row.get('pack_unit'))
        try:
            size = D(str(row['pack_size']))
        except Exception:
            size = D('0')
        if not size.is_finite() or not (0 < size < 10_000_000) or units.family(pack_unit) not in ('volume', 'weight'):
            raise ValidationError({'recipe': f'{what}: say how much one {product.unit} holds, in ml, l, g or kg.'})
        product = SimpleNamespace(name=product.name, unit=product.unit, pack_size=size.quantize(D('0.001')), pack_unit=pack_unit)
        if pack_updates is not None:
            pack_updates[code] = (product.pack_size, pack_unit)
    try:
        units.to_stock_unit(1, unit, product)
    except ValueError as exc:
        raise ValidationError({'recipe': f'{what}: {exc}'})
    return units.norm(unit)


def save_recipe(service, data):
    """Replace the service's recipe with `data` ({fixed: [...], groups: [...]}) and
    make the POS its owner, so SAP never overwrites it."""
    from django.db import transaction

    if not isinstance(data, dict):
        raise ValidationError({'recipe': 'Send the recipe as {fixed, groups}.'})
    fixed = data.get('fixed') or []
    groups = data.get('groups') or []
    if not isinstance(fixed, list) or not isinstance(groups, list):
        raise ValidationError({'recipe': 'Send the recipe as {fixed, groups}.'})

    clean_fixed, seen, pack_updates = [], set(), {}
    for row in fixed:
        code = (row.get('product_code') or '').strip() if isinstance(row, dict) else ''
        if not code:
            raise ValidationError({'recipe': 'Every fixed item needs a product.'})
        if code in seen:
            raise ValidationError({'recipe': f'{code} is listed twice among the fixed items.'})
        seen.add(code)
        _check_product_allowed(code, 'Fixed items')
        clean_fixed.append((code, _positive(row.get('quantity'), code),
                            _check_unit(code, row.get('unit'), code, row, pack_updates)))

    clean_groups, names = [], set()
    for number, group in enumerate(groups):
        name = (group.get('name') or '').strip() if isinstance(group, dict) else ''
        if not name:
            raise ValidationError({'recipe': 'Every pick-one group needs a name.'})
        if name.lower() in names:
            raise ValidationError({'recipe': f'There are two groups called {name}.'})
        names.add(name.lower())
        options = group.get('options') or []
        if not options:
            raise ValidationError({'recipe': f'{name}: add at least one option.'})
        clean_options, option_seen, defaults = [], set(), 0
        for option in options:
            code = (option.get('product_code') or '').strip()
            category = (option.get('category') or '').strip()
            if bool(code) == bool(category):
                raise ValidationError({'recipe': f'{name}: each option is either one product or one category.'})
            key = (code, category.lower())
            if key in option_seen:
                raise ValidationError({'recipe': f'{name}: {code or category} is listed twice.'})
            option_seen.add(key)
            if code:
                _check_product_allowed(code, name)
            if option.get('is_default'):
                defaults += 1
            clean_options.append((code, category, _positive(option.get('quantity'), f'{name} / {code or category}'),
                                  bool(option.get('is_default')),
                                  _check_unit(code, option.get('unit'), f'{name} / {code}', option, pack_updates)))
        if defaults > 1:
            raise ValidationError({'recipe': f'{name}: only one option can be the default.'})
        clean_groups.append((name, bool(group.get('required', True)), number, clean_options))

    with transaction.atomic():
        from inventory.models import Product
        for code, (size, pack_unit) in pack_updates.items():
            Product.objects.filter(product_code=code).update(pack_size=size, pack_unit=pack_unit)
        ServiceBOM.objects.filter(service=service).delete()
        ServiceRecipeGroup.objects.filter(service=service).delete()
        for code, amount, unit in clean_fixed:
            ServiceBOM.objects.create(service=service, product_code=code, quantity=amount, unit=unit)
        for name, required, order, options in clean_groups:
            group = ServiceRecipeGroup.objects.create(service=service, name=name, required=required, sort_order=order)
            for code, category, amount, is_default, unit in options:
                ServiceBOM.objects.create(service=service, group=group, product_code=code, category=category,
                                          quantity=amount, is_default=is_default, unit=unit)
        service.recipe_source = 'pos'
        service.save(update_fields=['recipe_source', 'updated_at'])
    return recipe_payload(service)


def product_choices(term='', limit=30):
    """Products the recipe form can pick, found by name or code: one row per product
    code (a product exists once per branch), never a retail one."""
    from inventory.models import Product
    term = (term or '').strip()
    rows = Product.objects.exclude(category__iexact='retail').exclude(product_code__isnull=True).exclude(product_code='')
    if term:
        from django.db.models import Q
        rows = rows.filter(Q(name__icontains=term) | Q(product_code__icontains=term))
    seen, out = set(), []
    for row in rows.order_by('name', 'id').values('product_code', 'name', 'category', 'unit', 'pack_size', 'pack_unit')[:limit * 6]:
        if row['product_code'] in seen:
            continue
        seen.add(row['product_code'])
        out.append({'product_code': row['product_code'], 'name': row['name'],
                    'category': row['category'] or '', 'unit': row['unit'] or 'pcs',
                    'unit_options': units.options(SimpleNamespace(**row))})
        if len(out) >= limit:
            break
    return out


def recipe_categories():
    """Categories a recipe option can offer: every category except Retail."""
    from inventory.models import Product
    names = (Product.objects.exclude(category__isnull=True).exclude(category='')
             .order_by().values_list('category', flat=True).distinct())
    seen, out = set(), []
    for name in sorted(names, key=lambda n: n.lower()):
        if name.strip().lower() == 'retail' or name.strip().lower() in seen:
            continue
        seen.add(name.strip().lower())
        out.append(name.strip())
    return out


def till_recipes(center):
    """What the till needs to offer choices at `center`: {service_id: {groups}} for every
    POS-owned service (one with no groups still lets the till add an extra product)."""
    from services.models import ServiceMaster
    products = _by_code(center)
    out = {}
    for service in ServiceMaster.objects.filter(recipe_source='pos'):
        groups = []
        for g in ServiceRecipeGroup.objects.filter(service=service).prefetch_related('options'):
            groups.append({
                'id': g.pk, 'name': g.name, 'required': g.required,
                'options': [{'product_code': o['product_code'], 'name': o['name'], 'unit': o['unit'],
                             'category': o['category'], 'quantity': plain(o['quantity']),
                             'stock': plain(o['stock']), 'is_default': o['is_default']}
                            for o in group_options(g, center, products)],
            })
        out[service.pk] = {'groups': groups}
    # Products the till may add to any recipe-owning service as an extra: used up, not retail.
    extras = [{'product_code': code, 'name': p.name, 'unit': p.unit, 'category': p.category or '',
               'stock': plain(p.current_stock)}
              for code, p in sorted(products.items(), key=lambda kv: kv[1].name.lower())
              if bom_role(p.category) == 'consumable']
    return {'recipes': out, 'extras': extras}
