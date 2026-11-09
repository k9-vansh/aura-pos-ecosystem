"""
Promotion application.

Fixes from the audit:
  * `Decimal(str(promo.discount_value))` raised InvalidOperation when the
    column was null. This 500'd invoice finalisation four times in production
    on 19 Aug 2026 and the operator could not complete the sale.
  * Not idempotent: apply_promo could be called repeatedly, stacking the
    discount toward zero and writing a PromotionUsage row each time.
  * `Promotion.center` / `level` were never checked, so a center-specific
    promotion applied everywhere.
  * `Promotion.members_only` was declared on the model and read by nothing.
  * Cashback was computed on the pre-discount subtotal.

Phase 6: the discount itself is worked out here (`bill_discount`) from the
campaign's own rules, for the billing engine. The till sends only the
promotion id; it used to send its own figure as well (X3).
"""

import datetime
import logging

from pos_backend.money import D, money, ZERO

logger = logging.getLogger(__name__)


class PromotionError(Exception):
    """A promotion cannot be applied. Message is safe to show the operator."""


def _client_is_member(client):
    """True if the client holds an active, unexpired membership."""
    if not client:
        return False
    from clients.models import ClientMembership
    return ClientMembership.objects.filter(
        client=client, is_active=True, expiry_date__gte=datetime.date.today()
    ).exists()


def check_promotion(promo, invoice):
    """Validate a promotion against an invoice. Raises PromotionError if not usable."""
    today = (invoice.created_at.date() if invoice.created_at else datetime.date.today())
    check_usable(promo, client=invoice.client, center=invoice.center, today=today,
                 exclude_invoice_id=invoice.pk)


def check_usable(promo, *, client, center, today, exclude_invoice_id=None):
    """Raise PromotionError unless `promo` may be used on a bill for `client`
    at `center` on `today`."""
    from marketing.models import PromotionUsage

    if promo.promo_type == 'Coupon':
        # A brand's coupon pays by its code (Gift Card), it is not an offer picked on the bill.
        raise PromotionError('A coupon is paid with its code under Gift Card, not chosen as an offer.')

    if not promo.is_active:
        raise PromotionError('This promotion is no longer active.')

    if promo.start_date and promo.end_date:
        if not (promo.start_date <= today <= promo.end_date):
            raise PromotionError(
                f'Promotion "{promo.name}" is only valid from {promo.start_date} to {promo.end_date}.'
            )

    # Center scoping. A promotion tied to a center must not apply elsewhere.
    center_id = getattr(center, 'id', center)
    if promo.center_id and center_id and promo.center_id != center_id:
        raise PromotionError(f'Promotion "{promo.name}" is not available at this center.')
    if promo.level == 'Center' and not promo.center_id:
        raise PromotionError(f'Promotion "{promo.name}" is center-specific but has no center assigned.')

    if promo.members_only and not _client_is_member(client):
        raise PromotionError(f'Promotion "{promo.name}" is for members only.')

    if promo.max_usage_per_client:
        if not client:
            raise PromotionError(
                f'Promotion "{promo.name}" has a per-client usage limit and requires a registered client.'
            )

        # Counting and then inserting is a check-then-act, and the insert
        # happens at the end of the checkout. Two bills for the same customer
        # running at once both counted 0 and both went through, so a
        # one-per-customer coupon was usable twice. Locking the customer row
        # makes the two transactions serialise here until commit. Locking the
        # CUSTOMER rather than the promotion keeps the contention per-person -
        # a busy coupon does not serialise every till in the business.
        from clients.models import Client

        Client.objects.select_for_update().filter(pk=client.pk).first()

        used = PromotionUsage.objects.filter(promotion=promo, client=client)
        if exclude_invoice_id:
            used = used.exclude(invoice_id=exclude_invoice_id)
        if used.exclude(releases_allowance=True).count() >= promo.max_usage_per_client:
            raise PromotionError(f'Usage limit reached for promotion "{promo.name}".')

    if promo.promo_type == 'Discount':
        value = _discount_value(promo)
        if value is None:
            raise PromotionError(
                f'Promotion "{promo.name}" is a discount promotion but has no discount value '
                f'configured. Set a value on the promotion before using it.'
            )
        if value <= 0:
            raise PromotionError(f'Promotion "{promo.name}" has a zero or negative discount value.')
        if _discount_type(promo) == 'Percentage' and value > 100:
            raise PromotionError(f'Promotion "{promo.name}" has a percentage discount above 100%.')


def _config(promo):
    return promo.config if isinstance(promo.config, dict) else {}


