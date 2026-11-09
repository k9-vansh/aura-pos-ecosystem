from django.utils.decorators import method_decorator
from django.core.cache import cache
# Removed cache_page due to cross-tenant leak
from decimal import Decimal
from rest_framework import viewsets, status, views
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from .models import PettyCashCategory, PettyCashEntry, DailyClosing, IncentiveConfig, Shift, IncentiveRule
from .serializers import PettyCashCategorySerializer, PettyCashEntrySerializer, DailyClosingSerializer, IncentiveConfigSerializer, ShiftSerializer, IncentiveRuleSerializer
from billing.models import Invoice, Payment, InvoiceItem, AdvancePayment
from inventory.models import PurchaseOrder, PurchaseOrderItem
from django.db.models import Sum, Count, Q, Max, F
from django.contrib.contenttypes.models import ContentType
from salon_admin.models import Center
from datetime import datetime
import datetime as dt_module
from collections import defaultdict
import calendar
import logging

from pos_backend.money import D, money, rupees, ZERO, split
from pos_backend.permissions import IsOwner, HasModulePermission
from pos_backend.scoping import (
    safe_date_param,
    scope_to_centers, assert_can_write_center, user_center_ids, coerce_center_id,
    narrow_to_selection, selected_center_ids, selection_key,
    can_use_center, resolve_write_center,
)
from rest_framework.exceptions import PermissionDenied

logger = logging.getLogger(__name__)



def _safe_int(raw, default):
    """Parse an int from a query param without raising on junk input."""
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError, AttributeError):
        return default

def _get_filtered_invoices(request, center_id, start_date, end_date, statuses=('paid', 'partial')):
    qs = Invoice.objects.filter(status__in=statuses)

    user = request.user
    perms = getattr(user.role, 'permissions', {}) or {}
    is_owner = IsOwner.check_is_owner(user)
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            qs = qs.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            qs = qs.filter(center=user.center)

        else:
            # Fail closed: a user assigned to no center sees nothing.
            qs = qs.none()
    # A malformed center_id used to reach the ORM and raise ValueError - a 500
    # on every report from one bad query param.
    resolved_center = coerce_center_id(center_id)
    if resolved_center is not None:
        qs = qs.filter(center_id=resolved_center)
    # Several centres ticked in the location dropdown.
    qs = narrow_to_selection(qs, 'center')

    # An unparseable date used to fall through to `created_at__date__gte=<junk>`,
    # which raises Django's ValidationError - also a 500. An unusable date is
    # now simply not applied.
    from datetime import datetime as _dt, timedelta as _td

    def _parse(raw):
        if not raw:
            return None
        try:
            return _dt.strptime(str(raw).strip(), '%Y-%m-%d')
        except (TypeError, ValueError):
            return None

    start = _parse(start_date)
    if start:
        qs = qs.filter(created_at__gte=start)

    end = _parse(end_date)
    if end:
        qs = qs.filter(created_at__lt=end + _td(days=1))

    # ── Never scan the whole history ──────────────────────────────────────
    #
    # With no dates supplied at all, this returned every invoice ever raised.
    # That is fine on day one and ruinous in year three: a report opened with no
    # filter scans the entire Invoice and InvoiceItem history, on a dev server
    # with one worker, while the tills are trying to bill.
    #
    # The default is the current month, which is what these screens open on
    # anyway. An explicit date always wins, so nothing an operator asked for is
    # silently narrowed - and `default_window_applied` says when the default was
    # used, so a caller can tell.
    if not start and not end and not _wants_all_time(request):
        import datetime as _dt_mod

        first_of_month = _dt_mod.date.today().replace(day=1)
        qs = qs.filter(created_at__gte=_dt_mod.datetime.combine(
            first_of_month, _dt_mod.time.min))

    return qs


#: How a caller asks for genuinely everything, having understood the cost.
UNBOUNDED_REPORT_PARAM = 'all_time'


def report_window_was_defaulted(request, start_date, end_date):
    """True when the caller supplied no dates and got the current month.

    Reports include this so a screen can say "showing this month" rather than
    implying it is showing everything.
    """
    return not start_date and not end_date and not _wants_all_time(request)


def _wants_all_time(request):
    """Did the caller explicitly ask for the whole history?

    Tolerates a stand-in request object whose query_params is a plain dict, which
    is what the internal report helpers construct.
    """
    params = getattr(request, 'query_params', None)
    if params is None:
        return False
    raw = params.get(UNBOUNDED_REPORT_PARAM)
    return str(raw or '').strip().lower() in ('1', 'true', 'yes')


def _compute_revenue_breakdown(invoices):
    """Revenue by item type. Amounts are tax-exclusive taxable value.

    InvoiceItem.total_price is now tax-exclusive (H2), so these figures and
    Invoice.subtotal are in the same units and `sum(items) == subtotal` holds.
    Tax is reported separately via `tax` so nothing double-counts it.
    """
    from services.models import ServiceMaster
    from inventory.models import Product
    from marketing.models import Membership, Package, ValueCard
    from django.db.models import Sum, Q

    empty = {k: Decimal('0.00') for k in
             ('services', 'products', 'memberships', 'packages', 'value_cards', 'other')}
    empty['tax'] = Decimal('0.00')

    try:
        ct_map = ContentType.objects.get_for_models(
            ServiceMaster, Product, Membership, Package, ValueCard
        )
    except Exception as e:
        logger.warning('[Finance] ContentType lookup failed during revenue breakdown: %s', e)
        return empty

    filters = {
        'services': Q(content_type=ct_map[ServiceMaster]),
        'products': Q(content_type=ct_map[Product]),
        'memberships': Q(content_type=ct_map[Membership]),
        'packages': Q(content_type=ct_map[Package]),
        'value_cards': Q(content_type=ct_map[ValueCard]),
        'other': Q(content_type__isnull=True),
    }

    items_qs = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False)

    aggregates = {key: Sum('total_price', filter=cond) for key, cond in filters.items()}
    aggregates['tax'] = Sum('tax_amount')
    result = items_qs.aggregate(**aggregates)

    return {k: Decimal(str(v or 0)) for k, v in result.items()}


def _compute_payment_breakdown(invoices, include_tips=False):
    """Payment totals per report bucket.

    Classification is delegated to billing.payment_methods, which matches the
    canonical method exactly. The old implementation matched substrings with
    'cash' checked first, and 'cash' is a substring of 'cashback wallet' - so
    every cashback redemption was reported as cash and the drawer could never
    be reconciled.

    Tips are excluded by default: they are not revenue against the bill.
    """
    from billing import payment_methods as pmods

    payments_qs = Payment.objects.filter(invoice__in=invoices)
    if not include_tips:
        payments_qs = payments_qs.filter(is_tip=False)

    payments_agg = payments_qs.values('payment_method').annotate(
        total_amount=Sum('amount'), count=Count('id')
    )

    result = {k: {'amount': Decimal('0.00'), 'count': 0} for k in pmods.REPORT_BUCKETS}

    for p in payments_agg:
        bucket = pmods.report_bucket(p['payment_method'])
        result[bucket]['amount'] += Decimal(str(p['total_amount'] or 0))
        result[bucket]['count'] += p['count']

    return result


def _compute_tip_total(invoices):
    """Tips collected in this period. Reported separately from revenue (H11)."""
    total = Payment.objects.filter(invoice__in=invoices, is_tip=True).aggregate(
        t=Sum('amount')
    )['t']
    if total:
        return Decimal(str(total))
    # Legacy rows carry the tip on the invoice header rather than a tip payment.
    header = invoices.aggregate(t=Sum('tip_amount'))['t']
    return Decimal(str(header or 0))


def _compute_tax_breakdown(invoices):
    """GST per item category, summed from the line items that carry it.

    Sourcing tax from the items rather than back-apportioning the invoice-level
    cgst+sgst means the split is real. Under the old approach the ratio was
    derived from service revenue, which was zero because of C1, so the entire
    tax liability was attributed to products.
    """
    from services.models import ServiceMaster
    from inventory.models import Product
    from marketing.models import Membership, Package, ValueCard
    from django.db.models import Sum, Q

    keys = ('services', 'products', 'memberships', 'packages', 'value_cards', 'other')
    out = {k: Decimal('0.00') for k in keys}
    out['total'] = Decimal('0.00')

    try:
        ct_map = ContentType.objects.get_for_models(
            ServiceMaster, Product, Membership, Package, ValueCard
        )
    except Exception as e:
        logger.warning('[Finance] ContentType lookup failed during tax breakdown: %s', e)
        return out

    conds = {
        'services': Q(content_type=ct_map[ServiceMaster]),
        'products': Q(content_type=ct_map[Product]),
        'memberships': Q(content_type=ct_map[Membership]),
        'packages': Q(content_type=ct_map[Package]),
        'value_cards': Q(content_type=ct_map[ValueCard]),
        'other': Q(content_type__isnull=True),
    }
    agg = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False).aggregate(
        total=Sum('tax_amount'),
        **{k: Sum('tax_amount', filter=c) for k, c in conds.items()},
    )
    return {k: Decimal(str(v or 0)) for k, v in agg.items()}


class PettyCashEntryViewSet(viewsets.ModelViewSet):
    queryset = PettyCashEntry.objects.all().select_related('user', 'center').order_by('-date', '-id')
    serializer_class = PettyCashEntrySerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'pettycash'
    permission_strict_read = True
    # The multi-salon tab lists petty cash per branch; the day-end screen that
    # holds Register Shifts shows the day's petty cash beside the shift.
    permission_read_also = [("finance", "multi_salon"), ("finance", "shifts")]
    # The bill picture comes as a multipart upload; plain JSON still works for everything else.
    parser_classes = [JSONParser, MultiPartParser, FormParser]
    def get_queryset(self):
        user = self.request.user
        queryset = self.queryset

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)
            else:
                return queryset.none()

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)

        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')
        # Use __date__ lookup so DateTimeField is compared correctly (avoids missing end-day entries)
        if start_date:
            queryset = queryset.filter(date__date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__date__lte=end_date)

        return queryset.order_by('-date')

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create petty cash entries for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create petty cash entries for this center.")
        entry = serializer.save(user=self.request.user)
        # The expense goes to SAP as a journal: debit the category's expense
        # account, credit the till cash. Queued here, posted by the worker.
        from pos_backend.sap_utils import create_sap_petty_cash_je, run_sap_sync_async
        run_sap_sync_async(create_sap_petty_cash_je, entry)

    def _refuse_if_posted(self, entry, verb):
        if entry.sap_sync_status:
            raise PermissionDenied(
                f"This petty cash entry is already in SAP, so it cannot be {verb}. "
                "Reverse the journal in SAP and record the expense again.")

    def perform_update(self, serializer):
        self._refuse_if_posted(serializer.instance, 'changed')
        entry = serializer.save()
        # Not posted yet (still queued or failed): the queued job reads the saved row.
        from pos_backend.sap_utils import create_sap_petty_cash_je, run_sap_sync_async
        run_sap_sync_async(create_sap_petty_cash_je, entry)

    def perform_destroy(self, instance):
        self._refuse_if_posted(instance, 'deleted')
        from pos_backend.models import BackgroundJob
        BackgroundJob.objects.filter(dedupe_key=f'sap:petty_cash_je:{instance.pk}').delete()
        instance.delete()


