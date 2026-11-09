"""
Post-save invoice finalisation: stock, perks, service logs, wallet deductions.

Changes from the audited version:
  * Idempotent. `finalized_at` is set under a row lock, so a double call (or a
    retry after a partial failure) cannot deduct stock or provision a
    membership twice. The docstring used to say "must only be called ONCE" with
    nothing enforcing it.
  * No Decimal/float mixing. The value-card incentive ratio was seeded with the
    float 1.0 when the invoice total was zero, which raised TypeError and 500'd
    any fully-redeemed bill.
  * Perk provisioning failures are fatal. They used to be logged and swallowed,
    so a customer could pay for a membership and never receive it.
  * Package redemption is driven by InvoiceItem.is_redemption and records
    which package was debited, instead of matching an emoji in the description.
  * Staff commission splits use exact paise distribution.
  * Wallet routing is delegated to billing.wallets, shared with the /pay/
    endpoint, which used to route cashback into the advance ledger.
"""

import datetime
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from pos_backend.money import D, money, split, split_weighted, ZERO, quantize_ratio
from billing import payment_methods as pm
from billing import wallets

logger = logging.getLogger(__name__)


class AlreadyFinalized(Exception):
    """Raised internally when finalisation has already run for an invoice."""


class PerkNotIssuable(ValidationError, ValueError):
    """A bill line sells a perk that cannot be issued (a package with no services, a card with no value, a master that was deleted).

    Still a ValueError, so a caller that catches that keeps working; but it is also a DRF ValidationError, so the till gets a
    400 with the reason instead of a 500, and the bill stays unpaid (the whole completion rolls back)."""


# ─── Totals ───────────────────────────────────────────────────────────────

def recompute_invoice_totals(invoice, save=True):
    """Recompute subtotal / tax / rounding / total from the stored line items.

    Anything that changes a saved invoice's discount must call this. It is the
    billing engine's own arithmetic (`billing.engine.recompute_stored`), so a
    recomputed bill matches one priced at the till.
    """
    from billing.engine import recompute_stored
    return recompute_stored(invoice, save=save)


# ─── Perks ────────────────────────────────────────────────────────────────

