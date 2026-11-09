import datetime
from inventory.quantities import qty as stock_qty
import hashlib
import json
import logging

from django.core.cache import cache
from decimal import Decimal
from django.db import transaction
from django.db.models import Count, F, Sum
from django.utils import timezone

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from pos_backend.money import D, money, ZERO
from pos_backend.permissions import HasModulePermission, IsOwner
from pos_backend.scoping import (
    assert_can_write_center, coerce_center_id, safe_date_param, scope_to_centers, user_center_ids,
)

from billing import payment_methods as pm
from billing import wallets
from billing.models import (
    AdvancePayment, BillChangeLog, CashbackTransaction, CheckoutIdempotency,
    Invoice, InvoiceItem, InvoiceRefund, Payment,
)
from billing.serializers import (
    AdvancePaymentSerializer, BillChangeLogSerializer, InvoiceSerializer,
)
from billing.services import finalize_invoice, recompute_invoice_totals

logger = logging.getLogger(__name__)

def _with_deadlock_retry(attempt, tries=3):
    """Run one whole transaction, running it again if the database picks it as
    a deadlock victim (MySQL 1213) or times out waiting for a lock (1205).

    Two tills touching the same rows in opposite order is rare but normal on a
    busy day, and MySQL's remedy is to abort one of them and ask it to try
    again. Showing that to a cashier as a failed bill is wrong: the first
    attempt rolled back completely, so running it again is safe. Never retries
    inside an enclosing transaction (it could not roll back just this part).
    """
    import random
    import time
    from django.db import connection
    from django.db.utils import OperationalError

    for n in range(tries):
        try:
            return attempt()
        except OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code not in (1213, 1205) or n == tries - 1 or connection.in_atomic_block:
                raise
            logger.warning('[Billing] database lock conflict (%s); retrying %s/%s', code, n + 1, tries - 1)
            time.sleep(random.uniform(0.05, 0.25) * (n + 1))


_INVOICE_PREFETCH = (
    'items__content_type', 'items__staff', 'items__staff_members', 'payments',
    'items__value_card__value_card',
)


def _open_shift(user, center):
    """The till shift a bill is taken on: the user's own open shift at the
    branch, else the branch's open shift (B5.10). None when no shift is open."""
    if center is None:
        return None
    from finance.models import Shift
    open_shifts = Shift.objects.filter(center=center, status='Open').order_by('-opened_at')
    if user is not None:
        mine = open_shifts.filter(opened_by=user).first()
        if mine is not None:
            return mine
    return open_shifts.first()