class DailyClosingViewSet(viewsets.ModelViewSet):
    queryset = DailyClosing.objects.all().select_related('user', 'center').order_by('-date', '-id')
    serializer_class = DailyClosingSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'pettycash'
    permission_strict_read = True
    # Whoever runs the shifts closes the day on this same screen.
    permission_read_also = [("finance", "shifts")]
    # A recorded day-end closing is changed only through "amend closing"
    # (finding R21): PUT/PATCH used to need nothing beyond petty-cash update and
    # left no trace, while only the create path guarded overwrites.
    permission_action_ticks = {
        'update': ('pettycash', 'amend_closing'),
        'partial_update': ('pettycash', 'amend_closing'),
        'destroy': ('pettycash', 'amend_closing'),
    }

    def get_queryset(self):
        user = self.request.user
        queryset = self.queryset

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)
            else:
                return queryset.none()

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        date = self.request.query_params.get('date', None)
        start_date = self.request.query_params.get('start_date', None)
        end_date = self.request.query_params.get('end_date', None)
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')
        if date:
            queryset = queryset.filter(date=date)
        if start_date:
            queryset = queryset.filter(date__gte=start_date)
        if end_date:
            queryset = queryset.filter(date__lte=end_date)
        return queryset.order_by('-date')

    def create(self, request, *args, **kwargs):
        """Record a day's closing.

        Two fixes here. The permission check used to be missing the
        single-center branch that perform_create has - and because this method
        overrides create() entirely, perform_create never ran, so a user with
        `user.center` set (and no `centers`) could write a closing for ANY
        center. And update_or_create silently overwrote an existing closing
        with no record of the previous figures; day-end cash reconciliation is
        exactly the thing that should not be quietly rewritable.
        """
        from django.db import transaction, IntegrityError

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user
        center = serializer.validated_data.get('center')
        if not center:
            return Response({'center': 'A center is required.'}, status=status.HTTP_400_BAD_REQUEST)
        assert_can_write_center(user, center)

        closing_date = serializer.validated_data.get('date')
        if not closing_date:
            return Response({'date': 'A date is required.'}, status=status.HTTP_400_BAD_REQUEST)

        defaults = serializer.validated_data.copy()
        defaults.pop('center', None)
        defaults.pop('date', None)
        # The server works the figures out (X4); only the counted cash and the
        # deposit come from the person at the till.
        from finance import closing as day_closing
        derived = day_closing.settle(center.pk, closing_date, defaults.get('cash_in_hand'),
                                     defaults.get('cash_deposit'))
        derived.pop('expected_cash', None)
        defaults.update(derived)

        with transaction.atomic():
            existing = (DailyClosing.objects.select_for_update()
                        .filter(center=center, date=closing_date).first())

            if existing:
                # An overwrite needs "amend closing" and always keeps the
                # previous figures in the audit log.
                from pos_backend.permissions import has_action_permission
                if not has_action_permission(user, 'finance', 'pettycash', 'amend_closing'):
                    return Response(
                        {'detail': f'A closing for {closing_date} already exists at this center. '
                                   f'Ask someone with the "amend closing" permission to change it.'},
                        status=status.HTTP_409_CONFLICT,
                    )
                self._log_closing_amendment(existing, defaults, user)
                for field, value in defaults.items():
                    setattr(existing, field, value)
                existing.save()
                return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

            try:
                instance = DailyClosing.objects.create(
                    center=center, date=closing_date, user=user, **defaults
                )
            except IntegrityError:
                # A concurrent request won the race for this center/date.
                return Response(
                    {'detail': 'A closing for this date was just recorded by someone else.'},
                    status=status.HTTP_409_CONFLICT,
                )

        if instance.difference != 0:
            from notifications.service import notify
            where = center.display_name or center.center_name
            notify('closing.variance', title=f'Closing out of balance at {where}',
                   body=f'The {closing_date} closing is Rs {instance.difference} '
                        f'{"over" if instance.difference > 0 else "short"}.',
                   url='/admin/finance', center=center, data={'closing_id': instance.pk})
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _log_closing_amendment(existing, new_values, user):
        """Preserve the superseded figures before an owner overwrites them."""
        from audit_logs.models import SystemLog

        changes = []
        for field, value in new_values.items():
            old = getattr(existing, field, None)
            if str(old) != str(value):
                changes.append(f'{field}: {old} -> {value}')
        if not changes:
            return
        summary = f'Amended daily closing for {existing.date}: ' + '; '.join(changes)
        try:
            SystemLog.objects.create(
                module='FINANCE',
                action='UPDATE',
                entity_type='Daily Closing',
                entity_id=str(existing.pk),
                path='/finance/api/daily-closing/',
                user=user if getattr(user, 'is_authenticated', False) else None,
                user_name=getattr(user, 'full_name', '') or '',
                user_email=getattr(user, 'email', '') or '',
                center_id=existing.center_id,
                center_name=(existing.center.display_name or existing.center.center_name
                             if existing.center else ''),
                human_description=summary[:1000],
                description=summary[:2000],
            )
        except Exception:
            logger.exception('[Finance] Could not log daily closing amendment')

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create petty cash entries for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create petty cash entries for this center.")
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center', serializer.instance.center)
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot update petty cash entries for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot update petty cash entries for this center.")
        # Same trail as an overwrite through create: the superseded figures go
        # to the audit log before the row changes (finding R21).
        from finance import closing as day_closing
        instance = serializer.instance
        merged = dict(serializer.validated_data)
        derived = day_closing.settle(
            instance.center_id, merged.get('date', instance.date),
            merged.get('cash_in_hand', instance.cash_in_hand),
            merged.get('cash_deposit', instance.cash_deposit))
        derived.pop('expected_cash', None)
        merged.update(derived)
        self._log_closing_amendment(instance, merged, user)
        # The closing keeps the user who recorded it; the amender is in the log.
        serializer.save(**derived)

    from rest_framework.decorators import action

    @action(detail=False, methods=['get'])
    def compute(self, request):
        """The figures a closing for this branch and day will hold, before anything
        is counted: cash and other methods taken, petty cash, opening balance and
        the cash the drawer should contain."""
        import datetime as _dt
        from finance import closing as day_closing

        center_id = coerce_center_id(request.query_params.get('center_id'))
        if not center_id:
            return Response({'center_id': 'A branch is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not can_use_center(request.user, center_id):
            raise PermissionDenied('You are not assigned to this center.')
        try:
            day = _dt.date.fromisoformat(request.query_params.get('date', ''))
        except (TypeError, ValueError):
            return Response({'detail': 'Invalid date; expected YYYY-MM-DD.'},
                            status=status.HTTP_400_BAD_REQUEST)
        return Response(day_closing.compute_day(center_id, day))

    @action(detail=False, methods=['get'])
    def opening_balance(self, request):
        """Carry the last recorded closing balance forward.

        The old version looked only at `date - 1 day`, so after any closed day
        (weekly off, holiday) the opening balance came back as zero and the
        day's `difference` was wrong by the entire float - telling the operator
        the drawer was short.
        """
        import datetime as _dt

        center_id = coerce_center_id(request.query_params.get('center_id'))
        date_str = request.query_params.get('date', '')
        if not center_id or not date_str:
            return Response({'opening_balance': money(0), 'from_date': None})

        if not can_use_center(request.user, center_id):
            raise PermissionDenied('You are not assigned to this center.')

        try:
            target = _dt.date.fromisoformat(date_str)
        except (TypeError, ValueError):
            return Response({'detail': 'Invalid date; expected YYYY-MM-DD.'},
                            status=status.HTTP_400_BAD_REQUEST)

        previous = (DailyClosing.objects
                    .filter(center_id=center_id, date__lt=target)
                    .order_by('-date').first())

        if not previous:
            return Response({'opening_balance': money(0), 'from_date': None,
                             'note': 'No earlier closing found for this center.'})

        gap_days = (target - previous.date).days
        return Response({
            'opening_balance': money(previous.closing_balance),
            'from_date': str(previous.date),
            # Surfaced so the operator can see the balance was carried across a
            # gap rather than silently assuming it came from yesterday.
            'days_since_last_closing': gap_days,
        })


class PettyCashCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """The petty-cash categories: SAP's "Petty-..." expense accounts.

    Read only. They are not typed in. Every read refreshes them from SAP
    (at most once a minute), so a new, renamed or switched-off account in SAP
    shows in the POS within a minute; "sync_from_sap" refreshes at once.
    """
    queryset = PettyCashCategory.objects.all()
    serializer_class = PettyCashCategorySerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'pettycash'
    permission_strict_read = True
    permission_action_verbs = {'sync_from_sap': 'update'}

    def list(self, request, *args, **kwargs):
        from pos_backend.sap_utils import sap_enabled, sync_petty_cash_categories
        if sap_enabled() and cache.add('petty_categories_synced', 1, 60):
            try:
                sync_petty_cash_categories()
            except Exception:
                # SAP unreachable: show the list the POS already has.
                cache.delete('petty_categories_synced')
                logger.warning('Petty cash categories could not be refreshed from SAP', exc_info=True)
        return super().list(request, *args, **kwargs)

    @action(detail=False, methods=['post'], url_path='sync-from-sap')
    def sync_from_sap(self, request):
        from pos_backend.sap_utils import SapDisabled, sync_petty_cash_categories
        try:
            created, updated, switched_off = sync_petty_cash_categories()
        except SapDisabled:
            return Response({'detail': 'SAP is switched off, so the categories cannot be loaded.'},
                            status=status.HTTP_409_CONFLICT)
        except Exception as exc:
            logger.warning('Petty cash category sync failed', exc_info=True)
            return Response({'detail': f'SAP could not be read: {str(exc)[:160]}'},
                            status=status.HTTP_502_BAD_GATEWAY)
        return Response({'created': created, 'updated': updated, 'switched_off': switched_off,
                         'categories': PettyCashCategorySerializer(
                             PettyCashCategory.objects.filter(is_active=True), many=True).data})


class ShiftViewSet(viewsets.ModelViewSet):
    queryset = Shift.objects.all().select_related('opened_by', 'closed_by').order_by('-opened_at')
    serializer_class = ShiftSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    # Its own submodule (finding R5): shifts were under "Manage" (incentive
    # rules), so the person doing the day-end could not open the register.
    permission_submodule = 'shifts'
    permission_strict_read = True
    # The day-end closing screen shows the open shift.
    permission_read_also = [("finance", "pettycash")]
    # Closing a shift updates the open shift, it does not create one.
    permission_action_verbs = {
        'close_shift': 'update',
        'x_report': 'read',
    }
    def get_queryset(self):
        user = self.request.user
        queryset = self.queryset

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)
            else:
                return queryset.none()

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        status = self.request.query_params.get('status')
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')
        if status:
            queryset = queryset.filter(status=status)
        return queryset.order_by('-opened_at')

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create shifts for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create shifts for this center.")
        center = serializer.validated_data.get('center')
        till = serializer.validated_data.get('till') or 'Till 1'
        if Shift.objects.filter(center=center, till=till, status='Open').exists():
            from rest_framework.exceptions import ValidationError as DRFValidationError
            raise DRFValidationError({'till': f'{till} already has an open shift. Close it first.'})
        serializer.save(opened_by=self.request.user, status='Open', till=till)

    from rest_framework.decorators import action
    @action(detail=True, methods=['post'])
    def close_shift(self, request, pk=None):
        from django.utils import timezone
        shift = self.get_object()
        if shift.status == 'Closed':
            return Response({'error': 'Shift already closed'}, status=400)

        # Expected cash is the server's figure (X4): the float, plus the cash
        # taken since the shift opened, less petty cash paid out in it. Only
        # the counted cash comes from the person closing.
        from finance import closing as day_closing
        try:
            actual_cash = money(request.data.get('actual_cash', 0))
        except Exception:
            return Response({'actual_cash': 'Enter the cash counted.'}, status=400)
        if actual_cash < 0:
            return Response({'actual_cash': 'The cash counted cannot be negative.'}, status=400)
        z_report = day_closing.shift_figures(shift)
        expected_cash = z_report['expected_cash']
        shift.expected_cash = expected_cash
        shift.actual_cash = actual_cash
        shift.variance = money(actual_cash - expected_cash)
        shift.status = 'Closed'
        shift.closed_by = request.user
        shift.closed_at = timezone.now()
        shift.save()
        if shift.variance != 0:
            from notifications.service import notify
            where = shift.center.display_name or shift.center.center_name
            notify('closing.variance', title=f'Shift out of balance at {where}',
                   body=f'{shift.till} closed Rs {shift.variance} '
                        f'{"over" if shift.variance > 0 else "short"} '
                        f'(counted {shift.actual_cash}, expected {shift.expected_cash}).',
                   url='/admin/finance', center=shift.center, data={'shift_id': shift.pk})
        return Response({**ShiftSerializer(shift).data, 'z_report': z_report})

    @action(detail=True, methods=['get'])
    def x_report(self, request, pk=None):
        """The shift so far, without closing it."""
        from finance import closing as day_closing
        return Response(day_closing.shift_figures(self.get_object()))


class IncentiveRuleViewSet(viewsets.ModelViewSet):
    queryset = IncentiveRule.objects.all().select_related('center').order_by('-created_at')
    serializer_class = IncentiveRuleSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    # Rules are governed by Finance > Manage (incentive rules). They sat under
    # `incentives`, so the Manage tick showed the tab but every save was refused.
    # Incentives holders still read the rules their payouts come from.
    permission_submodule = 'manage'
    permission_strict_read = True
    permission_read_also = [('finance', 'incentives')]
    # Duplicating a rule really does create one.
    permission_action_verbs = {
        'duplicate': 'create',
    }
    def get_queryset(self):
        user = self.request.user
        queryset = self.queryset

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(Q(center__in=user.centers.all()) | Q(center__isnull=True))
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(Q(center=user.center) | Q(center__isnull=True))
            else:
                queryset = queryset.filter(center__isnull=True)

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(Q(center_id=center_id) | Q(center__isnull=True))
        elif selected_center_ids():
            queryset = queryset.filter(Q(center_id__in=list(selected_center_ids())) | Q(center__isnull=True))

        frequency = self.request.query_params.get('frequency')
        if frequency:
            queryset = queryset.filter(frequency=frequency)

        category = self.request.query_params.get('category')
        if category:
            queryset = queryset.filter(category=category)

        is_active = self.request.query_params.get('is_active')
        if is_active is not None:
            if is_active.lower() in ('true', '1'):
                queryset = queryset.filter(is_active=True)
            elif is_active.lower() in ('false', '0'):
                queryset = queryset.filter(is_active=False)

        return queryset.order_by('-id')

    def _may_write_centre(self, center):
        """May this user create, change or remove a rule for `center` (None =
        organisation-wide)? Only an all-centres role touches an organisation-wide
        rule, and nobody touches another centre's: such a rule pays every centre,
        and the list used to let any branch manager edit it."""
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        if IsOwner.check_is_owner(user) or perms.get('all_centers', False):
            return True
        if center is None:
            return False
        mine = set(user.centers.values_list('id', flat=True))
        if getattr(user, 'center_id', None):
            mine.add(user.center_id)
        return center.pk in mine

    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        if not self._may_write_centre(serializer.validated_data.get('center')):
            raise PermissionDenied("You cannot create incentive rules for this center "
                                   "(an organisation-wide rule needs an all-centres role).")
        serializer.save()

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        new_center = serializer.validated_data.get('center', serializer.instance.center)
        if not (self._may_write_centre(serializer.instance.center) and self._may_write_centre(new_center)):
            raise PermissionDenied("You cannot change this incentive rule.")
        serializer.save()

    def perform_destroy(self, instance):
        from rest_framework.exceptions import PermissionDenied
        if not self._may_write_centre(instance.center):
            raise PermissionDenied("You cannot delete this incentive rule.")
        instance.delete()

    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """Copy a rule, optionally into another centre (`target_center_id`; the key
        present but empty means organisation-wide). The screen always sent the
        target and it was ignored, so every copy stayed in the original's centre."""
        from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
        from salon_admin.models import Center

        rule = self.get_object()
        target = rule.center
        if 'target_center_id' in request.data:
            raw = request.data.get('target_center_id')
            if raw in (None, '', 'null'):
                target = None
            else:
                target = Center.objects.filter(pk=coerce_center_id(raw)).first()
                if target is None:
                    raise DRFValidationError({'target_center_id': 'That centre does not exist.'})
        if not self._may_write_centre(target):
            raise PermissionDenied("You cannot create incentive rules for that center.")
        new_rule = IncentiveRule.objects.create(
            name=f"{rule.name} (Copy)",
            center=target,
            frequency=rule.frequency,
            category=rule.category,
            rule_type=rule.rule_type,
            applicable_role=rule.applicable_role,
            tiers=rule.tiers,
            flat_percent=rule.flat_percent,
            flat_amount=rule.flat_amount,
            effective_from=dt_module.date.today(),
            effective_to=None,
            is_active=True,
            description=rule.description,
        )
        serializer = self.get_serializer(new_rule)
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class IncentiveConfigViewSet(viewsets.ModelViewSet):
    queryset = IncentiveConfig.objects.all().prefetch_related('tiers')
    serializer_class = IncentiveConfigSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'manage'
    permission_strict_read = True
    permission_read_also = [('finance', 'incentives')]
    def get_queryset(self):
        user = self.request.user
        queryset = IncentiveConfig.objects.all()

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)
            else:
                queryset = queryset.filter(center__isnull=True)

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')
        return queryset.order_by('id')

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create incentive configs for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create incentive configs for this center.")
        serializer.save()

    def perform_update(self, serializer):
        """An edit may not move a config into a centre the user does not hold."""
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        if not IsOwner.check_is_owner(user) and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center', serializer.instance.center)
            mine = set(user.centers.values_list('id', flat=True))
            if getattr(user, 'center_id', None):
                mine.add(user.center_id)
            if center is None or center.pk not in mine:
                from rest_framework.exceptions import PermissionDenied
                raise PermissionDenied("You cannot change incentive configs for this center.")
        serializer.save()


