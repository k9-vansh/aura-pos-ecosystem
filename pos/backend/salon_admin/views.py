import logging

from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from django.db.models import Count
from .models import Center, Role
from .serializers import CenterSerializer, RoleSerializer
from pos_backend.scoping import coerce_center_id, safe_date_param
from pos_backend.permissions import IsOwner, HasModulePermission, module_permission, require_action

# This module had no logging at all, so a failed centre import was recorded
# nowhere on the server - the reason went to the operator's browser and was lost.
logger = logging.getLogger('bulk_upload')

from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

@module_permission('admin', 'centers')
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sap_branches_view(request):
    from django.core.cache import cache
    from pos_backend import hana_reads
    from pos_backend.sap_utils import _get_all

    # A minute's memory: this is a drop-down on the centre form, and SAP's list changes rarely.
    places = cache.get('sap_branches_list')
    if places is not None:
        return Response(places)
    try:
        # Straight from SAP's database (about 30 ms); the Service Layer is only the fallback.
        try:
            places = [{'BPLID': pid, 'BPLName': name} for pid, name in
                      hana_reads.rows('SELECT "BPLId","BPLName" FROM "OBPL" ORDER BY "BPLId"')]
        except hana_reads.HanaUnavailable:
            # All pages: the Service Layer returns 20 rows at a time.
            places = _get_all('BusinessPlaces?$select=BPLID,BPLName')
        cache.set('sap_branches_list', places, 60)
        return Response(places)
    except Exception:
        return Response([])

class CenterViewSet(viewsets.ModelViewSet):
    serializer_class = CenterSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'centers'
    # Every screen has a branch selector.
    permission_read_also = [('billing', None), ('appointments', None), ('finance', None), ('inventory', None), ('marketing', None), ('staff', None), ('dashboard', None), ('logs', None)]
    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Center.objects.none()

        role = getattr(user, 'role', None)
        perms = getattr(role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)

        if is_owner or perms.get('all_centers', False):
            qs = Center.all_objects.all()
        elif user.centers.exists():
            qs = user.centers.all()
        elif hasattr(user, 'center') and user.center:
            qs = Center.all_objects.filter(id=user.center.id)
        else:
            return Center.all_objects.none()

        if self.request.GET.get('with_revenue') == 'true':
            from django.db.models import Sum, Q
            from datetime import date
            today = date.today()
            first_day = today.replace(day=1)
            qs = qs.annotate(
                mtd_revenue=Sum(
                    'invoices__total_amount',
                    filter=Q(invoices__status__in=Invoice.REVENUE_STATUSES, invoices__created_at__gte=first_day)
                )
            )
        if getattr(self, 'action', None) == 'list' and self.request.GET.get('show_inactive') != 'true':
            qs = qs.filter(is_active=True)
        elif getattr(self, 'action', None) is None and self.request.GET.get('show_inactive') != 'true':
            # Fallback if action is somehow not set
            qs = qs.filter(is_active=True)
        return qs

    def perform_create(self, serializer):
        instance = serializer.save()
        
        try:
            from pos_backend.sap_utils import create_sap_warehouse, run_sap_sync_async
            run_sap_sync_async(create_sap_warehouse, instance)
        except Exception as e:
            logger.error(f"Failed to queue SAP sync for Center {instance.id}: {e}")

    def perform_update(self, serializer):
        instance = serializer.save()
        
        try:
            from pos_backend.sap_utils import create_sap_warehouse, run_sap_sync_async
            # Always re-push on update so changes like sap_branch_name propagate to SAP immediately
            run_sap_sync_async(create_sap_warehouse, instance)
        except Exception as e:
            logger.error(f"Failed to queue SAP sync for Center {instance.id}: {e}")


    def perform_destroy(self, instance):
        # Soft delete is recommended for Center due to cascading
        instance.is_active = False
        instance.save()