class InvoiceViewSet(viewsets.ModelViewSet):
    serializer_class = InvoiceSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'billing'
    # The screens page; an unpaged call (an old script, an export) used to get up to 30 MB of bills.
    unpaged_limit = 2000
    # Billing's two tabs are now enforced separately (finding R3). Viewing is
    # Invoice History; raising, editing, paying and applying a promotion are
    # New Invoice; cancelling and changing a payment method are action ticks
    # on Invoice History - the same ticks the Admin > Bills page uses (R4).
    # Cancel used to be "delete on any billing tab".
    permission_submodule = 'invoices'
    permission_action_submodules = {
        'create': 'new_invoice',
        'update': 'new_invoice',
        'partial_update': 'new_invoice',
        'destroy': 'new_invoice',
        'pay': 'new_invoice',
        'apply_promo': 'new_invoice',
        'quote': 'new_invoice',
        'check_coupon': 'new_invoice',
        'request_approval': 'new_invoice',
    }
    permission_action_verbs = {
        'quote': 'create',
        'check_coupon': 'create',
        'request_approval': 'create',
        'pay': 'create',
        'apply_promo': 'update',
        # Only drafts can be edited or deleted (completed bills are refused),
        # so finishing or discarding a held bill is part of raising one: a
        # cashier with Add on New Invoice could hold a bill but not complete it.
        'update': 'create',
        'partial_update': 'create',
        'destroy': 'create',
    }
    permission_action_ticks = {
        'cancel': ('invoices', 'cancel'),
        'refund': ('invoices', 'cancel'),
        'change_payment': ('invoices', 'change_payment'),
    }
    # Admin > Bills lists these same bills and offers the same buttons, so a role
    # granted that page can use it without also holding Billing > Invoice History
    # (it used to open the page and answer every request with "no permission").
    # Admin > Manager Discounts and Admin > Clients read bills too: the discount
    # report and a client's bill history.
    permission_read_also = [('admin', 'bills'), ('admin', 'manager_discounts'), ('admin', 'clients')]
    permission_action_ticks_also = {
        'cancel': [('admin', 'bills', 'cancel')],
        'refund': [('admin', 'bills', 'cancel')],
        'change_payment': [('admin', 'bills', 'change_payment')],
    }

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        if 'client_id' in request.query_params and 'page' not in request.query_params:
            queryset = queryset[:50]
        elif request.query_params.get('invoice_number') and 'page' not in request.query_params:
            # A search for a fragment of a number can match thousands of bills.
            queryset = queryset[:200]
        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)
        return Response(self.get_serializer(queryset, many=True).data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Counts and value of EVERY bill the list filters match, not of one page of them.

        Admin > Bills shows 500 bills a page; its cards used to add up only the page on
        screen, so with more bills than that they described a slice. Takes the same
        filters as the list (centre, dates, bill number, status)."""
        ids = self.filter_queryset(self.get_queryset()).values('id')
        rows = (Invoice.objects.filter(id__in=ids).order_by()
                .values('status').annotate(bills=Count('id'), value=Sum('total_amount')))
        by_status = {row['status']: row for row in rows}

        def bills(*statuses):
            return sum(by_status[s]['bills'] for s in statuses if s in by_status)

        def value(*statuses):
            return money(sum((by_status[s]['value'] or ZERO for s in statuses if s in by_status), ZERO))

        counted = [s for s in by_status if s not in ('cancelled', 'refunded')]
        return Response({
            'count': bills(*by_status),
            'paid': bills('paid', 'partial'),
            'cancelled': bills('cancelled'),
            'refunded': bills('refunded'),
            # What was actually sold: cancelled and refunded bills are not income.
            'value': value(*counted),
            'cancelled_value': value('cancelled'),
            'refunded_value': value('refunded'),
        })

    def get_queryset(self):
        qs = Invoice.objects.select_related(
            'client', 'center', 'staff', 'created_by'
        ).prefetch_related(*_INVOICE_PREFETCH).order_by('-created_at')

        # Fails closed for users assigned to no center.
        qs = scope_to_centers(
            qs, self.request.user,
            requested_center=coerce_center_id(self.request.query_params.get('center_id')),
        )

        client_id = self.request.query_params.get('client_id')
        if client_id:
            try:
                qs = qs.filter(client_id=int(client_id))
            except (TypeError, ValueError):
                return qs.none()

        start_date = safe_date_param(self.request.query_params.get('start_date'))
        if start_date:
            try:
                sd = datetime.datetime.strptime(start_date, '%Y-%m-%d')
                qs = qs.filter(created_at__gte=sd)
            except ValueError:
                pass

        end_date = safe_date_param(self.request.query_params.get('end_date'))
        if end_date:
            try:
                # Exclusive upper bound on the next day, so the last second of
                # the end date is included regardless of stored precision.
                ed = datetime.datetime.strptime(end_date, '%Y-%m-%d') + datetime.timedelta(days=1)
                qs = qs.filter(created_at__lt=ed)
            except ValueError:
                pass

        invoice_number = self.request.query_params.get('invoice_number')
        if invoice_number:
            term = str(invoice_number).strip()
            if term.isdigit():
                qs = qs.filter(id=int(term))
            else:
                # Match the GST serial directly now that invoices have one. A
                # number typed from its start (DL01-2627-...) can use the
                # unique index; only a fragment falls back to scanning every
                # invoice for it.
                starts = qs.filter(invoice_number__istartswith=term)
                qs = starts if starts.exists() else qs.filter(invoice_number__icontains=term)

        # `status=draft` lets the till's landing page ask for just the held bills
        # instead of downloading a month of invoices to filter them in the browser.
        wanted_status = (self.request.query_params.get('status') or '').strip().lower()
        if self.action == 'list' and wanted_status in {s for s, _ in Invoice.STATUS_CHOICES}:
            qs = qs.filter(status=wanted_status)

        if (self.request.query_params.get('exclude_drafts') or '').lower() == 'true':
            qs = qs.exclude(status='draft')

        if (self.request.query_params.get('manager_discount') or '').lower() == 'true':
            qs = qs.filter(items__manager_discount__gt=0).distinct()

        has_explicit_filter = client_id or start_date or end_date or invoice_number or self.action == 'retrieve' or self.kwargs.get('pk')
        if not has_explicit_filter:
            qs = qs.filter(created_at__gte=timezone.now() - datetime.timedelta(days=30))

        # Stale drafts are noise, not records. Compared against the start of
        # today rather than DATE(created_at) < today: wrapping the column in a
        # function stopped the date indexes being used for every list.
        start_of_today = datetime.datetime.combine(datetime.date.today(), datetime.time.min)
        qs = qs.exclude(status='draft', created_at__lt=start_of_today)
        return qs

    # ─── Payroll lock ─────────────────────────────────────────────────────

    def _check_payroll_lock(self, invoice):
        from staff.models import PayrollRecord, ServiceLog

        # ServiceLog.objects excludes cancelled/refunded invoices, so use the
        # unfiltered manager here - otherwise cancelling twice would bypass the
        # lock check the second time.
        staff_ids = list(
            ServiceLog.all_objects.filter(invoice=invoice)
            .values_list('staff_id', flat=True).distinct()
        )
        if not staff_ids:
            return
        invoice_date = invoice.created_at.date() if invoice.created_at else datetime.date.today()
        records = PayrollRecord.objects.filter(
            staff_id__in=staff_ids,
            month=invoice_date.month,
            year=invoice_date.year,
            status__in=['Locked', 'Paid'],
        )
        # A locked payroll only holds the bills that existed when it was worked
        # out. A bill raised after that is in nobody's settled figures, so it can
        # still be cancelled or changed (it used to be blocked until the month
        # ended, as soon as anyone locked the month's payroll).
        locked = False
        for record in records:
            settled_at = record.generated_at or record.locked_at or record.updated_at
            if invoice.created_at is None or settled_at is None or invoice.created_at <= settled_at:
                locked = True
                break
        if locked:
            raise ValidationError(
                'Cannot alter this invoice: staff payroll for '
                f'{invoice_date.month}/{invoice_date.year} is already Locked or Paid.'
            )

    # ─── Payment ──────────────────────────────────────────────────────────

    # ─── Quote ────────────────────────────────────────────────────────────

    @action(detail=False, methods=['post'])
    def quote(self, request):
        """Price a cart exactly as saving it would, without saving anything.

        The till shows this result: the offer's discount, the value card's
        cover, tax per rate, rounding and what is left to pay. It sends the
        same body it would save (items plus promo_id / membership_id /
        value_card_id), so the screen and the bill always agree."""
        payload = self._normalise_payload(request)
        serializer = self.get_serializer(data=payload, context={
            **self.get_serializer_context(), 'quote': True})
        serializer.is_valid(raise_exception=True)
        quote = serializer._quote
        if quote is None:
            return Response({'items': 'A quote needs the bill lines.'},
                            status=status.HTTP_400_BAD_REQUEST)
        from pos_backend import app_settings
        center = serializer.validated_data.get('center')
        data = quote.as_dict()
        # The ways this branch is paid (Admin > Settings), as the till lists them.
        raw = app_settings.get('billing.payment_methods', center=center) or ''
        data['payment_methods'] = [t.strip() for t in raw.split(',') if t.strip()]
        # What this bill needs approved before it can be completed (Phase 5).
        from approvals import service as approvals
        data['approvals_required'] = approvals.quote_requirements(center, quote)
        return Response(data)

    @action(detail=False, methods=['post'])
    def check_coupon(self, request):
        """The till's "Check" on a Gift Card code: would it pay on this bill, and how much?

        Answers 200 either way - `valid` says which, and `message` is what to tell
        the customer - so a refused code is not a red network error."""
        from marketing import coupons
        from salon_admin.models import Center

        center_id = coerce_center_id(request.data.get('center'))
        center = Center.objects.filter(pk=center_id).first() if center_id else None
        if center is not None:
            assert_can_write_center(request.user, center)
        amount = money(D(request.data.get('amount') or 0))
        if amount <= 0:
            return Response({'valid': False, 'message': 'The bill has no amount to pay yet.'})
        try:
            verdict = coupons.check_for_till(request.data.get('code'), center=center, bill_total=amount,
                                             user=request.user)
        except coupons.CouponError as exc:
            return Response({'valid': False, 'message': str(exc)})
        config = verdict.code.promotion.config if isinstance(verdict.code.promotion.config, dict) else {}
        return Response({
            'valid': True,
            'code': verdict.code.code,
            'brand': config.get('brand') or '',
            'promotion': verdict.code.promotion.name,
            'worth': str(verdict.worth),
            'applies': str(verdict.applies),
            'message': f'Valid: pays Rs {verdict.applies} on this bill.',
        })

    @action(detail=False, methods=['post'])
    def request_approval(self, request):
        """Ask the branch's approvers to approve what this bill needs.

        The server prices the bill itself and opens one request per need; the
        till only says "ask". Returns the requests to wait on."""
        from approvals import service as approvals

        payload = self._normalise_payload(request)
        serializer = self.get_serializer(data=payload, context={
            **self.get_serializer_context(), 'quote': True})
        serializer.is_valid(raise_exception=True)
        quote = serializer._quote
        center = serializer.validated_data.get('center')
        if quote is None or center is None:
            return Response({'detail': 'Send the bill lines and the branch.'},
                            status=status.HTTP_400_BAD_REQUEST)
        needed = approvals.quote_requirements(center, quote)
        if not needed:
            return Response({'requests': [], 'detail': 'This bill needs no approval.'})
        reason = str(request.data.get('reason') or '')
        made = [approvals.create_request(request.user, center, need, reason) for need in needed]
        from approvals.views import present
        return Response({'requests': [present(r, request.user) for r in made]},
                        status=status.HTTP_201_CREATED)

    # ─── Payment ──────────────────────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def pay(self, request, pk=None):
        """Record a payment against an invoice atomically."""
        invoice = self.get_object()  # applies RBAC scoping
        method = pm.canonical(request.data.get('payment_method', 'Cash'))
        is_tip = False
        if request.data.get('is_tip'):
            return Response({'detail': 'Tips are not taken on bills.'}, status=status.HTTP_400_BAD_REQUEST)
        if method == Payment.NEARBUY:
            return Response({'detail': 'Nearbuy is no longer accepted as a payment method.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if pm.is_value_card(method):
            return Response({'detail': 'A value card pays for the services it covers on the bill '
                                       'itself; choose it on the bill, not as a payment.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if not pm.is_wallet(method) and method not in pm.allowed_at(invoice.center):
            return Response({'detail': f'{method} is not a payment method at this branch.'},
                            status=status.HTTP_400_BAD_REQUEST)

        amt = money(request.data.get('amount'))
        if amt <= 0:
            return Response({'detail': 'Payment amount must be greater than zero.'},
                            status=status.HTTP_400_BAD_REQUEST)
        reference = str(request.data.get('reference') or '').strip()[:64]
        if pm.is_gift_card(method) and not reference:
            return Response({'detail': 'Enter the gift card / coupon code.'},
                            status=status.HTTP_400_BAD_REQUEST)
        tendered = request.data.get('tendered')
        if tendered not in (None, ''):
            tendered = money(tendered)
            if not pm.is_cash(method):
                return Response({'detail': 'Only cash is tendered; online payments are for the '
                                           'exact amount.'}, status=status.HTTP_400_BAD_REQUEST)
            if tendered < amt:
                return Response({'detail': 'The cash received is less than the amount being paid.'},
                                status=status.HTTP_400_BAD_REQUEST)
        else:
            tendered = None

        with transaction.atomic():
            invoice = (Invoice.objects.select_for_update()
                       .select_related('client', 'center').get(pk=invoice.pk))

            if invoice.status in ('cancelled', 'refunded'):
                return Response({'detail': f'Invoice is {invoice.status}; it cannot take payments.'},
                                status=status.HTTP_400_BAD_REQUEST)

            old_status = invoice.status
            from approvals import service as approvals
            spent = []

            if not is_tip:
                remaining = money(D(invoice.total_amount) - D(invoice.paid_amount))
                if remaining <= 0:
                    return Response({'detail': 'Invoice is already fully paid.'},
                                    status=status.HTTP_400_BAD_REQUEST)
                if amt > remaining:
                    return Response(
                        {'detail': f'Payment amount (Rs {amt}) exceeds the remaining balance (Rs {remaining}).'},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if old_status == 'draft':
                    ids = list(request.data.get('approval_ids') or [])
                    if request.data.get('approval_id'):
                        ids.append(request.data.get('approval_id'))
                    spent = approvals.enforce(
                        user=request.user, center=invoice.center,
                        needed=approvals.invoice_requirements(invoice), approval_ids=ids)
                    # A held bill is completed in full (#37); its value card,
                    # if any, is taken when it is completed from the till.
                    if invoice.items.filter(card_face__gt=0).exists():
                        return Response({'detail': 'This bill uses a value card: complete it from '
                                                   'the till.'}, status=status.HTTP_400_BAD_REQUEST)
                    if amt < remaining:
                        return Response(
                            {'detail': f'The full amount is required: Rs {remaining} (#37).'},
                            status=status.HTTP_400_BAD_REQUEST)

            card_id = pm.extract_value_card_id(
                request.data.get('payment_method'), request.data.get('value_card_id')
            )

            if not is_tip:
                from billing import pin_gate
                pin_gate.enforce(request, invoice.client, invoice.center, [{'payment_method': method}])

            payment = Payment.objects.create(
                invoice=invoice, amount=amt, payment_method=method,
                value_card_id=card_id, is_tip=is_tip, reference=reference, tendered=tendered,
            )

            # Apply the wallet/card debit through the shared router. Raises on
            # an insufficient balance rather than silently capping the payment
            # to whatever was available.
            if not is_tip:
                wallets.apply_payment_to_wallets(payment, invoice)

            if is_tip:
                Invoice.objects.filter(pk=invoice.pk).update(tip_amount=F('tip_amount') + amt)
            else:
                Invoice.objects.filter(pk=invoice.pk).update(paid_amount=F('paid_amount') + amt)
            invoice.refresh_from_db()

            if not is_tip:
                new_status = 'paid' if D(invoice.paid_amount) >= D(invoice.total_amount) else 'partial'
                Invoice.objects.filter(pk=invoice.pk).update(status=new_status)
                invoice.refresh_from_db()

            # Finalise on the transition out of draft. Errors propagate: a
            # checkout that cannot be finalised must not record a payment.
            # This used to be caught and logged, committing a payment whose
            # stock, perks and service logs never happened.
            if old_status == 'draft' and invoice.status in ('paid', 'partial'):
                approvals.consume(spent, invoice)
                finalize_invoice(invoice, None, {}, skip_payment_deductions=True)
            elif not is_tip and invoice.sap_doc_entry:
                # A part-payment on a bill already in SAP. The payment step
                # only ran at checkout, so this money never reached SAP; it
                # now posts just the payments SAP does not have yet.
                from pos_backend.sap_utils import create_sap_incoming_payment, run_sap_sync_async
                run_sap_sync_async(create_sap_incoming_payment, invoice)

            invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)

        # Live dashboard event - to this branch's channel only (finding R18).
        from pos_backend.realtime import broadcast_dashboard
        broadcast_dashboard(invoice.center_id, 'INVOICE_PAID', {
            'invoice_id': invoice.id, 'status': invoice.status,
            'paid_amount': str(invoice.paid_amount),
        })

        return Response(InvoiceSerializer(invoice).data)

    # ─── Cancel / refund ──────────────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None, new_status='cancelled'):
        """Cancel a bill: same day only, and not once something it sold is used.

        Owner decisions: bills are never refunded, only cancelled, and only on
        the day they were made (#45, #46, F19); a bill that sold a value card,
        membership, package or advance cannot be cancelled once that has been
        used (#47); cancelling a service bill cancels its appointment (#23).
        """
        invoice = self.get_object()
        self._check_payroll_lock(invoice)

        if invoice.status in ('cancelled', 'refunded'):
            return Response({'detail': f'Invoice is already {invoice.status}.'},
                            status=status.HTTP_400_BAD_REQUEST)

        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response({'reason': 'A reason is required to cancel a bill.'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            invoice = (Invoice.objects.select_for_update()
                       .select_related('client', 'center').get(pk=invoice.pk))
            if invoice.status in ('cancelled', 'refunded'):
                return Response({'detail': f'Invoice is already {invoice.status}.'},
                                status=status.HTTP_400_BAD_REQUEST)

            if invoice.status != 'draft':
                blocked = self._cancel_blocker(invoice)
                if blocked:
                    return Response({'detail': blocked}, status=status.HTTP_400_BAD_REQUEST)

            old_status = invoice.status
            # Taken before the wallet reversals below credit anything back.
            sold_advance = self._sold_advance(invoice)
            invoice.status = new_status
            invoice.save(update_fields=['status'])

            user = request.user if request.user and request.user.is_authenticated else None
            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center, user=user,
                action='Cancel Bill' if new_status == 'cancelled' else 'Refund Bill',
                notes=reason[:2000],
            )

            # Refund only what was actually collected, dated today. The old
            # code refunded invoice.total_amount even on a partially paid bill,
            # and reports keyed the refund off the invoice's creation date.
            collected = money(D(invoice.paid_amount) + D(invoice.tip_amount))
            if old_status in ('paid', 'partial') and collected > 0:
                InvoiceRefund.objects.create(
                    invoice=invoice, center=invoice.center, amount=collected,
                    reason=reason[:255], refunded_on=datetime.date.today(),
                )

            if old_status != 'draft':
                self._revert_stock(invoice)
                self._reverse_wallets(invoice, reason)
                self._withdraw_sold_advance(invoice, sold_advance, reason)
                self._restore_package_redemptions(invoice)
                self._deprovision_perks(invoice)
                self._reverse_promotions(invoice, reason, new_status)

                from pos_backend.sap_utils import (
                    create_sap_cancel_reversals, create_sap_credit_note, run_sap_sync_async)
                # The credit memo reverses the A/R invoice; this takes back the
                # payments and the card / membership / package / advance journals.
                run_sap_sync_async(create_sap_cancel_reversals, invoice)
                run_sap_sync_async(create_sap_credit_note, invoice)

            # A cancelled service bill cancels its appointment (#23).
            # Only an appointment that is still open or billed is cancelled (a
            # No-show stays a No-show), and not while another live bill still
            # stands on it.
            if invoice.appointment_id:
                from appointments.models import Appointment
                another_bill = (Invoice.objects
                                .filter(appointment_id=invoice.appointment_id,
                                        status__in=('paid', 'partial'))
                                .exclude(pk=invoice.pk).exists())
                if not another_bill:
                    Appointment.objects.filter(
                        pk=invoice.appointment_id,
                        status__in=Appointment.BOOKED + ('Completed',),
                    ).update(status='Cancelled')

            invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)

        return Response(InvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def refund(self, request, pk=None):
        """Refunds are not offered (owner decisions #45, F19): cancel the same day.

        Bills refunded before this change keep their 'refunded' status and
        records; they stay readable everywhere.
        """
        return Response(
            {'detail': 'Bills are not refunded. A bill can be cancelled on the day it was made.'},
            status=status.HTTP_400_BAD_REQUEST)

    # ─── Cancel rules ─────────────────────────────────────────────────────

    def _cancel_blocker(self, invoice):
        """Why this completed bill cannot be cancelled, or None."""
        from clients.models import ClientMembership, ClientPackage, ClientValueCard

        made_at = invoice.finalized_at or invoice.created_at
        if timezone.is_aware(made_at):
            made_at = timezone.localtime(made_at)
        made_on = made_at.date()
        if made_on != datetime.date.today():
            return (f'This bill was made on {made_on:%d %b %Y}. A bill can be cancelled only '
                    f'on the day it was made.')

        closed = ('cancelled', 'refunded')
        cards = list(ClientValueCard.objects.filter(source_invoice=invoice).values_list('id', flat=True))
        if cards and (Payment.objects.filter(value_card_id__in=cards)
                      .exclude(invoice=invoice).exclude(invoice__status__in=closed).exists()):
            return 'The value card sold on this bill has already been used, so the bill cannot be cancelled.'

        packages = ClientPackage.objects.filter(source_invoice=invoice)
        if packages.exists() and (InvoiceItem.objects.filter(redeemed_from_package__in=packages,
                                                             is_redemption=True)
                                  .exclude(invoice__status__in=closed).exists()):
            return 'The package sold on this bill has already been used, so the bill cannot be cancelled.'

        memberships = ClientMembership.objects.filter(source_invoice=invoice)
        if memberships.exists() and (Invoice.objects.filter(membership_used__in=memberships)
                                     .exclude(pk=invoice.pk).exclude(status__in=closed).exists()):
            return 'The membership sold on this bill has already been used, so the bill cannot be cancelled.'

        sold = self._sold_advance(invoice)
        if sold > 0 and invoice.client_id:
            available = wallets.advance_balance(invoice.client_id, lock=True)
            # This bill's own advance payments come back on cancel too.
            own_debits = money(sum((D(p.amount) for p in invoice.payments.all()
                                    if pm.canonical(p.payment_method) == Payment.ADVANCE), ZERO))
            if money(available + own_debits) < sold:
                return 'The advance sold on this bill has already been spent, so the bill cannot be cancelled.'
        return None

    def _sold_advance(self, invoice):
        """Advance credited to the client by this bill (its advance lines).

        What the wallet was actually credited: GST included for advances taken
        since the balance began to include it, the line value for older ones.
        """
        if not any(i.content_type and i.content_type.model == 'advancepayment'
                   for i in invoice.items.select_related('content_type')):
            return ZERO
        credited = AdvancePayment.objects.filter(
            invoice=invoice, amount__gt=0, reverses__isnull=True
        ).aggregate(t=Sum('amount'))['t']
        if credited is not None:
            return money(D(credited))
        return money(sum((D(i.total_price) for i in invoice.items.select_related('content_type')
                          if i.content_type and i.content_type.model == 'advancepayment'), ZERO))

    def _withdraw_sold_advance(self, invoice, amount, reason):
        """Take back the advance this bill sold. It was left with the client
        on cancel, so they kept the advance and got their money back too."""
        if amount <= 0 or not invoice.client_id:
            return
        AdvancePayment.objects.create(
            client=invoice.client, invoice=invoice, center=invoice.center,
            amount=-amount, notes=f'{reason} - advance sold on bill {invoice.display_number} withdrawn'[:255],
        )

    def _revert_stock(self, invoice):
        """Return sold products to stock, logging the delta actually applied."""
        from inventory.models import Product
        from inventory.stock import record_back

        for item in invoice.items.select_related('content_type').all():
            if not item.content_type:
                continue
                
            if item.content_type.app_label == 'services' and item.content_type.model == 'servicemaster':
                # What a service used (shampoo, colour, a facial's creams) is gone
                # once the service is done: cancelling the bill does not give it
                # back. The SAP credit memo does not either, so the two agree.
                continue

            if item.content_type.app_label != 'inventory':
                continue
            product = Product.objects.select_for_update().filter(pk=item.object_id).first()
            if not product:
                logger.warning('[Billing Cancel] Product id=%s missing; stock not restored for invoice #%s',
                               item.object_id, invoice.id)
                continue
            qty = stock_qty(item.quantity or 0)
            if qty <= 0:
                continue
            product.current_stock = stock_qty(product.current_stock) + qty
            product.save(update_fields=['current_stock'])
            from pos_backend.sap_utils import create_sap_stock_transaction, run_sap_sync_async
            # Each batch gets back what the bill took from it, so the goods keep
            # their expiry.
            for st in record_back(product, invoice.center, 'REFUND', qty,
                                  f'Reversed from Invoice {invoice.display_number}',
                                  invoice, ('SALE',)):
                run_sap_sync_async(create_sap_stock_transaction, st)

    def _reverse_wallets(self, invoice, reason):
        """Write compensating ledger entries for every wallet payment.

        Reversals are new rows. The old code deleted the negative
        AdvancePayment / CashbackTransaction rows outright, which destroyed the
        audit trail and made the ledger unauditable.
        """
        for payment in invoice.payments.all():
            if payment.is_tip:
                continue
            try:
                wallets.reverse_payment_wallets(payment, invoice, reason=reason)
            except Exception:
                logger.exception('[Billing Cancel] Failed to reverse payment %s on invoice #%s',
                                 payment.id, invoice.id)
                raise

    def _restore_package_redemptions(self, invoice):
        """Credit redeemed services back to the package they came from.

        Uses InvoiceItem.redeemed_from_package, so the credit goes to the right
        package. The old code credited the first package that happened to
        contain the service id - including expired ones, which it reactivated.
        """
        from clients.models import ClientPackage

        for item in invoice.items.filter(is_redemption=True):
            if not item.object_id:
                continue
            target = item.redeemed_from_package
            if target is None:
                logger.warning(
                    '[Billing Cancel] Redemption item %s on invoice #%s has no source package '
                    'recorded; not restoring to avoid crediting the wrong one.',
                    item.id, invoice.id,
                )
                continue
            cp = ClientPackage.objects.select_for_update().filter(pk=target.pk).first()
            if not cp:
                continue
            store = cp.services_remaining or {}
            key = str(item.object_id)
            store[key] = int(D(store.get(key, 0))) + int(item.quantity or 0)
            cp.services_remaining = store
            # Only reactivate if the package has not expired.
            if cp.expiry_date and cp.expiry_date >= datetime.date.today():
                cp.is_active = True
            cp.save(update_fields=['services_remaining', 'is_active'])

    def _deprovision_perks(self, invoice):
        """Remove entitlements issued by this invoice.

        Memberships are now always issued as their own row with source_invoice
        set (renewals stack instead of extending in place), so deleting by
        source_invoice fully reverses a refunded renewal.
        """
        from clients.models import ClientMembership, ClientPackage, ClientValueCard

        ClientMembership.objects.filter(source_invoice=invoice).delete()
        ClientPackage.objects.filter(source_invoice=invoice).delete()

        # A value card is DEACTIVATED, never destroyed.
        #
        # `Payment.value_card_id` is a plain IntegerField with no foreign key,
        # so deleting the card left payment rows pointing at an id that no
        # longer exists. Every later refund of a bill settled with that card
        # then failed silently: `credit_value_card` logs "cannot refund missing
        # value card" and returns None, so the customer's money was simply gone
        # with nothing on screen to say so.
        #
        # Zeroing the balance withdraws the entitlement just as effectively, and
        # keeps the row that the payment history refers to - the same
        # append-only discipline the wallet ledgers already follow.
        cancelled_cards = ClientValueCard.objects.filter(source_invoice=invoice)
        for card in cancelled_cards:
            card.is_active = False
            card.balance = Decimal('0.00')
            card.save(update_fields=['is_active', 'balance'])

    def _reverse_promotions(self, invoice, reason, new_status=None):
        """Claw back promotion cashback and void the usage record.

        Two defects lived here.

        The usage row was DELETED, which made a one-per-customer coupon
        farmable - bill with it, refund the bill, bill with it again,
        indefinitely - and erased the refunded bill from the marketing usage and
        ROI report for good. The row is now marked reversed and kept.

        And the cashback clawback wrote `-awarded` unconditionally, so if the
        customer had already SPENT the cashback on another bill the wallet went
        NEGATIVE. Award Rs 500, customer spends it, cancel the awarding bill:
        balance -500. That negative then silently eats their next top-up, and
        the shortfall is invisible - it is really a bad debt the salon has taken,
        not a customer liability. The clawback is now capped at what is actually
        left, and the uncollectable part is recorded in the note.
        """
        from marketing.models import PromotionUsage
        from billing.wallets import cashback_balance

        for usage in PromotionUsage.objects.filter(invoice=invoice):
            awarded = money(usage.cashback_awarded)
            if awarded <= 0 or not invoice.client:
                continue

            already = CashbackTransaction.objects.filter(
                invoice=invoice, amount__lt=0, reverses__isnull=False).exists()
            if already:
                continue

            available = cashback_balance(invoice.client_id, lock=True)
            original = (CashbackTransaction.objects.select_for_update()
                        .filter(invoice=invoice, amount__gt=0, reverses__isnull=True)
                        .first())
            # What is still unspent of THIS credit is what can be taken back.
            left = (money(original.remaining) if original is not None and original.kind
                    else awarded)
            recoverable = min(awarded, left, available)
            if recoverable < 0:
                recoverable = ZERO
            shortfall = awarded - recoverable

            if recoverable > 0:
                if original is not None and original.kind:
                    original.remaining = money(D(original.remaining) - recoverable)
                    original.save(update_fields=['remaining'])
                note = f'{reason} - cashback reversed for Invoice #{invoice.id}'
                if shortfall > 0:
                    note += (f' (Rs {shortfall} of the Rs {awarded} awarded had '
                             f'already been spent and cannot be recovered)')
                CashbackTransaction.objects.create(
                    client=invoice.client, invoice=invoice, center=invoice.center,
                    amount=-recoverable, reverses=original, notes=note[:255],
                    kind=CashbackTransaction.KIND_REVERSAL,
                )
            elif shortfall > 0:
                logger.warning(
                    '[Billing] Invoice #%s cancelled but its Rs %s cashback award '
                    'was already spent; nothing to claw back.',
                    invoice.id, awarded)

        # Keep the record. Only a cancellation - nothing delivered - gives the
        # customer their allowance back.
        releases = str(new_status or '').lower() == 'cancelled'
        PromotionUsage.objects.filter(invoice=invoice).update(
            is_reversed=True,
            releases_allowance=releases,
            reversal_reason=(reason or '')[:255],
        )

    # ─── Payment method change ────────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def change_payment(self, request, pk=None):
        """Change a payment's method, moving the money to match.

        The old implementation only rewrote the method string. Switching a
        payment from Value Card to Cash left the card debited; switching Cash to
        Advance never debited the wallet.
        """
        # Permission: the "change payment method" tick, checked before this runs.
        invoice = self.get_object()
        self._check_payroll_lock(invoice)

        new_method = pm.canonical(request.data.get('payment_method'))
        if not request.data.get('payment_method'):
            return Response({'payment_method': 'payment_method is required.'}, status=400)
        payment_id = request.data.get('payment_id')
        if pm.is_value_card(new_method):
            return Response({'detail': 'A value card is applied to the services on the bill, not '
                                       'swapped in as a payment. Cancel and re-bill to use one.'},
                            status=400)
        if pm.is_gift_card(new_method):
            return Response({'detail': 'A gift card coupon is entered with its code when the bill is '
                                       'made, not swapped in afterwards. Cancel and re-bill to use one.'},
                            status=400)
        if not pm.is_wallet(new_method) and new_method not in pm.allowed_at(invoice.center):
            return Response({'detail': f'{new_method} is not a payment method at this branch.'},
                            status=400)
        reference = str(request.data.get('reference') or '').strip()[:64]

        # Within the window set in Admin > Settings (decision #41: 6 hours).
        from pos_backend import app_settings
        hours = int(app_settings.get('billing.payment_change_hours', center=invoice.center))
        made_at = invoice.finalized_at or invoice.created_at
        if made_at and timezone.now() - made_at > datetime.timedelta(hours=hours):
            return Response({'detail': f'The payment method can be changed only within {hours} '
                                       f'hours of the bill.'}, status=400)

        with transaction.atomic():
            invoice = (Invoice.objects.select_for_update()
                       .select_related('client', 'center').get(pk=invoice.pk))
            if invoice.status in ('cancelled', 'refunded'):
                return Response({'detail': f'Invoice is {invoice.status}.'}, status=400)

            payments = [p for p in invoice.payments.filter(is_tip=False)
                        if not pm.is_value_card(p.payment_method)]
            if payment_id:
                payments = [p for p in payments if str(p.id) == str(payment_id)]
                if not payments:
                    return Response({'detail': 'payment_id not found for this invoice, or it is '
                                               'the value card\'s part, which cannot change.'},
                                    status=404)
            elif len(payments) > 1:
                return Response({'detail': 'Multiple payments exist. Provide a payment_id.'}, status=400)

            if any(pm.is_gift_card(p.payment_method) for p in payments):
                return Response({'detail': 'A gift card coupon payment cannot be changed: the brand is '
                                           'billed for it. Cancel the bill and make it again.'},
                                status=400)

            # Moving a payment onto the client's advance or cashback spends money they hold: the same PIN as at the till.
            if any(pm.canonical(p.payment_method) != new_method for p in payments):
                from billing import pin_gate
                pin_gate.enforce(request, invoice.client, invoice.center, [{'payment_method': new_method}])

            changed = []
            notes = []
            for payment in payments:
                old_method = pm.canonical(payment.payment_method)
                if old_method == new_method:
                    if reference and reference != payment.reference:
                        payment.reference = reference
                        payment.save(update_fields=['reference'])
                    continue
                # Give back whatever the old method took.
                wallets.reverse_payment_wallets(payment, invoice, reason='Payment method changed')
                payment.payment_method = new_method
                payment.value_card_id = None
                payment.reference = reference
                if not pm.is_cash(new_method):
                    payment.tendered = None
                payment.save(update_fields=['payment_method', 'value_card_id', 'reference',
                                            'tendered'])
                # And take it from the new one. Raises if the balance is short,
                # rolling the whole change back.
                wallets.apply_payment_to_wallets(payment, invoice)
                changed.append(payment.id)
                notes.append(f'Rs {payment.amount}: {old_method} -> {new_method}')

            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center,
                user=request.user if request.user.is_authenticated else None,
                action=f"Change Payment Type {'(Partial)' if payment_id else '(All)'}",
                notes=('; '.join(notes) or f'Changed to {new_method}')
                      + (f' | ref {reference}' if reference else ''),
            )

            if changed:
                # SAP has the old method: cancel those incoming payments and
                # post them again as they are now.
                from pos_backend.sap_utils import (
                    repost_incoming_payments, request_payment_repost, run_sap_sync_async)
                request_payment_repost(invoice, changed)
                run_sap_sync_async(repost_incoming_payments, invoice)

            invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)

        return Response(InvoiceSerializer(invoice).data)

    # ─── Promotions ───────────────────────────────────────────────────────

    @action(detail=True, methods=['post'])
    def apply_promo(self, request, pk=None):
        """Apply a promotion to an existing invoice, once."""
        from marketing.promotions import apply_promotion

        invoice = self.get_object()
        self._check_payroll_lock(invoice)
        promo_id = request.data.get('promo_id')
        if not promo_id:
            return Response({'promo_id': 'promo_id is required.'}, status=400)
        # An offer is part of pricing the bill, before any money is taken
        # (X10): a completed bill is cancelled and re-billed instead.
        if invoice.status != 'draft' or invoice.finalized_at:
            return Response({'detail': 'An offer can be added only to a held bill.'}, status=400)
        if (invoice.promotion_id or invoice.membership_used_id
                or invoice.items.filter(card_face__gt=0).exists()):
            return Response({'detail': 'This bill already has an offer or a value card; one per bill.'},
                            status=400)

        with transaction.atomic():
            invoice = (Invoice.objects.select_for_update()
                       .select_related('client', 'center').get(pk=invoice.pk))
            if invoice.status in ('cancelled', 'refunded'):
                return Response({'detail': f'Invoice is {invoice.status}.'}, status=400)

            # raise_on_error=True so a rejected promotion aborts the
            # transaction. Returning a Response here would be a normal exit,
            # committing any partial recompute the promotion had already done.
            discount, _ = apply_promotion(invoice, promo_id, raise_on_error=True)

            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center,
                user=request.user if request.user.is_authenticated else None,
                action='Apply Promotion',
                notes=f'Discount Rs {discount}; new total Rs {invoice.total_amount}',
            )
            invoice.refresh_from_db()

        return Response({
            'discount_applied': str(discount),
            'new_total': str(invoice.total_amount),
            'invoice': InvoiceSerializer(
                Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)
            ).data,
        })

    # ─── Create / update / delete ─────────────────────────────────────────

    def _idempotency_key(self, request):
        """The caller's key for this checkout ATTEMPT, if they supplied one.

        Deliberately NOT a hash of the request body. A salon sells the same
        basket over and over - two walk-ins buying the same haircut minutes
        apart produce byte-identical payloads - so a body hash cannot tell a
        retry from the next customer, and collapsing those two would lose a
        real sale and its revenue.

        The till generates one key per checkout and reuses it when it retries,
        which is the only thing that actually distinguishes the two cases. Sent
        as the `Idempotency-Key` header, or `idempotency_key` in the body.

        Without a key there is no safe way to dedupe, so the request goes
        through. That is the correct trade: a duplicated bill is visible and
        refundable, a silently swallowed sale is neither.
        """
        raw = (request.headers.get('Idempotency-Key')
               or request.headers.get('X-Idempotency-Key'))
        if not raw and hasattr(request, 'data') and hasattr(request.data, 'get'):
            raw = request.data.get('idempotency_key')

        raw = str(raw or '').strip()
        if not raw:
            return None

        # Scope to the user so one till's key cannot collide with another's.
        digest = hashlib.sha256(
            f'{request.user.pk}:{raw}'.encode('utf-8')).hexdigest()
        return digest

    def create(self, request, *args, **kwargs):
        """Create a bill, at most once per submission.

        The guard here was `cache.add(key, True, timeout=30)` against the default
        cache. That cache is LocMemCache, which is per PROCESS - so with more
        than one worker, two identical submissions land on different workers,
        neither sees the other's key, and BOTH complete: two invoices, two GST
        serial numbers, stock deducted twice, and the customer's wallet debited
        twice. LocMemCache also culls a third of its entries once it holds 300,
        so the key could be evicted mid-checkout even with a single worker.

        A unique index in the database is atomic across processes, survives a
        restart, and does not depend on Redis being configured.

        A retry also gets THE SAME BILL back rather than an error. A till whose
        network dropped after the invoice was written would otherwise show a
        failure, and staff would bill the customer again by hand.
        """
        cache_key = self._idempotency_key(request)
        if not cache_key:
            # The body could not be serialised, so there is nothing to key on.
            with transaction.atomic():
                Invoice.objects.filter(pk=-1).update(status='draft')
                return self._create_invoice(request)

        record, is_first_attempt = self._claim_checkout(cache_key)

        if not is_first_attempt:
            if record is not None and record.invoice_id:
                # The original attempt finished. Hand back what it produced.
                invoice = (Invoice.objects
                           .prefetch_related(*_INVOICE_PREFETCH)
                           .filter(pk=record.invoice_id)
                           .first())
                if invoice is not None:
                    return Response(InvoiceSerializer(invoice).data,
                                    status=status.HTTP_200_OK)
            return Response({'detail': 'This checkout is already being processed.'},
                            status=status.HTTP_409_CONFLICT)

        def attempt():
            with transaction.atomic():
                # --- SQLITE WAL DEADLOCK PREVENTION ---
                # Acquire a RESERVED lock immediately to prevent instant deadlocks
                # when upgrading from SHARED to EXCLUSIVE later. This ensures SQLite
                # waits up to the 20s timeout rather than failing instantly.
                CheckoutIdempotency.objects.filter(pk=record.pk).update(invoice_id=None)

                return self._create_invoice(request)

        try:
            response = _with_deadlock_retry(attempt)
        except Exception:
            # The attempt failed, so the same basket must be allowed through
            # again; leaving the claim would block a legitimate retry.
            CheckoutIdempotency.objects.filter(
                pk=record.pk, invoice__isnull=True).delete()
            raise

        invoice_id = None
        if getattr(response, 'data', None) and isinstance(response.data, dict):
            invoice_id = response.data.get('id')

        if invoice_id:
            CheckoutIdempotency.objects.filter(pk=record.pk).update(
                invoice_id=invoice_id, completed_at=timezone.now())
        else:
            # Nothing to replay, so do not hold the key.
            CheckoutIdempotency.objects.filter(pk=record.pk).delete()

        return response

    def _claim_checkout(self, key):
        """Claim this submission, or find the claim someone else already made.

        Returns (record, is_first_attempt). The INSERT is what makes this safe:
        of two racing workers, exactly one can create the row.
        """
        from django.db import IntegrityError

        try:
            # In its own atomic block: Django marks a transaction for rollback
            # after a failed statement, so doing this inside the caller's
            # transaction would poison it.
            with transaction.atomic():
                return CheckoutIdempotency.objects.create(key=key), True
        except IntegrityError:
            existing = CheckoutIdempotency.objects.filter(key=key).first()
            if existing is None:
                # Raced with a cleanup between the INSERT failing and this read.
                # Treat it as a fresh attempt rather than refusing a real sale.
                try:
                    with transaction.atomic():
                        return CheckoutIdempotency.objects.create(key=key), True
                except IntegrityError:
                    existing = CheckoutIdempotency.objects.filter(key=key).first()
            return existing, False

    def _validate_stock_for_items(self, center, items_data):
        if not center:
            return
            
        from inventory.models import Product
        from services.models import deducting_boms

        # Aggregate required quantities for all items
        required_products = {} # product_code -> { 'name': str, 'qty': int }
        # One query for every service on the bill, not one per line.
        from services.models import ServiceMaster
        service_ids = [it.get('object_id') for it in items_data
                       if it.get('content_type', '') in ('services.servicemaster', 'servicemaster') and it.get('object_id')]
        owners = ServiceMaster.objects.in_bulk(service_ids) if service_ids else {}
        
        for item in items_data:
            c_type = item.get('content_type', '')
            obj_id = item.get('object_id')
            qty = float(item.get('quantity') or 0)
            
            if qty <= 0 or not obj_id:
                continue
                
            if c_type in ('services.servicemaster', 'servicemaster'):
                from services import recipes
                owner = owners.get(int(obj_id))
                if owner is not None and recipes.uses_recipe(owner):
                    # A recipe the POS owns: the fixed items plus what the till chose (and
                    # the default for what it has not chosen yet), as a held bill may be short.
                    entries = recipes.resolve(owner, center, int(qty), item.get('recipe_choices'), strict=False)
                    for entry in entries:
                        if not entry['deduct']:
                            continue
                        if entry['product_code'] not in required_products:
                            required_products[entry['product_code']] = {'name': entry['name'], 'qty': 0}
                        required_products[entry['product_code']]['qty'] += float(entry['quantity'])
                    continue
                for bom in deducting_boms(obj_id, center):
                    req_qty = float(bom.quantity) * qty
                    if req_qty > 0:
                        if bom.product_code not in required_products:
                            # Try to get the name for a friendly error
                            p = Product.objects.filter(product_code=bom.product_code).first()
                            name = p.name if p else bom.product_code
                            required_products[bom.product_code] = {'name': name, 'qty': 0}
                        required_products[bom.product_code]['qty'] += req_qty
            
            elif c_type in ('inventory.product', 'product'):
                p = Product.objects.filter(pk=obj_id).first()
                if p:
                    if p.product_code not in required_products:
                        required_products[p.product_code] = {'name': p.name, 'qty': 0}
                    required_products[p.product_code]['qty'] += qty

        # Verify stock for aggregated requirements
        for p_code, data in required_products.items():
            product = Product.objects.filter(center=center, product_code=p_code).first()
            current_stock = float(stock_qty(product.current_stock)) if product else 0
            
            if current_stock < data['qty']:
                raise ValidationError({
                    'items': f"Not enough stock for consumable '{data['name']}'. Required: {data['qty']}, Available: {current_stock}."
                })

    def _create_invoice(self, request):
        payload = self._normalise_payload(request)

        client_id = payload.get('client')
        if client_id:
            from clients.models import Client
            client = Client.objects.filter(pk=client_id).first()
            if client and client.is_blacklisted:
                return Response({'detail': 'This client is blacklisted and cannot be billed.'},
                                status=status.HTTP_400_BAD_REQUEST)

        serializer = self.get_serializer(data=payload)
        serializer.is_valid(raise_exception=True)

        center = serializer.validated_data.get('center')
        if center:
            assert_can_write_center(request.user, center)
        elif user_center_ids(request.user) is not None:
            raise ValidationError({'center': 'A center is required.'})

        # Validate BOM stock levels before billing
        self._validate_stock_for_items(center, payload.get('items', []))

        user = request.user if request.user.is_authenticated else None
        invoice = serializer.save(created_by=user, shift=_open_shift(user, center))

        if invoice.status in ('paid', 'partial'):
            # `finalize_invoice` needs the line items, so fetch them once here.
            # This used to re-fetch with the full prefetch BEFORE the status
            # check as well, so a draft bill - which never calls finalize - paid
            # for a prefetch it did not use, and a finalised one paid for the
            # whole prefetch twice.
            invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)
            finalize_invoice(invoice, request.data.get('appointment_id'), request.data,
                             skip_payment_deductions=False)
            invoice.refresh_from_db()
            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center,
                user=request.user if request.user.is_authenticated else None,
                action='Create Bill',
                notes=f'Total Rs {invoice.total_amount} | Discount Rs {invoice.discount}',
            )

        # One authoritative read for the response, after everything that could
        # have changed the row.
        invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice.pk)
        return Response(InvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)

    def _normalise_payload(self, request):
        """Flatten nested staff objects and parse a JSON `items` string."""
        data = request.data.dict() if hasattr(request.data, 'dict') else dict(request.data)

        if isinstance(data.get('items'), str):
            try:
                data['items'] = json.loads(data['items'])
            except (TypeError, ValueError):
                raise ValidationError({'items': 'items must be a list or a valid JSON array.'})

        def flatten_staff(container):
            value = container.get('staff')
            if isinstance(value, dict) and 'id' in value:
                container['staff'] = value['id']
            elif isinstance(value, str) and value.isdigit():
                container['staff'] = int(value)

        flatten_staff(data)
        # `appointment_id` is the older spelling of `appointment`. Both mean the
        # same thing: link the bill to that appointment (and be checked).
        if data.get('appointment_id') and not data.get('appointment'):
            data['appointment'] = data['appointment_id']
        if isinstance(data.get('items'), list):
            items = []
            for it in data['items']:
                if isinstance(it, dict):
                    it = dict(it)
                    flatten_staff(it)
                items.append(it)
            data['items'] = items
        return data

    def update(self, request, *args, **kwargs):
        return _with_deadlock_retry(lambda: self._update_in_transaction(request, *args, **kwargs))

    @transaction.atomic
    def _update_in_transaction(self, request, *args, **kwargs):
        invoice_id = self.kwargs.get('pk')
        Invoice.objects.select_for_update().get(pk=invoice_id)

        invoice = self.get_object()
        self._check_payroll_lock(invoice)

        if invoice.status in ('cancelled', 'refunded'):
            return Response({'detail': f'Invoice is {invoice.status} and cannot be edited.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # Only a held bill is edited. A completed bill's stock, commission and
        # wallet records describe what was sold and paid; it changes through
        # Pay, Cancel or Change payment method (#44: cancel and re-bill).
        if invoice.status != 'draft' or invoice.finalized_at:
            return Response(
                {'detail': 'This bill has already been completed. Cancel it and raise a new bill '
                           'instead of editing it.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        old_status = invoice.status
        payload = self._normalise_payload(request)
        serializer = self.get_serializer(invoice, data=payload, partial=kwargs.get('partial', False))
        serializer.is_valid(raise_exception=True)

        center = serializer.validated_data.get('center', invoice.center)
        if center:
            assert_can_write_center(request.user, center)
        user = request.user if request.user.is_authenticated else None
        extra = {'shift': _open_shift(user, center)}
        if not invoice.created_by_id:
            extra['created_by'] = user
        serializer.save(**extra)

        invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice_id)

        if old_status == 'draft' and invoice.status in ('paid', 'partial'):
            finalize_invoice(invoice, request.data.get('appointment_id'), request.data,
                             skip_payment_deductions=False)
            invoice.refresh_from_db()
            BillChangeLog.objects.create(
                invoice=invoice, center=invoice.center,
                user=request.user if request.user.is_authenticated else None,
                action='Finalize Bill',
                notes=f'Total Rs {invoice.total_amount} | Discount Rs {invoice.discount}',
            )

        invoice = Invoice.objects.prefetch_related(*_INVOICE_PREFETCH).get(pk=invoice_id)
        return Response(InvoiceSerializer(invoice).data)

    def destroy(self, request, *args, **kwargs):
        """Only a held (draft) bill can be deleted (owner decision D13).

        A draft has no number, took no money and moved no stock, so deleting
        it loses nothing; the till's Discard used to fail on it, leaving the
        draft behind. A completed bill is a financial record and is never
        deleted - it is cancelled, which keeps the record and reverses stock,
        wallets and entitlements.
        """
        invoice = self.get_object()
        if invoice.status != 'draft' or invoice.finalized_at or invoice.invoice_number:
            return Response(
                {'detail': 'A completed bill cannot be deleted. Cancel it instead.'},
                status=status.HTTP_405_METHOD_NOT_ALLOWED,
            )
        with transaction.atomic():
            locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
            if locked.status != 'draft' or locked.finalized_at:
                return Response({'detail': 'This bill was completed meanwhile.'},
                                status=status.HTTP_409_CONFLICT)
            locked.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdvancePaymentViewSet(viewsets.ModelViewSet):
    queryset = AdvancePayment.objects.select_related(
        'client', 'invoice', 'client__center', 'center'
    ).order_by('-created_at')
    serializer_class = AdvancePaymentSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'billing'
    # Taking an advance is part of raising a bill.
    permission_submodule = 'new_invoice'
    # A client's wallet history is shown on Admin > Clients and next to their bills.
    permission_read_also = [('admin', 'clients'), ('billing', 'invoices'), ('admin', 'bills')]

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        if 'client_id' in request.query_params and 'page' not in request.query_params:
            queryset = queryset[:50]
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(self.get_serializer(page, many=True).data)
        return Response(self.get_serializer(queryset, many=True).data)

    def get_queryset(self):
        qs = scope_to_centers(
            super().get_queryset(), self.request.user, field='client__center',
            requested_center=coerce_center_id(self.request.query_params.get('center_id')),
        )
        client_id = self.request.query_params.get('client_id')
        if client_id:
            try:
                qs = qs.filter(client_id=int(client_id))
            except (TypeError, ValueError):
                return qs.none()
        return qs

    def perform_create(self, serializer):
        # An advance is taken on a bill: the till sells an advance line and the
        # server credits the wallet when that bill is completed, from the money
        # actually paid (billing/services.py). A bare POST here credited any
        # amount with no bill, payment or cash in the drawer, so it is kept only
        # for the owner's correcting entries.
        if not IsOwner.check_is_owner(self.request.user):
            raise PermissionDenied(
                'An advance is taken by selling an Advance line on a bill. '
                'Only the owner can post a correcting entry.')
        client = serializer.validated_data.get('client')
        center = getattr(client, 'center', None)
        if center:
            assert_can_write_center(self.request.user, center)
        advance = serializer.save(center=center, staff=serializer.validated_data.get('staff'))
        
        # Trigger SAP sync for Journal Entry
        from pos_backend.sap_utils import create_sap_advance_je, run_sap_sync_async
        run_sap_sync_async(create_sap_advance_je, advance)

    def update(self, request, *args, **kwargs):
        return Response(
            {'detail': 'Advance payments are a ledger and cannot be edited. '
                       'Record a correcting entry instead.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def partial_update(self, request, *args, **kwargs):
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        """The wallet balance is Sum(amount); deleting a row rewrites history.

        Deleting a negative row silently gifted the client free balance.
        """
        return Response(
            {'detail': 'Advance payments cannot be deleted. Record a correcting entry instead.'},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class BillChangeLogViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BillChangeLog.objects.select_related('invoice', 'center', 'user').order_by('-created_at')
    serializer_class = BillChangeLogSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'changes'
    # The bill audit trail is its own grant, not part of "any Admin tab".
    permission_strict_read = True

    def get_queryset(self):
        qs = scope_to_centers(
            super().get_queryset(), self.request.user,
            requested_center=coerce_center_id(self.request.query_params.get('center_id')),
        )
        start_date = safe_date_param(self.request.query_params.get('start_date'))
        if start_date:
            qs = qs.filter(created_at__date__gte=start_date)
        end_date = safe_date_param(self.request.query_params.get('end_date'))
        if end_date:
            qs = qs.filter(created_at__date__lte=end_date)
        return qs