def compute_register_summary(user, center_id, start_date, end_date,
                             all_time=False) -> dict:
    """Register summary for a period.

    `all_time` is threaded through because the stand-in request below carries no
    query parameters, so `_get_filtered_invoices` cannot see the caller's
    `?all_time=true` on its own. Without it the caller's explicit request for
    the full history would be silently narrowed to the current month.
    """
    class MockReq:
        # `_get_filtered_invoices` reads query_params to spot an explicit
        # all-time request. Give it something real to read.
        query_params = {}

    req = MockReq()
    req.user = user
    req.query_params = {'all_time': 'true'} if all_time else {}
    request = req
    # start_date and end_date are already passed as arguments, do not read from query_params


    invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

    revenue = _compute_revenue_breakdown(invoices)
    payments = _compute_payment_breakdown(invoices)

    # Advances received (positive advance payments in date range)
    from pos_backend.scoping import advances_in_centers
    from billing.wallets import advance_rows_that_count
    adv_qs = advance_rows_that_count(AdvancePayment.objects.filter(amount__gt=0))
    adv_used_qs = advance_rows_that_count(AdvancePayment.objects.filter(amount__lt=0))

    user = request.user
    allowed_centers = user_center_ids(user)
    if center_id:
        adv_qs = advances_in_centers(adv_qs, [center_id])
        adv_used_qs = advances_in_centers(adv_used_qs, [center_id])
    elif allowed_centers is not None:
        # Fail closed: a user assigned to no center sees nothing.
        adv_qs = advances_in_centers(adv_qs, allowed_centers)
        adv_used_qs = advances_in_centers(adv_used_qs, allowed_centers)
    if start_date:
        adv_qs = adv_qs.filter(created_at__date__gte=start_date)
        adv_used_qs = adv_used_qs.filter(created_at__date__gte=start_date)
    if end_date:
        adv_qs = adv_qs.filter(created_at__date__lte=end_date)
        adv_used_qs = adv_used_qs.filter(created_at__date__lte=end_date)

    advances_total = Decimal(str(adv_qs.aggregate(t=Sum('amount'))['t'] or 0))
    advance_redemptions = abs(Decimal(str(adv_used_qs.aggregate(t=Sum('amount'))['t'] or 0)))

    # Refunds, keyed off the date the refund was ISSUED, not the date the
    # original bill was raised. The old query filtered cancelled invoices by
    # created_at, so a January bill refunded in March showed up in January.
    from billing.models import InvoiceRefund
    refunds_qs = InvoiceRefund.objects.all()
    if center_id:
        refunds_qs = refunds_qs.filter(invoice__center_id=center_id)
    else:
        allowed = user_center_ids(user)
        if allowed is not None:
            refunds_qs = refunds_qs.filter(invoice__center_id__in=allowed) if allowed else refunds_qs.none()
    if start_date:
        refunds_qs = refunds_qs.filter(refunded_on__gte=start_date)
    if end_date:
        refunds_qs = refunds_qs.filter(refunded_on__lte=end_date)
    refunds_total = Decimal(str(refunds_qs.aggregate(t=Sum('amount'))['t'] or 0))

    totals = invoices.aggregate(t_discount=Sum('discount'), t_rounding=Sum('rounding'))
    total_discount = Decimal(str(totals['t_discount'] or 0))
    # Each bill is rounded to the whole rupee; the paise added or taken off are
    # real money in the bills' totals, so they have a line of their own. Without
    # it "Including Tax" fell short of the sum of the bills by that round-off.
    total_rounding = Decimal(str(totals['t_rounding'] or 0))

    # Tax comes from the line items themselves, summed per category. The old
    # code took the invoice-level cgst+sgst and back-apportioned it between
    # services and products using a ratio - which collapsed to 100% products
    # once C1 zeroed all service revenue, and fell back to a hardcoded 70/30
    # split when both were zero.
    tax_by_category = _compute_tax_breakdown(invoices)
    total_tax = Decimal(str(tax_by_category['total']))
    services_tax = Decimal(str(tax_by_category['services']))
    products_tax = Decimal(str(tax_by_category['products']))

    total_taxable = (
        revenue['services'] + revenue['products'] + revenue['value_cards']
        + revenue['memberships'] + revenue['packages'] + revenue['other']
    ) - total_discount

    # Liability drawdowns are not new money and must be deducted from the
    # collection figure. cashback_wallet used to always read zero here because
    # the classifier put cashback into the cash bucket.
    value_card_redemptions = Decimal(str(payments['value_card']['amount']))
    cashback_redemptions = Decimal(str(payments['cashback_wallet']['amount']))

    # Item totals are tax-exclusive now, so tax is added exactly once. The old
    # figure added total_tax on top of item totals that already included it.
    collection_before_tax = (
        total_taxable + advances_total
        - advance_redemptions - value_card_redemptions - cashback_redemptions
    )
    including_tax = collection_before_tax + total_tax + total_rounding

    tips_total = _compute_tip_total(invoices)

    # Revenue vs cash collected, reported separately.
    #
    # Every figure used to be Sum('total_amount') over paid AND partial
    # invoices, which books the whole bill as though it were collected. Billing
    # a partial invoice IS revenue under accrual accounting, but the unpaid
    # balance is a receivable, not cash - and nothing surfaced the gap, so the
    # two numbers could not be reconciled against the drawer.
    billed_agg = invoices.aggregate(billed=Sum('total_amount'), collected=Sum('paid_amount'))
    total_billed = Decimal(str(billed_agg['billed'] or 0))
    total_collected = Decimal(str(billed_agg['collected'] or 0))
    outstanding = total_billed - total_collected
    if outstanding < 0:
        outstanding = Decimal('0.00')

    # Real money actually received: every bucket that is not a liability
    # drawdown, plus tips. total_received used to sum every bucket including
    # advance / value card / cashback, which are balances being spent.
    from billing import payment_methods as _pmods
    real_money_received = sum(
        (Decimal(str(v['amount'])) for k, v in payments.items()
         if k not in _pmods.LIABILITY_BUCKETS),
        Decimal('0.00'),
    ) + tips_total

    target = 0
    target_achieved_percentage = 0
    target_center_ids = [center_id] if center_id else list(selected_center_ids() or [])
    if target_center_ids:
        from salon_admin.models import Center
        import calendar
        from datetime import datetime

        today = datetime.now()
        target = Decimal('0')
        any_current = False
        for center_obj in Center.objects.filter(id__in=target_center_ids):
            history = center_obj.monthly_targets_history or {}
            this_target = Decimal('0')
            is_past_range = False
            if start_date and end_date:
                try:
                    sd = datetime.strptime(start_date, '%Y-%m-%d')
                    ed = datetime.strptime(end_date, '%Y-%m-%d')
                    curr_y, curr_m = sd.year, sd.month
                    end_y, end_m = ed.year, ed.month
                    is_past_range = True
                    while (curr_y < end_y) or (curr_y == end_y and curr_m <= end_m):
                        if (curr_y > today.year) or (curr_y == today.year and curr_m >= today.month):
                            is_past_range = False
                        this_target += Decimal(str(history.get(f"{calendar.month_abbr[curr_m]}-{curr_y}", 0) or 0))
                        curr_m += 1
                        if curr_m > 12:
                            curr_m = 1
                            curr_y += 1
                except Exception:
                    import logging; logging.getLogger(__name__).error('Handled exception', exc_info=True)

            # Fall back to the centre's default monthly target when no history
            # applies, but only if the range includes the current or a future month.
            if this_target == 0 and not is_past_range:
                this_target = Decimal(str(center_obj.monthly_target or 0))
            target += this_target

        if target > 0:
            target_achieved_percentage = round((total_taxable / target) * 100, 2)

    response_data = {
        'revenues': {
            'services': {'amount': money(revenue['services']), 'tax': money(services_tax)},
            'service_redemptions': {'amount': money(0), 'tax': money(0)},
            'products': {'amount': money(revenue['products']), 'tax': money(products_tax)},
            'value_cards': {'amount': money(revenue['value_cards']),
                            'tax': money(tax_by_category['value_cards'])},
            'gift_cards': {'amount': money(0), 'tax': money(0)},
            'memberships': {'amount': money(revenue['memberships']),
                            'tax': money(tax_by_category['memberships'])},
            'packages': {'amount': money(revenue['packages']),
                         'tax': money(tax_by_category['packages'])},
            'advances': {'amount': money(advances_total), 'tax': money(0)},
            'change_to_advance': {'amount': money(0), 'tax': money(0)},
            'payment_redemptions': {
                'amount': money(-(advance_redemptions + value_card_redemptions + cashback_redemptions)),
                'tax': money(0),
            },
            # Refunds now carry a real figure, dated by when they were issued.
            'refunds': {'amount': money(-refunds_total), 'tax': money(0)},
            'tips': {'amount': money(tips_total), 'tax': money(0)},
            'other': {'amount': money(revenue.get('other', 0)),
                      'tax': money(tax_by_category['other'])},
            'discounts': {'amount': money(-total_discount), 'tax': money(0)},
            'rounding': {'amount': money(total_rounding), 'tax': money(0)},
            'taxable_value': money(total_taxable),
            'target': money(target),
            'target_achieved_percentage': target_achieved_percentage,
            'collection_before_tax': money(collection_before_tax),
            'total_tax': money(total_tax),
            'including_tax': money(including_tax),
        },
        'payment_methods': {
            **{k: {'amount': money(v['amount']), 'count': v['count']}
               for k, v in payments.items()},
            'tips': {'amount': money(tips_total), 'count': 0},
            # Only non-liability buckets plus tips: what actually came in.
            'total_received': money(real_money_received),
            # The old key, kept so existing dashboards keep working, but now
            # explicitly labelled as gross including liability drawdowns.
            'total_including_redemptions': money(
                sum((Decimal(str(v['amount'])) for v in payments.values()), Decimal('0.00'))
                + tips_total
            ),
        },
        'service_redemptions': {
            'value_cards': money(revenue['value_cards']),
            'service_balance': money(0),
            'total': money(revenue['value_cards']),
        },
        'payment_redemptions': {
            'gift_cards': money(0),
            'advance': money(advance_redemptions),
            'value_card': money(value_card_redemptions),
            'cashback': money(cashback_redemptions),
            'total': money(advance_redemptions + value_card_redemptions + cashback_redemptions),
        },
        'refunds': {
            'refunds_issued': money(refunds_total),
        },
        # Accrual vs cash, side by side, so the gap between what was billed and
        # what actually came in is visible instead of buried in one figure.
        'reconciliation': {
            'total_billed': money(total_billed),
            'total_collected': money(total_collected),
            'outstanding_receivable': money(outstanding),
            'tips_collected': money(tips_total),
            'real_money_received': money(real_money_received),
            'liability_redemptions': money(
                advance_redemptions + value_card_redemptions + cashback_redemptions
            ),
            'refunds_issued': money(refunds_total),
            # What the drawer and the digital settlements should add up to.
            # Refunds are NOT taken off again here: a cancelled bill is already
            # left out of `invoices`, so its payment is not in real_money_received.
            'expected_settlement': money(real_money_received),
        },
    }

    return response_data

class RegisterSummaryView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "register_summary"
    permission_strict_read = True
    # The day-end closing screen, the multi-salon tab and the incentive headline
    # tiles read this summary.
    permission_read_also = [("finance", "pettycash"), ("finance", "multi_salon"),
                            ("finance", "incentives"), ("finance", "shifts")]

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))

        cache_key = f"reg_summary_{request.user.id}_{center_id}_{selection_key()}_{start_date}_{end_date}_{_wants_all_time(request)}"
        response_data = cache.get(cache_key)
        
        if not response_data:
            response_data = compute_register_summary(
                request.user, center_id, start_date, end_date,
                all_time=_wants_all_time(request))
            cache.set(cache_key, response_data, 60)

        if request.query_params.get('export') == 'true':
            import openpyxl
            from django.http import HttpResponse
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(title="Register Summary")

            ws.append(["Metric", "Amount", "Tax"])
            for k, v in response_data['revenues'].items():
                if isinstance(v, dict):
                    ws.append([k.replace('_', ' ').title(), v.get('amount', 0), v.get('tax', 0)])
                else:
                    ws.append([k.replace('_', ' ').title(), v, ""])

            ws.append([])
            ws.append(["Payment Method", "Amount"])
            for k, v in response_data['payment_methods'].items():
                if isinstance(v, dict):
                    ws.append([k.replace('_', ' ').title(), v.get('amount', 0)])
                else:
                    ws.append([k.replace('_', ' ').title(), v])

            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=register_summary.xlsx'
            wb.save(response)
            return response

        return Response(response_data)