def _provision_marketing_perks(invoice, item, request_data=None):
    """Issue Memberships, Packages and Value Cards purchased on this invoice.

    Exceptions propagate. A silent failure here means the customer paid for an
    entitlement they never received, which must fail the checkout rather than
    leave a log line nobody reads.
    """
    if not invoice.client:
        return
    if not item.content_type or item.content_type.app_label != 'marketing':
        return

    from clients.models import ClientMembership, ClientPackage, ClientValueCard

    model_name = item.content_type.model
    today = invoice.created_at.date() if invoice.created_at else datetime.date.today()
    quantity = int(item.quantity or 1)

    if model_name == 'membership':
        membership = item.content_object
        if not membership:
            raise PerkNotIssuable(f'Membership id={item.object_id} no longer exists; cannot issue it.')
        days = int(membership.expiry_days or 0)
        if days <= 0:
            raise PerkNotIssuable(f'Membership "{membership.name}" has no validity period configured.')

        # Stack renewals as separate rows rather than extending an existing row
        # in place. An in-place extension was unrecoverable on cancellation:
        # the reversal only deleted rows whose source_invoice matched, so a
        # refunded renewal left the customer with the extra days for free.
        latest = (ClientMembership.objects
                  .filter(client=invoice.client, membership=membership, is_active=True)
                  .order_by('-expiry_date').first())
        cursor = today
        if latest and latest.expiry_date and latest.expiry_date > today:
            cursor = latest.expiry_date
        for _ in range(quantity):
            ClientMembership.objects.create(
                client=invoice.client, membership=membership,
                start_date=cursor,
                expiry_date=cursor + timedelta(days=days),
                source_invoice=invoice, center=invoice.center,
            )
            cursor = cursor + timedelta(days=days)

    elif model_name == 'package':
        package = item.content_object
        services_remaining = {}
        validity_days = 90  # default for ad-hoc bundles with no template

        if package:
            validity_days = int(package.validity_days or 90)
            for svc in (package.services_json or []):
                svc_id = str(svc.get('id') or '')
                if svc_id:
                    qty = int(D(svc.get('pkgQty', 1)) or 1) * quantity
                    services_remaining[svc_id] = services_remaining.get(svc_id, 0) + qty
        else:
            # Custom bundle: the service list only exists in the request body.
            for idata in (request_data or {}).get('items', []) or []:
                if not isinstance(idata, dict):
                    continue
                if idata.get('description') != item.description:
                    continue
                for svc in idata.get('custom_package_services') or []:
                    svc_id = str(svc.get('id') or '')
                    if svc_id:
                        qty = int(D(svc.get('pkgQty', 1)) or 1) * quantity
                        services_remaining[svc_id] = services_remaining.get(svc_id, 0) + qty
                break

        if not services_remaining:
            raise PerkNotIssuable(
                f'Package "{item.description}" was billed but contains no services; '
                'cannot issue an empty package.'
            )

        ClientPackage.objects.create(
            client=invoice.client, package=package,
            services_remaining=services_remaining,
            start_date=today,
            expiry_date=today + timedelta(days=validity_days),
            source_invoice=invoice, center=invoice.center,
        )

    elif model_name == 'valuecard':
        vcard = item.content_object
        if not vcard:
            raise PerkNotIssuable(f'Value card id={item.object_id} no longer exists; cannot issue it.')
        days = int(vcard.expiry_days or 0)
        if days <= 0:
            raise PerkNotIssuable(f'Value card "{vcard.title}" has no validity period configured.')
        value = D(vcard.value)
        if value <= 0:
            raise PerkNotIssuable(f'Value card "{vcard.title}" has no load value configured.')
        # The card's terms are fixed now (F5): its face value, the pre-tax
        # price actually paid for it and its GST rate. A later change to the
        # template never changes what a card already sold is worth.
        paid_pre_tax = money((D(item.total_price) - D(item.bill_discount_share)) / D(quantity))
        rate = D(vcard.gst_percent) if vcard.gst_percent is not None else D(item.tax_percentage)
        for _ in range(quantity):
            ClientValueCard.objects.create(
                client=invoice.client, value_card=vcard,
                balance=money(value), face_value=money(value),
                pre_tax_price=paid_pre_tax, gst_percent=rate,
                start_date=today,
                expiry_date=today + timedelta(days=days),
                source_invoice=invoice, center=invoice.center,
            )


def _deduct_package_service(invoice, item, active_packages):
    """Debit a redeemed service from the client's package entitlement.

    Records which package was debited on the item, so cancellation credits the
    right one back. Previously the reversal credited the first package that
    happened to contain the service id - including expired ones, which it then
    reactivated.
    """
    if not item.is_redemption or not invoice.client or not item.object_id:
        return

    svc_id = str(item.object_id)
    remaining_to_deduct = int(item.quantity or 1)
    debited = None

    # Oldest expiry first, so entitlements that lapse soonest are used first.
    for cp in sorted(active_packages, key=lambda p: p.expiry_date or datetime.date.max):
        store = cp.services_remaining or {}
        have = int(D(store.get(svc_id, 0)))
        if have <= 0:
            continue
        take = min(have, remaining_to_deduct)
        store[svc_id] = have - take
        cp.services_remaining = store
        if all(int(D(v)) <= 0 for v in store.values()):
            cp.is_active = False
        cp.save(update_fields=['services_remaining', 'is_active'])
        remaining_to_deduct -= take
        debited = debited or cp
        if remaining_to_deduct <= 0:
            break

    if remaining_to_deduct > 0:
        # The serializer already validated availability, so reaching here means
        # a concurrent redemption won the race. Fail the checkout.
        raise ValueError(
            f'Package entitlement for this service was exhausted by another '
            f'transaction ({remaining_to_deduct} short). Please re-check the bill.'
        )

    if debited and item.redeemed_from_package_id != debited.id:
        item.redeemed_from_package = debited
        item.save(update_fields=['redeemed_from_package'])


# ─── Stock ────────────────────────────────────────────────────────────────

