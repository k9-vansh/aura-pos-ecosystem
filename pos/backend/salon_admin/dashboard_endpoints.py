from decimal import Decimal
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from django.db.models import Sum, Count, Q, DateField, Max, Min, F, Exists, OuterRef
from django.db.models.functions import TruncDate, ExtractHour, ExtractYear, ExtractMonth, Cast, ExtractWeekDay
from datetime import datetime, timedelta
from django.utils import timezone
from datetime import date as date_type
import calendar

from billing.models import Invoice, InvoiceItem, Payment, AdvancePayment
from appointments.models import Appointment
from clients.models import Client, ClientMembership
from staff.models import StaffMember, ServiceLog
from services.models import ServiceMaster
from marketing.models import Membership, Package
from inventory.models import Product
from pos_backend.permissions import IsOwner, module_permission

# Each Dashboard tab is its own permission (Dashboard > Overview / Revenue / Clients /
# Finance / Services & Products / Staff), so a role can be given one tab and not the others
# (finding R2: they were open to any signed-in user). The Home page shows the same summary
# figures as the Overview tab, so Home access also grants read there.
DASHBOARD_TABS = ('summary', 'revenues', 'clients', 'finance', 'services_products', 'staff', 'usage_report')


def dashboard_tab(tab, also=()):
    """The permission a dashboard endpoint needs: its own tab's tick.

    `also` names other dashboard tabs whose tick opens the same endpoint: the Usage Report
    shows the Memberships and Value cards reports, so its tick must read them too, without
    opening anything else on the Finance tab."""
    assert tab in DASHBOARD_TABS, tab
    # Strict: holding one tab must not open the others (a read is otherwise
    # allowed by any tick in the module).
    options = dict(module='dashboard', submodule=tab, permission_strict_read=True)
    read_also = [('dashboard', other) for other in also]
    if tab == 'summary':
        read_also.append(('overview', 'home'))
    if read_also:
        options['permission_read_also'] = read_also
    return options


from pos_backend.scoping import coerce_center_id, scope_to_centers, safe_date_param, narrow_to_selection, selected_center_ids

# One definition of a line's sales value on every dashboard tab: its pre-tax total less its share of any
# bill-level discount (so the pieces add up to the Finance register's sales value).
LINE_VALUE = Sum(F('total_price') - F('bill_discount_share'))
# What a client spent on a bill, before tax and after discounts.
SPEND = Sum(F('subtotal') - F('discount'))


def _advance_pre_tax(qs):
    """Advances received in a ledger queryset, at their PRE-TAX value.

    With the 'charge GST on advances' setting on, the ledger credits the client what they paid, GST
    included, while the bill's tax total already holds that GST: counting the ledger figure whole
    counted the GST twice. The GST on a GST-inclusive credit is taken out here.
    """
    a = qs.filter(amount__gt=0).aggregate(t=Sum('amount'), g=Sum('gst_amount', filter=Q(gst_inclusive=True)))
    return Decimal(str(a['t'] or 0)) - Decimal(str(a['g'] or 0))


def _first_visits(request, client_ids):
    """{client_id: when their first settled bill was} within the user's centre selection.

    "New" on every tab means a first visit here (no earlier settled bill in the selection), not
    "registered in the period" - a client is often registered days before they first pay.
    """
    qs = _apply_security(request, Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, client_id__in=client_ids))
    return {r['client']: r['first'] for r in qs.values('client').annotate(first=Min('created_at'))}

def _apply_security(request, queryset, model_type='invoice'):
    user = request.user
    perms = getattr(user.role, 'permissions', {}) or {}
    is_owner = IsOwner.check_is_owner(user)
    
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            if model_type == 'staff':
                queryset = queryset.filter(center__in=user.centers.all())
            else:
                queryset = queryset.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            if model_type == 'staff':
                queryset = queryset.filter(center=user.center)
            else:
                queryset = queryset.filter(center=user.center)
    
        else:
            # Fail closed: a user assigned to no center sees nothing.
            queryset = queryset.none()
    # A malformed center_id used to reach the ORM and raise ValueError - a 500
    # from a stale bookmark or a frontend glitch. coerce_center_id returns None
    # for 'null', 'undefined' and anything non-numeric.
    center_id = coerce_center_id(request.GET.get('center_id'))
    if center_id is not None:
        queryset = queryset.filter(center_id=center_id)
    # Several centres ticked in the location dropdown.
    queryset = narrow_to_selection(queryset, 'center')

    return queryset

def _scope_advances(request, queryset):
    """Advance rows the signed-in user may see, narrowed to ?center_id when given."""
    from pos_backend.scoping import advances_in_centers
    from billing.wallets import advance_rows_that_count
    queryset = advance_rows_that_count(queryset)
    user = request.user
    perms = getattr(user.role, 'permissions', {}) or {}
    center_id = coerce_center_id(request.GET.get('center_id'))
    if center_id is not None:
        return advances_in_centers(queryset, [center_id])
    if selected_center_ids():
        queryset = advances_in_centers(queryset, selected_center_ids())
    if not IsOwner.check_is_owner(user) and not perms.get('all_centers', False):
        if user.centers.exists():
            return advances_in_centers(queryset, user.centers.values_list('id', flat=True))
        if getattr(user, 'center', None):
            return advances_in_centers(queryset, [user.center.id])
        return queryset.none()
    return queryset


def _payment_buckets(invoices):
    """What the bills were paid with: {bucket: {'amount', 'count'}}, tips left out."""
    from billing import payment_methods as pm
    out = {}
    rows = (Payment.objects.filter(invoice__in=invoices, is_tip=False)
            .values('payment_method').annotate(total=Sum('amount'), n=Count('id')))
    for r in rows:
        b = out.setdefault(pm.report_bucket(r['payment_method']), {'amount': Decimal('0.00'), 'count': 0})
        b['amount'] += Decimal(str(r['total'] or 0))
        b['count'] += r['n']
    return out