class MonthlySalesView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "monthly_sales"
    permission_strict_read = True

    def get(self, request):
        from django.db.models.functions import ExtractYear, ExtractMonth
        from django.db.models import Sum, Count, Q

        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))

        cache_key = f"monthly_sales_{request.user.id}_{center_id}_{selection_key()}_{start_date}_{end_date}_{_wants_all_time(request)}"
        cached_response = cache.get(cache_key)
        if cached_response is not None:
            return Response(cached_response)

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        # A month with no SALES can still have refunds, and a refund is that
        # month's activity - especially then. Returning early on "no revenue
        # invoices" meant a month whose only event was a refund reported nothing
        # at all, while the Refunds tab showed the figure.
        from billing.models import InvoiceRefund

        any_refunds = InvoiceRefund.objects.all()
        if center_id:
            any_refunds = any_refunds.filter(invoice__center_id=center_id)
        else:
            allowed_centers = user_center_ids(request.user)
            if allowed_centers is not None:
                any_refunds = (any_refunds.filter(invoice__center_id__in=allowed_centers)
                               if allowed_centers else any_refunds.none())
        if start_date:
            any_refunds = any_refunds.filter(refunded_on__gte=start_date)
        if end_date:
            any_refunds = any_refunds.filter(refunded_on__lte=end_date)

        if not invoices.exists() and not any_refunds.exists():
            return Response([])

        from services.models import ServiceMaster
        from inventory.models import Product
        from marketing.models import Membership, Package, ValueCard

        try:
            ct_map = ContentType.objects.get_for_models(ServiceMaster, Product, Membership, Package, ValueCard)
            service_ct = ct_map[ServiceMaster]
            product_ct = ct_map[Product]
            membership_ct = ct_map[Membership]
            package_ct = ct_map[Package]
            valuecard_ct = ct_map[ValueCard]
        except Exception:
            return Response([])

        # Fast monthly aggregations
        monthly = (
            invoices
            .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
            .values('year', 'month_num')
            .annotate(
                total_amount=Sum('total_amount'),
                total_cgst=Sum('cgst'),
                total_sgst=Sum('sgst'),
                total_discount=Sum('discount'),
                total_rounding=Sum('rounding'),
                invoice_count=Count('id'),
            )
            .order_by('-year', '-month_num')
        )

        item_monthly = (
            InvoiceItem.objects.filter(invoice__in=invoices)
            .annotate(year=ExtractYear('invoice__created_at'), month_num=ExtractMonth('invoice__created_at'))
            .values('year', 'month_num')
            .annotate(
                services=Sum('total_price', filter=Q(content_type=service_ct)),
                products=Sum('total_price', filter=Q(content_type=product_ct)),
                memberships=Sum('total_price', filter=Q(content_type=membership_ct)),
                packages=Sum('total_price', filter=Q(content_type=package_ct)),
                value_cards=Sum('total_price', filter=Q(content_type=valuecard_ct)),
                other=Sum('total_price', filter=Q(content_type__isnull=True))
            )
            .order_by('-year', '-month_num')
        )
        items_dict = {(row['year'], row['month_num']): row for row in item_monthly}

        # Advances
        from pos_backend.scoping import advances_in_centers
        from billing.wallets import advance_rows_that_count
        adv_qs = advance_rows_that_count(AdvancePayment.objects.filter(amount__gt=0))
        adv_used_qs = advance_rows_that_count(AdvancePayment.objects.filter(amount__lt=0))

        allowed_adv = user_center_ids(request.user)
        if center_id:
            adv_qs = advances_in_centers(adv_qs, [center_id])
            adv_used_qs = advances_in_centers(adv_used_qs, [center_id])
        elif allowed_adv is not None:
            # Fail closed: a user assigned to no center sees nothing.
            adv_qs = advances_in_centers(adv_qs, allowed_adv)
            adv_used_qs = advances_in_centers(adv_used_qs, allowed_adv)
        from django.db.models.functions import Coalesce
        adv_qs = adv_qs.annotate(eff_date=Coalesce('invoice__created_at', 'created_at'))
        adv_used_qs = adv_used_qs.annotate(eff_date=Coalesce('invoice__created_at', 'created_at'))

        if start_date:
            adv_qs = adv_qs.filter(eff_date__date__gte=start_date)
            adv_used_qs = adv_used_qs.filter(eff_date__date__gte=start_date)
        if end_date:
            adv_qs = adv_qs.filter(eff_date__date__lte=end_date)
            adv_used_qs = adv_used_qs.filter(eff_date__date__lte=end_date)

        adv_monthly = (
            adv_qs
            .annotate(year=ExtractYear('eff_date'), month_num=ExtractMonth('eff_date'))
            .values('year', 'month_num')
            .annotate(advances=Sum('amount'))
            .order_by('-year', '-month_num')
        )
        adv_dict = {(row['year'], row['month_num']): row for row in adv_monthly}

        adv_used_monthly = (
            adv_used_qs
            .annotate(year=ExtractYear('eff_date'), month_num=ExtractMonth('eff_date'))
            .values('year', 'month_num')
            .annotate(advances_used=Sum('amount'))
            .order_by('-year', '-month_num')
        )
        adv_used_dict = {(row['year'], row['month_num']): row for row in adv_used_monthly}

        # Value Card and Cashback Redemptions - use invoice__in for subquery lookup
        from billing.models import Payment
        liability_pmts = Payment.objects.filter(
            invoice__in=invoices
        ).filter(
            Q(payment_method__icontains='value card') | Q(payment_method__icontains='cashback')
        )
        liab_monthly = (
            liability_pmts
            .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
            .values('year', 'month_num')
            .annotate(liab_used=Sum('amount'))
            .order_by('-year', '-month_num')
        )
        liab_used_dict = {(row['year'], row['month_num']): row for row in liab_monthly}

        # ── Refunds ──────────────────────────────────────────────────────
        #
        # One definition, shared with the Refunds tab: the InvoiceRefund rows,
        # bucketed by the date the money went back (`refunded_on`), summing what
        # was actually returned.
        #
        # This used to aggregate CANCELLED INVOICES by `created_at` and sum
        # `total_amount`, which disagreed with the Refunds tab on three separate
        # counts for the same period:
        #   * wrong month - a January bill refunded in March landed in January;
        #   * wrong amount - the bill total rather than the sum refunded, so a
        #     partially paid bill overstated the refund by whatever was never
        #     collected;
        #   * and this figure was then thrown away and a literal 0 emitted
        #     below, so the Monthly Sales "Refunds" column always read zero
        #     while the Refunds tab showed a number.
        refunds_qs = any_refunds

        refunds_monthly = (
            refunds_qs
            .annotate(year=ExtractYear('refunded_on'),
                      month_num=ExtractMonth('refunded_on'))
            .values('year', 'month_num')
            .annotate(refunds=Sum('amount'))
            .order_by('-year', '-month_num')
        )
        refunds_dict = {
            (r['year'], r['month_num']): Decimal(str(r['refunds'] or 0))
            for r in refunds_monthly
        }

        # Fetch target history
        from salon_admin.models import Center
        centers_qs = Center.objects.all()
        user = request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                centers_qs = centers_qs.filter(id__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                centers_qs = centers_qs.filter(id=user.center.id)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                centers_qs = centers_qs.none()
        if center_id:
            centers_qs = centers_qs.filter(id=center_id)
        elif selected_center_ids():
            centers_qs = centers_qs.filter(id__in=list(selected_center_ids()))

        centers = list(centers_qs)

        # ── Which months to emit ─────────────────────────────────────────
        #
        # The month list came only from REVENUE invoices, so a month whose only
        # event was a refund produced no row at all and its refund silently
        # vanished from the report. A refund is a month's activity even when
        # nothing was sold - especially then.
        monthly_rows = list(monthly)
        seen_months = {(r['year'], r['month_num']) for r in monthly_rows}
        for (year, month_num) in sorted(refunds_dict, reverse=True):
            if (year, month_num) not in seen_months:
                monthly_rows.append({
                    'year': year, 'month_num': month_num,
                    'total_amount': 0, 'total_cgst': 0, 'total_sgst': 0,
                    'total_discount': 0, 'total_rounding': 0, 'invoice_count': 0,
                })
        monthly_rows.sort(key=lambda r: (r['year'], r['month_num']), reverse=True)

        # Build result
        result = []
        for row in monthly_rows:
            year = row['year']
            month_num = row['month_num']
            month_key = (year, month_num)
            month_str = f"{calendar.month_abbr[month_num]}-{year}"

            i_row = items_dict.get(month_key, {})
            services = Decimal(str(i_row.get('services') or 0))
            products = Decimal(str(i_row.get('products') or 0))
            memberships = Decimal(str(i_row.get('memberships') or 0))
            packages = Decimal(str(i_row.get('packages') or 0))
            value_cards = Decimal(str(i_row.get('value_cards') or 0))
            other = Decimal(str(i_row.get('other') or 0))

            adv = Decimal(str(adv_dict.get(month_key, {}).get('advances') or 0))
            adv_used = abs(Decimal(str(adv_used_dict.get(month_key, {}).get('advances_used') or 0)))
            liab_used = Decimal(str(liab_used_dict.get(month_key, {}).get('liab_used') or 0))

            total_redemptions = adv_used + liab_used
            # refunds_dict was built and then never read, which is why the
            # Refunds column always showed 0.
            refunds = refunds_dict.get(month_key, Decimal('0'))

            total_tax = Decimal(str(row['total_cgst'] or 0)) + Decimal(str(row['total_sgst'] or 0))
            total_discount = Decimal(str(row['total_discount'] or 0))
            total_rounding = Decimal(str(row['total_rounding'] or 0))
            total = (services + products + memberships + packages + value_cards + other + adv - total_redemptions) - total_discount

            # After the bill-level discount, as the Register Summary has it.
            taxable_value = services + products + value_cards + memberships + packages - total_discount

            target = Decimal('0.0')
            import datetime
            current_date = datetime.date.today()
            is_past_month = (year < current_date.year) or (year == current_date.year and month_num < current_date.month)

            for c in centers:
                hist = c.monthly_targets_history or {}
                raw_t = hist.get(month_str, 0)
                try:
                    t = Decimal(str(raw_t)) if raw_t not in [None, ""] else Decimal('0.0')
                except (ValueError, TypeError):
                    t = Decimal('0.0')

                # Only fallback to the current active monthly_target if we are in the current or future month.
                # Past months should remain 0 if they have no explicit history saved, preventing the active target from rewriting the past.
                if t == Decimal('0.0') and not is_past_month:
                    try:
                        t = Decimal(str(c.monthly_target)) if c.monthly_target not in [None, ""] else Decimal('0.0')
                    except (ValueError, TypeError):
                        t = Decimal('0.0')
                target += t

            target_achieved_percentage = 0
            if target > 0:
                target_achieved_percentage = round((taxable_value / target) * 100, 2)

            result.append({
                'month': month_str,
                'services': round(services, 2),
                'service_redemptions': 0,
                'products': round(products, 2),
                'value_cards': round(value_cards, 2),
                'gift_cards': 0,
                'memberships': round(memberships, 2),
                'packages': round(packages, 2),
                'advances': round(adv, 2),
                'change_to_advance': 0,
                'payment_redemptions': round(-total_redemptions, 2),
                # This was a literal 0, so the column never showed anything -
                # while the Refunds tab, reading the same events, showed a
                # figure. Negative because a refund reduces the month's sales.
                'refunds': round(-refunds, 2),
                'other': round(other, 2),
                'discounts': round(-total_discount, 2),
                'rounding': round(total_rounding, 2),
                'collection_before_tax': round(total, 2),
                'taxable_value': round(taxable_value, 2),
                'target': round(target, 2),
                'target_achieved_percentage': target_achieved_percentage,
                'total_tax': round(total_tax, 2),
                'including_tax': round(total + total_tax + total_rounding, 2)
            })

        cache.set(cache_key, result, 60)

        if request.query_params.get('export') == 'true':
            import openpyxl
            from django.http import HttpResponse
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(title="Monthly Sales")
            ws.append(["Month", "Total Sales", "Target", "Achieved %"])
            for row in result:
                ws.append([
                    row.get('month', ''),
                    row.get('including_tax', 0),
                    row.get('target', 0),
                    row.get('target_achieved_percentage', 0)
                ])
            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=monthly_sales.xlsx'
            wb.save(response)
            return response

        return Response(result)



class DetailedRevenuesView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "detailed_revenues"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        # FIXED: pagination support replaces hard-coded 200-record limit
        # `int(...)` on a raw query param made ?page=abc a 500.
        page = max(_safe_int(request.query_params.get('page'), 1), 1)
        page_size = min(max(_safe_int(request.query_params.get('page_size'), 100), 1), 500)
        offset = (page - 1) * page_size

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)
        invoices = invoices.select_related('client', 'center', 'staff', 'created_by')
        invoices = invoices.order_by('-created_at')

        total_count = invoices.count()
        invoices_page = invoices[offset: offset + page_size]

        from services.models import ServiceMaster
        from inventory.models import Product
        from marketing.models import Membership, Package, ValueCard

        try:
            ct_map = ContentType.objects.get_for_models(ServiceMaster, Product, Membership, Package, ValueCard)
            service_ct = ct_map[ServiceMaster]
            product_ct = ct_map[Product]
            membership_ct = ct_map[Membership]
            package_ct = ct_map[Package]
            valuecard_ct = ct_map[ValueCard]
        except Exception:
            service_ct = product_ct = membership_ct = package_ct = valuecard_ct = None

        invoices_page = list(invoices_page)  # realize the paginated slice for iteration

        # Use the sliced IDs (safe — max 500 per page) for efficient batch lookups
        invoice_ids = [inv.id for inv in invoices_page]

        from billing.models import Payment, AdvancePayment
        from django.db.models import Sum, Count, Q

        if invoice_ids:
            vc_used_invs = set(Payment.objects.filter(invoice_id__in=invoice_ids, value_card_id__isnull=False).values_list('invoice_id', flat=True))
            adv_used_invs = set(AdvancePayment.objects.filter(invoice_id__in=invoice_ids, amount__lt=0).values_list('invoice_id', flat=True))
            pkg_used_invs = set(InvoiceItem.objects.filter(invoice_id__in=invoice_ids, total_price=0, content_type__app_label='services').values_list('invoice_id', flat=True))
            item_counts = (
                InvoiceItem.objects.filter(invoice_id__in=invoice_ids)
                .values('invoice_id')
                .annotate(
                    service_count=Sum('quantity', filter=Q(content_type=service_ct, total_price__gt=0) if service_ct else Q(pk__isnull=True)),
                    product_count=Sum('quantity', filter=Q(content_type=product_ct, total_price__gt=0) if product_ct else Q(pk__isnull=True)),
                    membership_count=Sum('quantity', filter=Q(content_type=membership_ct) if membership_ct else Q(pk__isnull=True)),
                    package_count=Sum('quantity', filter=Q(content_type=package_ct) if package_ct else Q(pk__isnull=True)),
                    valuecard_count=Sum('quantity', filter=Q(content_type=valuecard_ct) if valuecard_ct else Q(pk__isnull=True)),
                )
            )
        else:
            vc_used_invs = set()
            adv_used_invs = set()
            pkg_used_invs = set()
            item_counts = []

        counts_by_invoice = {row['invoice_id']: row for row in item_counts}

        if request.query_params.get('export') == 'true':
            # For export: rebuild full invoice list using the full (non-paginated) queryset
            # We must iterate page-by-page to avoid loading all IDs at once
            invoices_page = list(invoices.select_related('client', 'center', 'staff', 'created_by'))
        result = []
        for inv in invoices_page:
            counts = counts_by_invoice.get(inv.id, {})
            billed_by = _billed_by(inv)

            client_name = ''
            client_gst = ''
            if inv.client:
                client_name = f"{inv.client.first_name} {inv.client.last_name or ''}".strip()
                client_gst = inv.client.gst_number or ''

            net = Decimal(str(inv.total_amount)) - Decimal(str(inv.cgst)) - Decimal(str(inv.sgst))

            redemptions = []
            if inv.id in vc_used_invs:
                redemptions.append('Value Card')
            if inv.id in adv_used_invs:
                redemptions.append('Advance')
            if inv.id in pkg_used_invs:
                redemptions.append('Package Redemption')

            # Primary source of truth: notes field stores "Promo Applied: <name>" from billing frontend
            if inv.notes and 'Promo Applied:' in str(inv.notes):
                promo_name = str(inv.notes).replace('Promo Applied:', '').strip()
                # Clean up emoji from name for display
                promo_clean = promo_name.strip()
                redemptions.append(promo_clean)

            applied_promo = ', '.join(redemptions) if redemptions else '—'

            result.append({
                'id': inv.id,
                'bill_no': inv.display_number,
                'date_time': inv.created_at.strftime('%d-%b-%Y, %H:%M'),
                'billed_by': billed_by,
                'client': client_name,
                'gst_no': client_gst,
                'discount': Decimal(str(inv.discount)),
                'net': round(net, 2),
                'cgst': Decimal(str(inv.cgst)),
                'sgst': Decimal(str(inv.sgst)),
                'total_gst': Decimal(str(inv.cgst)) + Decimal(str(inv.sgst)),
                'grand_total': Decimal(str(inv.total_amount)),
                'services': int(counts.get('service_count') or 0),
                'products': int(counts.get('product_count') or 0),
                'memberships': int(counts.get('membership_count') or 0),
                'packages': int(counts.get('package_count') or 0),
                'value_cards': int(counts.get('valuecard_count') or 0),
                'applied_promo': applied_promo,
                'status': inv.status,
            })


        if request.query_params.get('export') == 'true':
            import openpyxl
            from django.http import HttpResponse
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(title="Detailed Revenues")
            ws.append(["Bill No", "Date", "Client", "Billed By", "Net", "Tax", "Grand Total", "Status", "Payment Methods", "Applied Promos"])
            for row in result:
                ws.append([
                    row.get('bill_no', ''),
                    row.get('date_time', ''),
                    row.get('client', ''),
                    row.get('billed_by', ''),
                    row.get('net', 0),
                    row.get('total_gst', 0),
                    row.get('grand_total', 0),
                    row.get('status', ''),
                    '—',  # Payment methods omitted from this query for performance
                    row.get('applied_promo', '')
                ])
            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=detailed_revenues.xlsx'
            wb.save(response)
            return response

        return Response({
            'results': result,
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': -(-total_count // page_size),  # ceiling division
        })