def _use_recipe_items(invoice, item, service):
    """A service whose recipe the POS owns used these products: resolve them (every
    required choice must be made now), keep the list on the bill line and take the stock.
    The A/R Invoice lists the same items under the service, so SAP takes its stock off
    that one document; nothing is posted to SAP separately.

    Every item (technical ones too) is deducted; retail never reaches here."""
    from rest_framework.exceptions import ValidationError
    from inventory.models import Product
    from inventory.quantities import plain, qty
    from inventory.stock import record_out
    from services import recipes

    center = invoice.center or (invoice.client.center if invoice.client else None)
    if not center:
        return
    entries = recipes.resolve_item(item, strict=True)
    item.consumables = entries
    item.save(update_fields=['consumables'])

    # One stock movement per product, however many entries name it (a towel that is
    # fixed and also added as an extra is one stock movement and one invoice line).
    names = {e['product_code']: e['name'] for e in entries}
    for code, amount in recipes.deductions(entries).items():
        if amount <= 0:
            continue
        entry = {'product_code': code, 'name': names[code]}
        product = Product.objects.select_for_update().filter(center=center, product_code=code).first()
        if product is None:
            raise ValidationError({'items': f"'{entry['name']}' is not stocked at this branch, so "
                                            f"{item.description} cannot use it."})
        before = qty(product.current_stock)
        # Same rule as a product line: checked on the locked row, so two tills cannot
        # both use the last of an item.
        if amount > before:
            raise ValidationError({'items': f"Not enough stock for '{product.name}' used by {item.description}. "
                                            f"Required: {plain(amount)}, Available: {plain(before)}."})
        product.current_stock = before - amount
        product.save(update_fields=['current_stock'])
        rows = record_out(product, center, 'SERVICE_USE', amount,
                          f'{item.description} used for Invoice {invoice.display_number}', invoice=invoice)
        # SAP deducts these when the A/R Invoice (which lists them under the service) is
        # posted, so these rows must never be sent as Goods Issues too.
        for row in rows:
            row.sap_sync_status = True
            row.sap_sync_message = "Synced via AR Invoice (items used under the service)"
            row.save(update_fields=['sap_sync_status', 'sap_sync_message'])


def _deduct_stock(invoice, item, owners=None):
    """Deduct a sold product from stock and record the ledger entry.

    Logs the delta actually applied, not the delta requested. The old code
    floored the stock update at zero but recorded the full requested quantity,
    so `current_stock` and the StockTransaction ledger drifted apart and
    stock_history (which derives from the ledger) became wrong.
    """
    from inventory.models import Product, StockTransaction
    from inventory.quantities import ZERO, qty
    from inventory.stock import record_out

    if not item.content_type:
        return

    if item.content_type.app_label == 'services' and item.content_type.model == 'servicemaster':
        from services.models import ServiceMaster, deducting_boms
        from services import recipes
        owner = owners.get(item.object_id) if owners is not None else ServiceMaster.objects.filter(pk=item.object_id).first()
        if owner is not None and recipes.uses_recipe(owner):
            _use_recipe_items(invoice, item, owner)
            return
        requested_svc = int(item.quantity or 0)
        if requested_svc <= 0:
            return

        center = invoice.center or (invoice.client.center if invoice.client else None)
        if not center:
            return

        # Every used item is deducted (technical ones too); retail products are not part of a service.
        boms = deducting_boms(item.object_id, center)
        if not boms:
            return

        for bom in boms:
            product = Product.objects.select_for_update().filter(center=center, product_code=bom.product_code).first()
            if not product:
                logger.warning('[Billing] BOM Product code=%s not found in center=%s for service %s; cannot deduct.', 
                               bom.product_code, center.id, item.object_id)
                continue
                
            qty_to_deduct = qty(bom.quantity * requested_svc)
            if qty_to_deduct <= 0:
                continue
                
            before = qty(product.current_stock)
            # Same rule as a product line: checked on the locked row, so two
            # tills cannot both use the last of a consumable.
            if qty_to_deduct > before:
                from rest_framework.exceptions import ValidationError
                from inventory.quantities import plain
                raise ValidationError({
                    'items': f"Not enough stock for consumable '{product.name}'. "
                             f"Required: {plain(qty_to_deduct)}, Available: {plain(before)}."
                })
            product.current_stock = before - qty_to_deduct
            product.save(update_fields=['current_stock'])

            # One ledger row per batch used (earliest expiry first).
            for st in record_out(
                    product, center, 'BOM_CONSUMPTION', qty_to_deduct,
                    f'BOM consumption for {item.description} via Invoice {invoice.display_number}',
                    invoice=invoice):
                # DO NOT SYNC this to SAP via create_sap_stock_transaction! SAP deducts BOM components automatically
                # when the A/R Invoice (containing the Sales BOM) is synced.
                # This POS StockTransaction just keeps local inventory accurate.
                st.sap_sync_status = True
                st.sap_sync_message = "Synced via AR Invoice Sales BOM explosion"
                st.save(update_fields=['sap_sync_status', 'sap_sync_message'])
        return

    if item.content_type.app_label != 'inventory':
        return

    product = Product.objects.select_for_update().filter(pk=item.object_id).first()
    if not product:
        logger.warning('[Billing] Product id=%s not found; no stock deducted for invoice #%s',
                       item.object_id, invoice.id)
        return

    requested = qty(item.quantity or 0)
    if requested <= 0:
        return

    before = qty(product.current_stock)
    center = invoice.center or (invoice.client.center if invoice.client else None)

    # Nothing is sold without stock (owner decisions #52 and #18). The check
    # that runs when a bill is raised cannot see a sale another till is
    # finishing at the same moment, so it is repeated here, on the row this
    # transaction has locked: of two tills selling the last unit, one is
    # refused and its bill rolls back, nothing recorded.
    if requested > before:
        from rest_framework.exceptions import ValidationError
        from inventory.quantities import plain
        raise ValidationError({
            'items': f"Not enough stock for '{product.name}'. "
                     f"Required: {plain(requested)}, Available: {plain(before)}."
        })

    product.current_stock = before - requested
    product.save(update_fields=['current_stock'])
    record_out(product, center, 'SALE', requested, f'Sold via Invoice {invoice.display_number}',
               invoice=invoice)


