"""
Wallet and value-card debits, in one place.

`finalize_invoice` and the `/pay/` endpoint each had their own copy of this
logic and they disagreed: services.py correctly excluded 'cashback wallet' from
the advance branch, views.py did not, so paying by cashback debited the
customer's advance balance.

Both now call `apply_payment_to_wallets`. Every function here:
  * requires an open transaction (callers use transaction.atomic)
  * locks the balance row it is about to change
  * raises rather than silently capping, so a short balance fails the checkout
    instead of recording a payment that was never actually funded
  * writes reversals as new rows rather than deleting history
"""

import datetime
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from rest_framework.exceptions import ValidationError

from pos_backend.money import D, money, ZERO
from billing import payment_methods as pm
from billing.models import AdvancePayment, CashbackTransaction

logger = logging.getLogger(__name__)


class InsufficientBalance(ValidationError):
    """Raised when a wallet or card cannot fund the requested amount."""


def wallet_scope():
    """Whether a customer's wallet is shared across branches.

    This was never a decision anyone made - the balance queries simply had no
    center filter, so an advance paid at one branch was spendable at any other.
    It is a real policy with real consequences either way:

      'global' (the default, and what the system has always done)
          One balance per customer, spendable anywhere. Convenient for the
          customer, but branch B gives away service that branch A was paid for,
          so the branches owe each other money and somebody has to settle it.
          Every ledger row records its `center`, so that report is possible.

      'center'
          A balance per customer per branch. Clean per-branch accounting, but a
          customer who topped up at one branch is told they have nothing at
          another - so tell the counter staff before switching.

    Changed in Admin > Settings ("Advance and cashback can be used at"); the
    old WALLET_SCOPE server setting still applies until a value is saved there.
    The default is 'global' (owner decision #36).
    """
    from pos_backend import app_settings

    return 'center' if app_settings.get('wallets.scope') == 'center' else 'global'


def _scope_wallet_queryset(queryset, center):
    """Apply the wallet scope policy to a balance query."""
    if wallet_scope() != 'center':
        return queryset
    center_id = getattr(center, 'id', center)
    if center_id is None:
        # Under a per-branch policy, a balance with no branch recorded belongs
        # to no branch. Returning the global total here would let it be spent
        # anywhere, which is the behaviour this policy exists to prevent.
        return queryset.filter(center__isnull=True)
    return queryset.filter(center_id=center_id)


def advance_balance(client_id, lock=False, center=None):
    """Current advance balance for a client, as 2dp money.

    `center` is honoured only when WALLET_SCOPE is 'center'; see wallet_scope().
    """
    qs = _scope_wallet_queryset(
        AdvancePayment.objects.filter(client_id=client_id), center)
    if lock:
        # Lock the ledger rows so a concurrent checkout cannot spend the same
        # balance twice. Cheaper and more correct than locking the Client row,
        # which is what the old code did (the balance does not live there).
        list(qs.select_for_update().values_list('id', flat=True))
    return money(qs.aggregate(t=Sum('amount'))['t'] or 0)


def cashback_balance(client_id, lock=False, center=None):
    """Current cashback wallet balance for a client, as 2dp money.

    `center` is honoured only when WALLET_SCOPE is 'center'; see wallet_scope().
    """
    qs = _scope_wallet_queryset(
        CashbackTransaction.objects.filter(client_id=client_id), center)
    if lock:
        list(qs.select_for_update().values_list('id', flat=True))
    return money(qs.aggregate(t=Sum('amount'))['t'] or 0)


def debit_advance(client, amount, invoice=None, center=None, note=None):
    """Spend `amount` from a client's advance balance. Raises if short."""
    amount = money(amount)
    if amount <= 0:
        return None
    if not client:
        raise ValidationError({'payment_method': 'An advance payment requires a registered client.'})

    available = advance_balance(client.id, lock=True, center=center)
    if available < amount:
        scope_note = ('' if wallet_scope() == 'global'
                      else ' at this branch (wallets are per branch)')
        raise InsufficientBalance({
            'payment_method': (f'Insufficient advance balance{scope_note}. '
                               f'Requested Rs {amount}, available Rs {available}.')
        })
    return AdvancePayment.objects.create(
        client=client, invoice=invoice, center=center, amount=-amount,
        notes=note or (f'Used for Invoice #{invoice.id}' if invoice else 'Advance redeemed'),
    )