import openpyxl
from django.http import HttpResponse

class ExportFinanceView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "detailed_revenues"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date).select_related('client', 'center')

        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet(title="Invoices")

        headers = ["Invoice ID", "Date", "Client", "Center", "Subtotal", "Discount", "Tax", "Total", "Status"]
        ws.append(headers)

        for inv in invoices.iterator(chunk_size=1000):
            client_name = inv.client.full_name if inv.client else 'Walk-in'
            center_name = (inv.center.display_name or inv.center.center_name) if inv.center else "N/A"
            ws.append([
                inv.id,
                inv.created_at.strftime('%Y-%m-%d %H:%M'),
                client_name,
                center_name,
                Decimal(str(inv.subtotal)),
                Decimal(str(inv.discount)),
                Decimal(str(inv.cgst + inv.sgst)),
                Decimal(str(inv.total_amount)),
                inv.status
            ])

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename=finance_export.xlsx'
        wb.save(response)
        return response


class RefundsView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "refunds"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        # FIXED: pagination replaces hard-coded 100-record limit
        # `int(...)` on a raw query param made ?page=abc a 500.
        page = max(_safe_int(request.query_params.get('page'), 1), 1)
        page_size = min(max(_safe_int(request.query_params.get('page_size'), 100), 1), 500)
        offset = (page - 1) * page_size

        # ── One definition of a refund, shared with every other view ─────
        #
        # This listed CANCELLED INVOICES by `created_at` and summed
        # `total_amount`. Three things were wrong with that, and each made this
        # tab disagree with Register Summary and Monthly Sales for the same
        # period:
        #
        #   * the bill TOTAL is not what was refunded. A bill of Rs 1,050 with
        #     only Rs 400 ever collected can only return Rs 400; this claimed
        #     Rs 1,050;
        #   * `created_at` is the date the BILL was raised, so a January bill
        #     refunded in March appeared in January - and a closed month's
        #     figures changed after it was closed;
        #   * a cancelled bill that was never paid produced a refund row with
        #     nothing behind it.
        #
        # InvoiceRefund is what the refund actually wrote: the amount returned,
        # on the day it was returned.
        from django.db.models import Sum as DbSum

        from billing.models import InvoiceRefund

        refunds = InvoiceRefund.objects.select_related(
            'invoice', 'invoice__client', 'invoice__center', 'invoice__staff', 'invoice__created_by')

        if center_id:
            refunds = refunds.filter(invoice__center_id=center_id)
        else:
            allowed = user_center_ids(request.user)
            if allowed is not None:
                refunds = (refunds.filter(invoice__center_id__in=allowed)
                           if allowed else refunds.none())
        if start_date:
            refunds = refunds.filter(refunded_on__gte=start_date)
        if end_date:
            refunds = refunds.filter(refunded_on__lte=end_date)

        refunds = refunds.order_by('-refunded_on', '-id')

        total_count = refunds.count()
        # The Excel export takes every row; the screen takes one page. The
        # export used to read a variable that no longer existed and crashed.
        if request.query_params.get('export') == 'true':
            refund_page = refunds
        else:
            refund_page = refunds[offset: offset + page_size]
        all_refunds_total = refunds.aggregate(total=DbSum('amount'))['total'] or 0

        # The rows below still walk invoices, so expose the page as invoices
        # carrying the refunded amount and date.
        cancelled_page = []
        for row in refund_page:
            invoice = row.invoice
            if invoice is None:
                continue
            invoice.refunded_amount = row.amount
            invoice.refunded_on_date = row.refunded_on
            invoice.refund_reason = row.reason
            cancelled_page.append(invoice)

        result = []
        for inv in cancelled_page:
            client_name = ''
            if inv.client:
                client_name = f"{inv.client.first_name} {inv.client.last_name or ''}".strip()

            billed_by = _billed_by(inv)

            refunded =Decimal(str(getattr(inv, 'refunded_amount', 0) or 0))
            result.append({
                'id': inv.id,
                'bill_no': inv.display_number,
                'date_time': inv.created_at.strftime('%d-%b-%Y, %H:%M'),
                # When the money actually went back, which is what this tab is
                # a list of. The bill date is kept alongside it.
                'refunded_on': getattr(inv, 'refunded_on_date', None),
                'reason': getattr(inv, 'refund_reason', '') or '',
                'client': client_name,
                'billed_by': billed_by,
                # `amount` is the sum returned. `total_amount` stays for any
                # caller that already reads it, but it is the BILL total and is
                # not the refund - conflating the two is what made this tab
                # disagree with every other view.
                'amount': refunded,
                'total_amount': Decimal(str(inv.total_amount)),
                'status': inv.status,
            })


        if request.query_params.get('export') == 'true':
            import openpyxl
            from django.http import HttpResponse
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(title="Refunds")
            ws.append(["Bill No", "Date", "Client", "Refund Amount", "Reason", "Billed By"])
            for row in result:
                ws.append([
                    row.get('bill_no', ''),
                    row.get('date_time', ''),
                    row.get('client', ''),
                    row.get('total_amount', 0),
                    row.get('status', ''),
                    row.get('billed_by', '')
                ])
            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=refunds.xlsx'
            wb.save(response)
            return response

        return Response({
            'refunds': result,
            'total_refunded': Decimal(str(all_refunds_total)),  # FIXED: all pages total, not current page
            'count': total_count,
            'page': page,
            'page_size': page_size,
            'total_pages': -(-total_count // page_size),
        })


class ProcurementReportView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "procurement"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))

        # Apply center-level RBAC scoping for non-owners
        user = request.user
        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}

        pos = PurchaseOrder.objects.select_related('vendor', 'center').prefetch_related('items__product')
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                pos = pos.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                pos = pos.filter(center=user.center)
            else:
                pos = pos.none()
        if center_id:
            pos = pos.filter(center_id=center_id)
        pos = narrow_to_selection(pos, 'center')
        if start_date:
            pos = pos.filter(created_at__date__gte=start_date)
        if end_date:
            pos = pos.filter(created_at__date__lte=end_date)

        vendor_map = defaultdict(lambda: {
            'vendor_name': '',
            'gst_number': '',
            'num_pos': 0,
            'tax_total': Decimal('0.00'),
            'total': Decimal('0.00'),
        })

        for po in pos:
            vk = po.vendor.id
            vendor_map[vk]['vendor_name'] = po.vendor.name
            vendor_map[vk]['gst_number'] = po.vendor.cst_number or ''
            vendor_map[vk]['num_pos'] += 1
            vendor_map[vk]['total'] += Decimal(str(po.total_amount))

            # Tax from PO items
            for item in po.items.all():
                tax_amt = Decimal(str(item.rate)) * Decimal(str(item.quantity)) * Decimal(str(item.tax_percent)) / 100
                vendor_map[vk]['tax_total'] += tax_amt

        result = []
        for vk, data in vendor_map.items():
            result.append({
                'vendor_name': data['vendor_name'],
                'gst_number': data['gst_number'],
                'num_pos': data['num_pos'],
                'taxes': round(data['tax_total'], 2),
                'total': round(data['total'], 2),
            })

        result.sort(key=lambda x: x['total'], reverse=True)


        if request.query_params.get('export') == 'true':
            import openpyxl
            from django.http import HttpResponse
            wb = openpyxl.Workbook(write_only=True)
            ws = wb.create_sheet(title="Procurement Analysis")
            ws.append(["Vendor Name", "GST Number", "Number of POs", "Taxes (₹)", "Total Spent (₹)"])
            for row in result:
                ws.append([
                    row.get('vendor_name', ''),
                    row.get('gst_number', ''),
                    row.get('num_pos', 0),
                    row.get('taxes', 0),
                    row.get('total', 0)
                ])

            response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
            response['Content-Disposition'] = 'attachment; filename=procurement.xlsx'
            wb.save(response)
            return response
            return response

        return Response({
            'vendors': result,
            'grand_total': round(sum(r['total'] for r in result), 2),
            'total_pos': sum(r['num_pos'] for r in result),
        })


def _billed_by(invoice):
    """Who raised the bill: the staff member named on it, else the user who
    created it (the column was blank for every bill that carried no `staff`)."""
    staff = getattr(invoice, 'staff', None)
    if staff:
        return f"{staff.first_name} {staff.last_name or ''}".strip()
    creator = getattr(invoice, 'created_by', None)
    if creator:
        return (getattr(creator, 'full_name', '') or getattr(creator, 'email', '') or '').strip()
    return ''


def _split_tax(tax):
    """CGST and SGST that add back to `tax` exactly: the odd paisa goes to SGST,
    as it does on the invoice itself."""
    from decimal import ROUND_DOWN
    cgst = (D(tax) / 2).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
    return {'cgst': cgst, 'sgst': money(D(tax) - cgst)}


def _gstr1_sections(invoices):
    """What a GSTR-1 return is filled from: B2B and B2C totals, rate-wise totals
    and the HSN/SAC summary. Taxable value is the line's value after its share of
    the bill discount; tax is the tax stored on the line (CGST and SGST are
    halves of it for an intra-state supply)."""
    from billing.models import InvoiceItem

    items = (InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False)
             .values('hsn_sac', 'tax_percentage')
             .annotate(qty=Sum('quantity'), value=Sum('total_price'),
                       discount=Sum('bill_discount_share'), tax=Sum('tax_amount')))
    hsn, rates = {}, {}
    for row in items:
        taxable = money(D(row['value'] or 0) - D(row['discount'] or 0))
        tax = money(row['tax'] or 0)
        rate = str(D(row['tax_percentage'] or 0).normalize())
        code = row['hsn_sac'] or '(none)'
        for table, key in ((hsn, (code, rate)), (rates, (rate,))):
            cur = table.setdefault(key, {'quantity': 0, 'taxable_value': ZERO, 'tax': ZERO})
            cur['quantity'] += int(D(row['qty'] or 0))
            cur['taxable_value'] = money(cur['taxable_value'] + taxable)
            cur['tax'] = money(cur['tax'] + tax)

    def split(is_b2b):
        qs = invoices.exclude(customer_gstin='') if is_b2b else invoices.filter(customer_gstin='')
        agg = qs.aggregate(count=Count('id'), taxable=Sum('subtotal'), discount=Sum('discount'),
                           cgst=Sum('cgst'), sgst=Sum('sgst'), igst=Sum('igst'),
                           total=Sum('total_amount'))
        # Taxable value is what the tax was charged on: the bill less its discount.
        return {'invoices': agg['count'] or 0,
                'taxable_value': money(D(agg['taxable'] or 0) - D(agg['discount'] or 0)),
                'cgst': money(agg['cgst'] or 0), 'sgst': money(agg['sgst'] or 0),
                'igst': money(agg['igst'] or 0), 'total': money(agg['total'] or 0)}

    return {
        'b2b': split(True),
        'b2c': split(False),
        'rate_wise': [{'rate': k[0], **v} for k, v in sorted(rates.items(), key=lambda kv: D(kv[0][0]))],
        'hsn_summary': [{'hsn_sac': k[0], 'rate': k[1], **_split_tax(v['tax']), **v}
                        for k, v in sorted(hsn.items())],
    }