# ─── Commission logs ──────────────────────────────────────────────────────

def _real_money_ratio(invoice):
    """Fraction of this invoice settled with real money rather than a balance.

    Staff earn commission on the real-money proportion: a Rs 15,000 bill paid
    Rs 10,000 cash + Rs 5,000 value card earns commission on two thirds.
    Returns a Decimal - the old code returned the float 1.0 in the zero-total
    branch, which then blew up on `Decimal * float`.
    """
    total = D(invoice.total_amount)
    if total <= 0:
        return D('1')

    liability_paid = ZERO
    for payment in invoice.payments.all():
        if payment.is_tip:
            continue
        if pm.is_liability_drawdown(payment.payment_method):
            liability_paid += D(payment.amount)

    real_paid = total - liability_paid
    if real_paid <= 0:
        return ZERO
    if real_paid >= total:
        return D('1')
    return quantize_ratio(real_paid, total)

def redemption_earns_commission():
    """Does performing a service redeemed from a package earn commission?

    Another policy that was never decided, only implemented. A redemption line
    has `total_price = 0` (the customer paid when they bought the package), so:

      * the ServiceLog is written at Rs 0, and
      * `finance.incentives.calculate` skips redemption lines outright.

    So the stylist who actually performs the service earns nothing for it. That
    is defensible - the package sale already paid commission - but it is not
    obviously right either, because the person who sold the package and the
    person who does the work are often different people, and the performer sees
    a Rs 0 row against real work.

    Default False (owner decision #71). Changed in Admin > Settings ("Package
    redemptions earn commission") to pay the performer on the service's catalog
    value instead.
    """
    from pos_backend import app_settings

    return bool(app_settings.get('staff.package_redemption_earns_commission'))


def commission_base(item):
    """The line value commission is calculated on, before the money ratio.

    Normally the tax-exclusive line total. For a redemption that is zero, so
    when the policy pays for redemptions the catalog value of the service is
    used instead - what the line would have been worth had it been sold.
    """
    if getattr(item, 'is_redemption', False):
        if not redemption_earns_commission():
            return ZERO
        return money(D(item.unit_price) * D(item.quantity or 1))
    return D(item.total_price)