def _redeemed_by(invoices, dim):
    """Money paid out of a value card, advance or cashback wallet on these bills,
    keyed by month (year, month), day (date) or hour.

    Those payments are not new money: the card or advance was paid for when it
    was sold, and that sale is already in the bills. Revenue that counted both
    the sale and the services it later paid for counted the same rupees twice.
    """
    from billing import payment_methods as pm
    when = {'month': [ExtractYear('invoice__created_at'), ExtractMonth('invoice__created_at')],
            'day': [Cast('invoice__created_at', DateField())],
            'hour': [ExtractHour('invoice__created_at')]}[dim]
    names = [f'k{i}' for i in range(len(when))]
    qs = Payment.objects.filter(invoice__in=invoices, is_tip=False)
    for name, expr in zip(names, when):
        qs = qs.annotate(**{name: expr})
    out = {}
    for r in qs.values('payment_method', *names).annotate(total=Sum('amount')):
        if not pm.is_liability_drawdown(r['payment_method']):
            continue
        key = tuple(r[n] for n in names)
        key = key[0] if len(key) == 1 else key
        out[key] = out.get(key, Decimal('0.00')) + Decimal(str(r['total'] or 0))
    return out


def _outstanding_package_liability(client_packages):
    """Value of package services a client has bought but not yet consumed.

    Prices each remaining service at its current catalog price. Custom packages
    (no template) are included, which a `package__price` aggregate could not do.

    Returns {'total': Decimal, 'count': int}.
    """
    from services.models import ServiceMaster

    packages = list(client_packages)
    if not packages:
        return {'total': Decimal('0.00'), 'count': 0}

    # One query for every service id referenced across all packages.
    service_ids = set()
    for cp in packages:
        for svc_id in (cp.services_remaining or {}):
            try:
                service_ids.add(int(svc_id))
            except (TypeError, ValueError):
                continue

    prices = dict(
        ServiceMaster.objects.filter(id__in=service_ids)
        .values_list('id', 'default_price')
    )

    total = Decimal('0.00')
    for cp in packages:
        for svc_id, remaining in (cp.services_remaining or {}).items():
            try:
                count = int(Decimal(str(remaining)))
                price = Decimal(str(prices.get(int(svc_id), 0) or 0))
            except (TypeError, ValueError, ArithmeticError):
                continue
            if count > 0 and price > 0:
                total += price * count

    return {'total': total.quantize(Decimal('0.01')), 'count': len(packages)}


