import logging
from pos_backend.scoping import narrow_to_selection
import logging
import hashlib
import secrets
from billing.models import Invoice

from django.contrib.auth.hashers import check_password
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.decorators import action, throttle_classes
from rest_framework.throttling import AnonRateThrottle
import openpyxl
from datetime import datetime, time, date, timedelta
from decimal import Decimal
from salon_admin.models import Center
from rest_framework import viewsets, permissions, status
from rest_framework.response import Response
from rest_framework.decorators import api_view, permission_classes, action
from django.db.models import Sum, Count, Q

from pos_backend.money import D, money, ZERO
from pos_backend.permissions import (
    HasModulePermission, IsOwner as _IsOwner, has_module_permission, module_permission,
    require_action,
)


def _require_report_access(user, also=()):
    """Staff earnings reports need Staff > Reports (or a listed equivalent).

    These are actions on the staff directory, whose list every staff screen
    reads - so without this, anyone who could open the directory could read
    everyone's revenue and commission (finding R9).
    """
    from rest_framework.exceptions import PermissionDenied as _Denied

    if has_module_permission(user, 'staff', 'read', 'reports'):
        return
    for module, submodule in also:
        if has_module_permission(user, module, 'read', submodule):
            return
    raise _Denied('This report needs the Staff > Reports permission.')
from pos_backend.scoping import (
    safe_date_param,
    scope_to_centers, assert_can_write_center, coerce_center_id, can_use_center,
)
from rest_framework.exceptions import PermissionDenied, ValidationError as DRFValidationError
from .models import StaffMember, ServiceLog, StaffConsumptionLog, StaffTransfer, StaffToolTracker, PayrollRecord, Designation
from .serializers import (
    StaffMemberSerializer, ServiceLogSerializer, StaffConsumptionLogSerializer,
    StaffTransferSerializer, StaffToolTrackerSerializer, PayrollRecordSerializer, DesignationSerializer
)
from .utils import sync_staff_transfers_and_tools

class DesignationViewSet(viewsets.ModelViewSet):
    queryset = Designation.objects.all().order_by('name')
    serializer_class = DesignationSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'management'
    # Designations label staff wherever staff are listed.
    permission_read_also = [('billing', None), ('appointments', None), ('finance', None)]