def _create_service_logs(invoice, item, ratio):
    """Write one ServiceLog per credited staff member, splitting exactly."""
    from staff.models import ServiceLog

    # An advance is held for the client; it is not a sale credited to anyone.
    if item.content_type_id and item.content_type.model == 'advancepayment':
        return

    staff_list = list(item.staff_members.all())
    if item.staff and item.staff not in staff_list:
        staff_list.append(item.staff)
    if not staff_list:
        return

    # Commission is based on the tax-exclusive line value. total_price used to
    # be tax-inclusive, which inflated every commission by the GST rate.
    # `commission_base` is zero for a redemption unless the operator has chosen
    # to pay for those; see redemption_earns_commission().
    effective = money(commission_base(item) * ratio)
    # Each staff member's share follows the contribution % chosen at the till
    # (#49). It used to be split evenly whatever was chosen - the split never
    # even reached the server.
    allocations = item.staff_allocations if isinstance(item.staff_allocations, dict) else {}
    if allocations:
        weights = [allocations.get(str(m.id), allocations.get(m.id, 0)) for m in staff_list]
        shares = split_weighted(effective, weights)
    else:
        shares = split(effective, len(staff_list))

    ct = item.content_type
    service_type = 'Service'
    if ct:
        if ct.app_label == 'inventory':
            service_type = 'Product'
        elif ct.app_label == 'marketing':
            model = ct.model or ''
            if 'membership' in model:
                service_type = 'Membership'
            elif 'package' in model:
                service_type = 'Package'
            elif 'valuecard' in model:
                service_type = 'Value Card'
            else:
                service_type = 'Product'

    service_name = item.description or (
        getattr(item.content_object, 'name', None) or str(item.content_object)
    )
    log_date = invoice.created_at.date() if invoice.created_at else datetime.date.today()
    log_time = invoice.created_at.time() if invoice.created_at else timezone.now().time()

    for member, share in zip(staff_list, shares):
        center = (
            invoice.center
            or (invoice.client.center if invoice.client and invoice.client.center else None)
            or member.center
        )
        ServiceLog.objects.create(
            invoice=invoice,
            staff=member,
            center=center,
            client_name=invoice.client.full_name if invoice.client else 'Walk-in',
            service_name=(service_name or 'Item')[:255],
            service_type=service_type,
            price=share,
            date=log_date,
            time=log_time,
        )


# ─── Entry point ──────────────────────────────────────────────────────────

