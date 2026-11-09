"""
Invoice serialization with server-authoritative money math.

The previous version computed `expected_subtotal` from the client's own
unit_price values and then wrote that result into total_amount as "server
authoritative math" - so any internally-consistent payload was accepted at
whatever total it claimed. Separately, create() rewrote line items with
database prices but never recomputed the header, so items and header disagreed.

Now there is exactly one computation, `billing.engine.price_bill`. It prices
every line against the catalogue, works out the offer (promotion or
membership) and the value-card cover from their ids, and derives subtotal /
tax / rounding / total. validate() and create()/update() consume the same
result, and the `quote` endpoint returns it unsaved. Client-supplied monetary
fields on the header are ignored.
"""

import logging

from rest_framework import serializers

from pos_backend.serializer_guards import ServerFieldsReadOnly

from staff.models import StaffMember
from pos_backend.money import D, money, ZERO
from billing import engine, pricing, wallets
from billing import payment_methods as pm
from billing.models import (
    Invoice, InvoiceItem, AdvancePayment, Payment, BillChangeLog,
)

logger = logging.getLogger(__name__)

# Kept importable from here; the engine owns them now.
PricedLine = engine.PricedLine
_is_redemption_payload = engine._is_redemption_payload


class InvoiceItemSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    content_type = serializers.CharField(required=False, allow_null=True)
    staff = serializers.PrimaryKeyRelatedField(queryset=StaffMember.objects.all(), required=False, allow_null=True)
    staff_members = serializers.PrimaryKeyRelatedField(queryset=StaffMember.objects.all(), required=False, many=True, allow_empty=True)
    # Tax-inclusive line total, for display and printing. Read-only: the stored
    # total_price is tax-exclusive so that sum(items) == invoice.subtotal.
    gross_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = InvoiceItem
        fields = ('id', 'content_type', 'object_id', 'description', 'unit_price', 'discount',
                  'manager_discount', 'quantity', 'tax_percentage', 'tax_amount', 'total_price',
                  'gross_total', 'is_redemption', 'staff', 'staff_members', 'staff_allocations',
                  'item_code', 'hsn_sac', 'bill_discount_share', 'value_card', 'card_face',
                  'card_benefit', 'card_taxable', 'card_tax', 'card_tax_percentage',
                  'recipe_choices', 'consumables')
        # Every monetary field is derived server-side from the catalog.
        read_only_fields = ('tax_percentage', 'tax_amount', 'total_price', 'item_code', 'hsn_sac',
                            'bill_discount_share', 'value_card', 'card_face', 'card_benefit',
                            'card_taxable', 'card_tax', 'card_tax_percentage', 'consumables')

    def to_internal_value(self, data):
        if isinstance(data, dict):
            # Normalise nested staff objects -> IDs
            if 'staff' in data:
                staff_value = data.get('staff')
                if isinstance(staff_value, dict) and 'id' in staff_value:
                    data['staff'] = staff_value['id']
            if 'staff_members' in data and isinstance(data.get('staff_members'), list):
                new_members = []
                for s in data['staff_members']:
                    if isinstance(s, dict) and 'id' in s:
                        new_members.append(s['id'])
                    else:
                        new_members.append(s)
                data['staff_members'] = new_members
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # Return content_type as "app_label.model" (e.g. "services.servicemaster")
        ret['content_type'] = pricing.content_type_label(instance.content_type)
        # No derived-tax fallback here. The old code recomputed
        # tax_amount = total_price * pct/100 when tax_amount was falsy, but
        # total_price was tax-inclusive at the time, so it taxed the tax.
        ret['gross_total'] = str(instance.gross_total)
        ret['taxable_value'] = str(money(D(instance.total_price) - D(instance.bill_discount_share)))
        return ret


class PaymentSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(required=False)
    change_given = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Payment
        fields = ('id', 'amount', 'payment_method', 'value_card_id', 'card_face', 'reference',
                  'tendered', 'change_given', 'is_tip', 'created_at')
        read_only_fields = ('created_at', 'card_face')

    def validate_amount(self, value):
        if D(value) <= 0:
            raise serializers.ValidationError('Payment amount must be greater than zero.')
        return value

    def validate_is_tip(self, value):
        # Owner decision #39: no tips. Old bills keep their tip rows readable.
        if value:
            raise serializers.ValidationError('Tips are not taken on bills.')
        return value

    def validate_payment_method(self, value):
        # Owner decision #40: Nearbuy is no longer accepted.
        if pm.canonical(value) == Payment.NEARBUY:
            raise serializers.ValidationError('Nearbuy is no longer accepted as a payment method.')
        return value

    def validate_reference(self, value):
        value = (value or '').strip()
        if len(value) > 64:
            raise serializers.ValidationError('A payment reference is at most 64 characters.')
        return value

    def validate(self, data):
        method = pm.canonical(data.get('payment_method'))
        tendered = data.get('tendered')
        if tendered is not None:
            # Only cash can be handed over in excess, with change given (#38).
            if not pm.is_cash(method):
                raise serializers.ValidationError(
                    {'tendered': 'Only cash is tendered; online payments are for the exact amount.'})
            if money(tendered) < money(data.get('amount')):
                raise serializers.ValidationError(
                    {'tendered': 'The cash received is less than the amount being paid.'})
        if pm.is_gift_card(method) and not data.get('reference'):
            raise serializers.ValidationError({'reference': 'Enter the gift card / coupon code.'})
        return data


# What the till asks for when it saves a bill. The status itself is the
# server's decision: it follows from the payments actually taken.
INTENT_HOLD = 'draft'
INTENT_COMPLETE = 'complete'

_SNAPSHOT_FIELDS = (
    'document_type', 'customer_name', 'customer_phone', 'customer_gstin', 'customer_address',
    'branch_name', 'branch_legal_name', 'branch_gstin', 'branch_address', 'branch_phone',
    'place_of_supply',
)
_EINVOICE_FIELDS = ('einvoice_status', 'einvoice_error', 'irn', 'irn_ack_no', 'irn_ack_date',
                    'irn_signed_qr')