class TaxReportView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "tax_report"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.GET.get('center_id'))
        start_date = safe_date_param(request.GET.get('start_date'))
        end_date = safe_date_param(request.GET.get('end_date'))

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        # Calculate tax summary
        from django.db.models import Sum, F
        tax_summary = invoices.aggregate(
            total_cgst=Sum('cgst'),
            total_sgst=Sum('sgst')
        )
        total_cgst = Decimal(str(tax_summary['total_cgst'] or 0))
        total_sgst = Decimal(str(tax_summary['total_sgst'] or 0))
        total_tax = total_cgst + total_sgst

        # Also return itemized tax for invoices
        invoices_data = list(invoices.values(
            'id', 'client__first_name', 'client__last_name', 'created_at', 'subtotal',
            'cgst', 'sgst', 'total_amount', 'invoice_number'
        ))
        for inv in invoices_data:
            inv['created_at'] = inv['created_at'].strftime('%Y-%m-%d %H:%M') if inv['created_at'] else None
            inv['client_name'] = f"{inv.get('client__first_name', '')} {inv.get('client__last_name', '')}".strip()
            inv['invoice_number'] = inv.pop('invoice_number') or inv['id']
            inv['tax_amount'] = Decimal(str(inv['cgst'] or 0)) + Decimal(str(inv['sgst'] or 0))
            inv['cgst_amount'] = Decimal(str(inv['cgst'] or 0))
            inv['sgst_amount'] = Decimal(str(inv['sgst'] or 0))

        return Response({
            'summary': {
                'total_tax': total_tax,
                'total_cgst': total_cgst,
                'total_sgst': total_sgst,
            },
            'invoices': invoices_data,
            **_gstr1_sections(invoices),
        })

class ServiceDrilldownView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "service_drilldown"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.GET.get('center_id'))
        start_date = safe_date_param(request.GET.get('start_date'))
        end_date = safe_date_param(request.GET.get('end_date'))

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        from billing.models import InvoiceItem
        from django.db.models import F

        # Grouped by the ITEM, not its description: one service with two
        # different labels used to appear as two rows with its revenue split
        # between them. Redemptions are excluded - a Rs 0 entitlement draw is
        # not a sale, and counting it inflated `count`.
        items = InvoiceItem.objects.filter(
            invoice__in=invoices, is_redemption=False,
        ).values('object_id', 'content_type__model').annotate(
            count=Sum('quantity'),
            revenue=Sum('total_price'),
            tax=Sum('tax_amount'),
            label=Max('description'),
        ).order_by('-revenue')

        type_labels = {
            'servicemaster': 'Service', 'product': 'Product',
            'package': 'Package', 'membership': 'Membership',
            'valuecard': 'Value Card', 'advancepayment': 'Advance',
        }

        results = []
        for i in items:
            count = int(D(i['count']))
            revenue = money(i['revenue'])
            results.append({
                'item_id': i['object_id'],
                'name': i['label'] or 'Unknown',
                'type': type_labels.get(i['content_type__model'], 'Other'),
                'count': count,
                'revenue': revenue,
                'tax': money(i['tax']),
                'avg_price': money(revenue / count) if count else money(0),
            })

        return Response(results)

def credited_shares(item):
    """[(staff member, revenue share, tax share)] for one bill line.

    The people who did the work are `staff_members` plus the main `staff`. When the till saved
    a split (`staff_allocations`, e.g. 60/40) it is followed; otherwise the line is shared
    equally. The shares add up to the line to the paisa. A line nobody is named on has none.
    """
    members = list(item.staff_members.all())
    if item.staff and item.staff not in members:
        members.insert(0, item.staff)
    if not members:
        return []
    weights = [ZERO] * len(members)
    alloc = item.staff_allocations if isinstance(item.staff_allocations, dict) else {}
    if alloc:
        weights = [D(alloc.get(str(m.pk)) or alloc.get(m.pk) or 0) for m in members]
    if sum(weights, ZERO) <= 0:
        weights = [D(1)] * len(members)
    whole = sum(weights, ZERO)
    shares = []
    for amount in (D(item.total_price or 0), D(item.tax_amount or 0)):
        parts = [money(amount * w / whole) for w in weights]
        parts[0] = money(parts[0] + amount - sum(parts, ZERO))
        shares.append(parts)
    return [(m, shares[0][i], shares[1][i]) for i, m in enumerate(members)]


class StaffPerformanceReportView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "staff_performance"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.GET.get('center_id'))
        start_date = safe_date_param(request.GET.get('start_date'))
        end_date = safe_date_param(request.GET.get('end_date'))

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)
        from billing.models import InvoiceItem

        # Grouped by staff_id, not by name. Two staff members who share a name
        # used to be merged into one row, splitting neither's figures out.
        # Redemptions excluded: a Rs 0 entitlement draw is not work sold.
        # A line done by two stylists is shared between them by the split the
        # till saved (60/40, or equal when none), the way the incentive engine does it;
        # it used to land wholly on the first one.
        items = (InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False)
                 .select_related('staff__center', 'invoice__staff')
                 .prefetch_related('staff_members__center'))

        by_staff = {}
        for item in items:
            for member, revenue_share, tax_share in credited_shares(item):
                # A staff member with no last name used to render as "Ravi None".
                row = by_staff.setdefault(member.pk, {
                    'staff_id': member.pk,
                    'staff_name': f"{member.first_name or ''} {member.last_name or ''}".strip() or 'Unknown',
                    'designation': member.designation or '',
                    'center': (member.center.display_name if member.center else '') or '',
                    'services_done': 0, 'revenue_generated': money(0), 'tax_collected': money(0),
                    # This is gross sales attributed to the person who performed the
                    # work, not the commissionable figure. That is the incentive
                    # engine, which also discounts for wallet-settled portions.
                    'basis': 'gross sales',
                })
                row['services_done'] += int(D(item.quantity))
                row['revenue_generated'] = money(D(row['revenue_generated']) + revenue_share)
                row['tax_collected'] = money(D(row['tax_collected']) + tax_share)

        results = sorted(by_staff.values(), key=lambda r: D(r['revenue_generated']), reverse=True)
        return Response(results)

class ManagerDiscountsAuditView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "admin"
    permission_submodule = "manager_discounts"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.GET.get('center_id'))
        start_date = safe_date_param(request.GET.get('start_date'))
        end_date = safe_date_param(request.GET.get('end_date'))

        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        discounted_invoices = invoices.filter(discount__gt=0).values(
            'id', 'client__first_name', 'client__last_name', 'created_at', 'subtotal',
            'discount', 'total_amount', 'staff__first_name', 'staff__last_name'
        )

        results = []
        for inv in discounted_invoices:
            manager = f"{inv.get('staff__first_name', '')} {inv.get('staff__last_name', '')}".strip()
            client_name = f"{inv.get('client__first_name', '')} {inv.get('client__last_name', '')}".strip()
            results.append({
                'invoice_number': inv['id'],
                'client_name': client_name,
                'created_at': inv['created_at'].strftime('%Y-%m-%d %H:%M') if inv['created_at'] else None,
                'subtotal': Decimal(str(inv['subtotal'] or 0)),
                'discount_amount': Decimal(str(inv['discount'] or 0)),
                'total_amount': Decimal(str(inv['total_amount'] or 0)),
                'manager': manager or 'Unknown'
            })

        return Response(results)