class RoleViewSet(viewsets.ModelViewSet):
    queryset = Role.objects.annotate(users_count=Count('customuser', distinct=True)).all()
    serializer_class = RoleSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'roles'
    # Only the Roles screen, and the Users screen (to assign a role), read roles.
    permission_strict_read = True
    permission_read_also = [('admin', 'users')]
    def _check_delegation(self, wanted, existing=None):
        """Admin > Roles is a trusted grant: whoever holds it may create and edit
        any role (owner decision, 19 Oct 2026), so nothing is checked beyond the
        permission itself."""
        return

    def perform_create(self, serializer):
        self._check_delegation(serializer.validated_data.get('permissions', {}))
        serializer.save()

    def perform_update(self, serializer):
        self._check_delegation(serializer.validated_data.get('permissions'), serializer.instance)
        serializer.save()

    def perform_destroy(self, instance):
        self._check_delegation(None, instance)
        instance.delete()

from rest_framework.decorators import api_view, permission_classes
from decimal import Decimal as _Decimal
from rest_framework.response import Response
from django.db.models import Sum, Count
from billing.models import Invoice
from appointments.models import Appointment
from clients.models import Client
from datetime import datetime, timedelta

@module_permission('dashboard', 'summary', permission_strict_read=True, permission_read_also=[('overview', 'home')])
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def dashboard_view(request):
    start_date_str = safe_date_param(request.GET.get('start_date'))
    end_date_str = safe_date_param(request.GET.get('end_date'))
    center_id = coerce_center_id(request.GET.get('center_id'))

    invoices = Invoice.objects.filter(status__in=Invoice.REVENUE_STATUSES)
    appointments = Appointment.objects.all()
    clients = Client.objects.all()

    user = request.user
    perms = getattr(user.role, 'permissions', {}) if getattr(user, 'role', None) else {}
    is_owner = IsOwner.check_is_owner(user)
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            invoices = invoices.filter(center__in=user.centers.all())
            appointments = appointments.filter(center__in=user.centers.all())
            clients = clients.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            invoices = invoices.filter(center=user.center)
            appointments = appointments.filter(center=user.center)
            clients = clients.filter(center=user.center)

        else:
            # Fail closed: a user assigned to no center sees nothing.
            invoices = invoices.none()
    if center_id and center_id != 'null':
        invoices = invoices.filter(center_id=center_id)
        appointments = appointments.filter(center_id=center_id)
        clients = clients.filter(center_id=center_id)
    else:
        # Several centres ticked in the location dropdown.
        from pos_backend.scoping import narrow_to_selection
        invoices = narrow_to_selection(invoices, 'center')
        appointments = narrow_to_selection(appointments, 'center')
        clients = narrow_to_selection(clients, 'center')

    if start_date_str and end_date_str:
        try:
            end_date_obj = datetime.strptime(end_date_str, '%Y-%m-%d').date() + timedelta(days=1)
            invoices = invoices.filter(created_at__gte=start_date_str, created_at__lt=end_date_obj)
            appointments = appointments.filter(date__gte=start_date_str, date__lte=end_date_str)
            clients = clients.filter(created_at__gte=start_date_str, created_at__lt=end_date_obj)
        except ValueError:
            pass

    # Aggregate using database efficiently
    total_revenue = invoices.aggregate(total=Sum('total_amount'))['total'] or 0
    total_appointments = appointments.count()
    total_clients = clients.count()

    from django.db.models import DateField
    from django.db.models.functions import Cast

    # Generate daily breakdown directly efficiently
    daily_rev = invoices.annotate(day=Cast('created_at', DateField())).values('day').annotate(revenue=Sum('total_amount')).order_by('day')
    revenue_by_day = [{'day': item['day'].strftime('%Y-%m-%d') if hasattr(item['day'], 'strftime') else str(item['day']), 'revenue': _Decimal(str(item['revenue'] or 0))} for item in daily_rev if item['day']]

    daily_appts = appointments.values('date').annotate(count=Count('id')).order_by('date')
    appointments_by_day = [{'day': item['date'].strftime('%Y-%m-%d') if isinstance(item['date'], datetime) else str(item['date']), 'count': item['count']} for item in daily_appts if item['date']]

    appt_status_counts = appointments.values('status').annotate(count=Count('id'))
    appt_by_status = [{'status': item['status'], 'count': item['count']} for item in appt_status_counts]

    try:
        from billing.models import InvoiceItem
        service_count = InvoiceItem.objects.filter(invoice__in=invoices, content_type__model='servicemaster').aggregate(t=Sum('quantity'))['t'] or 0
        product_count = InvoiceItem.objects.filter(invoice__in=invoices, content_type__model='product').aggregate(t=Sum('quantity'))['t'] or 0
    except Exception:
        service_count = 0
        product_count = 0

    service_type_breakdown = [
        {'service_type': 'Service', 'count': service_count},
        {'service_type': 'Product', 'count': product_count}
    ]