def parse_report_date(raw):
    """Parse a YYYY-MM-DD query param into a date, or None.

    The old code only guarded the strptime path. On the DateField branch it
    passed the raw string straight to the ORM, so `?start_date=nope` raised
    Django's ValidationError - a 500 rather than a graceful response.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), '%Y-%m-%d').date()
    except (TypeError, ValueError):
        return None


def _apply_dates(request, queryset, date_field='created_at', is_datetime=True):
    start = parse_report_date(safe_date_param(request.GET.get('start_date')))
    end = parse_report_date(safe_date_param(request.GET.get('end_date')))

    if start:
        queryset = queryset.filter(**{f'{date_field}__gte': start})

    if end:
        if is_datetime:
            # Exclusive upper bound on the next day, so the whole end date is
            # included whatever the stored time precision.
            queryset = queryset.filter(**{f'{date_field}__lt': end + timedelta(days=1)})
        else:
            queryset = queryset.filter(**{f'{date_field}__lte': end})

    return queryset

@module_permission(**dashboard_tab('summary'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES).select_related('client')
    invoices = _apply_security(request, invoices)
    invoices = _apply_dates(request, invoices, 'created_at')
    
    clients = Client.objects.all()
    clients = _apply_security(request, clients)
    clients_period = _apply_dates(request, clients, 'created_at')
    
    bill_agg = invoices.aggregate(
        billed=Sum('total_amount'), collected=Sum('paid_amount'), bills=Count('id'),
        cgst=Sum('cgst'), sgst=Sum('sgst'), igst=Sum('igst'), rounding=Sum('rounding'),
        bill_discount=Sum('discount'))
    gross_billed = Decimal(str(bill_agg['billed'] or 0))
    payment_buckets = _payment_buckets(invoices)
    redeemed = {k: payment_buckets.get(b, {}).get('amount', Decimal('0.00'))
                for k, b in (('advance', 'advance'), ('value_card', 'value_card'),
                             ('cashback', 'cashback_wallet'))}
    redeemed['total'] = sum(redeemed.values(), Decimal('0.00'))
    # Revenue is what the bills brought in, less what was paid out of cards and
    # advances that had already been counted when they were sold. Same figure as
    # Finance > Register Summary > Including Tax.
    total_revenue = gross_billed - redeemed['total']
    
    # Dynamic Monthly Target based on selected center(s)
    from salon_admin.models import Center
    centers_qs = Center.objects.all()
    user = request.user
    is_owner = IsOwner.check_is_owner(user)
    perms = getattr(user.role, 'permissions', {}) or {}
    
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            centers_qs = centers_qs.filter(id__in=user.centers.values_list('id', flat=True))
        elif hasattr(user, 'center') and user.center:
            centers_qs = centers_qs.filter(id=user.center.id)
            
        else:
            # Fail closed: a user assigned to no center sees nothing.
            centers_qs = centers_qs.none()
    center_id = coerce_center_id(request.GET.get('center_id'))
    if center_id and center_id != 'null':
        centers_qs = centers_qs.filter(id=center_id)
    elif selected_center_ids():
        centers_qs = centers_qs.filter(id__in=list(selected_center_ids()))
        
    target_month_key = None
    start_date_str = safe_date_param(request.GET.get('start_date'))
    if start_date_str:
        try:
            target_date = datetime.strptime(start_date_str, '%Y-%m-%d')
            target_month_key = target_date.strftime('%b-%Y') # e.g. "Jul-2026"
        except Exception:
            import logging; logging.getLogger(__name__).error('Handled exception', exc_info=True)
            
    monthly_target = 0
    for center in centers_qs:
        history = center.monthly_targets_history or {}
        val = 0
        if target_month_key and target_month_key in history:
            try:
                val = Decimal(str(history[target_month_key]))
            except (ValueError, TypeError):
                pass
                
        if val == 0:
            val = Decimal(str(center.monthly_target or 0))
            
        monthly_target += val
    # ─── Revenue projection ───────────────────────────────────────────────
    # Extrapolate the run rate to the END OF THE SELECTED RANGE, not to a
    # calendar month.
    #
    # The old version had two defects:
    #   * `days_elapsed = max((today - sd).days, 1)` yielded 1 for a start date
    #     in the FUTURE, so selecting next month multiplied the revenue by the
    #     whole month length.
    #   * A 7-day range was projected onto `calendar.monthrange(...)` = 30 days,
    #     mixing two different period lengths in one figure.
    projected = Decimal(str(total_revenue))
    projection_basis = 'actual'
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))

    if start_date_str and end_date_str and total_revenue > 0:
        try:
            sd = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            ed = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            today = date_type.today()

            if ed < sd:
                # Inverted range: nothing sensible to project.
                projection_basis = 'actual'
            elif ed <= today:
                # The period is complete; the actual figure IS the total.
                projection_basis = 'actual'
            elif sd > today:
                # Entirely in the future. There is no run rate to extrapolate
                # from, so do not invent one.
                projection_basis = 'actual'
            else:
                # Partway through. Scale the observed daily rate across the
                # selected range, inclusive of both endpoints.
                days_elapsed = (today - sd).days + 1
                days_in_range = (ed - sd).days + 1
                if days_elapsed > 0 and days_in_range > days_elapsed:
                    daily_rate = Decimal(str(total_revenue)) / Decimal(days_elapsed)
                    projected = (daily_rate * Decimal(days_in_range)).quantize(Decimal('0.01'))
                    projection_basis = f'{days_elapsed} of {days_in_range} days elapsed'
        except (ValueError, TypeError):
            import logging
            logging.getLogger(__name__).warning(
                '[Dashboard] Could not project revenue for range %s..%s',
                start_date_str, end_date_str,
            )
    
    # Client metrics based strictly on invoiced clients
    # Counted from the bills themselves: a client registered at one centre who is billed at another is
    # still a client of the centre that billed them.
    invoiced_ids = set(invoices.exclude(client__isnull=True).values_list('client', flat=True).distinct())
    total_invoiced_clients = len(invoiced_ids)

    start_date = safe_date_param(safe_date_param(request.GET.get('start_date')), '2000-01-01')
    end_date = safe_date_param(safe_date_param(request.GET.get('end_date')), '2099-12-31')
    first_seen = _first_visits(request, list(invoiced_ids))
    period_start = parse_report_date(start_date)
    new_clients = sum(1 for c in invoiced_ids
                      if first_seen.get(c) and (period_start is None or first_seen[c].date() >= period_start))
    # A walk-in bill (no client record) is one new unknown visitor, so the Overview agrees with the Clients tab.
    walk_in_bills = invoices.filter(client__isnull=True).count()
    new_clients += walk_in_bills
    total_invoiced_clients += walk_in_bills
    repeat_clients = total_invoiced_clients - new_clients

    # Inventory - use DB aggregation instead of Python loop
    from django.db.models import F
    from django.db.models.functions import Coalesce
    products = Product.objects.all()
    products = _apply_security(request, products)
    inv_agg = products.aggregate(
        amount=Sum(Coalesce(F('current_stock'), Decimal('0')) * Coalesce(F('price'), Decimal('0.0'))),
        count=Sum('current_stock')
    )
    inv_amount = Decimal(str(inv_agg['amount'] or 0))
    inv_count = int(inv_agg['count'] or 0)
    
    # Invoices over weekdays (Sun-Sat)
    invoices_period = _apply_dates(request, invoices, 'created_at')
    weekday_inv = invoices_period.annotate(dow=ExtractWeekDay('created_at')).values('dow').annotate(count=Count('id')).order_by('dow')
    weekday_counts_list = [0]*7
    for item in weekday_inv:
        dow = item.get('dow')
        if dow and 1 <= dow <= 7:
            weekday_counts_list[dow-1] = item['count']
            
    # Revenue Breakdown
    items_period = InvoiceItem.objects.filter(invoice__in=invoices_period, is_redemption=False).select_related('content_type')
    breakdown_qs = items_period.values('content_type__model').annotate(revenue=LINE_VALUE)
    revenue_breakdown = { 'service': 0, 'product': 0, 'membership': 0, 'package': 0, 'card': 0, 'advance': 0 }
    for b in breakdown_qs:
        model = b['content_type__model']
        if model == 'servicemaster': revenue_breakdown['service'] += Decimal(str(b['revenue'] or 0))
        elif model == 'product': revenue_breakdown['product'] += Decimal(str(b['revenue'] or 0))
        elif model == 'membership': revenue_breakdown['membership'] += Decimal(str(b['revenue'] or 0))
        elif model == 'package': revenue_breakdown['package'] += Decimal(str(b['revenue'] or 0))
        elif model == 'valuecard': revenue_breakdown['card'] += Decimal(str(b['revenue'] or 0))
        
    from billing.models import AdvancePayment
    # Advance MONEY RECEIVED, not the net ledger: money later spent from an
    # advance is already inside the bills it paid, so adding the negative rows
    # here took it off a second time.
    advances_period = _scope_advances(request, AdvancePayment.objects.filter(amount__gt=0))
    advances_period = _apply_dates(request, advances_period, 'created_at')
    revenue_breakdown['advance'] += _advance_pre_tax(advances_period)
        
    # Balances and Memberships
    from clients.models import ClientValueCard, ClientPackage, ClientMembership
    from billing.models import AdvancePayment
    
    centers_ids = list(centers_qs.values_list('id', flat=True))

    # Cards, packages and advances are held with the centre that sold them (as on the Value Cards tab);
    # the client's own centre only stands in when a row records none.
    held_by = Q(center_id__in=centers_ids) | Q(center__isnull=True, client__center_id__in=centers_ids)

    # A card still counts only while it is unexpired and has something left: an expired card's balance can
    # no longer be redeemed (it is forfeited), so it is not a liability.
    active_vcs = ClientValueCard.objects.filter(held_by, is_active=True, expiry_date__gte=date_type.today(), balance__gt=0)
    vc_agg = active_vcs.aggregate(total=Sum('balance'), count=Count('id'))
    
    # `today` is only bound inside the projection branch above, so bind it here
    # rather than depending on a conditional.
    liability_cutoff = date_type.today()

    # EXPIRED packages are not a liability: the customer can no longer redeem
    # them. Without the expiry bound this loaded every package ever issued that
    # was still flagged active, across every branch, and hydrated each one as a
    # model instance to price it - so the figure was wrong AND the query grew
    # without limit. `only()` because only these three fields are read.
    active_pkgs = (
        ClientPackage.objects
        .filter(held_by,
                is_active=True,
                expiry_date__gte=liability_cutoff)
        .only('id', 'services_remaining', 'expiry_date')
    )
    # Outstanding package liability: the value of what the client has NOT yet
    # consumed, not the original purchase price.
    #
    # `Sum('package__price')` counted a package with 1 of 10 services left at
    # its full price, and excluded custom packages entirely (package=None) - so
    # the figure was overstated and incomplete at the same time.
    pkg_agg = _outstanding_package_liability(active_pkgs)
    
    # Advance held: what clients still have unspent (every ledger row counts toward a balance), and how
    # many clients hold some. It used to count ledger ROWS as if each were a client.
    adv_balances = {r['client']: r['bal'] for r in
                    AdvancePayment.objects.filter(held_by).values('client').annotate(bal=Sum('amount'))}
    held = {c: Decimal(str(b or 0)) for c, b in adv_balances.items() if (b or 0) > 0}
    advances_agg = {'total': sum(held.values(), Decimal('0')), 'count': len(held)}

    memberships_this_month = (ClientMembership.objects
                              .filter(held_by, created_at__date__gte=start_date, created_at__date__lte=end_date)
                              .exclude(source_invoice__status__in=list(Invoice.VOID_STATUSES)).count())
    
    balances = {
        'value_cards': {'count': vc_agg['count'] or 0, 'amount': Decimal(str(vc_agg['total'] or 0))},
        'services': {'count': pkg_agg['count'] or 0, 'amount': Decimal(str(pkg_agg['total'] or 0))},
        'advances': {'count': advances_agg['count'] or 0, 'amount': Decimal(str(advances_agg['total'] or 0))},
        'memberships': memberships_this_month
    }
    
    # Top Staff this month
    from staff.models import ServiceLog
    # ServiceLog.objects already excludes cancelled/refunded invoices. Adding
    # an explicit paid/partial filter also dropped manually recorded logs
    # (invoice=None), so those services earned nothing.
    logs_period = ServiceLog.objects.filter(date__gte=start_date, date__lte=end_date, center_id__in=centers_ids)
    top_staff_qs = logs_period.values('staff__first_name', 'staff__last_name').annotate(revenue=Sum('price')).order_by('-revenue')[:5]
    top_staff = [{'name': f"{s['staff__first_name']} {s['staff__last_name'] or ''}".strip(), 'revenue': Decimal(str(s['revenue'] or 0))} for s in top_staff_qs]

    
    # Top Services / Products — batch name lookup to avoid N+1 queries
    invoice_items = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False).select_related('content_type')

    services_qs = (
        invoice_items.filter(content_type__model='servicemaster')
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE, description=Max('description'))
        .order_by('-count')[:20]
    )
    top_services = [
        {'name': s['description'] or f'Service #{s["object_id"]}', 'count': int(s['count'] or 0), 'revenue': Decimal(str(s['revenue'] or 0))}
        for s in services_qs
    ]

    products_qs = (
        invoice_items.filter(content_type__model='product')
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE, description=Max('description'))
        .order_by('-count')[:20]
    )
    top_products = [
        {'name': p['description'] or f'Product #{p["object_id"]}', 'count': int(p['count'] or 0), 'revenue': Decimal(str(p['revenue'] or 0))}
        for p in products_qs
    ]

    memberships_qs = (
        invoice_items.filter(content_type__model='membership')
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE, description=Max('description'))
        .order_by('-count')[:20]
    )
    top_memberships = [
        {'name': m['description'] or f'Membership #{m["object_id"]}', 'count': int(m['count'] or 0), 'revenue': Decimal(str(m['revenue'] or 0))}
        for m in memberships_qs
    ]

    packages_qs = (
        invoice_items.filter(content_type__model='package')
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE, description=Max('description'))
        .order_by('-count')[:20]
    )
    top_packages = [
        {'name': p['description'] or f'Package #{p["object_id"]}', 'count': int(p['count'] or 0), 'revenue': Decimal(str(p['revenue'] or 0))}
        for p in packages_qs
    ]

    # Avg Client Spend — calculated in Python to avoid DB-level division errors with NULL clients
    # Spend is what the client consumed, so it stays at the full bill value.
    total_agg = invoices.aggregate(total=SPEND, clients=Count('client', distinct=True))
    all_spend = (Decimal(str(total_agg['total'] or 0)) / max(total_agg['clients'] or 1, 1))
    
    female_agg = invoices.filter(client__gender__iexact='Female').aggregate(total=SPEND, clients=Count('client', distinct=True))
    female_spend = (Decimal(str(female_agg['total'] or 0)) / max(female_agg['clients'] or 1, 1))
    
    male_agg = invoices.filter(client__gender__iexact='Male').aggregate(total=SPEND, clients=Count('client', distinct=True))
    male_spend = (Decimal(str(male_agg['total'] or 0)) / max(male_agg['clients'] or 1, 1))
    
    unknown_agg = invoices.exclude(client__gender__iexact='Female').exclude(client__gender__iexact='Male').aggregate(total=SPEND, clients=Count('client', distinct=True))
    unknown_spend = (Decimal(str(unknown_agg['total'] or 0)) / max(unknown_agg['clients'] or 1, 1))

    tax_agg = invoices.aggregate(cgst=Sum('cgst'), sgst=Sum('sgst'))
    tax_total = (tax_agg['cgst'] or 0) + (tax_agg['sgst'] or 0)
    # Without the tax AND without the paise each bill was rounded by, so it
    # matches the Register Summary's taxable figure.
    rounding_total = invoices.aggregate(r=Sum('rounding'))['r'] or 0
    revenue_without_tax = total_revenue - tax_total - rounding_total

    # ─── The rest of the money picture ────────────────────────────────────
    from billing import payment_methods as _pm
    line_agg = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False).aggregate(
        discount=Sum('discount'), card_benefit=Sum('card_benefit'))
    voided = _apply_dates(request, _apply_security(
        request, Invoice.objects.filter(status__in=Invoice.VOID_STATUSES)), 'created_at')
    void_agg = voided.aggregate(n=Count('id'), amount=Sum('total_amount'))
    bills = bill_agg['bills'] or 0
    real_money = sum((v['amount'] for k, v in payment_buckets.items()
                      if k not in _pm.LIABILITY_BUCKETS), Decimal('0.00'))
    money_in = {
        'bills': bills,
        'avg_bill': (gross_billed / bills).quantize(Decimal('0.01')) if bills else Decimal('0.00'),
        'gross_billed': gross_billed,
        'redeemed': redeemed,
        'real_money_received': real_money,
        'to_collect': max(gross_billed - Decimal(str(bill_agg['collected'] or 0)), Decimal('0.00')),
        'tax': {'cgst': Decimal(str(bill_agg['cgst'] or 0)), 'sgst': Decimal(str(bill_agg['sgst'] or 0)),
                'igst': Decimal(str(bill_agg['igst'] or 0))},
        'round_off': Decimal(str(bill_agg['rounding'] or 0)),
        'discounts': {
            'bill_level': Decimal(str(bill_agg['bill_discount'] or 0)),
            # Money taken off a line, not counting what a value card's bonus gave.
            'line_level': Decimal(str(line_agg['discount'] or 0)) - Decimal(str(line_agg['card_benefit'] or 0)),
        },
        'cancelled': {'count': void_agg['n'] or 0, 'amount': Decimal(str(void_agg['amount'] or 0))},
        'payments': [{'method': k, 'amount': v['amount'], 'count': v['count']}
                     for k, v in sorted(payment_buckets.items(), key=lambda kv: -kv[1]['amount'])
                     if v['amount']],
    }

    return Response({
        'revenue': total_revenue,
        'money_in': money_in,
        'revenue_without_tax': revenue_without_tax,
        'target': monthly_target,
        'projected': projected,
        'new_clients': new_clients,
        'repeat_clients': repeat_clients,
        'total_clients': total_invoiced_clients,
        'inventory': {
            'amount': inv_amount,
            'count': inv_count
        },
        'weekday_counts': weekday_counts_list,
        'revenue_breakdown': revenue_breakdown,
        'balances': balances,
        'top_staff': top_staff,
        'top_services': top_services,
        'top_products': top_products,
        'top_memberships': top_memberships,
        'top_packages': top_packages,
        'avg_spend': {
            'all': Decimal(str(all_spend)),
            'female': Decimal(str(female_spend)),
            'male': Decimal(str(male_spend)),
            'unknown': Decimal(str(unknown_spend))
        }
    })

@module_permission(**dashboard_tab('revenues'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_revenues(request):
    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES)
    invoices = _apply_security(request, invoices)
    
    # 6 months of monthly revenue — ExtractYear+ExtractMonth avoids TruncMonth crash on USE_TZ=False
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    if start_date_str:
        try:
            trend_start = datetime.strptime(start_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_start = timezone.now() - timedelta(days=180)
    else:
        trend_start = timezone.now() - timedelta(days=180)

    if end_date_str:
        try:
            trend_end = datetime.strptime(end_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_end = timezone.now()
    else:
        trend_end = timezone.now()

    six_months_ago = trend_start

    monthly_raw = (
        invoices
        .filter(created_at__gte=six_months_ago, created_at__lte=trend_end + timedelta(days=1))
        .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
        .values('year', 'month_num')
        .annotate(revenue=Sum('total_amount'))
        .order_by('year', 'month_num')
    )
    monthly_dict = {
        f"{calendar.month_abbr[item['month_num']]}-{item['year']}": Decimal(str(item['revenue'] or 0))
        for item in monthly_raw
    }
    # Net of what cards and advances paid, as in the headline figure.
    in_window = invoices.filter(created_at__gte=six_months_ago, created_at__lte=trend_end + timedelta(days=1))
    for (yr, mo), amt in _redeemed_by(in_window, 'month').items():
        key = f"{calendar.month_abbr[mo]}-{yr}"
        if key in monthly_dict:
            monthly_dict[key] -= amt
    monthly_data = []
    curr_m = six_months_ago
    target_end_m = trend_end.replace(day=1)
    months_keys = []
    while curr_m.replace(day=1) <= target_end_m:
        months_keys.append(f"{calendar.month_abbr[curr_m.month]}-{curr_m.year}")
        nm = curr_m.month + 1
        ny = curr_m.year
        if nm > 12:
            nm = 1
            ny += 1
        curr_m = curr_m.replace(year=ny, month=nm, day=1)
        if len(months_keys) > 24: # safety break
            break

    for mk in months_keys:
        monthly_data.append({'month': mk, 'revenue': monthly_dict.get(mk, 0)})
    
    # Daily for selected period
    invoices_period = _apply_dates(request, invoices, 'created_at')
    daily = invoices_period.annotate(day=Cast('created_at', DateField())).values('day').annotate(revenue=Sum('total_amount')).order_by('day')
    daily_dict = {item['day'].strftime('%Y-%m-%d') if hasattr(item['day'], 'strftime') else str(item['day']): Decimal(str(item['revenue'] or 0)) for item in daily if item['day']}
    for day, amt in _redeemed_by(invoices_period, 'day').items():
        key = day.strftime('%Y-%m-%d') if hasattr(day, 'strftime') else str(day)
        if key in daily_dict:
            daily_dict[key] -= amt
    
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    daily_data = []
    if start_date_str and end_date_str:
        try:
            s_d = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            e_d = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            if (e_d - s_d).days <= 366:
                curr = s_d
                while curr <= e_d:
                    ds = curr.strftime('%Y-%m-%d')
                    daily_data.append({'day': ds, 'revenue': daily_dict.get(ds, 0)})
                    curr += timedelta(days=1)
            else:
                daily_data = [{'day': k, 'revenue': v} for k, v in daily_dict.items()]
        except Exception:
            daily_data = [{'day': k, 'revenue': v} for k, v in daily_dict.items()]
    else:
        daily_data = [{'day': k, 'revenue': v} for k, v in daily_dict.items()]
    
    # Hourly across the selected period
    hourly = invoices_period.annotate(hour=ExtractHour('created_at')).values('hour').annotate(revenue=Sum('total_amount')).order_by('hour')
    hourly_dict = {item['hour']: Decimal(str(item['revenue'] or 0)) for item in hourly if item['hour'] is not None}
    for hr, amt in _redeemed_by(invoices_period, 'hour').items():
        if hr in hourly_dict:
            hourly_dict[hr] -= amt
    hourly_data = [{'hour': f"{h:02d}:00", 'revenue': hourly_dict.get(h, 0)} for h in range(24)]

    return Response({
        'monthly': monthly_data,
        'daily': daily_data,
        'hourly': hourly_data
    })

@module_permission(**dashboard_tab('clients'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_clients(request):
    clients = Client.objects.all()
    clients = _apply_security(request, clients)
    
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    if start_date_str:
        try:
            trend_start = datetime.strptime(start_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_start = timezone.now() - timedelta(days=180)
    else:
        trend_start = timezone.now() - timedelta(days=180)

    if end_date_str:
        try:
            trend_end = datetime.strptime(end_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_end = timezone.now()
    else:
        trend_end = timezone.now()

    six_months_ago = trend_start

    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, created_at__gte=six_months_ago, created_at__lte=trend_end + timedelta(days=1)).select_related('client')
    invoices = _apply_security(request, invoices)

    monthly_raw = (
        invoices
        .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
        .values('year', 'month_num', 'client__gender')
        .annotate(revenue=SPEND, count=Count('client', distinct=True),
                  walk_ins=Count('id', filter=Q(client__isnull=True)))
        .order_by('year', 'month_num')
    )

    trends = {}
    curr_m = six_months_ago
    target_end_m = trend_end.replace(day=1)
    months_keys = []
    while curr_m.replace(day=1) <= target_end_m:
        months_keys.append(f"{calendar.month_abbr[curr_m.month]}-{curr_m.year}")
        nm = curr_m.month + 1
        ny = curr_m.year
        if nm > 12:
            nm = 1
            ny += 1
        curr_m = curr_m.replace(year=ny, month=nm, day=1)
        if len(months_keys) > 24: # safety break
            break

    for mk in months_keys:
        trends[mk] = {
            'female': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}, 
            'male': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}, 
            'unknown': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}
        }

    for item in monthly_raw:
        m = f"{calendar.month_abbr[item['month_num']]}-{item['year']}"
        if m not in trends:
            trends[m] = {
                'female': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}, 
                'male': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}, 
                'unknown': {'revenue': 0, 'count': 0, 'new': 0, 'repeat': 0}
            }

        g = (item['client__gender'] or '').lower()
        if g == 'female':
            trends[m]['female']['revenue'] += Decimal(str(item['revenue'] or 0))
            trends[m]['female']['count'] += item['count']
        elif g == 'male':
            trends[m]['male']['revenue'] += Decimal(str(item['revenue'] or 0))
            trends[m]['male']['count'] += item['count']
        else:
            trends[m]['unknown']['revenue'] += Decimal(str(item['revenue'] or 0))
            # A bill with no client attached (a walk-in) is one unknown visitor.
            trends[m]['unknown']['count'] += item['count'] + item['walk_ins']

    # Monthly footfall matrix
    invoices_period = _apply_dates(request, invoices, 'created_at')

    start_date = safe_date_param(safe_date_param(request.GET.get('start_date')), '2000-01-01')
    end_date = safe_date_param(safe_date_param(request.GET.get('end_date')), '2099-12-31')
    period_start = parse_report_date(start_date)

    def gender_of(raw):
        g = (raw or '').strip().lower()
        return g if g in ('female', 'male') else 'unknown'

    # "New" is a first visit here (no earlier settled bill in the selection), the same on every tab.
    # Clients are counted from the bills, so one registered at another centre but billed here counts.
    first_seen = _first_visits(request, invoices.exclude(client__isnull=True).values('client'))

    period_pairs = list(invoices_period.exclude(client__isnull=True).values('client', 'client__gender').distinct())
    new_by_g = {'female': 0, 'male': 0, 'unknown': 0}
    total_by_g = {'female': 0, 'male': 0, 'unknown': 0}
    for row in period_pairs:
        g = gender_of(row['client__gender'])
        total_by_g[g] += 1
        first = first_seen.get(row['client'])
        if first and (period_start is None or first.date() >= period_start):
            new_by_g[g] += 1
    # A walk-in bill has no client record, so it cannot be tied to an earlier visit: it counts as one
    # new unknown visitor.
    walk_in_period = invoices_period.filter(client__isnull=True).count()
    new_by_g['unknown'] += walk_in_period
    total_by_g['unknown'] += walk_in_period
    rep_by_g = {g: total_by_g[g] - new_by_g[g] for g in total_by_g}
    footfall = {
        'new': {**new_by_g, 'total': sum(new_by_g.values())},
        'repeat': {**rep_by_g, 'total': sum(rep_by_g.values())},
        'total': {**total_by_g, 'total': sum(total_by_g.values())},
    }

    # A member bill is one where the client held a membership on the day of the bill.
    held = (ClientMembership.objects
            .filter(client=OuterRef('client'),
                    start_date__lte=Cast(OuterRef('created_at'), DateField()),
                    expiry_date__gte=Cast(OuterRef('created_at'), DateField()))
            .exclude(source_invoice__status__in=list(Invoice.VOID_STATUSES)))
    monthly_stats = (invoices
                     .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'), is_member=Exists(held))
                     .values('year', 'month_num')
                     .annotate(total_invoices=Count('id'), member_invoices=Count('id', filter=Q(is_member=True))))

    monthly_breakdown = {}
    for mk in months_keys:
        monthly_breakdown[mk] = {'new': 0, 'repeat': 0, 'member': 0, 'non_member': 0}
    for stat in monthly_stats:
        m = f"{calendar.month_abbr[stat['month_num']]}-{stat['year']}"
        if m in monthly_breakdown:
            monthly_breakdown[m]['member'] = stat['member_invoices']
            monthly_breakdown[m]['non_member'] = stat['total_invoices'] - stat['member_invoices']

    # New vs repeat clients in each month, overall and by gender
    month_pairs = (invoices.exclude(client__isnull=True)
                   .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
                   .values('year', 'month_num', 'client', 'client__gender').distinct())
    for row in month_pairs:
        m = f"{calendar.month_abbr[row['month_num']]}-{row['year']}"
        first = first_seen.get(row['client'])
        is_new = bool(first and (first.year, first.month) == (row['year'], row['month_num']))
        side = 'new' if is_new else 'repeat'
        if m in monthly_breakdown:
            monthly_breakdown[m][side] += 1
        if m in trends:
            trends[m][gender_of(row['client__gender'])][side] += 1

    walk_in_months = (invoices.filter(client__isnull=True)
                      .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
                      .values('year', 'month_num').annotate(n=Count('id')))
    for row in walk_in_months:
        m = f"{calendar.month_abbr[row['month_num']]}-{row['year']}"
        if m in monthly_breakdown:
            monthly_breakdown[m]['new'] += row['n']
        if m in trends:
            trends[m]['unknown']['new'] += row['n']

    # Daily footfall
    from django.db.models.functions import TruncDate
    daily_footfall_qs = (
        invoices_period
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(count=Count('client', distinct=True) + Count('id', filter=Q(client__isnull=True)))
        .order_by('date')
    )
    daily_footfall = [{'day': d['date'].strftime('%Y-%m-%d'), 'count': d['count']} for d in daily_footfall_qs]

    return Response({
        'trends': trends,
        'footfall': footfall,
        'monthly_breakdown': monthly_breakdown,
        'daily_footfall': daily_footfall
    })

@module_permission(**dashboard_tab('finance'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_finance(request):
    start_date = safe_date_param(request.GET.get('start_date'))
    end_date = safe_date_param(request.GET.get('end_date'))

    # Originally this just fetched 6 months unconditionally. We should ideally respect dates, 
    # but let's stick to the 7-month approach they had to not break existing logic if they rely on it.
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    if start_date_str:
        try:
            trend_start = datetime.strptime(start_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_start = timezone.now() - timedelta(days=180)
    else:
        trend_start = timezone.now() - timedelta(days=180)

    if end_date_str:
        try:
            trend_end = datetime.strptime(end_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_end = timezone.now()
    else:
        trend_end = timezone.now()

    six_months_ago = trend_start

    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES, created_at__gte=six_months_ago, created_at__lte=trend_end + timedelta(days=1))
    invoices = _apply_security(request, invoices)

    items = InvoiceItem.objects.filter(invoice__in=invoices, is_redemption=False).select_related('content_type')
    
    monthly_raw = (
        items
        .annotate(year=ExtractYear('invoice__created_at'), month_num=ExtractMonth('invoice__created_at'))
        .values('year', 'month_num', 'content_type__model')
        .annotate(revenue=LINE_VALUE, count=Count('id'))
        .order_by('year', 'month_num')
    )

    sources = {}
    curr_m = six_months_ago
    target_end_m = trend_end.replace(day=1)
    months_keys = []
    while curr_m.replace(day=1) <= target_end_m:
        months_keys.append(f"{calendar.month_abbr[curr_m.month]}-{curr_m.year}")
        nm = curr_m.month + 1
        ny = curr_m.year
        if nm > 12:
            nm = 1
            ny += 1
        curr_m = curr_m.replace(year=ny, month=nm, day=1)
        if len(months_keys) > 24: # safety break
            break

    for mk in months_keys:
        sources[mk] = {
            'services': {'revenue': 0, 'count': 0},
            'products': {'revenue': 0, 'count': 0},
            'memberships': {'revenue': 0, 'count': 0},
            'packages': {'revenue': 0, 'count': 0},
            'value_cards': {'revenue': 0, 'count': 0},
            'advances': {'revenue': 0, 'count': 0}
        }

    for item in monthly_raw:
        m = f"{calendar.month_abbr[item['month_num']]}-{item['year']}"
        if m not in sources:
            continue

        model = item['content_type__model']
        rev = Decimal(str(item['revenue'] or 0))
        cnt = item['count'] or 0
        
        if model == 'servicemaster': 
            sources[m]['services']['revenue'] += rev
            sources[m]['services']['count'] += cnt
        elif model == 'product': 
            sources[m]['products']['revenue'] += rev
            sources[m]['products']['count'] += cnt
        elif model == 'membership': 
            sources[m]['memberships']['revenue'] += rev
            sources[m]['memberships']['count'] += cnt
        elif model == 'package': 
            sources[m]['packages']['revenue'] += rev
            sources[m]['packages']['count'] += cnt
        elif model == 'valuecard':
            sources[m]['value_cards']['revenue'] += rev
            sources[m]['value_cards']['count'] += cnt

    # Advance Payments: money received, scoped to the branch that took it.
    from billing.models import AdvancePayment
    advances_qs = _scope_advances(request, AdvancePayment.objects.filter(
        amount__gt=0, created_at__gte=six_months_ago, created_at__lte=trend_end + timedelta(days=1)))
    adv_raw = (
        advances_qs
        .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
        .values('year', 'month_num')
        .annotate(revenue=Sum('amount'), gst=Sum('gst_amount', filter=Q(gst_inclusive=True)), count=Count('id'))
        .order_by('year', 'month_num')
    )

    for item in adv_raw:
        m = f"{calendar.month_abbr[item['month_num']]}-{item['year']}"
        if m in sources:
            # pre-tax, as on the Revenue tab (the GST in a GST-inclusive credit is counted through the tax)
            sources[m]['advances']['revenue'] += Decimal(str(item['revenue'] or 0)) - Decimal(str(item['gst'] or 0))
            sources[m]['advances']['count'] += item['count'] or 0

    return Response(sources)

@module_permission(**dashboard_tab('staff'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_staff(request):
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    if start_date_str:
        try:
            trend_start = datetime.strptime(start_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_start = timezone.now() - timedelta(days=180)
    else:
        trend_start = timezone.now() - timedelta(days=180)

    if end_date_str:
        try:
            trend_end = datetime.strptime(end_date_str, '%Y-%m-%d')
        except (TypeError, ValueError):
            trend_end = timezone.now()
    else:
        trend_end = timezone.now()

    six_months_ago = trend_start


    logs = ServiceLog.objects.filter(date__gte=six_months_ago.date(), date__lte=trend_end.date()).select_related('staff', 'invoice', 'invoice__client')
    # ServiceLog has no direct 'center' FK — scope via staff__center
    user = request.user
    role = getattr(user, 'role', None)
    perms = getattr(role, 'permissions', {}) if role else {}
    is_owner = IsOwner.check_is_owner(user)
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            logs = logs.filter(staff__center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            logs = logs.filter(staff__center=user.center)
        else:
            # Fail closed: a user assigned to no center sees nothing.
            logs = logs.none()
    center_id = coerce_center_id(request.GET.get('center_id'))
    if center_id and center_id != 'null':
        logs = logs.filter(staff__center_id=center_id)
    elif selected_center_ids():
        logs = logs.filter(staff__center_id__in=list(selected_center_ids()))

    # Trends: group by year+month on DateField — safe with USE_TZ=False
    monthly_raw = (
        logs
        .annotate(year=ExtractYear('date'), month_num=ExtractMonth('date'))
        .values('year', 'month_num')
        .annotate(revenue=Sum('price'))
        .order_by('year', 'month_num')
    )
    trends_dict = {
        f"{calendar.month_abbr[item['month_num']]}-{item['year']}": Decimal(str(item['revenue'] or 0))
        for item in monthly_raw
    }
    trends = []
    curr_m = six_months_ago
    target_end_m = trend_end.replace(day=1)
    months_keys = []
    while curr_m.replace(day=1) <= target_end_m:
        months_keys.append(f"{calendar.month_abbr[curr_m.month]}-{curr_m.year}")
        nm = curr_m.month + 1
        ny = curr_m.year
        if nm > 12:
            nm = 1
            ny += 1
        curr_m = curr_m.replace(year=ny, month=nm, day=1)
        if len(months_keys) > 24: # safety break
            break

    for mk in months_keys:
        trends.append({'month': mk, 'revenue': trends_dict.get(mk, 0)})
    
    # Table (for period)
    logs_period = _apply_dates(request, logs, 'date', is_datetime=False)
    staff_data = logs_period.values('staff__first_name', 'staff__last_name').annotate(
        revenue=Sum('price'), clients=Count('invoice__client', distinct=True)
    ).order_by('-revenue')
    
    table = []
    for s in staff_data:
        name = f"{s['staff__first_name']} {s['staff__last_name'] or ''}".strip()
        rev = Decimal(str(s['revenue'] or 0))
        clients = s['clients']
        avg = rev / clients if clients > 0 else 0
        table.append({
            'name': name,
            'revenue': rev,
            'clients': clients,
            'avg': avg
        })
        
    return Response({
        'trends': trends,
        'table': table
    })

@module_permission(**dashboard_tab('services_products'))
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_services_products(request):
    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES)
    invoices = _apply_security(request, invoices)
    invoices = _apply_dates(request, invoices, 'created_at')

    # Use DB-level aggregation instead of Python iteration to avoid N+1 queries
    from django.db.models import F as DbF
    items_qs = (
        InvoiceItem.objects
        .filter(invoice__in=invoices)
        .select_related('content_type')
    )

    services_raw = (
        items_qs.filter(content_type__app_label='services')
        .values(cat=DbF('description'))  # use description as category proxy
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE)
        .order_by('-revenue')
    )
    # Re-aggregate by service category using content_type model name
    from billing.models import InvoiceItem as II
    from django.contrib.contenttypes.models import ContentType
    try:
        svc_ct = ContentType.objects.get(app_label='services', model='servicemaster')
        prod_ct = ContentType.objects.get(app_label='inventory', model='product')
    except ContentType.DoesNotExist:
        return Response({'services': [], 'products': []})

    # Services — group by object_id
    svc_agg = (
        InvoiceItem.objects.filter(invoice__in=invoices, content_type=svc_ct)
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE)
        .order_by('-revenue')
    )
    from services.models import ServiceMaster
    svc_map = ServiceMaster.objects.in_bulk([s['object_id'] for s in svc_agg if s['object_id']])
    services_list = []
    for s in svc_agg:
        oid = s['object_id']
        name = svc_map[oid].name if oid and oid in svc_map else 'Unknown'
        services_list.append({'name': name, 'count': int(s['count'] or 0), 'revenue': Decimal(str(s['revenue'] or 0))})

    # Products — group by object_id
    prod_agg = (
        InvoiceItem.objects.filter(invoice__in=invoices, content_type=prod_ct)
        .values('object_id')
        .annotate(count=Sum('quantity'), revenue=LINE_VALUE)
        .order_by('-revenue')
    )
    from inventory.models import Product
    prod_map = Product.objects.in_bulk([p['object_id'] for p in prod_agg if p['object_id']])
    products_list = []
    for p in prod_agg:
        oid = p['object_id']
        name = prod_map[oid].name if oid and oid in prod_map else 'Unknown'
        products_list.append({'name': name, 'count': int(p['count'] or 0), 'revenue': Decimal(str(p['revenue'] or 0))})

    return Response({
        'services': services_list,
        'products': products_list
    })