def advance_rows_that_count(queryset):
    """The advance-ledger rows that are real money in or out, for reports.

    Cancelling a bill does not delete ledger rows, it adds compensating ones, and
    the bill itself stops counting as sales. Adding up every positive row as
    "advances received" and every negative row as "advances used" therefore
    counted the give-back of a cancelled bill as fresh money received, and its
    spend as money used, long after the bill was gone. Left out here:
      * every row of a cancelled or refunded bill, and
      * a compensating row and the row it compensates (a payment method changed
        on a live bill leaves both behind).
    A balance is still the sum of ALL rows; only reports use this.
    """
    from billing.models import Invoice
    return (queryset
            .exclude(invoice__status__in=Invoice.VOID_STATUSES)
            .filter(reverses__isnull=True, reversed_by__isnull=True))


def credit_advance(client, amount, invoice=None, center=None, note=None, staff=None, gst_amount=0, payment_method='Cash',
                   gst_inclusive=False):
    """Top up a client's advance balance.

    With `gst_inclusive`, `amount` is what the client paid, GST included."""
    amount = money(amount)
    if amount <= 0:
        raise ValidationError({'amount': 'Advance amount must be greater than zero.'})
    if not client:
        raise ValidationError({'client': 'An advance payment requires a registered client.'})
    return AdvancePayment.objects.create(
        client=client, invoice=invoice, center=center, staff=staff,
        amount=amount, notes=note or 'Advance received',
        gst_amount=gst_amount, payment_method=payment_method, gst_inclusive=gst_inclusive,
    )


def debit_cashback(client, amount, invoice=None, center=None, note=None):
    """Spend `amount` from a client's cashback wallet. Raises if short."""
    amount = money(amount)
    if amount <= 0:
        return None
    if not client:
        raise ValidationError({'payment_method': 'A cashback payment requires a registered client.'})

    available = cashback_balance(client.id, lock=True, center=center)
    if available < amount:
        scope_note = ('' if wallet_scope() == 'global'
                      else ' at this branch (wallets are per branch)')
        raise InsufficientBalance({
            'payment_method': (f'Insufficient cashback balance{scope_note}. '
                               f'Requested Rs {amount}, available Rs {available}.')
        })
    consumed = _consume_cashback(client, amount, center)
    return CashbackTransaction.objects.create(
        client=client, invoice=invoice, center=center, amount=-amount,
        kind=CashbackTransaction.KIND_SPENT, consumed=consumed,
        notes=note or (f'Used for Invoice #{invoice.id}' if invoice else 'Cashback redeemed'),
    )


def _consume_cashback(client, amount, center=None):
    """Take `amount` from the client's cashback credits, soonest expiry first
    (#35). Returns {credit_id: amount taken} for the spend row.

    The balance itself is still the ledger sum; `remaining` only decides which
    credits a spend used up, so expiry takes only what is really left of each.
    """
    credits = _scope_wallet_queryset(
        CashbackTransaction.objects.select_for_update()
        .filter(client=client, remaining__gt=0), center)
    # Credits with an expiry first, soonest first; open-ended ones last.
    ordered = sorted(credits, key=lambda c: (c.expires_on is None,
                                             c.expires_on or datetime.date.max, c.id))
    consumed = {}
    left = money(amount)
    for credit in ordered:
        if left <= 0:
            break
        take = credit.remaining if credit.remaining <= left else left
        credit.remaining = money(credit.remaining - take)
        credit.save(update_fields=['remaining'])
        consumed[str(credit.id)] = str(money(take))
        left = money(left - take)
    return consumed


def _restore_cashback(spend_row, amount):
    """Give up to `amount` of a spend back to the credits it was taken from.

    What is given back comes off the spend row's record, so a second reversal
    cannot restore the same credit twice. Returns the amount restored."""
    left = money(amount)
    consumed = dict(spend_row.consumed or {})
    restored = ZERO
    for credit_id, taken in list(consumed.items()):
        if left <= 0:
            break
        credit = (CashbackTransaction.objects.select_for_update()
                  .filter(pk=int(credit_id)).first())
        if credit is None:
            continue
        back = D(taken) if D(taken) <= left else left
        credit.remaining = money(D(credit.remaining) + back)
        credit.save(update_fields=['remaining'])
        consumed[credit_id] = str(money(D(taken) - back))
        left = money(left - back)
        restored = money(restored + back)
    spend_row.consumed = {k: v for k, v in consumed.items() if D(v) > 0}
    spend_row.save(update_fields=['consumed'])
    return restored