class StaffMemberViewSet(viewsets.ModelViewSet):
    serializer_class = StaffMemberSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'directory'
    # A stylist has to be picked on a bill line, on an appointment and on a stock checkout.
    permission_read_also = [('billing', None), ('appointments', None), ('inventory', None)]
    # Syncing transfers reconciles existing records under management.
    permission_action_verbs = {
        'bulk_upload': 'create',
        'sync_transfers': 'update',
    }
    permission_action_submodules = {
        'sync_transfers': 'management',
    }
    def get_queryset(self):
        user = self.request.user
        from django.db.models import Exists, OuterRef
        from .models import StaffToolTracker
        from .utils import sync_if_due
        from datetime import date
        sync_if_due()

        # Optimize N+1 queries by selecting related center and annotating overdue tools
        overdue_tools = StaffToolTracker.objects.filter(
            staff=OuterRef('pk'),
            status='Taken',
            expected_return_date__lt=date.today()
        )
        queryset = StaffMember.objects.all().select_related('center').annotate(
            has_overdue_tools_annotated=Exists(overdue_tools)
        ).order_by('first_name')
        role = getattr(user, 'role', None)
        is_owner = _IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')

        include_inactive = self.request.query_params.get('include_inactive', 'false')
        if self.action == 'list' and include_inactive != 'true':
            queryset = queryset.filter(is_active=True)

        return queryset

    @action(detail=False, methods=['post'])
    def sync_transfers(self, request):
        """Manually trigger transfer/tool expiry sync. Call from a scheduled task or management command."""
        from .utils import sync_staff_transfers_and_tools
        try:
            sync_staff_transfers_and_tools()
            return Response({'status': 'sync complete'})
        except Exception as e:
            logging.getLogger('bulk_upload').exception(
                '[Staff] %s failed', request.path)
            return Response(
                {'error': 'That request could not be completed. The details have '
                          'been logged.'}, status=500)


    @action(detail=False, methods=['get'])
    def bulk_upload_template(self, request):
        """Blank staff-import workbook.

        The guidance used to be written into the data sheet as row 2 ("Required",
        "Optional", "Male/Female" ...) and row 3 ("STF-001", "John", "Doe" ...).
        `bulk_upload` treats every row below the heading as data, so importing
        the template as downloaded tried to create a staff member called
        "Required" whose center was "Optional", which fails the whole upload on
        row 2. An operator who filled in the sheet without first deleting the two
        guide rows could not import at all - which matches the eight consecutive
        400s on this endpoint in the production log.

        The guidance now lives on its own `Instructions` sheet, and the import
        sheet contains nothing but the heading row.
        """
        import io

        try:
            import openpyxl as _openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
        except ImportError:
            return Response({'error': 'openpyxl not installed.'}, status=400)

        headers = [
            'staffCode', 'firstName', 'lastName', 'gender', 'designation', 'center',
            'salary', 'joiningDate', 'phone', 'email', 'address', 'city', 'state', 'pinCode'
        ]
        guidance = [
            'Optional - fill in to update an existing member',
            'Required',
            'Optional',
            'Male, Female or Other. Left blank on a new member: Female.',
            'Optional',
            'Required unless you have exactly one branch, or pass center_id',
            'Optional - monthly gross, digits only',
            'Optional - YYYY-MM-DD',
            'Optional',
            'Optional',
            'Optional',
            'Optional',
            'Optional',
            'Optional',
        ]
        example = [
            'STF-001', 'John', 'Doe', 'Male', 'Senior Stylist', 'Main Center',
            '25000', '2023-01-15', '9876543210', 'john@example.com',
            '123 Main St', 'Mumbai', 'Maharashtra', '400001',
        ]

        wb = _openpyxl.Workbook()
        ws = wb.active
        ws.title = 'Staff Import'
        ws.append(headers)

        header_fill = PatternFill(start_color='16A34A', end_color='16A34A', fill_type='solid')
        header_font = Font(color='FFFFFF', bold=True)
        for col_num, heading_cell in enumerate(ws[1], 1):
            heading_cell.fill = header_fill
            heading_cell.font = header_font
            heading_cell.alignment = Alignment(horizontal='center', vertical='center')
            ws.column_dimensions[
                _openpyxl.utils.get_column_letter(col_num)].width = 20

        ws.freeze_panes = 'A2'

        # Guidance on a separate sheet, so it can never be read as data.
        guide = wb.create_sheet('Instructions')
        guide.append(['Column', 'Required?', 'Example'])
        for guide_cell in guide[1]:
            guide_cell.fill = header_fill
            guide_cell.font = header_font
        for column, note, sample in zip(headers, guidance, example):
            guide.append([column, note, sample])
        guide.column_dimensions['A'].width = 18
        guide.column_dimensions['B'].width = 62
        guide.column_dimensions['C'].width = 22
        guide.append([])
        guide.append(['Fill in the "Staff Import" sheet. Do not rename or reorder '
                      'the heading row. Leave a cell empty to skip that field.'])

        response_io = io.BytesIO()
        wb.save(response_io)
        response_io.seek(0)

        from django.http import HttpResponse
        response = HttpResponse(
            response_io.getvalue(),
            content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="staff_import_template.xlsx"'
        return response

    @action(detail=False, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        """Import or update staff members from a .csv / .xlsx file.

        Fixes in this pass:
          * a junk `center_id` (`abc`, `undefined`) reached `int()` inside a
            `try/except Center.DoesNotExist` and raised ValueError - a 500;
          * one failing row poisoned the surrounding `transaction.atomic()`, so
            on MySQL every later row died with TransactionManagementError rather
            than the per-row warning that was intended. Each row now runs in its
            own savepoint;
          * `existing.gender = gender` was applied unconditionally, and `gender`
            defaults to 'Female' when the column is missing or unrecognised, so
            re-importing a file with no gender column silently changed every male
            member on record to Female;
          * `if salary:` skipped a legitimate zero, so a salary could never be
            corrected down to 0 by import;
          * database error text was returned verbatim to the browser, and the
            warning list was truncated to 20 without saying so;
          * there was no row limit, so one large file held a write transaction
            open across every branch.
        """
        from django.db import transaction as db_transaction

        from pos_backend.bulk_upload import (
            MAX_ROWS, BulkUploadError, ImportReport, bulk_upload_guard, cell,
            cell_date, cell_decimal, read_tabular_file, require_columns,
        )

        RECOGNISED = {
            'firstname', 'first_name', 'name', 'full_name', 'staff_name',
            'lastname', 'last_name', 'staffcode', 'staff_code', 'phone',
            'phone_number', 'mobile', 'email', 'address', 'city', 'state',
            'pincode', 'pin', 'pin_code', 'designation', 'gender', 'salary',
            'monthlygross', 'joiningdate', 'joining_date', 'joindate', 'center',
            'centre', 'center_name', 'location', 'location_name', 'commission',
            'commissionpercentage', 'productcommission',
            'productcommissionpercentage', 'isactive', 'active',
        }
        NAME_COLUMNS = ('firstname', 'name', 'fullname', 'staffname')
        GENDERS = {'male': 'Male', 'female': 'Female', 'other': 'Other'}

        # Text the old template wrote into its own data sheet. A file saved from
        # a template downloaded before this fix still carries these rows, so skip
        # them rather than importing "Required" as a person.
        TEMPLATE_GUIDE_VALUES = {
            'required', 'optional', 'optional (for update)', 'male/female',
            'yyyy-mm-dd',
        }

        with bulk_upload_guard('staff', request) as guard:
            rows = read_tabular_file(request.FILES.get('file'))
            require_columns(rows, RECOGNISED, NAME_COLUMNS, label='staff file')

            user = request.user
            has_all = (_IsOwner.check_is_owner(user)
                       or bool((getattr(user.role, 'permissions', {}) or {})
                               .get('all_centers', False)))

            # `int(...)` on an uncoerced value raised ValueError, which the
            # `except Center.DoesNotExist` below did not catch.
            center_id = coerce_center_id(
                request.data.get('center_id')
                or request.query_params.get('center_id'))

            if center_id is not None:
                if not can_use_center(user, center_id):
                    raise PermissionDenied('You are not assigned to this center.')
                target = Center.objects.filter(id=center_id).first()
                if not target:
                    raise BulkUploadError('That center no longer exists.')
                all_centers = [target]
            elif has_all:
                all_centers = list(Center.objects.all())
            else:
                all_centers = list(scope_to_centers(Center.objects.all(), user, field='id'))

            if not all_centers:
                if has_all and not Center.objects.exists():
                    # First run: nothing to import into yet.
                    all_centers = [Center.objects.create(center_name='Main Center')]
                else:
                    raise PermissionDenied('No accessible centers found.')

            by_name = {}
            for center in all_centers:
                by_name[(center.center_name or '').strip().lower()] = center
                if center.display_name:
                    by_name.setdefault(center.display_name.strip().lower(), center)

            report = ImportReport()
            gender_changes = 0

            with db_transaction.atomic():
                for index, row in enumerate(rows, start=2):
                    first_name = cell(row, 'firstname', 'first_name')
                    last_name = cell(row, 'lastname', 'last_name')

                    if not first_name:
                        whole = cell(row, 'name', 'full_name', 'fullname', 'staff_name',
                                     'staffname')
                        if whole:
                            parts = whole.split(' ', 1)
                            first_name = parts[0]
                            last_name = last_name or (parts[1] if len(parts) > 1 else '')

                    if not first_name:
                        report.skip(index, 'first name is empty.')
                        continue

                    if first_name.strip().lower() in TEMPLATE_GUIDE_VALUES:
                        # A guide row from an older template, not a person.
                        report.skip(
                            index,
                            'this looks like a guidance row from the template '
                            f'("{first_name}") rather than a staff member.')
                        continue

                    staff_code = cell(row, 'staffcode', 'staff_code')
                    if staff_code == '0':
                        staff_code = ''

                    phone = cell(row, 'phone', 'phone_number', 'mobile')
                    if phone.endswith('.0'):
                        # Excel hands back a phone column as a float.
                        phone = phone[:-2]

                    email = cell(row, 'email')
                    address = cell(row, 'address')
                    city = cell(row, 'city')
                    state = cell(row, 'state')
                    pin_code = cell(row, 'pincode', 'pin', 'pin_code')
                    if pin_code == '0':
                        pin_code = ''
                    designation = cell(row, 'designation')

                    # Only a recognised value counts. `gender` used to fall back
                    # to 'Female' and then be written over the existing record.
                    raw_gender = cell(row, 'gender').strip().lower()
                    gender = GENDERS.get(raw_gender)
                    if raw_gender and gender is None:
                        report.problems.append(
                            f'Row {index}: gender "{raw_gender}" not recognised; '
                            f'left unchanged.')

                    salary = cell_decimal(row, 'salary', 'monthlygross', 'monthly_gross')
                    if salary is not None and salary < 0:
                        report.skip(index, f'salary {salary} is negative.')
                        continue

                    comm_perc = cell_decimal(row, 'commissionpercentage',
                                             'commission_percentage', 'commission',
                                             'comm._%', 'comm_%')
                    prod_comm = cell_decimal(row, 'productcommissionpercentage',
                                             'product_commission_percentage',
                                             'productcommission', 'product_commission',
                                             'prod._comm._%', 'prod_comm_%')
                    joining_date = cell_date(row, 'joiningdate', 'joining_date', 'joindate')

                    loc_name = cell(row, 'center', 'centre', 'center_name',
                                    'location', 'location_name')
                    center = None
                    if loc_name:
                        lowered = loc_name.strip().lower()
                        center = by_name.get(lowered)
                        if not center:
                            # Substring fallback, kept for files that abbreviate.
                            center = next(
                                (c for c in all_centers
                                 if lowered in (c.center_name or '').lower()), None)
                    elif len(all_centers) == 1:
                        center = all_centers[0]

                    if not center:
                        # Raise, do not return: returning a Response is a normal
                        # exit, so the enclosing atomic() would COMMIT every row
                        # created before this one while reporting an error.
                        raise BulkUploadError(
                            f'Row {index}: center {loc_name or "(blank)"!r} could not '
                            f'be matched, so nothing was imported.',
                            accessible_centers=[c.center_name for c in all_centers],
                            hint=('Fill the center column with one of the names above, '
                                  'or pass center_id with the upload.'
                                  if len(all_centers) != 1 else
                                  'Leave the center column blank to use your only center.'),
                        )

                    existing = None
                    if staff_code:
                        existing = StaffMember.objects.filter(
                            staff_code__iexact=staff_code, center=center).first()
                    if not existing and phone:
                        existing = StaffMember.objects.filter(
                            phone=phone, center=center).first()
                    if not existing:
                        existing = StaffMember.objects.filter(
                            first_name__iexact=first_name,
                            last_name__iexact=last_name, center=center).first()

                    try:
                        # A savepoint per row. Without it, one IntegrityError
                        # leaves the outer atomic block unusable and every later
                        # row fails with TransactionManagementError instead of
                        # the intended per-row warning.
                        with db_transaction.atomic():
                            if existing:
                                existing.first_name = first_name
                                if last_name:
                                    existing.last_name = last_name
                                if staff_code:
                                    existing.staff_code = staff_code
                                if phone:
                                    existing.phone = phone
                                if email:
                                    existing.email = email
                                if address:
                                    existing.address = address
                                if city:
                                    existing.city = city
                                if state:
                                    existing.state = state
                                if pin_code:
                                    existing.pin_code = pin_code
                                if designation:
                                    existing.designation = designation
                                if gender and gender != existing.gender:
                                    existing.gender = gender
                                    gender_changes += 1
                                # `is not None`, not truthiness: a salary or
                                # commission of exactly 0 is a real value and
                                # could never be set by import before.
                                if salary is not None:
                                    existing.salary = salary
                                if comm_perc is not None:
                                    existing.commission_percentage = comm_perc
                                if prod_comm is not None:
                                    existing.product_commission_percentage = prod_comm
                                if joining_date:
                                    existing.joining_date = joining_date
                                existing.save()
                                report.updated += 1
                            else:
                                StaffMember.objects.create(
                                    center=center,
                                    first_name=first_name,
                                    last_name=last_name,
                                    staff_code=staff_code,
                                    phone=phone,
                                    email=email,
                                    address=address,
                                    city=city,
                                    state=state,
                                    pin_code=pin_code,
                                    designation=designation,
                                    # The model default is 'Female'; only pass a
                                    # value the file actually stated.
                                    **({'gender': gender} if gender else {}),
                                    salary=salary if salary is not None else ZERO,
                                    commission_percentage=(
                                        comm_perc if comm_perc is not None else ZERO),
                                    product_commission_percentage=(
                                        prod_comm if prod_comm is not None else ZERO),
                                    joining_date=joining_date,
                                    is_active=True,
                                )
                                report.created += 1
                    except Exception:
                        # The message used to be `str(e)`, which put database
                        # internals in the operator's browser.
                        logger.exception(
                            '[Bulk upload: staff] row %s failed for %r', index, first_name)
                        who = f'{first_name} {last_name}'.strip()
                        report.skip(
                            index,
                            f'{who} could not be saved. The reason has been logged.')

            payload = report.as_dict()
            payload['centers'] = [c.display_name or c.center_name for c in all_centers]
            if gender_changes:
                payload['gender_changed'] = gender_changes
            payload['row_limit'] = MAX_ROWS
            guard.result = Response(payload)

        return guard.response

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create staff for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create staff for this center.")
        serializer.save()

    def perform_update(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot transfer staff to this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot transfer staff to this center.")
        serializer.save()

    @action(detail=False, methods=['get'])
    def activity_feed(self, request):
        from django.utils import timezone
        feed = []

        user = request.user
        perms = getattr(user.role, 'permissions', {}) if hasattr(user, 'role') and user.role else {}
        is_owner = _IsOwner.check_is_owner(user)
        has_all = is_owner or perms.get('all_centers', False)

        center_id = coerce_center_id(request.query_params.get('center_id'))
        # `coerce_center_id` has already rejected 'null'/'undefined'.
        if center_id is not None:
            if not has_all:
                if user.centers.exists() and not user.centers.filter(id=center_id).exists():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have access to this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and str(user.center.id) != str(center_id):
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You do not have access to this center.")
            allowed_centers = [int(center_id)]
        else:
            if has_all:
                allowed_centers = None # all
            else:
                if user.centers.exists():
                    allowed_centers = list(user.centers.values_list('id', flat=True))
                elif hasattr(user, 'center') and user.center:
                    allowed_centers = [user.center.id]
                else:
                    allowed_centers = []

        # Several centres ticked in the location dropdown.
        from pos_backend.scoping import selected_center_ids
        if selected_center_ids() and center_id is None:
            picked = list(selected_center_ids())
            allowed_centers = picked if allowed_centers is None else [c for c in allowed_centers if c in picked]

        services_qs = ServiceLog.objects.exclude(
            invoice__status__in=Invoice.VOID_STATUSES
        ).select_related('staff', 'invoice__client').order_by('-date', '-time')
        consumptions_qs = StaffConsumptionLog.objects.select_related('staff').order_by('-date', '-time')
        tools_qs = StaffToolTracker.objects.select_related('staff').order_by('-created_at')
        transfers_qs = StaffTransfer.objects.select_related('staff', 'to_center', 'from_center').order_by('-created_at')

        if allowed_centers is not None:
            services_qs = services_qs.filter(staff__center_id__in=allowed_centers)
            consumptions_qs = consumptions_qs.filter(staff__center_id__in=allowed_centers)
            tools_qs = tools_qs.filter(staff__center_id__in=allowed_centers)
            transfers_qs = transfers_qs.filter(staff__center_id__in=allowed_centers)

        # Limit each source to 20 to reduce data load — final sort will pick top 50 overall
        services = services_qs[:20]
        for s in services:
            dt = datetime.combine(s.date, s.time if s.time else time())
            client_display = s.client_name
            if s.invoice and s.invoice.client:
                client_display = f"{s.invoice.client.first_name} {s.invoice.client.last_name or ''}".strip()

            feed.append({
                'id': f"srv_{s.id}",
                'type': 'service',
                'staff_name': f"{s.staff.first_name} {s.staff.last_name or ''}".strip(),
                'title': 'Logged a Service',
                'description': '',
                'details': {
                    'Client': client_display,
                    'Item': s.service_name,
                    'Type': s.service_type,
                    'Price': f"₹{s.price}"
                },
                'timestamp': dt.isoformat()
            })

        consumptions = consumptions_qs[:20]
        for c in consumptions:
            dt = datetime.combine(c.date, c.time if c.time else time())
            feed.append({
                'id': f"cons_{c.id}",
                'type': 'consumption',
                'staff_name': f"{c.staff.first_name} {c.staff.last_name or ''}".strip(),
                'title': 'Consumed a Perk',
                'description': '',
                'details': {
                    'Item': c.service_name,
                    'Payment': c.payment_method,
                    'Cost': f"₹{c.amount}"
                },
                'timestamp': dt.isoformat()
            })

        tools = tools_qs[:20]
        for t in tools:
            feed.append({
                'id': f"tool_{t.id}",
                'type': 'tool',
                'staff_name': f"{t.staff.first_name} {t.staff.last_name or ''}".strip(),
                'title': 'Tool Assigned' if t.status == 'Taken' else 'Tool Returned',
                'description': '',
                'details': {
                    'Tool': t.tool_name,
                    'Qty': t.amount,
                    'Status': t.status,
                    'Return By': t.expected_return_date.strftime('%d %b %Y') if t.expected_return_date else 'N/A'
                },
                'timestamp': t.created_at.isoformat()
            })

        transfers = transfers_qs[:20]
        for tr in transfers:
            feed.append({
                'id': f"trans_{tr.id}",
                'type': 'transfer',
                'staff_name': f"{tr.staff.first_name} {tr.staff.last_name or ''}".strip(),
                'title': 'Staff Transfer',
                'description': '',
                'details': {
                    'Type': tr.transfer_type,
                    'From': (tr.from_center.display_name or tr.from_center.center_name) if tr.from_center else 'Unknown',
                    'To': (tr.to_center.display_name or tr.to_center.center_name) if tr.to_center else 'Unknown',
                    'Duration': f"{tr.start_date.strftime('%d %b %Y')} to {tr.end_date.strftime('%d %b %Y') if tr.end_date else 'Permanent'}"
                },
                'timestamp': tr.created_at.isoformat()
            })

        feed.sort(key=lambda x: x['timestamp'], reverse=True)
        return Response(feed[:50])

    @action(detail=False, methods=['get'])
    def commission_report(self, request):
        """Flat-rate commission view.

        This is the simple "staff percentage x logged revenue" view, kept
        because it is a useful cross-check. It is NOT the payroll figure - the
        authoritative calculation is the IncentiveRule engine, reachable via
        /finance/api/reports/incentive-calculation/ and used by
        /staff/api/payrolls/generate/.

        Two corrections: memberships and packages are no longer lumped into
        "service revenue" (the rule engine treats them as their own
        categories), and the report says which engine produced it.
        """
        _require_report_access(request.user)
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        if not start_date or not end_date:
            return Response({'error': 'start_date and end_date are required'}, status=400)

        center_id = coerce_center_id(request.query_params.get('center_id'))
        staff_qs = self.get_queryset()
        if center_id is not None:
            if not can_use_center(request.user, center_id):
                raise PermissionDenied('You are not assigned to this center.')
            staff_qs = staff_qs.filter(center_id=center_id)

        rows = (
            ServiceLog.objects
            .filter(
                staff__in=staff_qs,
                date__gte=start_date,
                date__lte=end_date,
            )
            .values('staff_id', 'staff__first_name', 'staff__last_name',
                    'staff__commission_percentage', 'staff__product_commission_percentage',
                    'staff__salary', 'staff__center__display_name')
            .annotate(
                service_revenue=Sum('price', filter=Q(service_type='Service')),
                product_revenue=Sum('price', filter=Q(service_type='Product')),
                membership_revenue=Sum('price', filter=Q(service_type='Membership')),
                package_revenue=Sum('price', filter=Q(service_type='Package')),
                value_card_revenue=Sum('price', filter=Q(service_type='Value Card')),
            )
        )

        report = []
        for row in rows:
            service_revenue = money(row['service_revenue'] or 0)
            product_revenue = money(row['product_revenue'] or 0)
            membership_revenue = money(row['membership_revenue'] or 0)
            package_revenue = money(row['package_revenue'] or 0)
            value_card_revenue = money(row['value_card_revenue'] or 0)

            service_commission = money(
                service_revenue * D(row['staff__commission_percentage'] or 0) / D('100')
            )
            product_commission = money(
                product_revenue * D(row['staff__product_commission_percentage'] or 0) / D('100')
            )

            name = row['staff__first_name']
            if row['staff__last_name']:
                name += f" {row['staff__last_name']}"

            report.append({
                'staff_id': row['staff_id'],
                'staff_name': name,
                'center_name': row['staff__center__display_name'] or 'N/A',
                'salary': money(row['staff__salary'] or 0),
                'service_revenue': service_revenue,
                'product_revenue': product_revenue,
                'membership_revenue': membership_revenue,
                'package_revenue': package_revenue,
                'value_card_revenue': value_card_revenue,
                'total_revenue': money(service_revenue + product_revenue
                                       + membership_revenue + package_revenue
                                       + value_card_revenue),
                'service_commission': service_commission,
                'product_commission': product_commission,
                'total_commission': money(service_commission + product_commission),
            })

        return Response({
            'engine': 'flat-rate (cross-check only)',
            'authoritative_endpoint': '/finance/api/reports/incentive-calculation/',
            'results': report,
        })

    @action(detail=False, methods=['get'])
    def incentive_report(self, request):
        """Delegates to the single authoritative incentive engine.

        This endpoint used to be a third, independent implementation. It read
        its salary-multiple thresholds out of `IncentiveConfig.name` - a
        CharField - and queried `category='multiplier_tier'`, a value the model
        never writes, so it ALWAYS fell through to a hardcoded 5x/6x/7x tier
        table and ignored everything configured in the UI. Three engines
        produced three different numbers for the same staff member and period.
        """
        _require_report_access(request.user, also=[('finance', 'incentives')])
        from billing.models import Invoice, InvoiceItem
        from finance import incentives
        from finance.models import IncentiveRule

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        if not start_date or not end_date:
            return Response({'error': 'start_date and end_date are required'}, status=400)

        try:
            period_start = datetime.strptime(start_date, '%Y-%m-%d').date()
            period_end = datetime.strptime(end_date, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return Response({'error': 'Dates must be YYYY-MM-DD.'}, status=400)

        center_id = coerce_center_id(request.query_params.get('center_id'))
        staff_qs = self.get_queryset().select_related('center')
        if center_id is not None:
            if not can_use_center(request.user, center_id):
                raise PermissionDenied('You are not assigned to this center.')
            staff_qs = staff_qs.filter(center_id=center_id)

        invoices = Invoice.objects.filter(
            status__in=Invoice.REVENUE_STATUSES,
            created_at__gte=period_start,
            created_at__lt=period_end + timedelta(days=1),
        )
        items = (
            InvoiceItem.objects
            .filter(invoice__in=invoices, is_redemption=False)
            .filter(Q(staff__in=staff_qs) | Q(staff_members__in=staff_qs)
                    | Q(staff__isnull=True, staff_members__isnull=True,
                        invoice__staff__in=staff_qs))
            .select_related('invoice', 'content_type', 'staff')
            .prefetch_related('staff_members', 'invoice__payments')
            .distinct()
        )

        results = incentives.calculate(
            staff_qs=staff_qs,
            invoice_items=items,
            rules=list(IncentiveRule.objects.filter(is_active=True)),
            start_date=period_start,
            end_date=period_end,
            frequency=(request.query_params.get('frequency') or 'monthly').lower(),
        )

        # Shape kept compatible with the previous response so existing screens
        # keep working, but the numbers now come from the real engine.
        payload = []
        for result in results:
            row = result.as_dict()
            payload.append({
                'staff_id': row['staff_id'],
                'staff_name': row['staff_name'],
                'center_name': row['center'],
                'salary': row['salary'],
                'total_sales': row['total_sales'],
                'multiplier': row['salary_multiple'],
                'incentive_percentage': None,  # per-category now; see breakdown
                'incentive_amount': row['total_incentive'],
                'breakdown': {
                    'services': row['service_percent_incentive'],
                    'addons': row['service_addon_incentive'],
                    'products': row['product_incentive'],
                    'value_cards': row['card_incentive'],
                    'memberships': row['membership_incentive'],
                    'packages': row['package_incentive'],
                    'targets': row['service_target_incentive'],
                    'business_bonus': money(D(row['daily_business_incentive'])
                                            + D(row['overall_incentive'])),
                },
                'rules_applied': row['rules_applied'],
                'partial_period_rules': row['partial_period_rules'],
            })
        payload.sort(key=lambda r: r['total_sales'], reverse=True)
        return Response(payload)


class ServiceLogViewSet(viewsets.ModelViewSet):
    serializer_class = ServiceLogSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'logs'
    # Commission rows come from bills. Making or changing one by hand inflates
    # commission, so it is its own tick and is written to the activity log (X14).
    permission_action_ticks = {
        'create': ('logs', 'add_manual'),
        'update': ('logs', 'add_manual'),
        'partial_update': ('logs', 'add_manual'),
        'destroy': ('logs', 'add_manual'),
    }

    def _audit(self, verb, log):
        from audit_logs.models import SystemLog
        user = self.request.user
        text = (f'{verb} service log by hand: {log.staff} - {getattr(log, "service_name", "")} '
                f'Rs {getattr(log, "price", "")} on {log.date}')
        try:
            SystemLog.objects.create(
                module='STAFF', action=verb.upper()[:10], entity_type='Service Log', entity_id=str(log.pk),
                path='/staff/api/logs/', user=user, user_name=user.full_name or '',
                user_email=user.email or '', center_id=log.center_id,
                human_description=text[:1000], description=text[:2000])
        except Exception:                                            # noqa: BLE001
            pass

    def perform_update(self, serializer):
        self._audit('Changed', serializer.save())

    def perform_destroy(self, instance):
        self._audit('Deleted', instance)
        instance.delete()

    def get_queryset(self):
        user = self.request.user
        queryset = (
            ServiceLog.objects
            .exclude(invoice__status__in=Invoice.VOID_STATUSES)
            .select_related('staff', 'center', 'invoice', 'invoice__client')
            .order_by('-date', '-time')
        )
        role = getattr(user, 'role', None)
        is_owner = _IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')

        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)

        client_name = self.request.query_params.get('client_name')
        if client_name:
            queryset = queryset.filter(client_name__icontains=client_name)

        if getattr(self, '_skip_date_window', False):
            return queryset

        start_date = safe_date_param(self.request.query_params.get('start_date'))
        end_date = safe_date_param(self.request.query_params.get('end_date'))
        if start_date and end_date:
            queryset = queryset.filter(date__range=[start_date, end_date])
        elif start_date:
            queryset = queryset.filter(date__gte=start_date)
        elif end_date:
            queryset = queryset.filter(date__lte=end_date)
        else:
            # Default: return last 30 days to prevent unbounded full-table scan on list
            from datetime import date, timedelta
            queryset = queryset.filter(date__gte=date.today() - timedelta(days=30))

        return queryset

    @action(detail=False, methods=['get'])
    def staff_summary(self, request):
        """One staff member's work in counts: per service, per month, per bill.

        Same centre scoping as the log list (it reuses get_queryset), so it
        shows nothing the list would not. `start_date`/`end_date` bound the
        per-service and per-bill tables; the month history always covers the
        last 12 months. Commission is the flat staff-percentage estimate, the
        same cross-check as the commission report, and is only sent to people
        who may read Staff > Reports (payroll stays the authoritative figure).
        """
        staff_id = request.query_params.get('staff_id')
        if not staff_id or not str(staff_id).isdigit():
            return Response({'error': 'staff_id is required'}, status=400)
        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        if not start_date or not end_date:
            return Response({'error': 'start_date and end_date are required'}, status=400)

        staff = StaffMember.objects.filter(pk=staff_id).first()
        if staff is None:
            return Response({'error': 'Staff member not found'}, status=404)

        show_commission = has_module_permission(request.user, 'staff', 'read', 'reports')
        rates = {
            'Service': D(staff.commission_percentage or 0),
            'Product': D(staff.product_commission_percentage or 0),
        }

        def commission(service_type, amount):
            return money(D(amount or 0) * rates.get(service_type, ZERO) / D('100'))

        self._skip_date_window = True
        base = self.get_queryset()

        from billing.models import InvoiceItem
        from marketing.models import PromotionUsage

        in_range = base.filter(date__range=[start_date, end_date])
        range_logs = list(in_range.select_related('invoice'))

        # A package redemption is logged as a service with no price. Mark those
        # so they read as "used from a package", not as a sale.
        invoice_ids = {lg.invoice_id for lg in range_logs if lg.invoice_id}
        redeemed = set(
            InvoiceItem.objects.filter(invoice_id__in=invoice_ids, is_redemption=True)
            .values_list('invoice_id', 'description'))

        def is_redemption(lg):
            return bool(lg.invoice_id and (lg.invoice_id, lg.service_name) in redeemed)

        by_service_map = {}
        for lg in range_logs:
            key = (lg.service_name, lg.service_type, is_redemption(lg))
            row = by_service_map.setdefault(key, {'count': 0, 'revenue': ZERO})
            row['count'] += 1
            row['revenue'] += D(lg.price or 0)
        by_service = []
        for (name, kind, redemption), row in sorted(
                by_service_map.items(), key=lambda kv: (-kv[1]['count'], kv[0][0])):
            item = {
                'service_name': name, 'service_type': kind,
                'is_redemption': redemption,
                'count': row['count'], 'revenue': money(row['revenue']),
            }
            if show_commission:
                item['commission'] = commission(kind, row['revenue'])
            by_service.append(item)

        # Every category the till can credit, always present so the screen can
        # show a zero instead of hiding the type.
        by_category = {t: {'category': t, 'sold': 0, 'redeemed': 0, 'revenue': ZERO}
                       for t in ('Service', 'Product', 'Membership', 'Package', 'Value Card')}
        for lg in range_logs:
            cat = by_category.setdefault(
                lg.service_type, {'category': lg.service_type, 'sold': 0, 'redeemed': 0, 'revenue': ZERO})
            cat['redeemed' if is_redemption(lg) else 'sold'] += 1
            cat['revenue'] += D(lg.price or 0)
        categories = []
        for cat in by_category.values():
            out = {**cat, 'revenue': money(cat['revenue'])}
            if show_commission:
                out['commission'] = commission(cat['category'], cat['revenue'])
            categories.append(out)

        # Bills in this period that used a promotion, and what it took off.
        promo_by_invoice = {}
        promo_rows = {}
        if invoice_ids:
            for u in (PromotionUsage.objects
                      .filter(invoice_id__in=invoice_ids, is_reversed=False)
                      .select_related('promotion')):
                promo_by_invoice.setdefault(u.invoice_id, []).append(u.promotion.name)
                p = promo_rows.setdefault(u.promotion_id, {
                    'name': u.promotion.name, 'bills': 0, 'discount_given': ZERO})
                p['bills'] += 1
                p['discount_given'] += max(D(u.bill_amount_before) - D(u.bill_amount_after), ZERO)
        promotions = [
            {**p, 'discount_given': money(p['discount_given'])}
            for p in sorted(promo_rows.values(), key=lambda x: -x['bills'])]

        today = date.today()
        month_start = date(today.year, today.month, 1)
        for _ in range(11):
            month_start = (month_start - timedelta(days=1)).replace(day=1)
        monthly = {}
        for row in (base.filter(date__gte=month_start)
                    .values('date__year', 'date__month', 'service_type')
                    .annotate(count=Count('id'), revenue=Sum('price'))):
            key = f"{row['date__year']:04d}-{row['date__month']:02d}"
            m = monthly.setdefault(key, {'month': key, 'services': 0, 'revenue': ZERO,
                                         'commission': ZERO, 'by_type': {}})
            m['by_type'][row['service_type']] = m['by_type'].get(row['service_type'], 0) + row['count']
            m['services'] += row['count']
            m['revenue'] += D(row['revenue'] or 0)
            m['commission'] += commission(row['service_type'], row['revenue'])
        by_month = []
        for key in sorted(monthly, reverse=True):
            m = monthly[key]
            by_month.append({
                'month': key, 'services': m['services'], 'revenue': money(m['revenue']),
                'by_type': m['by_type'],
                **({'commission': money(m['commission'])} if show_commission else {}),
            })

        bills = {}
        for log in range_logs:
            inv = log.invoice
            key = inv.pk if inv else f'manual-{log.pk}'
            b = bills.setdefault(key, {
                'invoice_number': (inv.invoice_number or f'DRAFT-{inv.pk}') if inv else 'Manual entry',
                'date': log.date, 'client_name': log.client_name,
                'items': 0, 'revenue': ZERO, 'commission': ZERO,
                'promotions': promo_by_invoice.get(log.invoice_id, []),
            })
            b['items'] += 1
            b['revenue'] += D(log.price or 0)
            b['commission'] += commission(log.service_type, log.price)
        by_bill = []
        for b in sorted(bills.values(), key=lambda x: x['date'], reverse=True):
            row = {'invoice_number': b['invoice_number'], 'date': b['date'],
                   'client_name': b['client_name'], 'items': b['items'],
                   'revenue': money(b['revenue']), 'promotions': b['promotions']}
            if show_commission:
                row['commission'] = money(b['commission'])
            by_bill.append(row)

        totals = {
            'services': sum(r['count'] for r in by_service),
            'revenue': money(sum((D(r['revenue']) for r in by_service), ZERO)),
            'clients': len({lg.client_name for lg in range_logs}),
        }
        if show_commission:
            totals['commission'] = money(sum((D(r['commission']) for r in by_service), ZERO))
        return Response({
            'show_commission': show_commission,
            'commission_note': 'Flat-rate estimate; payroll is the final figure.',
            'totals': totals, 'by_service': by_service,
            'by_category': categories, 'promotions': promotions,
            'by_month': by_month, 'by_bill': by_bill[:200],
        })

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create logs for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create logs for this center.")
        self._audit('Added', serializer.save())

# The Billing screen's "Staff Activity" card reads today's figures from here,
# so taking bills also reads it (the card predates Phase 1; kept as it was).
@module_permission('staff', 'reports', permission_strict_read=True,
                   permission_read_also=[('billing', 'new_invoice')])
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def revenue_report(request):
    user = request.user
    queryset = ServiceLog.objects.all().select_related('staff', 'center', 'invoice')
    role = getattr(user, 'role', None)
    is_owner = _IsOwner.check_is_owner(user)
    perms = getattr(role, 'permissions', {}) or {}
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            queryset = queryset.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            queryset = queryset.filter(center=user.center)

        else:
            # Fail closed: a user assigned to no center sees nothing.
            queryset = queryset.none()
    center_id = coerce_center_id(request.query_params.get('center_id'))
    if center_id:
        queryset = queryset.filter(center_id=center_id)
    queryset = narrow_to_selection(queryset, 'center')

    start_date = safe_date_param(request.query_params.get('start_date'))
    end_date = safe_date_param(request.query_params.get('end_date'))
    if start_date and end_date:
        queryset = queryset.filter(date__range=[start_date, end_date])

    queryset = queryset.filter(Q(invoice__isnull=True) | Q(invoice__status__in=Invoice.REVENUE_STATUSES))

    total_revenue = queryset.aggregate(total=Sum('price'))['total'] or 0
    total_services = queryset.count()
    total_staff = queryset.values('staff').distinct().count()

    staff_breakdown = queryset.values('staff__first_name', 'staff__last_name', 'center__display_name').annotate(
        services=Count('id'),
        revenue=Sum('price')
    ).order_by('-revenue')

    breakdown = []
    for item in staff_breakdown:
        name = item['staff__first_name']
        if item['staff__last_name']:
            name += f" {item['staff__last_name']}"
        breakdown.append({
            'staff_name': name,
            'location': item['center__display_name'],
            'services': item['services'],
            'revenue': item['revenue']
        })

    feed = []
    for log in queryset.select_related('staff').order_by('-date', '-time')[:50]:
        staff_name = log.staff.first_name
        if log.staff.last_name:
            staff_name += f" {log.staff.last_name}"
        feed.append({
            'staff_name': staff_name,
            'client_name': log.client_name,
            'service_name': log.service_name,
            'service_type': log.service_type,
            'time': log.time.strftime('%I:%M %p') if log.time else '',
            'price': float(log.price)
        })

    return Response({
        'total_revenue': total_revenue,
        'total_services': total_services,
        'total_staff': total_staff,
        'breakdown': breakdown,
        'activity_feed': feed
    })

@module_permission('staff', 'reports', permission_strict_read=True, permission_read_also=[('inventory', None)])
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def usage_report(request):
    user = request.user
    queryset = ServiceLog.objects.all().select_related('staff', 'center', 'invoice')
    role = getattr(user, 'role', None)
    is_owner = _IsOwner.check_is_owner(user)
    perms = getattr(role, 'permissions', {}) or {}
    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            queryset = queryset.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            queryset = queryset.filter(center=user.center)

        else:
            # Fail closed: a user assigned to no center sees nothing.
            queryset = queryset.none()
    start_date = safe_date_param(request.query_params.get('start_date'))
    end_date = safe_date_param(request.query_params.get('end_date'))
    center_id = request.query_params.get('center_id')

    if start_date and end_date:
        queryset = queryset.filter(date__range=[start_date, end_date])
    
    if center_id:
        queryset = queryset.filter(center_id=center_id)
    queryset = narrow_to_selection(queryset, 'center')

    queryset = queryset.filter(Q(invoice__isnull=True) | Q(invoice__status__in=Invoice.REVENUE_STATUSES))

    services_count = queryset.filter(service_type='Service').count()
    products_count = queryset.filter(service_type='Product').count()
    memberships_count = queryset.filter(service_type='Membership').count()
    packages_count = queryset.filter(service_type='Package').count()
    value_cards_count = queryset.filter(service_type='Value Card').count()

    item_breakdown = queryset.values('service_name', 'service_type').annotate(
        times_used=Count('id'),
        revenue=Sum('price')
    ).order_by('-times_used')

    return Response({
        'services_count': services_count,
        'products_count': products_count,
        'memberships_count': memberships_count,
        'packages_count': packages_count,
        'value_cards_count': value_cards_count,
        'breakdown': list(item_breakdown)
    })

class StaffConsumptionLogViewSet(viewsets.ModelViewSet):
    serializer_class = StaffConsumptionLogSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'logs'
    # Importing consumption logs creates them.
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    def get_queryset(self):
        user = self.request.user
        queryset = StaffConsumptionLog.objects.all().select_related('staff', 'center').order_by('-date', '-time')
        role = getattr(user, 'role', None)
        is_owner = _IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')

        start_date = safe_date_param(self.request.query_params.get('start_date'))
        end_date = safe_date_param(self.request.query_params.get('end_date'))
        if start_date and end_date:
            queryset = queryset.filter(date__range=[start_date, end_date])

        return queryset


    @action(detail=False, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        return Response({'detail': 'Bulk upload for this module is not supported yet.'}, status=400)

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create logs for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create logs for this center.")

        payment_method = serializer.validated_data.get('payment_method', 'Points')
        amount = serializer.validated_data.get('amount', 0)
        staff = serializer.validated_data.get('staff')
        service_name = serializer.validated_data.get('service_name')

        remaining_money_amount = 0

        if payment_method == 'Points':
            if staff.allocated_points < amount:
                if staff.allocated_points <= 0:
                    serializer.validated_data['payment_method'] = 'Money'
                    payment_method = 'Money'
                else:
                    points_to_deduct = staff.allocated_points
                    remaining_money_amount = amount - points_to_deduct

                    serializer.validated_data['amount'] = points_to_deduct
                    staff.allocated_points = 0
                    staff.save()
            else:
                staff.allocated_points -= amount
                staff.save()

        try:
            # A perk that uses up a product is a stock movement like any other:
            # it takes from the earliest-expiring batch, never below zero, and
            # leaves a ledger row (Phase 9; it used to subtract silently).
            from django.db import transaction as db_transaction
            from inventory.models import Product
            from inventory.quantities import ZERO, qty as stock_qty
            from inventory.stock import record_out
            with db_transaction.atomic():
                product = (Product.objects.select_for_update()
                           .filter(name__iexact=service_name, center=staff.center).first())
                if product:
                    wanted = stock_qty(serializer.validated_data.get('quantity', 1)
                                       if 'quantity' in serializer.fields else 1)
                    applied = min(wanted, max(stock_qty(product.current_stock), ZERO))
                    if applied > 0:
                        product.current_stock = stock_qty(product.current_stock) - applied
                        product.save(update_fields=['current_stock'])
                        from pos_backend.sap_utils import create_sap_stock_transaction, run_sap_sync_async
                        for row in record_out(product, staff.center, 'CHECKOUT', applied,
                                              f'Staff perk used by {staff.first_name}: {service_name}',
                                              user=self.request.user):
                            # Stock used up leaves SAP as a goods issue too.
                            run_sap_sync_async(create_sap_stock_transaction, row)
        except Exception as e:
            logging.getLogger(__name__).exception('[StaffConsumptionLog] Error deducting stock: %s', e)

        serializer.save()

        if remaining_money_amount > 0:
            StaffConsumptionLog.objects.create(
                staff=staff,
                center=serializer.validated_data.get('center'),
                service_name=service_name,
                date=serializer.validated_data.get('date'),
                time=serializer.validated_data.get('time'),
                payment_method='Money',
                amount=remaining_money_amount
            )

@module_permission('staff', 'reports', permission_strict_read=True)
@api_view(['GET'])
@permission_classes([permissions.IsAuthenticated])
def consumption_report(request):
    user = request.user
    queryset = StaffConsumptionLog.objects.all().select_related('staff', 'center', 'product')
    role = getattr(user, 'role', None)
    is_owner = _IsOwner.check_is_owner(user)
    perms = getattr(role, 'permissions', {}) or {}

    if not is_owner and not perms.get('all_centers', False):
        if user.centers.exists():
            queryset = queryset.filter(center__in=user.centers.all())
        elif hasattr(user, 'center') and user.center:
            queryset = queryset.filter(center=user.center)

        else:
            # Fail closed: a user assigned to no center sees nothing.
            queryset = queryset.none()
    center_id = coerce_center_id(request.query_params.get('center_id'))
    if center_id:
        queryset = queryset.filter(center_id=center_id)
    queryset = narrow_to_selection(queryset, 'center')

    start_date = safe_date_param(request.query_params.get('start_date'))
    end_date = safe_date_param(request.query_params.get('end_date'))
    if start_date and end_date:
        queryset = queryset.filter(date__range=[start_date, end_date])

    report = queryset.values('staff__first_name', 'staff__last_name', 'service_name', 'payment_method', 'center__display_name').annotate(
        total_amount=Sum('amount'),
        times_used=Count('id')
    ).order_by('-total_amount')

    formatted_report = []
    for item in report:
        name = item['staff__first_name']
        if item['staff__last_name']:
            name += f" {item['staff__last_name']}"

        formatted_report.append({
            'staff_name': name,
            'service_name': item['service_name'],
            'payment_method': item['payment_method'],
            'center_name': item['center__display_name'],
            'times_used': item['times_used'],
            'total_amount': item['total_amount']
        })

    return Response(formatted_report)

class StaffTransferViewSet(viewsets.ModelViewSet):
    serializer_class = StaffTransferSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'management'
    # Importing transfers creates them.
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    def get_queryset(self):
        user = self.request.user
        from .utils import sync_if_due
        sync_if_due()
        queryset = StaffTransfer.objects.all().select_related('staff', 'from_center', 'to_center').order_by('-created_at')
        role = getattr(user, 'role', None)
        is_owner = _IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(to_center__in=user.centers.all()) | queryset.filter(from_center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(to_center=user.center) | queryset.filter(from_center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)

        return queryset.distinct()


    @action(detail=False, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        return Response({'detail': 'Bulk upload for this module is not supported yet.'}, status=400)

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            from_center = serializer.validated_data.get('from_center')
            if from_center:
                if user.centers.exists() and from_center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot transfer staff from this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and from_center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot transfer staff from this center.")

        transfer = serializer.save()
        # A transfer that starts later is scheduled: the person moves on its
        # start date (staff.utils.sync_staff_transfers_and_tools), not today.
        if transfer.start_date <= date.today():
            staff = transfer.staff
            staff.center = transfer.to_center
            staff.save()
            transfer.applied = True
            transfer.save(update_fields=['applied'])

    def _send_back(self, transfer):
        """Return the person to the centre they were moved from, if they are
        still where the transfer put them."""
        if not transfer.applied:
            return
        staff = transfer.staff
        if staff.center_id == transfer.to_center_id:
            staff.center = transfer.from_center
            staff.save()

    def perform_update(self, serializer):
        was_active = serializer.instance.status == 'Active'
        transfer = serializer.save()
        # Cancelling (or completing early) hands the person back; before, the
        # transfer was marked cancelled and the person stayed at the new centre.
        if was_active and transfer.status in ('Cancelled', 'Completed'):
            self._send_back(transfer)

    def perform_destroy(self, instance):
        if instance.status == 'Active':
            self._send_back(instance)
        instance.delete()

class StaffToolTrackerViewSet(viewsets.ModelViewSet):
    queryset = StaffToolTracker.objects.all().select_related('staff', 'staff__center').order_by('-created_at')
    serializer_class = StaffToolTrackerSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'management'
    # Importing tool records creates them.
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()

        # Shared fail-closed branch scoping (finding R23): this checked the role
        # NAME "owner", ignored a user's several branches, and showed every
        # branch's tools to a user assigned to none.
        from pos_backend.scoping import scope_to_centers
        queryset = scope_to_centers(queryset, user, field='staff__center')

        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)

        return queryset


    @action(detail=False, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        return Response({'detail': 'Bulk upload for this module is not supported yet.'}, status=400)

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)

        if not is_owner and not perms.get('all_centers', False):
            staff = serializer.validated_data.get('staff')
            if staff and staff.center:
                if user.centers.exists() and staff.center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot track tools for staff in this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and staff.center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot track tools for staff in this center.")
        serializer.save()

class PayrollRecordViewSet(viewsets.ModelViewSet):
    queryset = PayrollRecord.objects.all().select_related('staff', 'center').order_by('-created_at')
    serializer_class = PayrollRecordSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'staff'
    permission_submodule = 'payrolls'
    # Salary figures: only Payrolls reads them, not "any Staff access" (QA M11).
    permission_strict_read = True
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    # Finalising payroll is a set of grantable actions (decision #6) - they
    # used to be hard-wired to a role NAMED "owner".
    permission_action_ticks = {
        'generate': ('payrolls', 'generate'),
        'lock': ('payrolls', 'lock'),
        'unlock': ('payrolls', 'unlock'),
        'mark_paid': ('payrolls', 'mark_paid'),
    }
    def get_queryset(self):
        user = self.request.user
        queryset = super().get_queryset()
        role = getattr(user, 'role', None)
        is_owner = _IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        staff_id = self.request.query_params.get('staff_id')
        if staff_id:
            queryset = queryset.filter(staff_id=staff_id)

        month = self.request.query_params.get('month')
        year = self.request.query_params.get('year')
        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)

        return queryset


    @action(detail=False, methods=['post'], parser_classes=[MultiPartParser, FormParser])
    def bulk_upload(self, request):
        return Response({'detail': 'Bulk upload for this module is not supported yet.'}, status=400)

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = _IsOwner.check_is_owner(user)
        if not is_owner and not perms.get('all_centers', False):
            center = serializer.validated_data.get('center')
            if center:
                if user.centers.exists() and center not in user.centers.all():
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create payrolls for this center.")
                elif not user.centers.exists() and hasattr(user, 'center') and center != user.center:
                    from rest_framework.exceptions import PermissionDenied
                    raise PermissionDenied("You cannot create payrolls for this center.")
        serializer.save()

    @action(detail=True, methods=['post'])
    def lock(self, request, pk=None):
        """Freeze a payroll period. Owner-only.

        Locking blocks every invoice edit for that month, so it is not
        something a receptionist should be able to do - it previously required
        nothing beyond being logged in.
        """
        from django.utils import timezone


        payroll = self.get_object()
        if payroll.status == 'Paid':
            return Response({'detail': 'This payroll is already paid.'}, status=400)

        expected = payroll.computed_net_pay
        if money(payroll.net_pay) != expected:
            return Response(
                {'detail': f'Net pay Rs {money(payroll.net_pay)} does not match its components '
                           f'(Rs {expected}). Correct it before locking.'},
                status=400,
            )

        payroll.status = 'Locked'
        payroll.locked_at = timezone.now()
        payroll.locked_by = request.user
        payroll.save(update_fields=['status', 'locked_at', 'locked_by', 'updated_at'])
        return Response(PayrollRecordSerializer(payroll, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def unlock(self, request, pk=None):
        """Reopen a locked payroll period. Owner-only.

        There was no way back from Locked except editing the row directly,
        which also meant the lock was trivially bypassable via PATCH. Status
        is now read-only on the serializer and moves only through these
        actions.
        """

        payroll = self.get_object()
        if payroll.status == 'Paid':
            return Response(
                {'detail': 'A paid payroll cannot be reopened. Raise an adjustment instead.'},
                status=400,
            )
        payroll.status = 'Draft'
        payroll.locked_at = None
        payroll.locked_by = None
        payroll.save(update_fields=['status', 'locked_at', 'locked_by', 'updated_at'])
        return Response(PayrollRecordSerializer(payroll, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark payroll as disbursed. Owner-only, and only from Locked."""
        from django.utils import timezone


        payroll = self.get_object()
        if payroll.status == 'Paid':
            return Response({'detail': 'This payroll is already marked paid.'}, status=400)
        if payroll.status != 'Locked':
            return Response(
                {'detail': 'Lock the payroll before marking it paid, so the figures are '
                           'frozen at the point of payment.'},
                status=400,
            )

        payroll.status = 'Paid'
        payroll.paid_at = timezone.now()
        payroll.save(update_fields=['status', 'paid_at', 'updated_at'])
        return Response(PayrollRecordSerializer(payroll, context={'request': request}).data)

    def _assert_editable(self, payroll):
        if payroll.status != 'Draft':
            raise DRFValidationError(
                f'This payroll is {payroll.status} and cannot be edited. '
                f'Unlock it first.'
            )

    def update(self, request, *args, **kwargs):
        self._assert_editable(self.get_object())
        return super().update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self._assert_editable(self.get_object())
        return super().partial_update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        payroll = self.get_object()
        if payroll.status != 'Draft':
            return Response(
                {'detail': f'A {payroll.status} payroll record cannot be deleted.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['post'])
    def generate(self, request):
        """Build Draft payroll records from the incentive engine.

        Closes the gap where nothing wrote PayrollRecord at all: payroll was
        hand-entered from a report you could not reconcile against it, and
        net_pay was never checked against its own components.
        """
        from django.db import transaction
        from django.utils import timezone
        from billing.models import Invoice, InvoiceItem
        from finance import incentives
        from finance.models import IncentiveRule


        try:
            month = int(request.data.get('month'))
            year = int(request.data.get('year'))
        except (TypeError, ValueError):
            return Response({'detail': 'month and year are required integers.'}, status=400)
        if not 1 <= month <= 12:
            return Response({'month': 'Month must be between 1 and 12.'}, status=400)

        import calendar as _cal
        period_start = date(year, month, 1)
        period_end = date(year, month, _cal.monthrange(year, month)[1])

        center_id = coerce_center_id(request.data.get('center_id'))
        staff_qs = scope_to_centers(
            StaffMember.objects.select_related('center').filter(is_active=True),
            request.user, requested_center=center_id,
        )

        invoices = Invoice.objects.filter(
            status__in=Invoice.REVENUE_STATUSES,
            created_at__gte=period_start,
            created_at__lt=period_end + timedelta(days=1),
        )
        items = (
            InvoiceItem.objects
            .filter(invoice__in=invoices, is_redemption=False)
            .filter(Q(staff__in=staff_qs) | Q(staff_members__in=staff_qs)
                    | Q(staff__isnull=True, staff_members__isnull=True,
                        invoice__staff__in=staff_qs))
            .select_related('invoice', 'content_type', 'staff')
            .prefetch_related('staff_members', 'invoice__payments')
            .distinct()
        )

        results = incentives.calculate(
            staff_qs=staff_qs,
            invoice_items=items,
            rules=list(IncentiveRule.objects.filter(is_active=True)),
            start_date=period_start,
            end_date=period_end,
            frequency='monthly',
        )

        created, updated, skipped = 0, 0, []
        with transaction.atomic():
            for result in results:
                row = result.as_dict()
                existing = PayrollRecord.objects.filter(
                    staff_id=row['staff_id'], month=month, year=year
                ).first()
                if existing and existing.status != 'Draft':
                    skipped.append({
                        'staff': row['staff_name'],
                        'reason': f'existing record is {existing.status}',
                    })
                    continue

                # service_commission is the percentage on services alone. Everything
                # else, including the daily parts (daily slab, add-ons, targets), is
                # "other", so each part of the payroll can be traced to where it
                # was earned (see breakdown).
                other = money(
                    D(row['card_incentive']) + D(row['membership_incentive'])
                    + D(row['package_incentive']) + D(row['service_target_incentive'])
                    + D(row['daily_business_incentive']) + D(row['overall_incentive'])
                    + D(row['service_addon_incentive'])
                )
                values = {
                    'center_id': row['center_id'],
                    'base_salary': money(row['salary']),
                    'service_commission': money(row['service_percent_incentive']),
                    'product_commission': money(row['product_incentive']),
                    'other_incentive': other,
                    'deductions': money(existing.deductions) if existing else ZERO,
                    'generated_at': timezone.now(),
                    'breakdown': {
                        'total_sales': str(row['total_sales']),
                        'salary_multiple': str(row['salary_multiple']),
                        'services_revenue': str(row['services_revenue']),
                        'products_revenue': str(row['products_revenue']),
                        'cards_revenue': str(row['cards_revenue']),
                        'memberships_revenue': str(row['memberships_revenue']),
                        'packages_revenue': str(row['packages_revenue']),
                        # Where the incentive came from. monthly + daily = all incentive.
                        'monthly_incentive': str(row['monthly_incentive']),
                        'daily_incentive': str(row['daily_incentive']),
                        'service_percent': str(row['service_percent_incentive']),
                        'product_percent': str(row['product_incentive']),
                        'cards': str(row['card_incentive']),
                        'memberships': str(row['membership_incentive']),
                        'packages': str(row['package_incentive']),
                        'overall_bonus': str(row['overall_incentive']),
                        'daily_slab_bonus': str(row['daily_business_incentive']),
                        'daily_slab_days': [{'date': d['date'], 'sales': str(d['sales']), 'bonus': str(d['bonus']),
                                             'rule': d['rule']} for d in row['daily_bonus_days']],
                        'addon_bonus': str(row['service_addon_incentive']),
                        'target_bonus': str(row['service_target_incentive']),
                        'rules_applied': row['rules_applied'],
                        'partial_period_rules': row['partial_period_rules'],
                    },
                }
                values['net_pay'] = money(
                    values['base_salary'] + values['service_commission']
                    + values['product_commission'] + values['other_incentive']
                    - values['deductions']
                )

                if existing:
                    for field, value in values.items():
                        setattr(existing, field, value)
                    existing.save()
                    updated += 1
                else:
                    PayrollRecord.objects.create(
                        staff_id=row['staff_id'], month=month, year=year, **values
                    )
                    created += 1

        if created or updated:
            from notifications.service import notify
            where_center = None
            if center_id:
                from salon_admin.models import Center
                where_center = Center.objects.filter(pk=center_id).first()
            notify('payroll.ready', title=f'Payroll {month:02d}/{year} is ready',
                   body=f'{created + updated} payroll record(s) were generated for {month:02d}/{year}. '
                        f'Review, then lock.', url='/admin/staff', center=where_center,
                   data={'month': month, 'year': year})

        return Response({
            'status': 'success',
            'period': f'{month:02d}/{year}',
            'created': created,
            'updated': updated,
            'skipped': skipped,
        })