def finalize_invoice(invoice, appointment_id=None, request_data=None, skip_payment_deductions=False):
    """Run every post-save finalisation task for an invoice, exactly once.

    Must be called inside a transaction. Raises on any failure - a checkout
    that cannot be finalised must not be recorded as paid.

    Args:
        skip_payment_deductions: True when the caller has already applied the
            wallet debits itself (the /pay/ endpoint does).
    """
    from billing.models import Invoice

    # Idempotency guard under a row lock.
    locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
    if locked.finalized_at is not None:
        logger.info('[Billing] Invoice #%s already finalized at %s; skipping.',
                    invoice.pk, locked.finalized_at)
        return invoice
    invoice.finalized_at = timezone.now()
    Invoice.objects.filter(pk=invoice.pk).update(finalized_at=invoice.finalized_at)

    # Allocate the GST serial number now that the invoice is leaving draft.
    if not invoice.invoice_number and invoice.center_id:
        invoice.assign_invoice_number()

    # Freeze who and where the bill is for, so a reprint never changes.
    from pos_backend.gst import snapshot_invoice
    fields = snapshot_invoice(invoice)
    Invoice.objects.filter(pk=invoice.pk).update(**{f: getattr(invoice, f) for f in fields})

    active_packages = []
    if invoice.client:
        from clients.models import ClientPackage
        active_packages = list(
            ClientPackage.objects.select_for_update().filter(
                client=invoice.client, is_active=True,
                expiry_date__gte=datetime.date.today(),
            )
        )
        # A package is redeemed only at the branch that sold it (#32).
        from pos_backend import app_settings
        if app_settings.get('perks.selling_branch_only') and invoice.center_id:
            from billing.engine import perk_branch_id
            active_packages = [cp for cp in active_packages
                               if perk_branch_id(cp) in (None, invoice.center_id)]

    ratio = _real_money_ratio(invoice)

    items = list(
        invoice.items
        .select_related('content_type', 'staff')
        .prefetch_related('staff_members')
        .all()
    )

    # The services on the bill, in one query, so each line knows whose recipe it follows.
    from services.models import ServiceMaster
    service_ids = [i.object_id for i in items
                   if i.content_type and i.content_type.app_label == 'services' and i.content_type.model == 'servicemaster']
    owners = ServiceMaster.objects.in_bulk(service_ids) if service_ids else {}

    for item in items:
        _deduct_stock(invoice, item, owners)
        _deduct_package_service(invoice, item, active_packages)
        _provision_marketing_perks(invoice, item, request_data)
        if item.content_type and item.content_type.model == 'advancepayment':
            # Try to determine primary payment method
            primary_method = 'Cash'
            primary_payment = invoice.payments.exclude(payment_method__in=['Advance', 'Value Card', 'Cashback Wallet', 'Membership']).first()
            if primary_payment:
                primary_method = primary_payment.payment_method
                
            # The client is credited what they paid, GST included: Rs 20,000
            # + Rs 1,000 GST is a Rs 21,000 balance (SAP scenario 01).
            adv_gst = money(D(item.tax_amount or 0))
            adv_obj = wallets.credit_advance(
                client=invoice.client,
                amount=money(D(item.total_price) + adv_gst),
                invoice=invoice,
                center=invoice.center,
                staff=item.staff,
                gst_amount=adv_gst,
                payment_method=primary_method,
                gst_inclusive=True,
            )
            from pos_backend.sap_utils import create_sap_advance_je, run_sap_sync_async
            run_sap_sync_async(create_sap_advance_je, adv_obj)

        # SAP Journal Entry for Value Cards, Memberships and Packages sold
        if item.content_type and item.content_type.app_label == 'marketing':
            model = item.content_type.model
            if model == 'valuecard':
                from pos_backend.sap_utils import create_sap_valuecard_je, run_sap_sync_async
                run_sap_sync_async(create_sap_valuecard_je, invoice, item)
            elif model == 'membership':
                from pos_backend.sap_utils import create_sap_membership_je, run_sap_sync_async
                run_sap_sync_async(create_sap_membership_je, invoice, item)
            elif model == 'package':
                from pos_backend.sap_utils import create_sap_package_je, run_sap_sync_async
                run_sap_sync_async(create_sap_package_je, invoice, item)

        _create_service_logs(invoice, item, ratio)

    # Something the client can spend later was sold: they now have a PIN, and it is sent to them.
    sold = None
    for item in items:
        model = item.content_type.model if item.content_type else ''
        if model in ('advancepayment', 'valuecard', 'membership', 'package'):
            sold = {'advancepayment': 'advance', 'valuecard': 'value card', 'membership': 'membership',
                    'package': 'package'}[model]
    if sold and invoice.client_id:
        try:
            from clients import pins
            pins.on_sale(invoice.client, sold, invoice.pk)
        except Exception:                                                  # noqa: BLE001
            # A bill is never held up by a PIN message; the master PIN covers a client who did not get theirs.
            logger.warning('[Billing] could not give the client a PIN for invoice %s', invoice.pk, exc_info=True)

    # Mark the appointment served: the one this bill is linked to (the
    # serializer has already checked it is this visit), and only while it is
    # still open. `appointment_id` is accepted for older callers but no longer
    # decides anything by itself - an appointment the bill does not point at
    # is never completed.
    if invoice.appointment_id and invoice.status in ('paid', 'partial'):
        from appointments.models import Appointment
        Appointment.objects.filter(
            id=invoice.appointment_id, status__in=Appointment.BOOKED,
        ).update(status='Completed')

    # Wallet / value-card debits.
    if not skip_payment_deductions:
        for payment in invoice.payments.all():
            if payment.is_tip:
                continue
            wallets.apply_payment_to_wallets(payment, invoice)

    # Promotion effects (usage tracking, cashback award). The billing engine
    # already put the offer's discount into the bill and capped a membership's
    # before any payment was written (X3, X10); this only records it.
    if invoice.promotion_id:
        from marketing.promotions import apply_promotion
        apply_promotion(invoice, invoice.promotion_id, raise_on_error=True, discount_in_bill=True)

    # A B2B tax invoice gets its IRN from the e-invoice portal (Q4b).
    from billing import einvoice
    einvoice.queue_for(invoice)

    # --- SAP Integration ---
    from pos_backend.sap_utils import create_sap_invoice, run_sap_sync_async
    run_sap_sync_async(create_sap_invoice, invoice)

    logger.info('[Billing] finalize_invoice completed for invoice %s', invoice.display_number)
    return invoice