def _discount_type(promo):
    return promo.discount_type or _config(promo).get('discount_type') or 'Percentage'


def _discount_value(promo):
    """The promotion's discount value. The Marketing form writes it into
    `config`; older rows have it on the column."""
    if promo.discount_value is not None:
        return D(promo.discount_value)
    raw = _config(promo).get('discount_value')
    if raw in (None, ''):
        return None
    return D(raw)


def _named(lines, name, ids=None):
    """The bill's service lines for the service a campaign names.

    By id when the campaign carries them (see marketing.service_links) - a
    renamed service still matches - otherwise by name, as older campaigns do."""
    if ids:
        wanted_ids = {str(i) for i in ids}
        return [line for line in lines if str(line.object_id) in wanted_ids]
    wanted = str(name or '').strip().lower()
    if not wanted:
        return []
    return [line for line in lines if line.service_name() == wanted]


def bill_discount(promo, services, *, subtotal, client, center, today, exclude_invoice_id=None):
    """What `promo` takes off a bill whose service lines are `services`.

    Every promotion discounts services only, on their pre-tax value (#30, D1);
    products are always billed at their MRP. The campaign's own rules decide
    the rest (#28, #53):

      Discount   % or flat Rs, on the whole of the services or on the one
                 service the campaign names; valid only from the campaign's
                 minimum bill.
      FlatPrice  the named service is sold at the flat price; with no service
                 named, a flat Rs amount off the services.
      Trigger    when both of the campaign's services are on the bill, its %
                 comes off the services (F16, D1).
      Cashback   nothing off this bill; the cashback is credited when the bill
                 is completed.
    """
    check_usable(promo, client=client, center=center, today=today,
                 exclude_invoice_id=exclude_invoice_id)
    from marketing.service_links import with_current_names
    config = with_current_names(_config(promo))

    min_bill = D(config.get('min_bill_amount'))
    if min_bill > 0 and money(subtotal) < min_bill:
        raise PromotionError(
            f'Promotion "{promo.name}" needs a bill of at least Rs {money(min_bill)}.')

    base = money(sum((line.gross for line in services), ZERO))
    discount = ZERO

    if promo.promo_type == 'Discount':
        value = _discount_value(promo) or ZERO
        target = services
        if config.get('target') == 'Specific Service' and config.get('specific_service'):
            target = _named(services, config.get('specific_service'),
                            config.get('specific_service_ids'))
            if not target:
                raise PromotionError(
                    f'Promotion "{promo.name}" is for {config.get("specific_service")}, '
                    f'which is not on this bill.')
        target_base = money(sum((line.gross for line in target), ZERO))
        if _discount_type(promo) == 'Percentage':
            discount = money(target_base * value / D('100'))
        else:
            discount = money(value)
        if discount > target_base:
            discount = target_base

    elif promo.promo_type == 'FlatPrice':
        flat = money(D(config.get('flat_price')))
        if config.get('specific_service'):
            target = _named(services, config.get('specific_service'),
                            config.get('specific_service_ids'))
            if not target:
                raise PromotionError(
                    f'Promotion "{promo.name}" is for {config.get("specific_service")}, '
                    f'which is not on this bill.')
            for line in target:
                if D(line.unit_price) > flat:
                    discount += money((D(line.unit_price) - flat) * D(line.quantity))
        else:
            discount = flat

    elif promo.promo_type == 'Trigger':
        pair = [s for s in (config.get('trigger_services') or []) if s]
        id_groups = config.get('trigger_service_ids') or []
        if len(id_groups) == 2 and all(id_groups):
            met = all(_named(services, None, group) for group in id_groups)
        else:
            met = len(pair) >= 2 and all(_named(services, s) for s in pair)
        if not met:
            raise PromotionError(
                f'Promotion "{promo.name}" needs both '
                f'{" and ".join(pair) or "of its services"} on the bill.')
        discount = money(base * D(config.get('trigger_discount')) / D('100'))

    if discount > base:
        discount = base
    return money(discount)