class StaffIncentiveCalculationView(views.APIView):
    """Staff incentives for a period, from the IncentiveRule engine.

    The calculation itself lives in finance/incentives.py. It used to be ~570
    lines inline here, and carried unreachable membership/package branches, a
    Decimal/float crash on percentage slabs, category detection by description
    substring, cumulative-instead-of-highest tier selection, and a header total
    that did not equal the sum of its own detail rows.
    """

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'incentives'
    permission_strict_read = True

    def get(self, request):
        from django.db.models import Q
        from staff.models import StaffMember
        from billing.models import Invoice, InvoiceItem
        from finance import incentives
        from finance.models import IncentiveRule

        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = self._parse_date(safe_date_param(request.query_params.get('start_date')))
        end_date = self._parse_date(safe_date_param(request.query_params.get('end_date')))
        frequency = (request.query_params.get('frequency') or 'monthly').lower()
        export = (request.query_params.get('export') or '').lower() == 'true'

        # Staff visible to this user, honouring the center filter. Fails closed.
        # `center_ids=1,2,3` reports several chosen centres at once. It can only
        # narrow what the user may already see, never widen it.
        picked = []
        for part in (request.query_params.get('center_ids') or '').split(','):
            if part.strip().isdigit():
                picked.append(int(part))
        staff_qs = scope_to_centers(
            StaffMember.objects.select_related('center').filter(is_active=True),
            request.user, requested_center=None if picked else center_id,
        )
        if picked:
            staff_qs = staff_qs.filter(center_id__in=picked)

        # Invoices are NOT center-filtered: a staff member's salary multiple
        # must reflect their total sales, including work done at another
        # center. Attribution is by staff, below.
        invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES)
        if start_date:
            invoices = invoices.filter(created_at__gte=start_date)
        if end_date:
            invoices = invoices.filter(
                created_at__lt=end_date + dt_module.timedelta(days=1)
            )

        items = (
            InvoiceItem.objects
            .filter(invoice__in=invoices, is_redemption=False)
            .filter(Q(staff__in=staff_qs)
                    | Q(staff_members__in=staff_qs)
                    | Q(staff__isnull=True, staff_members__isnull=True,
                        invoice__staff__in=staff_qs))
            .select_related('invoice', 'invoice__center', 'invoice__client',
                            'content_type', 'staff')
            .prefetch_related('staff_members', 'invoice__payments')
            .distinct()
        )

        rules = list(IncentiveRule.objects.filter(is_active=True))

        results = incentives.calculate(
            staff_qs=staff_qs,
            invoice_items=items,
            rules=rules,
            start_date=start_date,
            end_date=end_date,
            frequency=frequency,
        )
        payload = [r.as_dict() for r in results]
        payload.sort(key=lambda row: row['total_sales'], reverse=True)

        # Reconciliation guard: the header total must equal the sum of the
        # detail rows. If it ever does not, say so rather than shipping two
        # numbers that disagree.
        for row in payload:
            detail_sum = money(sum(
                (D(line['calculated_incentive']) for line in row['details']), ZERO
            ))
            per_line_total = money(
                D(row['service_incentive']) + D(row['product_incentive'])
                + D(row['card_incentive']) + D(row['membership_incentive'])
                + D(row['package_incentive'])
            )
            row['details_reconcile'] = (detail_sum == per_line_total)
            row['details_total'] = detail_sum

        if export:
            return self._export_excel(payload, start_date, end_date)
        return Response(payload)

    @staticmethod
    def _parse_date(text):
        if not text:
            return None
        try:
            return datetime.strptime(str(text), '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return None

    def _export_excel(self, rows, start_date, end_date):
        """Write the incentive report to an .xlsx file.

        The previous implementation created a write-only Workbook and then
        called ws.cell() and iterated ws.columns - neither of which exists on
        openpyxl's WriteOnlyWorksheet - so this endpoint returned a 500 every
        single time it was called.
        """
        import openpyxl
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        from django.http import HttpResponse

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = 'Staff Incentives'

        headers = [
            'Staff Name', 'Role', 'Center', 'Salary',
            'Services Rev', 'Products Rev', 'Cards Rev', 'Memberships Rev', 'Packages Rev',
            'Total Sales', 'Salary Multiple',
            # MONTHLY incentive: on the month's figures
            'Monthly: Service %', 'Monthly: Product %', 'Monthly: Value Card', 'Monthly: Membership',
            'Monthly: Package', 'Monthly: Overall Bonus', 'MONTHLY TOTAL',
            # DAILY incentive: on single days or single bills
            'Daily: Business Slab', 'Daily: Service Add-on', 'Daily: Volume Target', 'DAILY TOTAL',
            'Total Incentive',
            'Rules Applied',
        ]
        header_fill = PatternFill(start_color='1F4E79', end_color='1F4E79', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True)

        for column, title in enumerate(headers, start=1):
            cell = sheet.cell(row=1, column=column, value=title)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        sheet.freeze_panes = 'A2'

        for row in rows:
            sheet.append([
                row['staff_name'],
                row['role'],
                row['center'],
                float(row['salary']),
                float(row['services_revenue']),
                float(row['products_revenue']),
                float(row['cards_revenue']),
                float(row['memberships_revenue']),
                float(row['packages_revenue']),
                float(row['total_sales']),
                float(row['salary_multiple']),
                float(row['service_percent_incentive']),
                float(row['product_incentive']),
                float(row['card_incentive']),
                float(row['membership_incentive']),
                float(row['package_incentive']),
                float(row['overall_incentive']),
                float(row['monthly_incentive']),
                float(row['daily_business_incentive']),
                float(row['service_addon_incentive']),
                float(row['service_target_incentive']),
                float(row['daily_incentive']),
                float(row['total_incentive']),
                '; '.join(f'{k}: {v}' for k, v in (row.get('rules_applied') or {}).items()),
            ])

        last_number_column = len(headers) - 1
        for index in range(4, last_number_column + 1):
            for cell in sheet[get_column_letter(index)][1:]:
                cell.number_format = '#,##0.00'

        widths = [24, 18, 20] + [14] * (len(headers) - 4) + [50]
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width

        # Second sheet: one row for every rupee, with the place it was earned.
        where = workbook.create_sheet('Where earned')
        where_headers = ['Staff', 'Centre', 'Kind', 'Date', 'Invoice', 'Client', 'Item / Detail',
                         'Net revenue', 'Incentive', 'How it was worked out']
        for column, title in enumerate(where_headers, start=1):
            cell = where.cell(row=1, column=column, value=title)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center', vertical='center')
        where.freeze_panes = 'A2'
        for row in rows:
            who, centre = row['staff_name'], row['center']
            for line in row['details']:
                monthly = line.get('monthly_amount', line.get('incentive_amount') or 0)
                if D(monthly) != 0 or D(line.get('addon_amount') or 0) == 0:
                    where.append([who, centre, 'MONTHLY: ' + (line.get('type') or ''), line['date'],
                                  line['invoice_number'], line['client_name'], line['item_name'],
                                  float(line['revenue']), float(monthly), line.get('calculation_rule') or ''])
                if D(line.get('addon_amount') or 0) != 0:
                    where.append([who, centre, 'DAILY: Service add-on', line['date'],
                                  line['invoice_number'], line['client_name'], line['item_name'],
                                  float(line['revenue']), float(line['addon_amount']), line.get('addon_label') or ''])
            for day in row.get('daily_bonus_days') or []:
                where.append([who, centre, 'DAILY: Business slab', day['date'], '', '',
                              f"Day's sales Rs {day['sales']}", float(day['sales']), float(day['bonus']),
                              f"{day['rule']}: {day['slab']}"])
            for hit in row.get('target_achievements') or []:
                where.append([who, centre, 'DAILY: Volume target', hit.get('date') or 'Period', '', '',
                              hit['target_name'], '', float(hit['reward']),
                              f"{hit['achieved_count']} done, {hit['target_count']} needed"])
        for index in (8, 9):
            for cell in where[get_column_letter(index)][1:]:
                cell.number_format = '#,##0.00'
        for index, width in enumerate([24, 20, 24, 12, 18, 26, 34, 14, 14, 60], start=1):
            where.column_dimensions[get_column_letter(index)].width = width

        period = f"{start_date or 'all'}_to_{end_date or 'all'}"
        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename="staff_incentives_{period}.xlsx"'
        workbook.save(response)
        return response


class MultiSalonExportView(views.APIView):
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "multi_salon"
    permission_strict_read = True

    def get(self, request):
        import openpyxl
        from django.http import HttpResponse

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        user = request.user

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}

        centers = Center.objects.all()
        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                centers = centers.filter(id__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                centers = centers.filter(id=user.center.id)
            else:
                centers = Center.objects.none()

        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet(title="Multi Salon Report")
        ws.append(["Center Name", "Total Sales", "Target", "Target Achieved %"])

        rows = []
        for c in centers:
            data = compute_register_summary(request.user, str(c.id), start_date, end_date)

            sales = data['revenues']['collection_before_tax']
            target = data['revenues']['target']
            achieved = data['revenues']['target_achieved_percentage']

            rows.append({
                'center_name': c.display_name or c.center_name,
                'sales': sales,
                'target': target,
                'achieved': achieved
            })

        rows.sort(key=lambda x: x['achieved'])

        for r in rows:
            ws.append([r['center_name'], r['sales'], r['target'], r['achieved']])

        response = HttpResponse(content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename=multi_salon_report.xlsx'
        wb.save(response)
        return response

# ─────────────────────────────────────────────────────────────────────────────
# Multi-salon reports
#
# Reviewed and rewritten in the third audit pass. Defects fixed here:
#
#   R1  ServiceDrilldown searched `category__name__icontains`, but
#       ServiceMaster.category is a CharField - FieldError, a guaranteed 500 on
#       any search.
#   R2  ProductDrilldown searched `content_object__brand__name__icontains`;
#       content_object is a GenericForeignKey and cannot be filtered on at all -
#       also a guaranteed 500 on any search.
#   R3  The categories report read `hsn_code` off Product, which has no such
#       field (it has sac_code), so the HSN column was permanently blank -
#       silently missing GST filing data.
#   R4  Redemption lines (Rs 0 package redemptions) were counted as sales, so
#       free redeemed services inflated the "sold" counts.
#   R5  `sum(services_remaining.values())` raised TypeError when the JSON held
#       string quantities.
#   R6  `f"{first_name} {last_name}"` rendered "Asha None" for the many clients
#       with no last name; Client.full_name already handles this.
#   R7  center_id was passed through unparsed, so the literal string 'null' the
#       frontend sends raised ValueError - a 500.
#   R8  An unrecognised item_type in the sales export silently exported EVERY
#       line type instead of erroring.
#   R9  "Bill No" was the primary key rather than the GST invoice number.
#   R10 N+1 queries in the balances report (no select_related on client,
#       value_card or package).
#   R11 Advance count/total mixed top-ups and redemptions into one figure.
#   R12 Service balance was hardcoded to 0.0 with a comment saying it was too
#       hard to compute; _outstanding_package_liability now does it.
#   R13 "Is member" ignored expiry, so a lapsed membership still counted.
#   R14 float() arithmetic on money, and staff revenue splits that leaked paise.
#   R15 Categories grouped by description, splitting one service across rows.
# ─────────────────────────────────────────────────────────────────────────────

def _multi_salon_centers(request):
    """Centers this user may report on, honouring ?center_id. Fails closed."""
    from salon_admin.models import Center

    return scope_to_centers(
        Center.objects.all(), request.user, field='id',
        requested_center=coerce_center_id(request.query_params.get('center_id')),
    )


def _remaining_total(services_remaining):
    """Total remaining services in a ClientPackage JSON blob.

    `sum(dict.values())` raised TypeError when the JSON held string
    quantities, which it does for records written by older code.
    """
    if not isinstance(services_remaining, dict):
        return 0
    total = 0
    for value in services_remaining.values():
        try:
            total += int(D(value))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return total


def _tax_code_for(obj):
    """HSN (goods) or SAC (services) code for a catalog object.

    Product has `sac_code` and no `hsn_code`, so reading hsn_code off it
    silently produced an empty column.
    """
    for field in ('hsn_code', 'sac_code'):
        value = getattr(obj, field, None)
        if value:
            return str(value)
    return ''


class MultiSalonBalancesView(views.APIView):
    """Outstanding customer liabilities per center: cards, advances, packages."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    def get(self, request):
        from datetime import date

        from django.db.models import Sum

        from billing.models import AdvancePayment
        from clients.models import ClientPackage, ClientValueCard

        centers_qs = _multi_salon_centers(request)
        center_ids = list(centers_qs.values_list('id', flat=True))
        center_names = {c.id: (c.display_name or c.center_name) for c in centers_qs}
        today = date.today()

        results = []

        # 1. Value cards with a live balance.
        #    select_related kills the N+1 that walked client and value_card per row.
        value_cards = (
            ClientValueCard.objects
            .filter(client__center_id__in=center_ids, balance__gt=0,
                    expiry_date__gte=today, is_active=True)
            .select_related('client', 'value_card')
        )
        for card in value_cards:
            results.append({
                'center_name': center_names.get(card.client.center_id, 'Unknown'),
                'type': 'Value Card',
                'name': card.value_card.title if card.value_card else 'Unknown',
                'count': 1,
                'value': money(card.balance),
                'client': card.client.full_name,
                'phone': card.client.phone,
                'expiry': card.expiry_date.isoformat() if card.expiry_date else None,
            })

        # 2. Advance balances. Reported as the NET balance, with the gross
        #    top-up and redemption shown separately - one combined figure made
        #    "5 top-ups and 5 redemptions" indistinguishable from "1 top-up".
        advances = (
            AdvancePayment.objects
            .filter(client__center_id__in=center_ids)
            .values('client_id', 'client__first_name', 'client__last_name',
                    'client__phone', 'client__center_id')
            .annotate(
                net=Sum('amount'),
                topped_up=Sum('amount', filter=Q(amount__gt=0)),
                redeemed=Sum('amount', filter=Q(amount__lt=0)),
            )
            .filter(net__gt=0)
        )
        for row in advances:
            last = row['client__last_name'] or ''
            results.append({
                'center_name': center_names.get(row['client__center_id'], 'Unknown'),
                'type': 'Advance',
                'name': 'Advance Balance',
                'count': 1,
                'value': money(row['net']),
                'topped_up': money(row['topped_up'] or 0),
                'redeemed': money(abs(D(row['redeemed'] or 0))),
                'client': f"{row['client__first_name']} {last}".strip(),
                'phone': row['client__phone'],
                'expiry': None,
            })

        # 3. Packages, valued at what is actually left rather than shown as '-'.
        packages = (
            ClientPackage.objects
            .filter(client__center_id__in=center_ids,
                    expiry_date__gte=today, is_active=True)
            .select_related('client', 'package')
        )
        # Price every package's remaining services in ONE pass.
        #
        # This called `_outstanding_package_liability_value(package)` inside the
        # loop, and that helper runs a query per call - so a business with 2,000
        # live packages issued 2,000 queries to draw one screen. The batched
        # form of the same helper already existed 460 lines below in this file.
        package_list = [
            p for p in packages if _remaining_total(p.services_remaining) > 0
        ]
        liabilities = _package_liabilities_by_id(package_list)

        for package in package_list:
            remaining = _remaining_total(package.services_remaining)
            liability = liabilities.get(package.id, Decimal('0.00'))
            results.append({
                'center_name': center_names.get(package.client.center_id, 'Unknown'),
                'type': 'Package',
                'name': package.package.name if package.package else 'Custom Package',
                'count': remaining,
                'value': liability,
                'client': package.client.full_name,
                'phone': package.client.phone,
                'expiry': package.expiry_date.isoformat() if package.expiry_date else None,
            })

        return Response(results)


def _package_liabilities_by_id(client_packages):
    """Rupee value of each package's remaining services, keyed by package id.

    Replaces a per-package helper that was called inside a loop - one query per
    package to draw a single screen. The prices are fetched once for every
    service referenced across every package, then applied in Python.

    The per-row version is deliberately gone rather than kept alongside: leaving
    it there is how it gets called from a loop again.
    """
    from services.models import ServiceMaster

    packages = list(client_packages)
    if not packages:
        return {}

    service_ids = set()
    for package in packages:
        for service_id in (package.services_remaining or {}):
            try:
                service_ids.add(int(service_id))
            except (TypeError, ValueError):
                continue

    prices = dict(
        ServiceMaster.objects.filter(id__in=service_ids)
        .values_list('id', 'default_price')
    )

    totals = {}
    for package in packages:
        total = Decimal('0.00')
        for service_id, remaining in (package.services_remaining or {}).items():
            try:
                count = int(remaining)
                price = prices.get(int(service_id))
            except (TypeError, ValueError):
                continue
            if count > 0 and price is not None:
                total += Decimal(str(price)) * count
        totals[package.id] = total
    return totals


class MultiSalonSalesExportView(views.APIView):
    """Line-level sales export for one item type."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    ITEM_TYPES = {
        'service': 'servicemaster',
        'product': 'product',
        'membership': 'membership',
        'package': 'package',
        'valuecard': 'valuecard',
        'giftcard': 'valuecard',
        'advances': None,
    }

    def get(self, request):
        import openpyxl
        from openpyxl.utils import get_column_letter
        from django.http import HttpResponse

        from billing.models import AdvancePayment, InvoiceItem

        item_type = (request.query_params.get('item_type') or '').strip().lower()
        if not item_type:
            return Response({'error': 'item_type is required.'}, status=400)
        # An unrecognised type used to fall through with target_model=None and
        # export EVERY line type, silently answering a different question.
        if item_type not in self.ITEM_TYPES:
            return Response(
                {'error': f'Unknown item_type {item_type!r}.',
                 'valid': sorted(self.ITEM_TYPES)},
                status=400,
            )

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        center_id = coerce_center_id(request.query_params.get('center_id'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        workbook = openpyxl.Workbook(write_only=True)
        sheet = workbook.create_sheet(title=f'{item_type.capitalize()} Sales')

        if item_type == 'advances':
            sheet.append(['Date', 'Center', 'Client', 'Phone', 'Amount', 'Kind'])
            advances = (
                AdvancePayment.objects
                .filter(invoice__in=invoices)
                .select_related('client', 'client__center', 'center')
            )
            for advance in advances:
                center = advance.center or (advance.client.center if advance.client else None)
                amount = money(advance.amount)
                sheet.append([
                    advance.created_at.date().isoformat() if advance.created_at else '',
                    (center.display_name or center.center_name) if center else 'Unknown',
                    advance.client.full_name if advance.client else '',
                    advance.client.phone if advance.client else '',
                    float(amount),
                    'Redeemed' if amount < 0 else 'Top-up',
                ])
        else:
            items = (
                InvoiceItem.objects
                .filter(invoice__in=invoices,
                        content_type__model=self.ITEM_TYPES[item_type],
                        # Redemptions are Rs 0 entitlement draws, not sales.
                        is_redemption=False)
                .select_related('invoice', 'invoice__client', 'invoice__center', 'staff')
            )
            sheet.append([
                'Date', 'Center', 'Bill No', 'Client', 'Staff', 'Item Name',
                'Quantity', 'Unit Price', 'Discount', 'Taxable', 'Tax', 'Gross',
            ])
            for item in items:
                invoice = item.invoice
                center = invoice.center
                sheet.append([
                    invoice.created_at.date().isoformat() if invoice.created_at else '',
                    (center.display_name or center.center_name) if center else 'Unknown',
                    # The GST serial, not the primary key.
                    invoice.display_number,
                    invoice.client.full_name if invoice.client else 'Walk-in',
                    (f'{item.staff.first_name} {item.staff.last_name or ""}'.strip()
                     if item.staff else ''),
                    item.description,
                    int(item.quantity or 0),
                    float(money(item.unit_price)),
                    float(money(item.discount)),
                    float(money(item.total_price)),
                    float(money(item.tax_amount)),
                    float(item.gross_total),
                ])

        response = HttpResponse(
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        )
        response['Content-Disposition'] = f'attachment; filename={item_type}_sales_report.xlsx'
        workbook.save(response)
        return response


class MultiSalonCategoriesView(views.APIView):
    """Sales grouped by category, plus per-item service and product tables."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    def get(self, request):
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Count, Q, Sum

        from billing.models import AdvancePayment, InvoiceItem
        from inventory.models import Product
        from marketing.models import Membership, Package, ValueCard
        from services.models import ServiceMaster

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        center_id = coerce_center_id(request.query_params.get('center_id'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        try:
            ct_map = ContentType.objects.get_for_models(
                ServiceMaster, Product, Membership, Package, ValueCard
            )
        except Exception:
            logger.warning('[Finance] ContentType lookup failed in multi-salon categories')
            return Response({'categories': [], 'services': [], 'products': []})

        service_ct = ct_map[ServiceMaster]
        product_ct = ct_map[Product]

        # Redemptions excluded: a free redeemed service is not a sale.
        items = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False)

        by_type = items.values('content_type').annotate(
            count=Sum('quantity'), total=Sum('total_price'),
        )
        totals = {row['content_type']: row for row in by_type}

        def bucket(label, content_type):
            row = totals.get(content_type.id, {})
            return {
                'type': label,
                'count': int(row.get('count') or 0),
                'amount': money(row.get('total') or 0),
            }

        # Advances split into top-ups and redemptions rather than netted, so a
        # busy day of both does not look like a quiet one.
        advances = AdvancePayment.objects.filter(invoice__in=invoices).aggregate(
            topped_up=Sum('amount', filter=Q(amount__gt=0)),
            redeemed=Sum('amount', filter=Q(amount__lt=0)),
            topup_count=Count('id', filter=Q(amount__gt=0)),
        )

        categories_summary = [
            bucket('Memberships', ct_map[Membership]),
            bucket('Value Cards', ct_map[ValueCard]),
            bucket('Packages', ct_map[Package]),
            {
                'type': 'Advances',
                'count': int(advances['topup_count'] or 0),
                'amount': money(advances['topped_up'] or 0),
                'redeemed': money(abs(D(advances['redeemed'] or 0))),
            },
        ]

        return Response({
            'categories': categories_summary,
            'services': self._item_table(items, service_ct, ServiceMaster),
            'products': self._item_table(items, product_ct, Product),
        })

    @staticmethod
    def _item_table(items, content_type, model):
        """Per-item sales, grouped by the ITEM rather than by its description.

        Grouping on description split one service across several rows whenever
        the label varied between bills.
        """
        from django.db.models import Sum

        scoped = items.filter(content_type=content_type)

        grouped = list(
            scoped.values('object_id')
            .annotate(count=Sum('quantity'), amount=Sum('total_price'),
                      tax=Sum('tax_amount'))
            .order_by('-amount')
        )
        object_ids = [row['object_id'] for row in grouped if row['object_id']]

        catalog = {obj.id: obj for obj in model.objects.filter(id__in=object_ids)}

        # One query for the centers each item sold at.
        centers_by_item = {}
        for row in scoped.values(
            'object_id', 'invoice__center__center_name', 'invoice__center__display_name'
        ).distinct():
            name = (row['invoice__center__display_name']
                    or row['invoice__center__center_name'] or 'Unknown')
            centers_by_item.setdefault(row['object_id'], set()).add(name)

        results = []
        for row in grouped:
            obj = catalog.get(row['object_id'])
            centers = sorted(centers_by_item.get(row['object_id'], set()))
            results.append({
                'item_id': row['object_id'],
                'name': (getattr(obj, 'name', None) or getattr(obj, 'title', None)
                         or f'#{row["object_id"]}'),
                # HSN for goods, SAC for services, whichever the record carries.
                'hsn': _tax_code_for(obj) if obj else '',
                'centers': ', '.join(centers) if centers else 'Unknown',
                'count': int(row['count'] or 0),
                'amount': money(row['amount'] or 0),
                'tax': money(row['tax'] or 0),
            })
        return results


class _MultiSalonDrilldownBase(views.APIView):
    """Shared drilldown: one item type, broken down by center."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    model = None

    def search_filter(self, search_term):
        """Return a Q for the search box, or None. Overridden per subclass."""
        raise NotImplementedError

    def get(self, request):
        from django.contrib.contenttypes.models import ContentType
        from django.db.models import Sum

        from billing.models import InvoiceItem

        search_term = (request.query_params.get('search') or '').strip()
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        center_id = coerce_center_id(request.query_params.get('center_id'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        try:
            content_type = ContentType.objects.get_for_model(self.model)
        except Exception:
            return Response([])

        items = InvoiceItem.objects.filter(
            invoice__in=invoices, content_type=content_type, is_redemption=False,
        )

        if search_term:
            condition = self.search_filter(search_term)
            if condition is not None:
                items = items.filter(condition)

        grouped = items.values(
            'invoice__center__center_name', 'invoice__center__display_name'
        ).annotate(count=Sum('quantity'), amount=Sum('total_price')).order_by('-amount')

        return Response([
            {
                'center_name': (row['invoice__center__display_name']
                                or row['invoice__center__center_name'] or 'Unknown'),
                'count': int(row['count'] or 0),
                'amount': money(row['amount'] or 0),
            }
            for row in grouped
        ])


class MultiSalonServiceDrilldownView(_MultiSalonDrilldownBase):
    from services.models import ServiceMaster as _ServiceMaster
    model = _ServiceMaster

    def search_filter(self, search_term):
        """Match the service name, category or the stored line description.

        The old filter used `category__name__icontains`, but
        ServiceMaster.category is a CharField - so any search raised FieldError
        and returned a 500.
        """
        from django.db.models import Q

        from services.models import ServiceMaster

        matching = ServiceMaster.objects.filter(
            Q(name__icontains=search_term)
            | Q(category__icontains=search_term)
            | Q(sub_category__icontains=search_term)
            | Q(brand__icontains=search_term)
        ).values_list('id', flat=True)

        return Q(object_id__in=list(matching)) | Q(description__icontains=search_term)


class MultiSalonProductDrilldownView(_MultiSalonDrilldownBase):
    from inventory.models import Product as _Product
    model = _Product

    def search_filter(self, search_term):
        """Match the product name, brand or category.

        The old filter used `content_object__brand__name__icontains`.
        content_object is a GenericForeignKey and cannot be filtered on at all,
        so any search raised FieldError and returned a 500.
        """
        from django.db.models import Q

        from inventory.models import Product

        matching = Product.objects.filter(
            Q(name__icontains=search_term)
            | Q(brand__icontains=search_term)
            | Q(category__icontains=search_term)
            | Q(barcode__icontains=search_term)
        ).values_list('id', flat=True)

        return Q(object_id__in=list(matching)) | Q(description__icontains=search_term)


class MultiSalonClientsView(views.APIView):
    """Per-client spend and outstanding balances across centers."""

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    def get(self, request):
        import datetime as _dt

        from django.db.models import Count, Max, Prefetch, Sum

        from billing.models import AdvancePayment
        from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        center_id = coerce_center_id(request.query_params.get('center_id'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        client_ids = [
            cid for cid in invoices.values_list('client_id', flat=True).distinct() if cid
        ]
        today = _dt.date.today()

        clients = Client.objects.filter(id__in=client_ids, is_active=True).prefetch_related(
            Prefetch('memberships', queryset=ClientMembership.objects.filter(
                is_active=True, expiry_date__gte=today).select_related('membership')),
            Prefetch('value_cards', queryset=ClientValueCard.objects.filter(
                is_active=True, expiry_date__gte=today).select_related('value_card')),
            Prefetch('packages', queryset=ClientPackage.objects.filter(
                is_active=True, expiry_date__gte=today).select_related('package')),
        )

        advance_balances = {
            row['client_id']: row['total']
            for row in AdvancePayment.objects.filter(client_id__in=client_ids)
            .values('client_id').annotate(total=Sum('amount'))
        }

        stats_map = {
            row['client_id']: row
            for row in invoices.values('client_id').annotate(
                visits=Count('id'),
                total_spend=Sum('total_amount'),
                total_paid=Sum('paid_amount'),
                last_visit=Max('created_at'),
            ) if row['client_id']
        }

        results = []
        for client in clients:
            stats = stats_map.get(client.id, {})
            visits = int(stats.get('visits') or 0)
            total_spend = money(stats.get('total_spend') or 0)

            memberships = list(client.memberships.all())
            cards = list(client.value_cards.all())
            packages = list(client.packages.all())

            results.append({
                'id': client.id,
                'name': client.full_name,
                'phone': client.phone,
                # Prefetch is now filtered on expiry, so a lapsed membership no
                # longer counts the customer as a member.
                'is_member': 'Yes' if memberships else 'No',
                'gender': self._gender_code(client.gender),
                'visits': visits,
                'total_spend': total_spend,
                'total_paid': money(stats.get('total_paid') or 0),
                'avg_spend': money(total_spend / visits) if visits else money(0),
                'last_visit': stats.get('last_visit'),
                # Was hardcoded to 0.0 with a comment saying it was too hard.
                'serv_balance': _outstanding_package_liability_value_many(packages),
                'card_balance': money(sum((D(c.balance) for c in cards), D('0'))),
                'advance': money(advance_balances.get(client.id, 0)),
                'details': {
                    'memberships': [
                        {'name': m.membership.name if m.membership else 'Unknown',
                         'expiry': m.expiry_date.isoformat() if m.expiry_date else None}
                        for m in memberships
                    ],
                    'cards': [
                        {'name': c.value_card.title if c.value_card else 'Unknown',
                         'balance': money(c.balance)}
                        for c in cards
                    ],
                    'packages': [
                        {'service': p.package.name if p.package else 'Custom Package',
                         'remaining': _remaining_total(p.services_remaining)}
                        for p in packages
                        if _remaining_total(p.services_remaining) > 0
                    ],
                },
            })

        results.sort(key=lambda row: row['total_spend'], reverse=True)
        return Response(results)

    @staticmethod
    def _gender_code(gender):
        value = (gender or '').strip().lower()
        if value.startswith('m'):
            return 'M'
        if value.startswith('f'):
            return 'F'
        return gender or ''


def _outstanding_package_liability_value_many(client_packages):
    from salon_admin.dashboard_endpoints import _outstanding_package_liability

    return _outstanding_package_liability(list(client_packages))['total']


class MultiSalonStaffView(views.APIView):
    """Gross sales per staff member across centers.

    This is GROSS sales attributed to whoever performed the work. It is
    deliberately not the commissionable figure - that is the incentive engine at
    /finance/api/reports/incentive-calculation/, which additionally discounts
    for the portion of each bill settled from a wallet or value card. The two
    numbers answer different questions and will differ.
    """

    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'finance'
    permission_submodule = 'multi_salon'
    permission_strict_read = True

    def get(self, request):
        from django.contrib.contenttypes.models import ContentType

        from billing.models import InvoiceItem
        from inventory.models import Product
        from marketing.models import Membership, Package, ValueCard
        from services.models import ServiceMaster

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        center_id = coerce_center_id(request.query_params.get('center_id'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        try:
            ct_map = ContentType.objects.get_for_models(
                ServiceMaster, Product, Membership, Package, ValueCard
            )
        except Exception:
            return Response([])

        bucket_by_ct = {
            ct_map[ServiceMaster].id: 'services',
            ct_map[Product].id: 'products',
            ct_map[Package].id: 'packages',
            ct_map[Membership].id: 'memberships',
            ct_map[ValueCard].id: 'cards',
        }

        items = (
            InvoiceItem.objects
            .filter(invoice__in=invoices, is_redemption=False)
            .select_related('staff', 'invoice', 'invoice__center', 'invoice__staff',
                            'content_type')
            .prefetch_related('staff_members')
        )

        stats = {}
        for item in items:
            credited = [m for m, _, _ in credited_shares(item)]
            if credited:
                # The saved split (60/40) when there is one, else equal; exact to the paisa.
                shares = [revenue for _, revenue, _ in credited_shares(item)]
            elif item.invoice.staff:
                credited = [item.invoice.staff]
                shares = [money(item.total_price)]
            else:
                continue

            center = item.invoice.center
            center_name = ((center.display_name or center.center_name)
                           if center else 'Unknown')
            bucket = bucket_by_ct.get(item.content_type_id)

            for member, share in zip(credited, shares):
                row = stats.setdefault(member.id, {
                    'name': f'{member.first_name or ""} {member.last_name or ""}'.strip(),
                    'centers': set(),
                    'revenue': ZERO,
                    'services': ZERO, 'products': ZERO,
                    'packages': ZERO, 'memberships': ZERO, 'cards': ZERO,
                })
                row['centers'].add(center_name)
                row['revenue'] += share
                if bucket:
                    row[bucket] += share

        results = [
            {
                'staff_id': staff_id,
                'staff_name': row['name'],
                'salon': ', '.join(sorted(row['centers'])) or 'Unknown',
                'revenue': money(row['revenue']),
                'services': money(row['services']),
                'products': money(row['products']),
                'packages': money(row['packages']),
                'memberships': money(row['memberships']),
                'cards': money(row['cards']),
                'service_red': money(0),
                'value_card_red': money(0),
                'gift_cards': money(0),
                'basis': 'gross sales',
            }
            for staff_id, row in stats.items()
        ]
        results.sort(key=lambda row: row['revenue'], reverse=True)
        return Response(results)



class SapReconciliationView(views.APIView):
    """POS against SAP, per branch: what the POS billed and how much of it SAP has.

    The difference is money that is billed but not (yet) in SAP - the first place
    to look when the books do not agree. It reads the POS's own posting state; it
    never calls SAP.
    """
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = "finance"
    permission_submodule = "register_summary"
    permission_strict_read = True

    def get(self, request):
        center_id = coerce_center_id(request.query_params.get('center_id'))
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        invoices = _get_filtered_invoices(request, center_id, start_date, end_date)

        rows = []
        totals = {'bills': 0, 'billed': ZERO, 'in_sap': ZERO, 'not_in_sap': ZERO}
        for row in (invoices.values('center_id', 'center__display_name', 'center__center_name')
                    .annotate(bills=Count('id'), billed=Sum('total_amount')).order_by('center__center_name')):
            posted = invoices.filter(center_id=row['center_id'], sap_sync_status=True)
            agg = posted.aggregate(n=Count('id'), amount=Sum('total_amount'))
            billed = money(row['billed'] or 0)
            in_sap = money(agg['amount'] or 0)
            rows.append({
                'center_id': row['center_id'],
                'center': row['center__display_name'] or row['center__center_name'],
                'bills': row['bills'], 'billed': billed,
                'bills_in_sap': agg['n'] or 0, 'in_sap': in_sap,
                'not_in_sap': money(billed - in_sap),
            })
            totals['bills'] += row['bills']
            totals['billed'] += billed
            totals['in_sap'] += in_sap
        totals['not_in_sap'] = money(totals['billed'] - totals['in_sap'])
        totals['billed'], totals['in_sap'] = money(totals['billed']), money(totals['in_sap'])
        return Response({'branches': rows, 'totals': totals})