# New vs Repeat Clients Logic
    try:
        new_client_count = 0
        repeat_client_count = 0

        # Get all distinct clients who had an invoice in this period
        visiting_client_ids = invoices.exclude(client__isnull=True).values_list('client_id', flat=True).distinct()
        total_visiting = visiting_client_ids.count()  # use .count() — avoids loading all IDs into Python memory

        if start_date_str:
            sd_obj = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            # Out of the visitors, how many were created on or after the start date?
            new_client_count = Client.objects.filter(id__in=visiting_client_ids, created_at__date__gte=sd_obj).count()
            repeat_client_count = total_visiting - new_client_count
        else:
            # If no date range is given, count all visitors as repeat
            repeat_client_count = total_visiting
    except Exception as e:
        new_client_count = 0
        repeat_client_count = 0

    new_vs_repeat = [
        {'label': 'New', 'count': new_client_count},
        {'label': 'Repeat', 'count': repeat_client_count}
    ]

    try:
        from billing.models import InvoiceItem
        from services.models import ServiceMaster

        # Group by the SERVICE, not by the description text.
        #
        # Grouping on `description` split one service across several rows
        # whenever the label varied - a redemption prefix, a per-bill edit, a
        # trailing space - so the same service appeared two or three times in
        # the breakdown with its revenue divided between them.
        items = InvoiceItem.objects.filter(
            invoice__in=invoices,
            content_type__model='servicemaster',
            is_redemption=False,
        )
        grouped = (
            items.values('object_id')
            .annotate(revenue=Sum('total_price'), qty=Sum('quantity'))
            .order_by('-revenue')
        )
        grouped = list(grouped)

        # One query for the canonical names.
        names = dict(
            ServiceMaster.objects
            .filter(id__in=[row['object_id'] for row in grouped if row['object_id']])
            .values_list('id', 'name')
        )

        service_revenue_breakdown = [
            {
                'service_id': row['object_id'],
                'name': names.get(row['object_id']) or f'Service #{row["object_id"]}',
                'count': int(row['qty'] or 0),
                'revenue': _Decimal(str(row['revenue'] or 0)),
            }
            for row in grouped
        ]
        top_5_services = service_revenue_breakdown[:5]
    except Exception:
        import logging
        logging.getLogger(__name__).exception('[Dashboard] service revenue breakdown failed')
        service_revenue_breakdown = []
        top_5_services = []

    footfall = invoices.count()
    avg_spend = (total_revenue / footfall) if footfall > 0 else 0

    return Response({
        'total_revenue': total_revenue,
        'total_appointments': total_appointments,
        'total_clients': total_clients,
        'footfall': footfall,
        'avg_spend': round(_Decimal(str(avg_spend)), 2),
        'revenue_by_day': revenue_by_day,
        'service_type_breakdown': service_type_breakdown,
        'service_revenue_breakdown': service_revenue_breakdown,
        'top_5_services': top_5_services,
        'new_vs_repeat': new_vs_repeat,
        'appointments_by_day': appointments_by_day,
        'appt_by_status': appt_by_status,
        'visits_by_day': appointments_by_day,
    })