def credit_cashback(client, amount, invoice=None, center=None, note=None, expires_on=None):
    """Award cashback to a client's wallet, expiring on `expires_on` (None:
    never)."""
    amount = money(amount)
    if amount <= 0:
        return None
    if not client:
        return None
    return CashbackTransaction.objects.create(
        client=client, invoice=invoice, center=center, amount=amount,
        kind=CashbackTransaction.KIND_EARNED, remaining=amount, expires_on=expires_on,
        notes=note or 'Cashback earned',
    )


def expire_cashback(today=None):
    """Write off what is left of every cashback credit past its expiry (#35).

    A negative 'expired' row per credit, so the balance (the ledger sum) drops
    by exactly what had not been spent. Returns how many credits expired."""
    today = today or datetime.date.today()
    count = 0
    due = (CashbackTransaction.objects
           .filter(remaining__gt=0, expires_on__lt=today)
           .values_list('id', flat=True))
    for credit_id in list(due):
        with transaction.atomic():
            credit = (CashbackTransaction.objects.select_for_update()
                      .filter(pk=credit_id, remaining__gt=0).first())
            if credit is None:
                continue
            lost = money(credit.remaining)
            CashbackTransaction.objects.create(
                client_id=credit.client_id, center_id=credit.center_id, amount=-lost,
                kind=CashbackTransaction.KIND_EXPIRED, reverses=credit,
                notes=f'Cashback expired on {credit.expires_on:%d %b %Y}',
            )
            credit.remaining = ZERO
            credit.save(update_fields=['remaining'])
            count += 1
    return count


def legacy_card_face(card, amount):
    """Face value a pre-Phase 6 value-card payment of `amount` took.

    The old debit scaled the bill amount up by face / post-tax price, so the
    same scaling gives back what was taken."""
    tpl = getattr(card, 'value_card', None)
    face = D(getattr(tpl, 'value', None))
    post = D(getattr(tpl, 'post_tax_price', None))
    if face > 0 and post > 0 and post < face:
        return money(D(amount) * face / post)
    return money(amount)


def debit_value_card(card_id, face, client=None):
    """Take `face` of face value off a ClientValueCard. Raises if short or not
    the client's.

    The bill works out the face value a card pays for (billing.engine), so
    nothing is scaled here. The old debit scaled a bill amount up by
    face/post-tax in floats, while the cancel gave back the unscaled amount.
    """
    from clients.models import ClientValueCard

    amount = money(face)
    if amount <= 0:
        return None
    if not card_id:
        raise ValidationError({
            'value_card_id': 'A value card payment must identify which card to debit.'
        })

    try:
        card = ClientValueCard.objects.select_for_update().get(id=card_id)
    except ClientValueCard.DoesNotExist:
        raise ValidationError({'value_card_id': 'Value card not found.'})

    # Unconditional. The check used to be `if client and ...`, so an invoice
    # with no client attached - a walk-in, which is most of them - skipped it
    # entirely and ANY card id in the request body could be debited. A card
    # payment is only ever meaningful against a named customer who owns it.
    if client is None:
        raise ValidationError({
            'value_card_id': 'A value card can only be used on a bill that has '
                             'a customer. Attach the customer first.'})
    if card.client_id != client.id:
        raise ValidationError({'value_card_id': 'This value card does not belong to the invoice client.'})

    if card.expiry_date and card.expiry_date < datetime.date.today():
        raise ValidationError({'value_card_id': 'This value card has expired.'})

    balance = money(card.balance)
    if balance < amount:
        raise InsufficientBalance({
            'value_card_id': f'Insufficient value card balance. Requested Rs {amount}, available Rs {balance}.'
        })

    card.balance = money(balance - amount)
    if card.balance <= 0:
        card.is_active = False
    card.save(update_fields=['balance', 'is_active'])
    return card


def credit_value_card(card_id, face, reactivate=True):
    """Return `face` of face value to a ClientValueCard (cancel path)."""
    from clients.models import ClientValueCard

    amount = money(face)
    if amount <= 0 or not card_id:
        return None
    try:
        card = ClientValueCard.objects.select_for_update().get(id=card_id)
    except ClientValueCard.DoesNotExist:
        logger.warning('[Wallets] Cannot refund missing value card id=%s', card_id)
        return None
    card.balance = money(D(card.balance) + amount)
    expired = card.expiry_date and card.expiry_date < datetime.date.today()
    if reactivate and card.balance > 0 and not expired and not card.forfeited_on:
        card.is_active = True
    card.save(update_fields=['balance', 'is_active'])
    return card


def payment_card_face(payment):
    """Face value a value-card payment took off its card."""
    if payment.card_face is not None:
        return money(payment.card_face)
    from clients.models import ClientValueCard
    card_id = pm.extract_value_card_id(payment.payment_method, payment.value_card_id)
    card = ClientValueCard.objects.select_related('value_card').filter(id=card_id).first()
    return legacy_card_face(card, payment.amount) if card else money(payment.amount)