class InvoiceSerializer(serializers.ModelSerializer):
    items = InvoiceItemSerializer(many=True)
    payments = PaymentSerializer(many=True, required=False)
    invoice_number = serializers.CharField(read_only=True)
    display_number = serializers.CharField(read_only=True)
    amount_due = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = (
            'id', 'invoice_number', 'display_number', 'client', 'center', 'staff', 'appointment',
            'subtotal', 'discount', 'cgst', 'sgst', 'igst', 'rounding',
            'total_amount', 'paid_amount', 'tip_amount', 'amount_due', 'status', 'notes',
            'created_at', 'finalized_at', 'items', 'payments', 'sap_sync_status', 'sap_sync_message',
            'promotion', 'membership_used', 'created_by', 'shift',
        ) + _SNAPSHOT_FIELDS + _EINVOICE_FIELDS
        # The header money is computed, never accepted. So is the status
        # (finding R19): `status` in a request is read only as the till's
        # intent - hold the bill, or complete it - and the status stored is
        # derived from the payments actually taken. The SAP fields are the
        # sync's alone (R20), tips are no longer taken (#39), the offer and the
        # snapshots are the server's, and the e-invoice fields are the IRP's.
        read_only_fields = ('created_at', 'finalized_at', 'subtotal', 'cgst', 'sgst', 'igst',
                            'rounding', 'total_amount', 'paid_amount', 'status',
                            'tip_amount', 'sap_sync_status', 'sap_sync_message',
                            'promotion', 'membership_used', 'created_by', 'shift',
                            ) + _SNAPSHOT_FIELDS + _EINVOICE_FIELDS

    def _initial(self, key):
        initial = self.initial_data if isinstance(self.initial_data, dict) else {}
        value = initial.get(key)
        return None if value in (None, '', 'null', 0, '0') else value

    def _intent(self):
        """'draft' (hold), 'complete', or None when the request does not say -
        then the payments alone decide, as before."""
        raw = self.initial_data.get('status') if isinstance(self.initial_data, dict) else None
        raw = str(raw or '').strip().lower()
        if not raw:
            return None
        return INTENT_HOLD if raw == 'draft' else INTENT_COMPLETE

    @property
    def is_quote(self):
        return bool(self.context.get('quote'))

    # ─── Pricing ──────────────────────────────────────────────────────────

    def _price(self, data, center, client):
        items_data = data.get('items') or []
        raw_items = self.initial_data.get('items') if isinstance(self.initial_data, dict) else None
        if not isinstance(raw_items, list):
            raw_items = [{}] * len(items_data)
        return engine.price_bill(
            center=center, client=client, items_data=items_data, raw_items=raw_items,
            header_discount=D(data.get('discount', 0)),
            promo_id=self._initial('promo_id'),
            membership_id=self._initial('membership_id'),
            value_card_id=self._initial('value_card_id'),
            allocations=self._clean_allocations,
            exclude_invoice_id=self.instance.pk if self.instance is not None else None,
            # Completing a bill needs every required recipe choice made; a quote or a held
            # bill does not, so the till can show totals before the client has chosen.
            strict_recipe=(not self.is_quote) and self._completing(data.get('payments') or []),
        )

    # ─── Validation ───────────────────────────────────────────────────────

    def validate(self, data):
        center = data.get('center')
        if not center and self.instance:
            center = self.instance.center

        client = data.get('client')
        if client is None and self.instance:
            client = self.instance.client

        # Staff must belong to the invoice's center, and still work here: a
        # deactivated person earns no commission on a new bill.
        for item in data.get('items', []):
            staff = item.get('staff')
            if staff and getattr(staff, 'center', None) and staff.center != center:
                raise serializers.ValidationError('Staff member does not belong to this center.')
            if staff and not staff.is_active:
                raise serializers.ValidationError(
                    {'items': f'{staff.first_name} is no longer active and cannot be credited on a bill.'})
            for sm in item.get('staff_members', []):
                if sm and getattr(sm, 'center', None) and sm.center != center:
                    raise serializers.ValidationError('One or more staff members do not belong to this center.')
                if sm and not sm.is_active:
                    raise serializers.ValidationError(
                        {'items': f'{sm.first_name} is no longer active and cannot be credited on a bill.'})

        self._check_appointment(data, center, client)

        # Products must belong to the invoice's center.
        for item in data.get('items', []):
            ct_value = item.get('content_type')
            ct_label = ct_value if isinstance(ct_value, str) else pricing.content_type_label(ct_value)
            if ct_label == pricing.CT_PRODUCT and item.get('object_id'):
                from inventory.models import Product
                prod = Product.objects.filter(pk=item.get('object_id')).first()
                if prod and prod.center_id and center and prod.center_id != center.id:
                    raise serializers.ValidationError('Product does not belong to this center.')

        if 'items' in data:
            quote = self._price(data, center, client)
            self._quote = quote

            # Overwrite every monetary header field with the computed value.
            data['subtotal'] = quote.subtotal
            data['discount'] = quote.discount
            data['cgst'] = quote.cgst
            data['sgst'] = quote.sgst
            data['rounding'] = quote.rounding
            data['total_amount'] = quote.total_amount

            # Package redemption limits, whether or not a payment is taken.
            self._validate_redemptions(quote.lines, client, center)
        else:
            self._quote = None

        if self.is_quote:
            return data

        payments = data.get('payments')
        if payments:
            self._validate_payment_rows(payments, client, center)

        # Completing a bill needs something sold: an empty bill used to complete
        # as PAID for Rs 0 and take a GST invoice number. A held draft may be
        # empty; finishing it may not. (The till also makes the stylist
        # compulsory on every line; the server does not, so older clients keep
        # working.)
        if self._quote is not None and self._completing(payments or []) and not self._quote.lines:
            raise serializers.ValidationError({'items': 'Add at least one item before completing the bill.'})

        if self._completing(payments or []) and client is None:
            from pos_backend import app_settings
            if app_settings.get('billing.require_client'):
                raise serializers.ValidationError(
                    {'client': 'Choose the client before completing the bill (no anonymous bills).'})

        # Holding vs completing. A held bill takes no money; completing a bill
        # needs ALL of the money (#37) - a free bill (redemptions only)
        # completes at Rs 0.
        intent = self._intent()
        taken = money(sum((money(p.get('amount')) for p in (payments or [])), ZERO))
        if intent == INTENT_HOLD and payments:
            raise serializers.ValidationError(
                {'payments': 'A held bill takes no payment. Complete the bill to take payment.'})
        if self._quote is not None:
            payable = self._quote.payable
            if taken > payable:
                raise serializers.ValidationError({
                    'payments': (
                        f'Payments total Rs {taken} but Rs {payable} is to be paid. Enter the '
                        f'amount being applied to the bill, not the cash tendered - change '
                        f'given is not a payment. Put the cash handed over in "Cash received" '
                        f'and the change is shown.'),
                })
            if intent == INTENT_COMPLETE and payable > 0 and taken <= 0:
                raise serializers.ValidationError(
                    {'payments': 'Take the payment before completing the bill, or hold it as a draft.'})
            if intent != INTENT_HOLD and (payments or intent == INTENT_COMPLETE):
                # Split tender is fine; part payment is not (#37).
                if taken < payable:
                    raise serializers.ValidationError(
                        {'payments': f'The full amount is required: Rs {money(payable - taken)} '
                                     f'is still to be paid (#37).'})

        # Approvals (Phase 5): a price change or a discount above the free
        # limit needs an approver's yes, once the policy is switched on. A
        # held bill may carry anything; completing it is what is checked.
        self._approvals = []
        if self._completing(payments or []):
            self._approvals = self._check_approvals(center)

        # Stock is enforced by the POS ledger at checkout; SAP is never on the
        # till's path (the old live SAP stock check is gone).
        return data

    def _check_appointment(self, data, center, client):
        """The appointment a bill is raised for must be this visit.

        Same branch, same client, and still open (Scheduled or Checked-in): a
        bill completes the appointment it is linked to, so a link to another
        branch's, another client's, a cancelled or an already billed one would
        corrupt the calendar.
        """
        from appointments.models import Appointment

        appointment = data.get('appointment')
        if appointment is None:
            return
        # The same appointment already on this (held) bill is not re-judged.
        if self.instance is not None and self.instance.appointment_id == appointment.pk:
            return

        def refuse(message):
            raise serializers.ValidationError({'appointment': message})

        if center is not None and appointment.center_id != center.id:
            refuse('This appointment is at another branch.')
        if client is not None:
            if appointment.client_id:
                if appointment.client_id != client.id:
                    refuse('This appointment is for another client.')
            else:
                def digits(value):
                    return ''.join(ch for ch in str(value or '') if ch.isdigit())[-10:]
                booked, billed = digits(appointment.client_phone), digits(client.phone)
                if booked and billed and booked != billed:
                    refuse('This appointment is for another client.')
        if appointment.status not in Appointment.BOOKED:
            if appointment.status == 'Completed':
                refuse('This appointment has already been billed.')
            refuse(f'This appointment is {appointment.status}. Restore it on the calendar '
                   f'before billing it.')

    def _approval_ids(self):
        initial = self.initial_data if isinstance(self.initial_data, dict) else {}
        ids = list(initial.get('approval_ids') or [])
        if initial.get('approval_id'):
            ids.append(initial.get('approval_id'))
        return ids

    def _check_approvals(self, center):
        from approvals import service as approvals

        if self._quote is not None:
            needed = approvals.quote_requirements(center, self._quote)
        elif self.instance is not None:
            needed = approvals.invoice_requirements(self.instance)
        else:
            return []
        if not needed:
            return []
        request = self.context.get('request')
        return approvals.enforce(user=getattr(request, 'user', None), center=center,
                                 needed=needed, approval_ids=self._approval_ids())

    def _spend_approvals(self, invoice):
        if getattr(self, '_approvals', None) and invoice.status in ('paid', 'partial'):
            from approvals import service as approvals
            approvals.consume(self._approvals, invoice)

    def _validate_payment_rows(self, payments, client, center):
        """The till's payment rows: methods this branch takes, wallet balances."""
        allowed = pm.allowed_at(center)
        for p in payments:
            method = pm.canonical(p.get('payment_method'))
            if pm.is_value_card(method):
                raise serializers.ValidationError({
                    'payments': 'A value card is not entered as a payment: choose the card on '
                                'the bill (value_card_id) and it pays for the services it covers.'})
            if not pm.is_wallet(method) and method not in allowed:
                raise serializers.ValidationError(
                    {'payments': f'{method} is not a payment method at this branch.'})

        # A brand coupon: one per bill, and its code has to be good.
        gifts = [p for p in payments if pm.is_gift_card(p.get('payment_method')) and not p.get('is_tip')]
        if len(gifts) > 1:
            raise serializers.ValidationError(
                {'payments': 'Only one gift card coupon can be used on a bill.'})
        if gifts:
            self._validate_gift_card(gifts[0], center)

        # Wallet balances, aggregated per wallet so two payments against the
        # same balance are checked together.
        wallets.validate_payments_against_balances(client, [
            {'payment_method': p.get('payment_method'), 'amount': p.get('amount'),
             'value_card_id': p.get('value_card_id')}
            for p in payments if not p.get('is_tip')
        ])

    def _validate_gift_card(self, row, center):
        """The coupon code on a Gift Card payment is real, usable here, and the
        amount is no more than it pays on this bill. (The code is spent only when
        the bill is completed; this is the early, friendly refusal.)"""
        from marketing import coupons

        quote = getattr(self, '_quote', None)
        bill_total = quote.total_amount if quote is not None else getattr(self.instance, 'total_amount', 0)
        request = self.context.get('request')
        try:
            verdict = coupons.check_for_till(row.get('reference'), center=center, bill_total=bill_total,
                                             user=getattr(request, 'user', None))
        except coupons.CouponError as exc:
            raise serializers.ValidationError({'payments': str(exc)})
        if money(row.get('amount')) > verdict.applies:
            raise serializers.ValidationError({'payments': (
                f'This coupon pays at most Rs {verdict.applies} on this bill; the Gift Card '
                f'amount is Rs {money(row.get("amount"))}.')})

    @staticmethod
    def _clean_allocations(idx, item_data):
        """The line's staff contribution split, checked (#49).

        `{staff_id: percent}`. Every staff member named must be credited on
        the line, and the percentages must add up to 100. The till collected
        this split but never sent it, so commission was always split evenly.
        """
        raw = item_data.get('staff_allocations') or {}
        if not isinstance(raw, dict) or not raw:
            return {}
        credited = {str(getattr(s, 'pk', s)) for s in (item_data.get('staff_members') or [])}
        staff = item_data.get('staff')
        if staff:
            credited.add(str(getattr(staff, 'pk', staff)))

        cleaned = {}
        for staff_id, value in raw.items():
            share = D(value)
            if share < 0:
                raise serializers.ValidationError(
                    {'items': f'Line {idx + 1}: a staff share cannot be negative.'})
            if share == 0:
                continue
            if str(staff_id) not in credited:
                raise serializers.ValidationError(
                    {'items': f'Line {idx + 1}: the staff split names someone not credited on the line.'})
            cleaned[str(staff_id)] = str(money(share))

        total = money(sum((D(v) for v in cleaned.values()), ZERO))
        # The till allows 0.1 for thirds (33.3 x 3); shares are weighted, so
        # 99.9 still divides the whole line value.
        if cleaned and abs(total - D('100')) > D('0.1'):
            raise serializers.ValidationError(
                {'items': f'Line {idx + 1}: the staff split adds up to {total}%, not 100%.'})
        return cleaned

    def _validate_redemptions(self, lines, client, center=None):
        """Ensure the client has the package entitlements being redeemed, at
        this branch (#32) and unexpired."""
        import datetime
        from clients.models import ClientPackage
        from pos_backend import app_settings

        requested = {}
        for line in lines:
            if not line.is_redemption:
                continue
            if not line.object_id:
                raise serializers.ValidationError(
                    {'items': 'A redemption line must identify which service is being redeemed.'}
                )
            key = str(line.object_id)
            requested[key] = requested.get(key, 0) + int(line.quantity)

        if not requested:
            return

        if not client:
            raise serializers.ValidationError(
                {'items': 'Package redemption requires a registered client.'}
            )

        branch_only = app_settings.get('perks.selling_branch_only')
        available = {}
        elsewhere = {}
        packages = ClientPackage.objects.select_related('source_invoice').filter(
            client=client, is_active=True, expiry_date__gte=datetime.date.today()
        )
        for cp in packages:
            sold_at = engine.perk_branch_id(cp)
            usable_here = not (branch_only and sold_at and center is not None
                               and sold_at != center.id)
            for svc_id, remaining in (cp.services_remaining or {}).items():
                # services_remaining is JSON; a value may arrive as a string.
                bucket = available if usable_here else elsewhere
                bucket[str(svc_id)] = bucket.get(str(svc_id), 0) + int(D(remaining))

        for svc_id, qty in requested.items():
            have = available.get(svc_id, 0)
            if qty > have:
                if elsewhere.get(svc_id):
                    raise serializers.ValidationError(
                        {'items': 'This package can be redeemed only at the branch that sold it.'})
                raise serializers.ValidationError(
                    {'items': f'Cannot redeem {qty} of this service. Only {have} remaining in the package.'}
                )

    def to_internal_value(self, data):
        """Normalise nested staff objects at invoice and item level."""
        if isinstance(data, dict):
            staff = data.get('staff')
            if isinstance(staff, dict) and 'id' in staff:
                data['staff'] = staff['id']
            items = data.get('items')
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        s = it.get('staff')
                        if isinstance(s, dict) and 'id' in s:
                            it['staff'] = s['id']
        return super().to_internal_value(data)

    # ─── Persistence ──────────────────────────────────────────────────────

    def _write_lines(self, invoice, lines):
        """Create InvoiceItem rows from the priced lines."""
        created = []
        for line in lines:
            item = InvoiceItem(
                invoice=invoice,
                content_type=line.content_type,
                object_id=line.object_id,
                description=line.description[:255],
                unit_price=line.unit_price,
                discount=line.discount,
                manager_discount=line.manager_discount,
                quantity=line.quantity,
                tax_percentage=line.tax_percentage,
                tax_amount=line.tax_amount,
                total_price=line.total_price,
                is_redemption=line.is_redemption,
                is_complimentary=bool(line.is_redemption or (line.total_price == 0 and line.unit_price == 0)),
                staff=line.staff,
                staff_allocations=line.staff_allocations or {},
                item_code=line.item_code or '',
                hsn_sac=line.hsn_sac or '',
                bill_discount_share=line.bill_discount_share,
                recipe_choices=line.recipe_choices or {},
                value_card=line.value_card,
                card_face=line.card_face,
                card_benefit=line.card_benefit,
                card_taxable=line.card_taxable,
                card_tax=line.card_tax,
                card_tax_percentage=line.card_tax_percentage,
            )
            item.save()
            if line.staff_members:
                item.staff_members.set(line.staff_members)
            created.append(item)
        return created

    def _apply_offer(self, invoice):
        quote = self._quote
        if quote is None:
            return
        invoice.promotion = quote.promotion
        invoice.membership_used = quote.membership

    @staticmethod
    def _derive_status(total, paid, intent):
        """The stored status follows the money (finding R19)."""
        total, paid = money(total), money(paid)
        if paid > 0 and paid >= total:
            return 'paid'
        if paid > 0:
            return 'partial'
        if total <= 0 and intent == INTENT_COMPLETE:
            return 'paid'            # redemptions only: nothing to collect
        return 'draft'

    def _apply_payments(self, invoice, payments_data):
        """Create Payment rows and derive paid_amount / status.

        The value card's part is a payment row the server writes itself: the
        amount the card settled, and the face value it takes off the card.
        Tips are no longer taken (#39); validate_is_tip refuses them.
        """
        rows = []
        quote = self._quote
        # Held money (advance, value card, cashback) is spent only with the client's PIN or the master PIN.
        from billing import pin_gate
        pin_gate.enforce(self.context.get('request'), invoice.client, invoice.center, payments_data, quote)
        if quote is not None and quote.value_card is not None and quote.card_settled > 0:
            rows.append(Payment(
                invoice=invoice, amount=quote.card_settled, payment_method=Payment.VALUE_CARD,
                value_card_id=quote.value_card.id, card_face=quote.card_face,
            ))
        for pay_data in payments_data:
            method = pm.canonical(pay_data.get('payment_method'))
            tendered = pay_data.get('tendered')
            rows.append(Payment(
                invoice=invoice, amount=money(pay_data.get('amount')), payment_method=method,
                value_card_id=pm.extract_value_card_id(pay_data.get('payment_method'),
                                                       pay_data.get('value_card_id')),
                reference=(pay_data.get('reference') or '')[:64],
                tendered=money(tendered) if tendered is not None else None,
            ))

        paid = ZERO
        for row in rows:
            row.save()
            paid += row.amount

        # Change given to the customer is not revenue and must not be recorded
        # as payment (#38): the cash handed over is kept in `tendered`.
        _paid_total = money(paid)
        if _paid_total > money(invoice.total_amount):
            raise serializers.ValidationError({
                'payments': (
                    f'Payments total Rs {_paid_total} but the bill is Rs '
                    f'{money(invoice.total_amount)}. Enter the amount being applied to the '
                    f'bill, not the cash tendered - change given is not a payment.'),
            })
        invoice.paid_amount = _paid_total
        invoice.status = self._derive_status(invoice.total_amount, _paid_total, self._intent())
        return invoice

    def _completing(self, payments_data):
        """Will this save take money (and so need the card's payment row)?"""
        intent = self._intent()
        if intent == INTENT_HOLD:
            return False
        return bool(payments_data) or intent == INTENT_COMPLETE

    def create(self, validated_data):
        validated_data.pop('items', None)
        payments_data = validated_data.pop('payments', [])
        quote = getattr(self, '_quote', None)

        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            invoice = Invoice(**validated_data)
            self._apply_offer(invoice)
            invoice.save()
        except DjangoValidationError as e:
            raise serializers.ValidationError(
                e.message_dict if hasattr(e, 'message_dict') else list(e.messages)
            )

        if quote:
            self._write_lines(invoice, quote.lines)

        if self._completing(payments_data):
            self._apply_payments(invoice, payments_data)
        else:
            invoice.status = self._derive_status(invoice.total_amount, ZERO, self._intent())

        invoice.save()
        self._spend_approvals(invoice)

        if quote and quote.audit_notes:
            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center,
                user=invoice.created_by,
                action='Price Adjusted',
                notes=' | '.join(quote.audit_notes)[:2000],
            )
        return invoice

    def update(self, instance, validated_data):
        """Update a DRAFT invoice, re-running the full pricing pipeline.

        Only drafts are edited (the view refuses anything else): a finalised
        bill changes through Pay, Cancel or Change payment method, which move
        the money with it. A draft has taken no money - wallets are debited
        when it is finalised - so its payment rows can simply be replaced.
        """
        validated_data.pop('items', None)
        payments_data = validated_data.pop('payments', None)
        quote = getattr(self, '_quote', None)

        if instance.status != 'draft':
            raise serializers.ValidationError(
                {'detail': 'Only a held (draft) bill can be edited. Use Pay, Cancel or '
                           'Change payment method on a completed bill.'})

        for attr, value in validated_data.items():
            setattr(instance, attr, value)

        if quote is not None:
            self._apply_offer(instance)
            # Replace lines wholesale from the recomputed pricing.
            instance.items.all().delete()
            instance.save()
            self._write_lines(instance, quote.lines)

        if payments_data is not None:
            instance.payments.all().delete()
            instance.paid_amount = ZERO
            if self._completing(payments_data):
                self._apply_payments(instance, payments_data)
            else:
                instance.status = self._derive_status(instance.total_amount, ZERO, self._intent())
        elif self._intent() is not None:
            if self._completing([]):
                instance.payments.all().delete()
                self._apply_payments(instance, [])
            else:
                instance.status = self._derive_status(instance.total_amount, instance.paid_amount,
                                                      self._intent())

        from django.core.exceptions import ValidationError as DjangoValidationError
        try:
            instance.save()
        except DjangoValidationError as e:
            raise serializers.ValidationError(
                e.message_dict if hasattr(e, 'message_dict') else list(e.messages)
            )
        self._spend_approvals(instance)
        return instance

    def to_representation(self, instance):
        repr_data = super().to_representation(instance)

        if instance.client:
            repr_data['client'] = {
                'id': instance.client.id,
                'first_name': instance.client.first_name,
                'last_name': instance.client.last_name or '',
                'phone': instance.client.phone,
                'gst_number': instance.client.gst_number or '',
            }

        if instance.center:
            repr_data['center_detail'] = {
                'id': instance.center.id,
                'name': instance.center.display_name or instance.center.center_name or '',
                'address': instance.center.address or '',
                'phone': getattr(instance.center, 'phone', '') or '',
                'gst_number': instance.center.gst_number or '',
            }

        # Enrich items with staff_members objects. `instance.items` is
        # prefetched by the viewset, so this does not re-query per item.
        items = list(instance.items.all())
        items_by_id = {i.id: i for i in items}
        for item_repr in repr_data.get('items', []):
            item_instance = items_by_id.get(item_repr.get('id'))
            if not item_instance:
                item_repr['staff_members'] = []
                continue
            item_repr['staff_members'] = [
                {'id': s.id, 'name': f"{s.first_name} {s.last_name or ''}".strip()}
                for s in item_instance.staff_members.all()
            ]

        # What the receipt needs beyond the stored columns: the per-rate tax
        # table, and the value card's block (F5: covered vs paid now).
        repr_data['tax_breakdown'] = engine.stored_tax_breakdown(instance)
        card_items = [i for i in items if D(i.card_face) > 0]
        card_paid = money(sum((D(p.amount) for p in instance.payments.all()
                               if pm.is_value_card(p.payment_method)), ZERO))
        if card_items:
            card = card_items[0].value_card
            settled = money(sum((i.card_settled for i in card_items), ZERO))
            repr_data['value_card_cover'] = {
                'card_id': card.id if card else None,
                'number': card.number if card else '',
                'title': getattr(getattr(card, 'value_card', None), 'title', 'Value Card'),
                'face_used': str(money(sum((D(i.card_face) for i in card_items), ZERO))),
                'benefit': str(money(sum((D(i.card_benefit) for i in card_items), ZERO))),
                'taxable': str(money(sum((D(i.card_taxable) for i in card_items), ZERO))),
                'tax': str(money(sum((D(i.card_tax) for i in card_items), ZERO))),
                'settled': str(settled),
                'balance_after': str(money(card.balance)) if card else '',
                'item_ids': [i.id for i in card_items],
            }
            repr_data['paid_now'] = str(money(D(instance.total_amount) - settled))
        else:
            repr_data['value_card_cover'] = None
            repr_data['paid_now'] = str(money(D(instance.total_amount) - card_paid))
        repr_data['change_given'] = str(money(sum((p.change_given for p in instance.payments.all()),
                                                  ZERO)))
        repr_data['created_by_name'] = (getattr(instance.created_by, 'full_name', '') or ''
                                        if instance.created_by_id else '')
        from pos_backend import app_settings
        repr_data['receipt_settings'] = {
            'size': app_settings.get('billing.receipt_size', center=instance.center),
            'footer': app_settings.get('billing.receipt_footer', center=instance.center),
        }
        return repr_data


class AdvancePaymentSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    client_name = serializers.SerializerMethodField()

    class Meta:
        model = AdvancePayment
        fields = ('id', 'client', 'client_name', 'invoice', 'staff', 'center',
                  'amount', 'gst_amount', 'payment_method', 'notes', 'created_at')
        read_only_fields = ('created_at', 'center')

    def get_client_name(self, obj):
        return obj.client.full_name if obj.client else ''

    def validate_amount(self, value):
        """A top-up must be positive.

        Negative rows are redemptions and are only ever written by the billing
        flow. Allowing a negative here let anyone with API access drain or
        inflate a client's wallet directly.
        """
        if D(value) <= 0:
            raise serializers.ValidationError('Advance amount must be greater than zero.')
        return value


class BillChangeLogSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    center_name = serializers.SerializerMethodField()
    bill_amount = serializers.SerializerMethodField()
    bill_date = serializers.SerializerMethodField()
    bill_number = serializers.SerializerMethodField()

    class Meta:
        model = BillChangeLog
        fields = ('id', 'invoice', 'bill_number', 'center', 'center_name', 'user', 'user_name',
                  'action', 'notes', 'created_at', 'bill_amount', 'bill_date')

    def get_user_name(self, obj):
        if not obj.user:
            return ""
        return obj.user.full_name or getattr(obj.user, 'email', '') or str(obj.user)

    def get_center_name(self, obj):
        if obj.center:
            return obj.center.display_name or obj.center.center_name
        return None

    def get_bill_number(self, obj):
        return obj.invoice.display_number if obj.invoice else ''

    def get_bill_amount(self, obj):
        return str(obj.invoice.total_amount) if obj.invoice else ''

    def get_bill_date(self, obj):
        if obj.invoice and obj.invoice.created_at:
            return obj.invoice.created_at.isoformat()
        return ''