# ─────────────────────────────────────────────────────────
#  BULK IMPORT CENTRES FROM EXCEL
# ─────────────────────────────────────────────────────────
@module_permission('admin', 'centers')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def bulk_import_centers(request):
    import io
    try:
        import openpyxl
    except ImportError:
        return Response({'error': 'openpyxl not installed. Run: pip install openpyxl'}, status=400)

    # A grantable action (Admin > Centres > "bulk import centres"), not an
    # "owner" rule (decision #6).
    require_action(request.user, 'admin', 'centers', 'bulk_import')
    user = request.user

    uploaded_file = request.FILES.get('file')
    if not uploaded_file:
        return Response({'error': 'No file uploaded. Send the Excel as multipart key "file".'}, status=400)

    COLUMN_MAP = {
        'center name': 'center_name', 'centre name': 'center_name',
        'name': 'center_name', 'salon name': 'center_name', 'legal name': 'center_name',
        'display name': 'display_name', 'short name': 'display_name',
        'address': 'address', 'location': 'address', 'full address': 'address',
        'region': 'region', 'city': 'region', 'state': 'region',
        'phone': 'phone', 'phone 1': 'phone', 'mobile': 'phone', 'contact': 'phone',
        'landline 1': 'landline_1', 'landline1': 'landline_1', 'phone 2': 'landline_1', 'alternate phone': 'landline_1',
        'landline 2': 'landline_2', 'landline2': 'landline_2',
        'center email': 'center_email', 'centre email': 'center_email', 'email': 'center_email',
        'gst': 'gst_number', 'gst number': 'gst_number', 'gst no': 'gst_number', 'gstin': 'gst_number',
        'pan': 'pan_number', 'pan number': 'pan_number', 'pan no': 'pan_number',
        'monthly target': 'monthly_target', 'target': 'monthly_target',
        'owner name': 'owner_name', 'owner': 'owner_name', 'owner 1': 'owner_name',
        'owner phone': 'owner_phone', 'owner mobile': 'owner_phone',
        'owner email': 'owner_email_1', 'owner email 1': 'owner_email_1',
        'owner name 2': 'owner_name_2', 'owner phone 2': 'owner_phone_2', 'owner email 2': 'owner_email_2',
        'owner name 3': 'owner_name_3', 'owner phone 3': 'owner_phone_3', 'owner email 3': 'owner_email_3',
        'accountant': 'accountant_name_1', 'accountant name': 'accountant_name_1', 'accountant name 1': 'accountant_name_1',
        'accountant phone': 'accountant_phone_1', 'accountant email': 'accountant_email_1',
        'accountant name 2': 'accountant_name_2', 'accountant phone 2': 'accountant_phone_2', 'accountant email 2': 'accountant_email_2',
        'launched on': 'launched_on', 'opening date': 'launched_on', 'start date': 'launched_on', 'date': 'launched_on',
        'salon/studio': 'display_name', 'salon / studio': 'display_name', 'type': None,
    }

    try:
        wb = openpyxl.load_workbook(io.BytesIO(uploaded_file.read()), data_only=True)
        ws = wb.active
    except Exception:
        # The openpyxl message named internal paths and offsets. It goes to the
        # log; the caller gets something they can act on.
        logger.warning('[Bulk import: centers] could not open %s',
                       getattr(uploaded_file, 'name', '?'), exc_info=True)
        return Response({
            'error': 'That file could not be opened. Make sure it is a genuine '
                     '.xlsx file, is not password protected, and is not still '
                     'open in Excel.',
        }, status=400)

    # Fix mapping based on user's screenshot
    COLUMN_MAP['location'] = 'center_name' # In their sheet, Location is the Center Name
    COLUMN_MAP['state'] = 'region'
    COLUMN_MAP['date'] = 'launched_on'

    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return Response({'error': 'Excel file is empty.'}, status=400)

    # There was no limit at all, so one oversized sheet could hold a write
    # transaction open across the whole estate.
    from pos_backend.bulk_upload import MAX_ROWS
    if len(rows) > MAX_ROWS:
        return Response({
            'error': f'The file has {len(rows)} rows, which is over the '
                     f'{MAX_ROWS} row limit. Split it into smaller files.',
            'rows': len(rows), 'limit': MAX_ROWS,
        }, status=400)

    # Find the header row (scan first 10 rows)
    header_row_idx = 0
    col_map = {}
    header = []

    for i in range(min(10, len(rows))):
        current_header = [str(h).strip().lower() if h is not None else '' for h in rows[i]]
        current_map = {}
        for idx, h in enumerate(current_header):
            field = COLUMN_MAP.get(h)
            if field:
                current_map[idx] = field
        if len(current_map) > len(col_map):
            col_map = current_map
            header = current_header
            header_row_idx = i

    if not col_map:
        return Response({
            'error': 'No recognised column headers found.',
            'hint': 'Use headers like: Center Name, Display Name, Address, Region, Phone, GST Number, Owner Name, etc.',
            'found_headers': [str(h) for h in (rows[0] if len(rows) > 0 else [])],
        }, status=400)

    created_list, updated_list, skipped_list, errors_list = [], [], [], []
    import datetime as dt_mod

    from django.db import transaction as db_transaction

    from pos_backend.bulk_upload import cell_decimal

    for row_num, row in enumerate(rows[header_row_idx+1:], start=header_row_idx+2):
        data = {}
        for col_idx, field_name in col_map.items():
            val = row[col_idx] if col_idx < len(row) else None
            if val is not None and str(val).strip():
                data[field_name] = str(val).strip()

        center_name = data.get('center_name', '').strip()
        if not center_name:
            skipped_list.append({'row': row_num, 'reason': 'Missing center name'})
            continue

        # The template's grey "Required / Optional" help row is not a centre named "Required".
        from pos_backend.bulk_upload import is_template_row
        if is_template_row(data):
            continue

        if 'launched_on' in data:
            raw = data['launched_on']
            parsed = None
            for fmt in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d', '%m/%d/%Y', '%d-%b-%Y'):
                try:
                    parsed = dt_mod.datetime.strptime(str(raw), fmt).strftime('%Y-%m-%d')
                    break
                except ValueError:
                    continue
            if parsed:
                data['launched_on'] = parsed
            else:
                data.pop('launched_on', None)

        # `monthly_target` is a DecimalField and every mapped cell arrives here
        # as a string, so a perfectly ordinary "25,000" or "Rs 25000" raised
        # InvalidOperation on save and lost the whole row - reporting the
        # database's own error text to the browser.
        if 'monthly_target' in data:
            target = cell_decimal({'v': data['monthly_target']}, 'v')
            if target is None or target < 0:
                skipped_list.append({
                    'row': row_num, 'center_name': center_name,
                    'reason': f'monthly target {data["monthly_target"]!r} is not a '
                              f'number; the rest of the row was imported.',
                })
                data.pop('monthly_target', None)
            else:
                data['monthly_target'] = target

        try:
            display_name = data.get('display_name', '').strip()

            # A savepoint per row. Without one, a single failed INSERT leaves the
            # connection unusable inside an outer atomic block and every later
            # row fails with TransactionManagementError.
            with db_transaction.atomic():
                if display_name:
                    existing = Center.objects.filter(
                        center_name__iexact=center_name,
                        display_name__iexact=display_name).first()
                else:
                    existing = Center.objects.filter(
                        center_name__iexact=center_name).first()

                if existing:
                    for field, value in data.items():
                        if field != 'center_name':
                            setattr(existing, field, value)
                    existing.save()
                    updated_list.append({'row': row_num, 'center_name': center_name})
                else:
                    center = Center(center_name=center_name)
                    for field, value in data.items():
                        if field != 'center_name':
                            setattr(center, field, value)
                    center.save()
                    created_list.append({'row': row_num, 'center_name': center_name})
        except Exception:
            logger.exception('[Bulk import: centers] row %s (%r) failed',
                             row_num, center_name)
            errors_list.append({
                'row': row_num, 'center_name': center_name,
                'error': 'This row could not be saved. The reason has been logged.',
            })

    # `'success': True` was returned even when every single row had failed, and
    # `total_rows` assumed the heading was row 1 - it is found by scanning, so a
    # sheet with a title above the headings reported the wrong total.
    REPORT_CAP = 100
    data_rows = max(0, len(rows) - (header_row_idx + 1))
    payload = {
        'success': not errors_list,
        'summary': {
            'total_rows': data_rows,
            'created': len(created_list),
            'updated': len(updated_list),
            'skipped': len(skipped_list),
            'errors': len(errors_list),
        },
        'created': created_list[:REPORT_CAP],
        'updated': updated_list[:REPORT_CAP],
        'skipped': skipped_list[:REPORT_CAP],
        'errors': errors_list[:REPORT_CAP],
    }
    truncated = {
        name: len(rows_reported)
        for name, rows_reported in (('created', created_list), ('updated', updated_list),
                                    ('skipped', skipped_list), ('errors', errors_list))
        if len(rows_reported) > REPORT_CAP
    }
    if truncated:
        # Returning every row of a large import made the response enormous, and
        # cutting the lists without saying so made a long list look short.
        payload['lists_truncated_at'] = REPORT_CAP
        payload['full_counts'] = truncated
    return Response(payload)