def forfeit_expired_cards(today=None):
    """Forfeit the unused balance of every value card past its expiry (F7).

    The balance goes to zero and what was lost is kept on the card. How the
    forfeiture posts to SAP is the SAP team's to specify (D9). Returns how
    many cards were forfeited."""
    from clients.models import ClientValueCard

    today = today or datetime.date.today()
    count = 0
    due = (ClientValueCard.objects.filter(expiry_date__lt=today, balance__gt=0,
                                          forfeited_on__isnull=True)
           .values_list('id', flat=True))
    for card_id in list(due):
        with transaction.atomic():
            card = (ClientValueCard.objects.select_for_update()
                    .filter(pk=card_id, forfeited_on__isnull=True).first())
            if card is None or money(card.balance) <= 0:
                continue
            card.forfeited_amount = money(D(card.forfeited_amount) + money(card.balance))
            card.forfeited_on = today
            card.balance = ZERO
            card.is_active = False
            card.save(update_fields=['forfeited_amount', 'forfeited_on', 'balance', 'is_active'])
            count += 1
    return count


def apply_payment_to_wallets(payment, invoice):
    """Route one Payment row to the correct ledger, exactly once.

    This is the only place that decides which wallet a payment method touches.
    Cash, card and digital methods are no-ops here - they bring in real money
    and have no balance to draw down.
    """
    method = pm.canonical(payment.payment_method)
    center = invoice.center or (invoice.client.center if invoice.client else None)
    amount = money(payment.amount)

    if pm.is_cashback(method):
        return debit_cashback(invoice.client, amount, invoice=invoice, center=center)

    if pm.is_advance(method):
        return debit_advance(invoice.client, amount, invoice=invoice, center=center)

    if pm.is_value_card(method):
        card_id = pm.extract_value_card_id(payment.payment_method, payment.value_card_id)
        if card_id and card_id != payment.value_card_id:
            # Backfill the column so reversal does not have to re-parse.
            payment.value_card_id = card_id
            payment.save(update_fields=['value_card_id'])
        return debit_value_card(card_id, payment_card_face(payment), client=invoice.client)

    if pm.is_gift_card(method):
        # A brand coupon: the code typed on the payment is spent, once.
        from marketing import coupons
        return coupons.redeem_for_payment(payment, invoice)

    return None


def _remaining_exposure(model, invoice):
    """How much of this invoice's wallet spend has not yet been given back.

    `debited` is the total taken from the wallet for this invoice; `returned` is
    the total already handed back. The difference is what a reversal may still
    return.
    """
    rows = model.objects.filter(invoice=invoice)
    debited = -money(
        rows.filter(amount__lt=0).aggregate(t=Sum('amount'))['t'] or 0)
    returned = money(
        rows.filter(amount__gt=0, reverses__isnull=False)
            .aggregate(t=Sum('amount'))['t'] or 0)
    remaining = debited - returned
    return remaining if remaining > 0 else ZERO


def _reverse_ledger(model, invoice, center, amount, note):
    """Give back up to `amount` of what this invoice took from a wallet.

    The old version matched ONE debit row per wallet per invoice:

        original = model.objects.filter(
            invoice=invoice, amount__lt=0, reverses__isnull=True).first()
        if original and model.objects.filter(reverses=original).exists():
            return None

    `reverses` is set on the REVERSAL row, never on the debit, so `.first()`
    kept returning the same debit. On a split-tender bill - two Advance payments
    of Rs 500 and Rs 550, which is routine at a till - the first payment was
    reversed and the second hit the `already reversed` branch and returned None.
    The customer permanently lost Rs 550 of their own prepaid balance, and the
    cancel returned HTTP 200 as though it had worked. Changing a payment method
    (Advance -> Cash -> Advance -> Cash) lost money the same way.

    Reversing against the invoice's REMAINING EXPOSURE fixes it for split
    tenders, for repeated payment-method changes, and for rows already in the
    database that have no per-payment link to match on. It also keeps the
    double-cancel protection that mattered: once the full exposure is returned,
    further reversals are no-ops rather than gifts.
    """
    remaining = _remaining_exposure(model, invoice)
    if remaining <= 0:
        return None

    giving_back = amount if amount <= remaining else remaining

    # Link to a debit row for the audit trail. Which specific row matters less
    # than the total being right, so prefer one that has no reversal against it.
    original = (model.objects
                .filter(invoice=invoice, amount__lt=0)
                .exclude(id__in=model.objects
                         .filter(invoice=invoice, reverses__isnull=False)
                         .values_list('reverses_id', flat=True))
                .first())
    if original is None:
        original = model.objects.filter(invoice=invoice, amount__lt=0).first()

    extra = {}
    if model is CashbackTransaction:
        # The money goes back to the credits it came from, with their expiry.
        left = giving_back
        for spend in (CashbackTransaction.objects.select_for_update()
                      .filter(invoice=invoice, amount__lt=0, reverses__isnull=True)
                      .order_by('id')):
            if left <= 0:
                break
            left = money(left - _restore_cashback(spend, left))
        extra['kind'] = CashbackTransaction.KIND_REVERSAL

    return model.objects.create(
        client=invoice.client, invoice=invoice, center=center,
        amount=giving_back, notes=note, reverses=original, **extra,
    )