def compute_discount(promo, invoice):
    """The discount this promotion grants on a saved invoice. Never raises.

    Used by apply_promo on a held bill; the checkout prices offers with
    `bill_discount` through the billing engine."""
    from billing import pricing
    from billing.engine import PricedLine

    lines = []
    for item in invoice.items.select_related('content_type').all():
        line = PricedLine(ct_label=pricing.content_type_label(item.content_type),
                          object_id=item.object_id,
                          unit_price=item.unit_price, quantity=item.quantity,
                          description=item.description, is_redemption=item.is_redemption,
                          catalog=pricing.CatalogPrice(obj=item.content_object))
        if line.is_service:
            lines.append(line)
    try:
        return bill_discount(promo, lines, subtotal=money(invoice.subtotal),
                             client=invoice.client, center=invoice.center,
                             today=(invoice.created_at.date() if invoice.created_at
                                    else datetime.date.today()),
                             exclude_invoice_id=invoice.pk)
    except PromotionError:
        return ZERO


def cashback_expiry(promo, earned_on):
    """When cashback from `promo` expires (#35): the campaign's fixed date, or
    its number of valid days from the bill; None when it sets neither."""
    config = _config(promo)
    raw_date = config.get('cashback_specific_date')
    if raw_date:
        try:
            return datetime.date.fromisoformat(str(raw_date)[:10])
        except ValueError:
            pass
    days = int(D(config.get('cashback_validity_days')))
    if days > 0:
        return earned_on + datetime.timedelta(days=days)
    return None


def _award_cashback(promo, invoice, taxable_after_discount):
    """Credit a cashback promotion to the client's wallet."""
    from billing import wallets

    if not invoice.client:
        return ZERO

    config = _config(promo)
    min_bill = D(config.get('cashback_min_bill'))
    percent = D(config.get('cashback_discount'))

    if percent <= 0:
        raise PromotionError(
            f'Cashback promotion "{promo.name}" has no cashback percentage configured.'
        )
    if taxable_after_discount < min_bill:
        return ZERO

    # Cashback on what the customer actually spent, not the pre-discount total.
    amount = money(taxable_after_discount * percent / D('100'))
    cap = D(config.get('cashback_max'))
    if cap > 0 and amount > cap:
        amount = money(cap)
    if amount <= 0:
        return ZERO

    earned_on = invoice.created_at.date() if invoice.created_at else datetime.date.today()
    wallets.credit_cashback(
        invoice.client, amount, invoice=invoice, center=invoice.center,
        note=f'Cashback from promotion: {promo.name}',
        expires_on=cashback_expiry(promo, earned_on),
    )
    return amount


def apply_promotion(invoice, promotion_id, raise_on_error=False, discount_in_bill=False):
    """Apply a promotion to an invoice, once.

    Returns (discount_applied, error_message). With raise_on_error=True the
    error is raised as a DRF ValidationError instead of returned, so a bad
    promotion fails the checkout loudly rather than being logged and ignored.

    discount_in_bill=True is the checkout: the billing engine has already put
    the offer's discount into the bill, so it is recorded, not added a second
    time (finding X3). apply_promo on a held bill still adds it.

    Idempotent: a second call for the same (promotion, invoice) is a no-op.
    """
    from rest_framework.exceptions import ValidationError as DRFValidationError
    from marketing.models import Promotion, PromotionUsage
    from billing.engine import recompute_stored

    def _fail(message):
        if raise_on_error:
            raise DRFValidationError({'promo_id': message})
        return ZERO, message

    promo = Promotion.objects.filter(id=promotion_id).first()
    if not promo:
        return _fail('Promotion not found.')

    # Idempotency: this promotion has already been recorded on this invoice.
    if PromotionUsage.objects.filter(promotion=promo, invoice=invoice).exists():
        logger.info('[Promotions] %s already applied to invoice %s; skipping.',
                    promo.name, invoice.display_number)
        return ZERO, None

    try:
        check_promotion(promo, invoice)
    except PromotionError as exc:
        return _fail(str(exc))

    before = money(invoice.subtotal)
    discount = ZERO if discount_in_bill else compute_discount(promo, invoice)

    if discount > 0:
        invoice.discount = money(D(invoice.discount) + discount)
        invoice.promotion = promo
        invoice.save(update_fields=['promotion'])
        # Recompute the whole header so tax follows the new discount and the
        # rounding column stays truthful.
        recompute_stored(invoice)

    taxable_after = money(D(invoice.subtotal) - D(invoice.discount))

    cashback = ZERO
    if promo.promo_type == 'Cashback':
        try:
            cashback = _award_cashback(promo, invoice, taxable_after)
        except PromotionError as exc:
            return _fail(str(exc))

    PromotionUsage.objects.create(
        promotion=promo,
        invoice=invoice,
        center=invoice.center,
        client=invoice.client,
        bill_amount_before=before,
        bill_amount_after=taxable_after,
        revenue_generated=taxable_after,
        cashback_awarded=cashback,
    )

    return discount, None