@module_permission('admin', 'centers')
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def bulk_import_template(request):
    import io
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
    except ImportError:
        return Response({'error': 'openpyxl not installed.'}, status=400)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Centres Import"

    headers = [
        'Center Name', 'Display Name', 'Address', 'Region', 'Phone', 'Landline 1',
        'Landline 2', 'Center Email',
        'GST Number', 'PAN Number', 'Monthly Target', 'Owner Name', 'Owner Phone',
        'Owner Email', 'Owner Name 2', 'Owner Phone 2', 'Owner Email 2',
        'Owner Name 3', 'Owner Phone 3', 'Owner Email 3',
        'Accountant Name', 'Accountant Phone', 'Accountant Email',
        'Accountant Name 2', 'Accountant Phone 2', 'Accountant Email 2',
        'Launched On'
    ]

    ws.append(headers)

    # Styling
    header_fill = PatternFill(start_color="16A34A", end_color="16A34A", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col_num, cell in enumerate(ws[1], 1):
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        # Adjust width
        ws.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = 20

    help_text = [
        'Required', 'Optional', 'Optional', 'Optional', 'Optional', 'Optional',
        'Optional', 'Optional',
        'Optional', 'Optional', 'Optional Number', 'Optional', 'Optional',
        'Optional', 'Optional', 'Optional', 'Optional',
        'Optional', 'Optional', 'Optional',
        'Optional', 'Optional', 'Optional',
        'Optional', 'Optional', 'Optional',
        'YYYY-MM-DD or DD-MM-YYYY'
    ]
    ws.append(help_text)

    help_fill = PatternFill(start_color="F0FDF4", end_color="F0FDF4", fill_type="solid")
    help_font = Font(color="15803D", italic=True)
    for cell in ws[2]:
        cell.fill = help_fill
        cell.font = help_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    response_io = io.BytesIO()
    wb.save(response_io)
    response_io.seek(0)

    from django.http import HttpResponse
    response = HttpResponse(response_io.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    response['Content-Disposition'] = 'attachment; filename="centres_import_template.xlsx"'
    return response