def reverse_payment_wallets(payment, invoice, reason='Invoice cancelled'):
    """Undo one Payment's wallet effect by writing a compensating entry.

    Deliberately does NOT delete the original debit row. The old cancel path
    deleted negative ledger rows, which destroyed the audit trail and made a
    double-cancel silently gift the client their money back twice.
    """
    method = pm.canonical(payment.payment_method)
    center = invoice.center or (invoice.client.center if invoice.client else None)
    amount = money(payment.amount)
    note = f'{reason} - reversal for Invoice #{invoice.id}'

    if pm.is_cashback(method):
        return _reverse_ledger(CashbackTransaction, invoice, center, amount, note)

    if pm.is_advance(method):
        return _reverse_ledger(AdvancePayment, invoice, center, amount, note)

    if pm.is_value_card(method):
        card_id = pm.extract_value_card_id(payment.payment_method, payment.value_card_id)
        # The face value the payment took - not the bill amount it settled.
        return credit_value_card(card_id, payment_card_face(payment))

    if pm.is_gift_card(method):
        # The bill is gone, so the coupon goes back to being usable.
        from marketing import coupons
        return coupons.release_for_payment(payment)

    return None


def validate_payments_against_balances(client, payments):
    """Pre-flight check for a batch of payments, before anything is written.

    `payments` is an iterable of dicts with `payment_method`, `amount` and
    optionally `value_card_id`. Aggregating per wallet first matters: two
    Rs 500 advance payments on one invoice must be checked against Rs 1000 of
    balance, not Rs 500 twice.
    """
    from clients.models import ClientValueCard

    totals = {'advance': ZERO, 'cashback': ZERO}
    per_card = {}

    for p in payments:
        method = pm.canonical(p.get('payment_method'))
        amount = money(p.get('amount') or 0)
        if amount <= 0:
            raise ValidationError({'payments': 'Payment amounts must be greater than zero.'})

        if pm.is_advance(method):
            totals['advance'] += amount
        elif pm.is_cashback(method):
            totals['cashback'] += amount
        elif pm.is_value_card(method):
            card_id = pm.extract_value_card_id(p.get('payment_method'), p.get('value_card_id'))
            if not card_id:
                raise ValidationError({
                    'value_card_id': 'A value card payment must identify which card to debit.'
                })
            per_card[card_id] = per_card.get(card_id, ZERO) + amount

    needs_client = totals['advance'] > 0 or totals['cashback'] > 0 or per_card
    if needs_client and not client:
        raise ValidationError({
            'payments': 'Advance, cashback and value card payments require a registered client. '
                        'Use cash or card for a walk-in.'
        })

    if totals['advance'] > 0:
        available = advance_balance(client.id)
        if available < totals['advance']:
            raise InsufficientBalance({
                'payments': f'Insufficient advance balance. Requested Rs {totals["advance"]}, '
                            f'available Rs {available}.'
            })

    if totals['cashback'] > 0:
        available = cashback_balance(client.id)
        if available < totals['cashback']:
            raise InsufficientBalance({
                'payments': f'Insufficient cashback balance. Requested Rs {totals["cashback"]}, '
                            f'available Rs {available}.'
            })

    for card_id, amount in per_card.items():
        try:
            card = ClientValueCard.objects.get(id=card_id, client=client)
        except ClientValueCard.DoesNotExist:
            raise ValidationError({'value_card_id': 'Value card not found or does not belong to client.'})
        if money(card.balance) < amount:
            raise InsufficientBalance({
                'value_card_id': f'Insufficient value card balance. Requested Rs {amount}, '
                                 f'available Rs {money(card.balance)}.'
            })
