import logging
from pos_backend.scoping import narrow_to_selection
from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from rest_framework.decorators import action
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from django.db.models import Sum
from decimal import Decimal, InvalidOperation
from inventory.quantities import qty as qty_, plain as plain_qty
from inventory.stock import add_batch, record_out
import datetime
from rest_framework.exceptions import APIException
from pos_backend.bulk_upload import BulkUploadError

from rest_framework.exceptions import ValidationError as DRFValidationError
from pos_backend.permissions import IsOwner, HasModulePermission
from pos_backend.scoping import (
    coerce_center_id, can_use_center, assert_can_write_center, scope_to_centers,
)
from .models import (
    Vendor, Product, PurchaseOrder, PurchaseOrderItem, ProductLot, StockTransaction, StockTransferItem,
    StockRequestItem,
)
from .serializers import VendorSerializer, ProductSerializer, PurchaseOrderSerializer, ProductLotSerializer, StockTransactionSerializer

class InventoryBaseViewSet(viewsets.ModelViewSet):
    # HasModulePermission was missing here, so the permission_module declared
    # on the subclasses was never actually enforced.
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    #: The branch foreign key on this model. A stock request's branch is
    #: `requesting_center`: assuming `center` here made the Stock Requests list
    #: crash for every non-owner (finding R6).
    center_field = 'center'

    def requested_center_id(self):
        # coerce_center_id tolerates the literal string 'null', which the
        # frontend does send. `center` is accepted too: the Stock Requests
        # screen sends that name.
        params = self.request.query_params
        return coerce_center_id(params.get('center_id') or params.get('center'))

    def get_queryset(self):
        from django.db.models import Q
        from pos_backend.scoping import scope_to_centers, user_center_ids

        user = self.request.user
        queryset = super().get_queryset()
        center_id = self.requested_center_id()

        if self.center_field != 'transfer':
            # Shared fail-closed scoping; the superuser and all-branches roles
            # may narrow to any branch, everyone else only to their own.
            return scope_to_centers(queryset, user, field=self.center_field,
                                    requested_center=center_id)

        # A transfer belongs to both of its branches.
        allowed = user_center_ids(user)
        if allowed is not None:
            if not allowed or (center_id is not None and center_id not in allowed):
                return queryset.none()
            queryset = queryset.filter(Q(from_center_id__in=allowed) | Q(to_center_id__in=allowed))
        if center_id is not None:
            queryset = queryset.filter(Q(from_center_id=center_id) | Q(to_center_id=center_id))
        return queryset

    def perform_create(self, serializer):
        from pos_backend.scoping import user_center_ids

        user = self.request.user
        # The superuser and all-branches roles are unrestricted. An
        # all-branches role used to be treated as an ordinary user here and
        # refused for having no assigned branch.
        is_owner = user_center_ids(user) is None

        if not is_owner:
            # For non-owners, resolve center from request — honor which center they're working in
            center_id_param = (
                self.request.data.get('center') or
                self.request.data.get('center_id') or
                coerce_center_id(self.request.query_params.get('center_id'))
            )
            if center_id_param:
                try:
                    center_id_param = int(center_id_param)
                except (TypeError, ValueError):
                    center_id_param = None

            if user.centers.exists():
                allowed_centers = user.centers.all()
                if center_id_param:
                    matched = allowed_centers.filter(id=center_id_param).first()
                    if not matched:
                        raise PermissionDenied("You are not assigned to this center.")
                    serializer.save(center=matched)
                else:
                    if allowed_centers.count() == 1:
                        serializer.save(center=allowed_centers.first())
                    else:
                        raise PermissionDenied("Center is required.")
            elif getattr(user, 'center', None):
                serializer.save(center=user.center)
            else:
                raise PermissionDenied("You are not assigned to any center.")
        else:
            serializer.save()


class VendorViewSet(InventoryBaseViewSet):
    # `mapped_products` is serialised for every vendor: prefetched, it is one
    # query for the whole list instead of one per vendor.
    queryset = Vendor.objects.order_by('id').prefetch_related('mapped_products')
    serializer_class = VendorSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    permission_submodule = 'vendors'
    # Vendor contacts are not part of "any Inventory access" (QA M11): the screens
    # that pick a vendor - Product Master, Purchase Orders, PO History - read them.
    permission_strict_read = True
    permission_read_also = [('inventory', 'products'), ('inventory', 'purchase_orders'),
                            ('inventory', 'po_history')]
    # Importing vendors creates vendor records.
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    def list(self, request, *args, **kwargs):
        # `?group=code` (the "All locations" view): every branch keeps its own copy of each vendor, so the plain
        # list shows the same vendor once per branch and is cut at 10,000 rows. Grouped, a vendor (same vendor
        # code, else the same name) is ONE row: the first branch's copy, with its own mapped products.
        paged = 'page' in request.query_params or 'page_size' in request.query_params
        if request.query_params.get('group') != 'code' or paged:
            return super().list(request, *args, **kwargs)
        from django.db.models import Min, Value
        from django.db.models.functions import Coalesce, Lower, NullIf
        from pos_backend.pagination import OptionalPagination

        scoped = self.filter_queryset(self.get_queryset())
        key = Lower(Coalesce(NullIf('vendor_code', Value('')), 'name'))
        firsts = scoped.order_by().annotate(_key=key).values('_key').annotate(first=Min('id')).values('first')
        limit = OptionalPagination.UNPAGED_LIMIT
        rows = list(Vendor.objects.filter(pk__in=firsts).order_by('id').prefetch_related('mapped_products')[:limit + 1])
        response = Response(self.get_serializer(rows[:limit], many=True).data)
        if len(rows) > limit:
            response['X-Truncated'] = 'true'
        return response

    def perform_create(self, serializer):
        super().perform_create(serializer)
        vendor = serializer.instance
        from pos_backend.sap_utils import create_sap_vendor, run_sap_sync_async
        run_sap_sync_async(create_sap_vendor, vendor)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        # A vendor changed in the POS is changed in SAP too (name, phone, e-mail, tax id).
        from pos_backend.sap_utils import create_sap_vendor, run_sap_sync_async
        run_sap_sync_async(create_sap_vendor, serializer.instance)

    def destroy(self, request, *args, **kwargs):
        vendor = self.get_object()
        if vendor.purchase_orders.exists():
            return Response({"error": "Cannot delete vendor with existing purchase orders. Please disable or hide the vendor instead."}, status=400)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['get'])
    def bulk_upload_template(self, request):
        import io
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
        except ImportError:
            return Response({'error': 'openpyxl not installed.'}, status=400)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Vendors Import"

        headers = [
            'vendorCode', 'vendorName', 'shortName', 'phone', 'email',
            'gstNumber', 'panNumber', 'address', 'city', 'state', 'pinCode'
        ]
        ws.append(headers)

        header_fill = PatternFill(start_color="16A34A", end_color="16A34A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)

        for col_num, cell in enumerate(ws[1], 1):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = 20

        help_text = [
            'Optional (for update)', 'Required', 'Optional', 'Optional', 'Optional',
            'Optional', 'Optional', 'Optional', 'Optional', 'Optional', 'Optional'
        ]
        ws.append(help_text)

        help_fill = PatternFill(start_color="F0FDF4", end_color="F0FDF4", fill_type="solid")
        help_font = Font(color="15803D", italic=True)
        for cell in ws[2]:
            cell.fill = help_fill
            cell.font = help_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        example_row = [
            'VND-001', 'Acme Supplies', 'Acme', '9876543210', 'contact@acme.com',
            '22AAAAA0000A1Z5', 'AAAAA0000A', '123 Market St', 'Mumbai', 'Maharashtra', '400001'
        ]
        ws.append(example_row)

        response_io = io.BytesIO()
        wb.save(response_io)
        response_io.seek(0)

        from django.http import HttpResponse
        response = HttpResponse(response_io.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="vendors_import_template.xlsx"'
        return response

    @action(detail=False, methods=['post'])
    def bulk_upload(self, request):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({'error': 'No file uploaded'}, status=400)
        try:
            import pandas as pd
            if file_obj.name.endswith('.csv'):
                df = pd.read_csv(file_obj)
            elif file_obj.name.endswith('.xlsx') or file_obj.name.endswith('.xls'):
                df = pd.read_excel(file_obj)
            else:
                return Response({'error': 'Unsupported file format.'}, status=400)

            df = df.fillna('')
            def normalize_col(c):
                return str(c).strip().lower().replace(' ', '').replace('_', '')
            df.columns = [normalize_col(c) for c in df.columns]
            from pos_backend.bulk_upload import is_template_row
            # The template's help row and worked example are not vendors.
            records = [r for r in df.to_dict('records') if not is_template_row(r)]

            user = request.user
            # This used to be `Center.objects.all()` - every branch in the
            # business, with no permission check - and an unknown center_id fell
            # back to every branch rather than failing closed. A manager of one
            # salon importing a vendor list wrote rows into branches they cannot
            # see. `resolve_target_centers` is the shared, permission-scoped
            # resolver every importer now uses.
            from pos_backend.bulk_upload import describe_targets, resolve_target_centers

            all_centers, center_scope = resolve_target_centers(request, user)

            created_count = 0
            updated_count = 0
            skipped_count = 0
            errors = []

            from django.db import transaction as db_transaction
            with db_transaction.atomic():
                for idx, row in enumerate(records, start=2):
                    name = str(row.get('vendorname', row.get('name', ''))).strip()
                    if not name or name in ('nan', 'None'):
                        errors.append(f'Row {idx}: Skipped – Vendor Name is empty.')
                        skipped_count += 1
                        continue

                    vendor_code = str(row.get('vendorcode', row.get('code', ''))).strip()
                    if vendor_code in ('nan', 'None', '0'): vendor_code = ''
                    short_name = str(row.get('shortname', '')).strip()
                    phone = str(row.get('phone', '')).strip()
                    if phone in ('nan', 'None'): phone = ''
                    email = str(row.get('email', '')).strip()
                    if email in ('nan', 'None'): email = ''
                    cst_number = str(row.get('gstnumber', row.get('gst', row.get('cstnumber', '')))).strip()
                    pan_number = str(row.get('pannumber', row.get('pan', ''))).strip()
                    address = str(row.get('address', '')).strip()
                    city = str(row.get('city', '')).strip()
                    state = str(row.get('state', '')).strip()
                    pin_code = str(row.get('pincode', row.get('pin', ''))).strip()
                    if pin_code in ('nan', 'None', '0'): pin_code = ''

                    for center in all_centers:
                        existing = None
                        if vendor_code:
                            existing = Vendor.objects.filter(vendor_code__iexact=vendor_code, center=center).first()
                        if not existing:
                            existing = Vendor.objects.filter(name__iexact=name, center=center).first()

                        if existing:
                            existing.name = name
                            if vendor_code: existing.vendor_code = vendor_code
                            if short_name: existing.short_name = short_name
                            if phone: existing.phone = phone
                            if email: existing.email = email
                            if cst_number: existing.cst_number = cst_number
                            if pan_number: existing.pan_number = pan_number
                            if address: existing.address = address
                            if city: existing.city = city
                            if state: existing.state = state
                            if pin_code: existing.pin_code = pin_code
                            existing.save()
                            updated_count += 1
                        else:
                            vendor = Vendor.objects.create(
                                center=center,
                                vendor_code=vendor_code,
                                name=name,
                                short_name=short_name,
                                phone=phone,
                                email=email,
                                cst_number=cst_number,
                                pan_number=pan_number,
                                address=address,
                                city=city,
                                state=state,
                                pin_code=pin_code
                            )
                            from pos_backend.sap_utils import create_sap_vendor, run_sap_sync_async
                            run_sap_sync_async(create_sap_vendor, vendor)
                            created_count += 1

            result = {'message': f'Upload complete: {created_count} created, {updated_count} updated, {skipped_count} skipped.',
                      'created': created_count, 'updated': updated_count, 'skipped': skipped_count}
            # Say which branches the rows went to. A silent all-branches
            # import is impossible to distinguish from a one-branch one.
            result.update(describe_targets(all_centers, center_scope))

            if errors:
                result['warnings'] = errors[:20]
                # Truncating silently made 500 problems look like 20.
                if len(errors) > 20:
                    result['warnings_truncated'] = len(errors) - 20
                    result['total_warnings'] = len(errors)
            return Response(result)

        except BulkUploadError as exc:
            # A problem with the FILE, with a message the operator can act on.
            return exc.as_response()

        except APIException:
            # PermissionDenied and friends already carry the right status code.
            # Swallowing them into the generic 400 below reported "bad file" for
            # what was really "you may not import into that branch".
            raise

        except Exception:
            # The traceback goes to the LOG, never to the response. Shipping
            # traceback.format_exc() to the caller leaked file paths and code
            # structure, and because this module had no logging the reason for
            # every failed import was lost entirely.
            import logging
            logging.getLogger('bulk_upload').exception(
                '[Bulk upload] %s failed for %s',
                request.path,
                getattr(getattr(request, 'user', None), 'email', 'unknown'),
            )
            return Response(
                {'error': 'The file could not be imported. The details have been '
                          'logged; please send the file to your administrator.'},
                status=400,
            )

from django.utils.decorators import method_decorator
from django.utils.decorators import method_decorator
from django.views.decorators.vary import vary_on_headers

class ProductViewSet(InventoryBaseViewSet):
    queryset = Product.objects.all().select_related('center').prefetch_related('lots')
    serializer_class = ProductSerializer
    permission_module = 'inventory'
    permission_submodule = 'products'
    # Products are sold from the till and counted in reports.
    permission_read_also = [('billing', None), ('finance', None)]
    # Checkout and audit are their own submodules, not product master.
    permission_action_verbs = {
        'audit': 'update',
        'bulk_upload': 'create',
        'checkout': 'create',
    }
    permission_action_submodules = {
        'audit': 'audit',
        'checkout': 'checkout',
    }

    def get_serializer_class(self):
        # An opening stock figure is legitimate at creation time; after that,
        # stock only moves through checkout / audit / billing, each of which
        # writes a matching StockTransaction.
        if self.action == 'create':
            from .serializers import ProductStockCreateSerializer
            return ProductStockCreateSerializer
        return super().get_serializer_class()

    def perform_create(self, serializer):
        super().perform_create(serializer)
        product = serializer.instance
        
        # --- SAP Integration ---
        from pos_backend.sap_utils import create_sap_item, run_sap_sync_async
        run_sap_sync_async(create_sap_item, product)
        opening = qty_(getattr(product, 'current_stock', 0))
        if opening:
            # Record the opening balance so the ledger reconciles from day one.
            opening_row = StockTransaction.objects.create(
                product=product, center=product.center,
                transaction_type='AUDIT', quantity_change=opening,
                created_by=self.request.user if self.request.user.is_authenticated else None,
                notes='Opening stock recorded at product creation',
            )
            # The stock reaches SAP as a goods receipt, queued after the item itself (items go first).
            from pos_backend.sap_utils import create_sap_stock_transaction
            run_sap_sync_async(create_sap_stock_transaction, opening_row)

    def create(self, request, *args, **kwargs):
        create_all = str(request.data.get('create_all_centers', 'false')).lower() == 'true'
        if create_all:
            from salon_admin.models import Center
            centers = Center.objects.all()
            if not centers.exists():
                return super().create(request, *args, **kwargs)
            instances = []
            for center in centers:
                data = request.data.copy()
                data['center'] = center.id
                serializer = self.get_serializer(data=data)
                serializer.is_valid(raise_exception=True)
                self.perform_create(serializer)
                instances.append(serializer.data)
            from rest_framework.response import Response
            from rest_framework import status
            return Response(instances[0], status=status.HTTP_201_CREATED)
        else:
            return super().create(request, *args, **kwargs)

    @method_decorator(vary_on_headers('Authorization'))
    def list(self, request, *args, **kwargs):
        if 'page' in request.query_params or 'page_size' in request.query_params:
            return super().list(request, *args, **kwargs)
        # The whole list (the till and the stock screens load it): built from plain rows, which is
        # several times faster than the serializer and the same JSON (tests_fast_lists compares them).
        from .fast_lists import aggregate_rows, product_rows
        from pos_backend.pagination import OptionalPagination
        build = aggregate_rows if request.query_params.get('group') == 'code' else product_rows
        rows, truncated = build(self.filter_queryset(self.get_queryset()), OptionalPagination.UNPAGED_LIMIT)
        response = Response(rows)
        if truncated:
            response['X-Truncated'] = 'true'
        return response

    def perform_update(self, serializer):
        instance = serializer.save()
        update_all = self.request.query_params.get('update_all_centers', 'false').lower() == 'true'
        if update_all:
            match_kwargs = {}
            if instance.product_code:
                match_kwargs['product_code__iexact'] = instance.product_code
            elif instance.product_id_str:
                match_kwargs['product_id_str__iexact'] = instance.product_id_str
            else:
                match_kwargs['name__iexact'] = instance.name

            # Find matching products in other centers and sync ONLY catalog/metadata fields.
            # NEVER sync current_stock — each center owns its own independent stock level.
            from django.db import transaction
            with transaction.atomic():
                others = Product.objects.filter(**match_kwargs).exclude(id=instance.id)
                for other in others:
                    other.name = instance.name
                    other.brand = instance.brand
                    other.category = instance.category
                    other.sub_category = instance.sub_category
                    other.vendor_name = instance.vendor_name
                    other.price = instance.price
                    other.gst_percent = instance.gst_percent
                    other.barcode = instance.barcode
                    other.sac_code = instance.sac_code
                    other.reorder_level = instance.reorder_level
                    other.reorder_quantity = instance.reorder_quantity
                    other.is_active = instance.is_active
                    # current_stock intentionally NOT synced — each center tracks its own stock
                    other.save(update_fields=[
                        'name', 'brand', 'category', 'sub_category', 'vendor_name',
                        'price', 'gst_percent', 'barcode', 'sac_code',
                        'reorder_level', 'reorder_quantity', 'is_active'
                    ])

        # --- SAP Integration ---
        from pos_backend.sap_utils import update_sap_item, run_sap_sync_async
        run_sap_sync_async(update_sap_item, instance)

    def destroy(self, request, *args, **kwargs):
        product = self.get_object()
        if product.transactions.exists() or product.purchaseorderitem_set.exists():
            return Response({"error": "Cannot delete product that has stock history or purchase orders. Please mark it as inactive instead."}, status=400)
        return super().destroy(request, *args, **kwargs)

    @action(detail=False, methods=['post'])
    def bulk_upload(self, request):
        """Import or update products from a .csv / .xlsx file.

        Fixes from the third audit pass:
          * The handler ended in `return Response({'error': str(e), 'detail':
            traceback.format_exc()}, status=400)` - shipping a full Python
            traceback to the caller while writing NOTHING to the server log
            (this module had no logging at all). That is why the repeated 400s
            on the import endpoints carry no diagnostic information.
          * A seeded opening stock wrote no StockTransaction, so current_stock
            and the ledger diverged from the first import and stock_history was
            wrong from then on.
          * `safe_float(row.get('tax', row.get('gstpercent', 0.18)))` returned
            0.0 for a PRESENT-but-empty tax column, so those products were
            imported at 0% GST rather than the intended default.
          * `if 0 < gst <= 1: gst *= 100` turned a genuine 1% tax rate into
            100%.
          * There was no row limit, so one large file could hold a write
            transaction open across every center.
        """
        from django.db import transaction as db_transaction

        from salon_admin.models import Center
        from pos_backend.bulk_upload import (
            BulkUploadError, ImportReport, bulk_upload_guard, cell, cell_bool,
            cell_decimal, cell_int, cell_percent, read_tabular_file, require_columns,
        )

        RECOGNISED = {
            'productid', 'id', 'productname', 'name', 'category', 'subcategory',
            'vendor', 'vendorname', 'brand', 'defaultprice', 'defaultpri', 'price',
            'sac', 'saccode', 'hsn', 'hsncode', 'barcode', 'productcode', 'productco',
            'tax', 'gstpercent', 'gst', 'active', 'isactive',
            'reorderlevel', 'reorderquantity', 'currentstock', 'stock',
        }
        DEFAULT_GST = cell_decimal({'v': '18'}, 'v')

        with bulk_upload_guard('products', request) as guard:
            rows = read_tabular_file(request.FILES.get('file'))
            require_columns(rows, RECOGNISED, {'productname', 'name'}, label='product file')

            user = request.user
            requested_center = (request.data.get('center_id')
                                or self.request.query_params.get('center_id'))
            center_id = coerce_center_id(requested_center)

            single_center_upload = False
            if center_id is not None:
                if not can_use_center(user, center_id):
                    raise PermissionDenied('You are not assigned to this center.')
                center = Center.objects.filter(id=center_id).first()
                if not center:
                    raise BulkUploadError('That center no longer exists.')
                target_centers = [center]
                # Only a single-center import can know the physical stock.
                single_center_upload = True
            else:
                target_centers = list(
                    scope_to_centers(Center.objects.all(), user, field='id')
                )

            if not target_centers:
                raise BulkUploadError(
                    'You are not assigned to any center, so there is nowhere to '
                    'import these products.'
                )

            report = ImportReport()
            stock_seeded = []
            parsed_rows = []

            with db_transaction.atomic():
                for index, row in enumerate(rows, start=2):  # 2 = first data row
                    name = cell(row, 'productname', 'name')
                    if not name:
                        report.skip(index, 'product name is empty.')
                        continue

                    from pos_backend.bulk_upload import is_template_row
                    if is_template_row(row):
                        # The template's grey help row and its worked example are not products.
                        report.skip(index, f'this looks like a guidance row from the template ("{name}") '
                                           'rather than a product.')
                        continue

                    product_id_str = cell(row, 'productid', 'id')
                    product_code = cell(row, 'productcode', 'productco')
                    category = cell(row, 'category')
                    sub_category = cell(row, 'subcategory')
                    vendor_name = cell(row, 'vendor', 'vendorname')
                    brand = cell(row, 'brand')
                    sac_code = cell(row, 'sac', 'saccode', 'hsn', 'hsncode')
                    barcode = cell(row, 'barcode')
                    is_active = cell_bool(row, 'active', 'isactive', default=True)

                    price = cell_decimal(row, 'defaultprice', 'defaultpri', 'price')
                    if price is not None and price < 0:
                        report.skip(index, f'price {price} is negative.')
                        continue

                    # A blank tax column falls back to the default; a present
                    # value is taken literally, so 1 means 1% and not 100%.
                    raw_gst = cell(row, 'tax', 'gstpercent', 'gst')
                    if raw_gst:
                        typed_gst = cell_decimal({'v': raw_gst.rstrip('%').strip()}, 'v')
                        if typed_gst is None or typed_gst < 0 or typed_gst > 100:
                            report.skip(index, f'GST "{raw_gst}" is not a percentage from 0 to 100.')
                            continue
                    gst_percent = cell_percent(row, 'tax', 'gstpercent', 'gst',
                                               default=DEFAULT_GST)

                    reorder_level = cell_int(row, 'reorderlevel', default=0) or 0
                    reorder_quantity = cell_int(row, 'reorderquantity', default=0) or 0
                    opening_stock = qty_(cell_int(row, 'currentstock', 'stock', default=0) or 0)
                    if opening_stock < 0:
                        report.skip(index, f'stock {opening_stock} is negative.')
                        continue

                    parsed_rows.append({
                        'index': index, 'name': name, 'product_id_str': product_id_str,
                        'product_code': product_code, 'category': category, 'sub_category': sub_category,
                        'vendor_name': vendor_name, 'brand': brand, 'sac_code': sac_code, 'barcode': barcode,
                        'is_active': is_active, 'price': price, 'gst_percent': gst_percent,
                        'reorder_level': reorder_level, 'reorder_quantity': reorder_quantity,
                        'opening_stock': opening_stock,
                    })

                # Branch by branch: each branch's products are read once and the rows are matched in memory
                # (see ProductIndex), instead of three queries per row per branch.
                from .product_index import ProductIndex
                for center in target_centers:
                    known = ProductIndex(center)
                    for item in parsed_rows:
                        name, product_id_str, product_code = item['name'], item['product_id_str'], item['product_code']
                        found = known.find(product_code, product_id_str, name)
                        existing = Product.objects.get(pk=found) if found is not None else None

                        if existing:
                            before = (existing.product_code, existing.product_id_str, existing.name)
                            existing.name = name
                            if product_id_str:
                                existing.product_id_str = product_id_str
                            if product_code:
                                existing.product_code = product_code
                            if item['category']:
                                existing.category = item['category']
                            if item['sub_category']:
                                existing.sub_category = item['sub_category']
                            if item['vendor_name']:
                                existing.vendor_name = item['vendor_name']
                            if item['brand']:
                                existing.brand = item['brand']
                            if item['price'] is not None:
                                existing.price = item['price']
                            if item['sac_code']:
                                existing.sac_code = item['sac_code']
                            if item['barcode']:
                                existing.barcode = item['barcode']
                            if item['gst_percent'] is not None:
                                existing.gst_percent = item['gst_percent']
                            existing.is_active = item['is_active']
                            existing.reorder_level = item['reorder_level']
                            existing.reorder_quantity = item['reorder_quantity']
                            # current_stock is deliberately NOT touched on
                            # update: it belongs to the ledger, not the file.
                            existing.save()
                            after = (existing.product_code, existing.product_id_str, existing.name)
                            if after != before:
                                known.remove(existing.pk, *before)
                                known.add(existing.pk, *after)
                            report.updated += 1
                            continue

                        # User requested: apply the stock to all centers if uploaded for all centers.
                        initial_stock = item['opening_stock']
                        product = Product.objects.create(
                            center=center,
                            product_id_str=product_id_str,
                            product_code=product_code,
                            name=name,
                            category=item['category'],
                            sub_category=item['sub_category'],
                            vendor_name=item['vendor_name'],
                            brand=item['brand'],
                            price=item['price'],
                            sac_code=item['sac_code'],
                            barcode=item['barcode'],
                            gst_percent=item['gst_percent'] if item['gst_percent'] is not None else 0,
                            is_active=item['is_active'],
                            reorder_level=item['reorder_level'],
                            reorder_quantity=item['reorder_quantity'],
                            current_stock=initial_stock,
                        )
                        known.add(product.pk, product_code, product_id_str, name)
                        report.created += 1

                        # Record the opening balance so current_stock and the
                        # ledger agree from the very first row.
                        if initial_stock:
                            StockTransaction.objects.create(
                                product=product, center=center,
                                transaction_type='AUDIT',
                                quantity_change=initial_stock,
                                created_by=user if user.is_authenticated else None,
                                notes=f"Opening stock from bulk import (row {item['index']})",
                            )
                            stock_seeded.append(product.name)

            payload = report.as_dict()
            payload['centers'] = [c.display_name or c.center_name for c in target_centers]
            
            if stock_seeded:
                payload['opening_stock_recorded'] = len(stock_seeded)
            guard.result = Response(payload)

        return guard.response

    @action(detail=False, methods=['get'])
    def bulk_upload_template(self, request):
        import io
        try:
            import openpyxl
            from openpyxl.styles import Font, PatternFill, Alignment
        except ImportError:
            return Response({'error': 'openpyxl not installed.'}, status=400)

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Products Import"

        headers = [
            'productID', 'productName', 'category', 'subCategory', 'vendor',
            'defaultPrice', 'sac', 'barcode', 'productCode', 'brand', 'tax', 'active',
            'currentStock', 'reorderLevel', 'reorderQuantity'
        ]

        ws.append(headers)

        # Styling
        header_fill = PatternFill(start_color="16A34A", end_color="16A34A", fill_type="solid")
        header_font = Font(color="FFFFFF", bold=True)

        for col_num, cell in enumerate(ws[1], 1):
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_num)].width = 20

        # Helper row
        help_text = [
            'Leave blank for new', 'Required', 'Retail / Professional', 'Optional', 'Optional',
            'Number (e.g. 100.00)', 'Optional', 'Optional', 'Optional', 'Optional', 'Fraction (e.g. 0.18)', 'TRUE / FALSE',
            'Number (e.g. 10)', 'Number', 'Number'
        ]
        ws.append(help_text)

        help_fill = PatternFill(start_color="F0FDF4", end_color="F0FDF4", fill_type="solid")
        help_font = Font(color="15803D", italic=True)
        for cell in ws[2]:
            cell.fill = help_fill
            cell.font = help_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        # Example row
        example_row = [
            '', 'Loreal Shampoo', 'Retail', 'Hair Care', 'Loreal',
            1500.00, '9902', '123456789', 'PRD-001', 'Loreal', 0.18, 'TRUE',
            50, 10, 20
        ]
        ws.append(example_row)

        response_io = io.BytesIO()
        wb.save(response_io)
        response_io.seek(0)

        from django.http import HttpResponse
        response = HttpResponse(response_io.getvalue(), content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
        response['Content-Disposition'] = 'attachment; filename="products_import_template.xlsx"'
        return response

    def _resolve_stock_center(self, request):
        """Which center this stock movement applies to."""
        from salon_admin.models import Center

        requested = coerce_center_id(request.data.get('center_id'))
        user = request.user
        if requested is not None:
            if not can_use_center(user, requested):
                raise PermissionDenied('You are not assigned to this center.')
            center = Center.objects.filter(id=requested).first()
            if not center:
                raise DRFValidationError({'center_id': 'Center not found.'})
            return center

        center = user.center or (user.centers.first() if user.centers.exists() else None)
        if not center:
            raise DRFValidationError(
                {'center_id': 'You are not assigned to a center; specify center_id.'}
            )
        return center

    def _parse_stock_lines(self, request, center, allow_zero=False):
        """Validate the whole payload BEFORE anything is written.

        The old implementation mutated products inside the loop and returned a
        400 Response on the first bad line. Returning a Response is a normal
        exit, so `transaction.atomic()` COMMITTED the rows already changed -
        the operator saw an error while earlier lines had really been deducted.
        Validating up front, and raising rather than returning, fixes both.
        """
        items = request.data.get('items')
        if not isinstance(items, list) or not items:
            raise DRFValidationError({'items': 'Provide a non-empty list of items.'})

        parsed = []
        for index, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                raise DRFValidationError({'items': f'Line {index}: expected an object.'})

            product_id = item.get('product_id')
            product = Product.objects.filter(id=product_id).first() if product_id else None
            if not product:
                raise DRFValidationError(
                    {'items': f'Line {index}: product_id {product_id!r} not found.'}
                )
            if product.center_id != center.id:
                raise DRFValidationError(
                    {'items': f'Line {index}: {product.name} does not belong to {center}.'}
                )

            raw_qty = item.get('quantity')
            if raw_qty is None or str(raw_qty).strip() == '':
                raise DRFValidationError({'items': f'Line {index}: quantity is required.'})
            # Quantities are decimal to the thousandth (X8).
            try:
                qty = qty_(raw_qty)
            except (InvalidOperation, ValueError, TypeError):
                raise DRFValidationError(
                    {'items': f'Line {index}: quantity {raw_qty!r} is not a number.'}
                )
            if qty < 0:
                # A negative checkout used to pass the `qty > current_stock`
                # test and then ADD stock: `40 - (-5) = 45`.
                raise DRFValidationError(
                    {'items': f'Line {index}: quantity cannot be negative.'}
                )
            if qty >= 1_000_000:
                raise DRFValidationError(
                    {'items': f'Line {index}: quantity must be less than 1,000,000.'}
                )
            if qty == 0 and not allow_zero:
                raise DRFValidationError(
                    {'items': f'Line {index}: quantity must be greater than zero.'}
                )
            parsed.append({'product': product, 'quantity': qty, 'line': index})

        return parsed

    @action(detail=False, methods=['post'])
    def checkout(self, request):
        """Consume stock for salon use."""
        from django.db import transaction

        center = self._resolve_stock_center(request)
        lines = self._parse_stock_lines(request, center)

        with transaction.atomic():
            locked = {
                # A real row lock (X9): the dict was called 'locked' but the
                # query took no lock, so a sale could slip in between.
                p.id: p for p in Product.objects.select_for_update().filter(
                    id__in=[l['product'].id for l in lines])
            }
            # Check every line against live stock before mutating anything.
            for line in lines:
                product = locked[line['product'].id]
                available = qty_(product.current_stock)
                if line['quantity'] > available:
                    raise DRFValidationError({
                        'items': f"Line {line['line']}: cannot check out {line['quantity']} of "
                                 f'{product.name} - only {available} in stock.'
                    })

            for line in lines:
                product = locked[line['product'].id]
                product.current_stock = qty_(product.current_stock) - line['quantity']
                product.save(update_fields=['current_stock'])
                from pos_backend.sap_utils import create_sap_stock_transaction, run_sap_sync_async
                for st in record_out(product, center, 'CHECKOUT', line['quantity'],
                                     request.data.get('notes') or 'Checked out for salon use',
                                     user=request.user):
                    run_sap_sync_async(create_sap_stock_transaction, st)

        return Response({
            'status': 'success',
            'message': f'Checked out {len(lines)} item(s).',
            'items': len(lines),
        })

    @action(detail=False, methods=['post'])
    def audit(self, request):
        """Reconcile recorded stock to a physical count.

        Owner-only: this overwrites recorded stock to an arbitrary figure, which
        is the single most powerful write in the inventory module. It previously
        required nothing beyond being logged in.
        """
        from django.db import transaction

        if not IsOwner.check_is_owner(request.user):
            perms = getattr(getattr(request.user, 'role', None), 'permissions', None) or {}
            if not (perms.get('inventory', {}) or {}).get('audit', {}).get('update'):
                raise PermissionDenied(
                    'Stock audit requires the inventory audit permission.'
                )

        center = self._resolve_stock_center(request)
        lines = self._parse_stock_lines(request, center, allow_zero=True)

        adjustments = []
        with transaction.atomic():
            locked = {
                # A real row lock (X9): the dict was called 'locked' but the
                # query took no lock, so a sale could slip in between.
                p.id: p for p in Product.objects.select_for_update().filter(
                    id__in=[l['product'].id for l in lines])
            }
            for line in lines:
                product = locked[line['product'].id]
                counted = line['quantity']
                recorded = qty_(product.current_stock)
                delta = counted - recorded
                if delta == 0:
                    continue
                product.current_stock = counted
                product.save(update_fields=['current_stock'])
                audit_note = (f'Physical count {plain_qty(counted)} vs recorded {plain_qty(recorded)}. '
                              f'{request.data.get("notes") or ""}').strip()
                if delta < 0:
                    # A shortfall comes out of the earliest-expiring batch first.
                    rows = record_out(product, center, 'AUDIT', -delta, audit_note, user=request.user)
                else:
                    rows = [StockTransaction.objects.create(
                        product=product, center=center, transaction_type='AUDIT',
                        quantity_change=delta, created_by=request.user, notes=audit_note)]
                from pos_backend.sap_utils import create_sap_stock_transaction, run_sap_sync_async
                for st in rows:
                    run_sap_sync_async(create_sap_stock_transaction, st)
                adjustments.append({
                    'product': product.name, 'recorded': recorded,
                    'counted': counted, 'delta': delta,
                })

        return Response({
            'status': 'success',
            'message': f'Audited {len(lines)} item(s); {len(adjustments)} adjusted.',
            'adjustments': adjustments,
        })

    @action(detail=False, methods=['get'])
    def stock_history(self, request):
        date_str = request.query_params.get('date') # Format: YYYY-MM-DD
        center_id = coerce_center_id(request.query_params.get('center_id'))

        if not date_str:
            return Response({"error": "date parameter required"}, status=400)

        try:
            target_date = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return Response({"error": "Invalid date format, use YYYY-MM-DD"}, status=400)

        products = self.get_queryset()
        if center_id:
            products = products.filter(center_id=center_id)
        products = narrow_to_selection(products, 'center')

        # Target date end of day
        naive_dt = datetime.datetime.combine(target_date, datetime.time.max)
        target_datetime = naive_dt

        # What moved after that day, per product, from the ledger alone. Joining every product to its whole
        # ledger and filtering inside the SUM (what this did) reads all of the ledger for every call; this reads
        # only the rows after the date, through the (product, date) index.
        from django.db.models import Sum

        future = {
            row['product_id']: row['total']
            for row in StockTransaction.objects
            .filter(created_at__gt=target_datetime, product__in=products.order_by().values('pk'))
            .values('product_id').annotate(total=Sum('quantity_change'))
        }
        products = products.select_related(None).prefetch_related(None).only(
            'id', 'name', 'brand', 'category', 'price', 'current_stock')

        results = []
        for p in products:
            # We want stock as of target_datetime.
            # Current stock = stock as of NOW
            # Stock as of target = Current stock - sum(transactions after target_datetime)
            future_sum = future.get(p.id) or 0
            historical_stock = p.current_stock - future_sum

            results.append({
                "product_id": p.id,
                "name": p.name,
                "brand": p.brand,
                "category": p.category,
                "price": p.price,
                "historical_stock": historical_stock
            })

        return Response(results)

class ProductLotViewSet(InventoryBaseViewSet):
    queryset = ProductLot.objects.order_by('-id')
    serializer_class = ProductLotSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    permission_submodule = 'products'
    center_field = 'product__center'

    def perform_create(self, serializer):
        from pos_backend.scoping import can_use_center

        product_id = self.request.data.get('product')
        if not product_id:
            raise PermissionDenied("Product ID is required")
        product = Product.objects.filter(id=product_id).first()
        if product is None:
            raise PermissionDenied("Product not found.")
        if not can_use_center(self.request.user, product.center_id):
            raise PermissionDenied("You do not have access to the product's center.")
        serializer.save(product=product)

def _with_products(model, related='items'):
    """Prefetch a document's lines WITH their product in the same query.

    `prefetch_related('items__product')` is two queries, and the second one asks for every product id of
    every line at once: with a few thousand documents that is more values than SQLite accepts in one
    "IN (...)" (a 500 on the all-centres list). The lines come back with their product joined instead."""
    from django.db.models import Prefetch
    return Prefetch(related, queryset=model.objects.select_related('product').order_by('id'))


class PurchaseOrderViewSet(InventoryBaseViewSet):
    # `vendor_details` carries the vendor's mapped products: prefetched, that is one query for the whole list
    # instead of one per order.
    queryset = (PurchaseOrder.objects.all().select_related('vendor', 'center')
                .prefetch_related(_with_products(PurchaseOrderItem), 'vendor__mapped_products')
                .order_by('-created_at', '-id'))
    serializer_class = PurchaseOrderSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    unpaged_limit = 2000        # an unpaged call used to get 36 MB at full size
    permission_submodule = 'purchase_orders'
    # Purchase orders carry costs: only Purchase Orders and PO History read them (QA M11).
    permission_strict_read = True
    permission_read_also = [('inventory', 'po_history')]
    def partial_update(self, request, *args, **kwargs):
        return super().partial_update(request, *args, **kwargs)

    def update(self, request, *args, **kwargs):
        return super().update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        po = self.get_object()
        if po.status == 'Delivered':
            return Response({"error": "Cannot delete a delivered purchase order. Please refund or create a manual adjustment instead."}, status=400)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=['post'])
    def receive_grpo(self, request, pk=None):
        from django.db import transaction
        po = self.get_object()
        items_data = request.data.get('items', [])
        
        if po.status in ['Draft', 'Delivered']:
            return Response({"error": "Cannot receive against a Draft or already Delivered PO."}, status=400)
            
        with transaction.atomic():
            from inventory.models import Product, PurchaseOrder, PurchaseOrderItem, StockTransaction

            # Lock the order: two receipts at once each read the same
            # "received so far" and could both pass the check below.
            PurchaseOrder.objects.select_for_update().filter(pk=po.pk).first()

            delta_lines = []

            # A receipt must name goods that are on this order. An empty list,
            # an unknown line or a zero/negative quantity used to be skipped
            # silently and the order was still marked Delivered.
            if not isinstance(items_data, list) or not items_data:
                return Response({'error': 'Enter the items received.'}, status=400)
            on_order = set(po.items.values_list('id', flat=True))
            for data in items_data:
                if not isinstance(data, dict) or data.get('id') not in on_order:
                    return Response({'error': 'A received line is not on this purchase order.'}, status=400)
                try:
                    q = qty_(data.get('received_quantity', 0))
                except ValueError:
                    return Response({'error': 'Enter the received quantity as a number.'}, status=400)
                if q < 0:
                    return Response({'error': 'A received quantity cannot be negative.'}, status=400)
            if not any(qty_(d.get('received_quantity', 0)) > 0 for d in items_data):
                return Response({'error': 'Enter a received quantity greater than zero.'}, status=400)

            # The batch dates must make sense: a batch cannot expire before it was made, and
            # goods already past their expiry date are not put on the shelf as sellable stock.
            import datetime as _dt
            for data in items_data:
                if qty_(data.get('received_quantity', 0)) <= 0:
                    continue
                parsed = {}
                for key, label in (('mfg_date', 'manufacturing'), ('expiry_date', 'expiry')):
                    raw = data.get(key)
                    if not raw:
                        continue
                    try:
                        parsed[key] = _dt.date.fromisoformat(str(raw)[:10])
                    except ValueError:
                        return Response({'error': f'Enter the {label} date as a date.'}, status=400)
                if len(parsed) == 2 and parsed['expiry_date'] < parsed['mfg_date']:
                    return Response({'error': 'The expiry date is before the manufacturing date.'}, status=400)
                if 'expiry_date' in parsed and parsed['expiry_date'] < _dt.date.today():
                    return Response({'error': 'This batch has already expired, so it cannot be received into stock.'},
                                    status=400)

            for data in items_data:
                item_id = data.get('id')
                qty = qty_(data.get('received_quantity', 0))
                batch_number = data.get('batch_number')
                mfg_date = data.get('mfg_date')
                expiry_date = data.get('expiry_date')

                if qty <= 0:
                    continue

                item = po.items.select_for_update().select_related('product').filter(id=item_id).first()
                if not item: continue

                # No more than was ordered (blueprint Phase 3, over-receipt):
                # extra goods are a new order, not a bigger receipt.
                outstanding = qty_(item.quantity) - qty_(item.received_quantity)
                if qty > outstanding:
                    transaction.set_rollback(True)
                    return Response({"error": (
                        f"{item.product.name}: receiving {qty} but only {max(outstanding, 0)} "
                        f"of the {item.quantity} ordered are outstanding.")}, status=400)

                # Update PO item received quantity and save batch details for next time
                item.received_quantity += qty
                if batch_number: item.batch_number = batch_number
                if mfg_date: item.mfg_date = mfg_date
                if expiry_date: item.expiry_date = expiry_date
                item.save(update_fields=['received_quantity', 'batch_number', 'mfg_date', 'expiry_date'])
                
                # 1. Update actual stock by just the delta, under a row lock
                # so a sale or another receipt cannot overwrite it (X9).
                product = Product.objects.select_for_update().get(pk=item.product_id)
                product.current_stock = qty_(product.current_stock) + qty
                product.save(update_fields=['current_stock'])
                
                batch_row = add_batch(product, qty, batch_number or '',
                                      expiry_date if expiry_date else None, source='receipt')

                # Create ProductLot if batch number is provided
                lot = None
                if batch_number:
                    from inventory.models import ProductLot
                    lot = ProductLot.objects.create(
                        product=product,
                        lot_number=batch_number,
                        net_price=item.rate,
                        mrp=item.rate,  # Use rate as fallback for MRP
                        manufacturing_date=mfg_date if mfg_date else None,
                        expiry_date=expiry_date if expiry_date else None
                    )
                
                # 2. Create StockTransaction for just the delta
                StockTransaction.objects.create(
                    product=product,
                    center=po.center,
                    transaction_type='PO_RECEIPT',
                    quantity_change=qty,
                    lot=lot, batch=batch_row,
                    notes=f'Receipt from PO-{po.id}',
                )
                
                # 3. Prepare delta for SAP
                delta_lines.append({
                    'ItemCode': product.product_code or f"P{product.id:06d}",
                    'Quantity': plain_qty(qty),
                    'UnitPrice': str(item.rate),
                    'WarehouseCode': (po.center.sap_warehouse_code or '') if po.center else ''
                })
                    
            # Delivered only when every line is fully in, whatever this receipt named.
            # Read the lines afresh: `po.items` may be a prefetched copy from before this receipt.
            all_received = not any(i.received_quantity < i.quantity
                                   for i in PurchaseOrderItem.objects.filter(purchase_order_id=po.pk))
            if all_received:
                po.status = 'Delivered'
            else:
                po.status = 'Partially Received'
            po.save(update_fields=['status'])
            
            # Send just the newly received deltas to SAP as a new GRPO document
            # Uses run_sap_sync_async so a failure is queued and retried safely.
            if delta_lines and po.vendor:
                from pos_backend.sap_utils import create_sap_purchase_delivery_note, run_sap_sync_async
                run_sap_sync_async(create_sap_purchase_delivery_note, po)
            
        return Response({'status': po.status})


class StockTransferViewSet(InventoryBaseViewSet):
    from .models import StockTransfer
    from .serializers import StockTransferSerializer
    # Who made / sent / received it is shown on every row: joined here, it is no query per row.
    queryset = (StockTransfer.objects.all()
                .select_related('from_center', 'to_center', 'created_by', 'dispatched_by', 'received_by')
                .prefetch_related(_with_products(StockTransferItem))
                .order_by('-created_at', '-id'))
    serializer_class = StockTransferSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    permission_submodule = 'stock_transfers'
    permission_action_verbs = {'verify': 'update', 'receive': 'update'}
    center_field = 'transfer'
    unpaged_limit = 2000        # an unpaged call used to get 34 MB at full size
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    @action(detail=True, methods=['post'])
    def receive(self, request, pk=None):
        """The receiving branch signs the goods in: what it counted (scanned + typed, per line), a note when
        less arrived than was sent, and the bill that came with them (a PDF or picture).

        Multipart or JSON: `lines` = [{"id", "scanned", "manual"}] (a JSON string in multipart), `note`, `bill`."""
        import json
        from inventory import transfer_flow

        transfer = self.get_object()
        lines = request.data.get('lines')
        if isinstance(lines, str):
            try:
                lines = json.loads(lines or '[]')
            except ValueError:
                raise DRFValidationError({'detail': 'The counts could not be read.'})
        if lines is not None and not isinstance(lines, list):
            raise DRFValidationError({'detail': 'The counts could not be read.'})
        bill = request.FILES.get('bill')
        done = transfer_flow.receive(transfer, request.user, lines, request.data.get('note'), bill)
        return Response(self.get_serializer(done).data)

    @action(detail=True, methods=['post'])
    def verify(self, request, pk=None):
        """Record the hand count of the goods: how many were scanned and how many typed in.

        Body: {"sent": [{"id": item, "scanned": n, "manual": n}], "received": [...]}. The sending side's count is
        taken at the source branch, the receiving side's at the destination; each needs access to its own branch.
        Counts only record what was checked; they never move stock."""
        from django.db import transaction
        from pos_backend.scoping import can_use_center

        transfer = self.get_object()
        sections = {'sent': ('scanned_quantity', 'manual_quantity', transfer.from_center_id),
                    'received': ('received_scanned_quantity', 'received_manual_quantity', transfer.to_center_id)}
        body = request.data if isinstance(request.data, dict) else {}
        for name, (_, _, center_id) in sections.items():
            if body.get(name) and not can_use_center(request.user, center_id):
                raise PermissionDenied('Only someone at the sending branch counts what was sent, and only '
                                       'someone at the receiving branch counts what arrived.')
        if transfer.status == 'Completed' and body.get('sent'):
            raise DRFValidationError({'detail': 'The sending count of a completed transfer cannot be changed.'})
        if body.get('received') and transfer.status == 'Draft':
            raise DRFValidationError({'detail': 'Nothing has been sent yet, so there is nothing to count as received.'})
        on_transfer = {item.pk: item for item in transfer.items.all()}
        with transaction.atomic():
            for name, (scanned_field, manual_field, _) in sections.items():
                for row in body.get(name) or []:
                    item = on_transfer.get(row.get('id')) if isinstance(row, dict) else None
                    if item is None:
                        raise DRFValidationError({'detail': 'A counted line is not on this transfer.'})
                    for key, field in (('scanned', scanned_field), ('manual', manual_field)):
                        raw = row.get(key)
                        if raw in (None, ''):
                            setattr(item, field, None)
                            continue
                        try:
                            value = qty_(raw)
                        except ValueError:
                            raise DRFValidationError({'detail': 'A count must be a number.'})
                        if value < 0 or value >= 1_000_000:
                            raise DRFValidationError({'detail': 'A count must be zero or more, and under a million.'})
                        setattr(item, field, value)
                    item.save(update_fields=[scanned_field, manual_field])
        return Response(self.get_serializer(self.get_object()).data)

    def perform_create(self, serializer):
        """Stock leaves the source branch, so the user must be allowed there.

        The shared create saved a `center` field transfers do not have, so
        creating a transfer crashed for every non-owner (finding R17).
        """
        from pos_backend.scoping import can_use_center

        source = serializer.validated_data.get('from_center')
        if source is None or not can_use_center(self.request.user, source.pk):
            raise PermissionDenied('You can only send stock from a branch you are assigned to.')
        if serializer.validated_data.get('stock_request') is not None:
            from pos_backend.permissions import has_module_permission
            if not has_module_permission(self.request.user, 'inventory', 'update', 'fulfill_stock_requests'):
                raise PermissionDenied('You do not have permission to send stock requests.')
        serializer.save(created_by=self.request.user)

    def perform_update(self, serializer):
        """Dispatching is the source branch's act and receiving the destination's:
        each needs access to its own branch (#63, #65)."""
        from pos_backend.scoping import can_use_center

        from inventory import transfer_flow

        transfer = serializer.instance
        new_status = serializer.validated_data.get('status', transfer.status)
        old_status = transfer.status
        user = self.request.user
        if transfer.status != 'In Transit' and new_status in ('In Transit', 'Completed'):
            if not can_use_center(user, transfer.from_center_id):
                raise PermissionDenied('Only someone at the sending branch can dispatch this transfer.')
            transfer_flow.check_sent_counts(transfer)
        if new_status == 'Completed' and transfer.status != 'Completed':
            if not can_use_center(user, transfer.to_center_id):
                raise PermissionDenied('Only someone at the receiving branch can receive this transfer.')
            # The branch checks what arrived: the person who sent it does not sign it in themselves
            # (the owner may, when there is no one else).
            if (transfer.dispatched_by_id and transfer.dispatched_by_id == user.pk
                    and not getattr(user, 'is_superuser', False)):
                raise PermissionDenied('You sent this transfer, so someone at the receiving branch has to receive it.')
        if new_status == 'Completed' and old_status != 'Completed':
            raise DRFValidationError({'detail': 'Sign a transfer in with Receive: count it and attach the bill.'})
        saved = serializer.save()
        if old_status != 'In Transit' and new_status == 'In Transit':
            transfer_flow.tell_sent(saved, user)
        elif old_status != 'Completed' and new_status == 'Completed':
            transfer_flow.tell_received(saved, user)


class TransferBillViewSet(InventoryBaseViewSet):
    """Every received transfer with the bill its branch attached, and a summary across the branches.

    Read only. Filters: center_id (sending or receiving), date_from / date_to (the day it was received),
    has_bill=1|0, difference=1, q (a transfer number, a branch or a person). Paged: page, page_size (50, max 200)."""
    from .models import StockTransfer
    from .serializers import TransferBillSerializer
    queryset = (StockTransfer.objects.filter(status='Completed')
                .select_related('from_center', 'to_center', 'received_by', 'dispatched_by')
                .prefetch_related('items__product').order_by('-received_at', '-id'))
    serializer_class = TransferBillSerializer
    permission_submodule = 'transfer_bills'
    permission_strict_read = True          # values and bills: only for roles ticked for Transfer Bills
    center_field = 'transfer'
    http_method_names = ['get', 'head', 'options']

    def filtered(self):
        from django.db.models import Q
        qs = self.get_queryset()
        p = self.request.query_params
        if p.get('date_from'):
            qs = qs.filter(received_at__date__gte=p['date_from'])
        if p.get('date_to'):
            qs = qs.filter(received_at__date__lte=p['date_to'])
        if p.get('has_bill') in ('1', 'true'):
            qs = qs.exclude(bill_file='').exclude(bill_file__isnull=True)
        elif p.get('has_bill') in ('0', 'false'):
            qs = qs.filter(Q(bill_file='') | Q(bill_file__isnull=True))
        if p.get('difference') in ('1', 'true'):
            qs = qs.filter(has_difference=True)
        term = (p.get('q') or '').strip()
        if term:
            cond = (Q(from_center__center_name__icontains=term) | Q(from_center__display_name__icontains=term)
                    | Q(to_center__center_name__icontains=term) | Q(to_center__display_name__icontains=term)
                    | Q(received_by__full_name__icontains=term) | Q(received_by__email__icontains=term))
            digits = term.upper().replace('ST-', '')
            if digits.isdigit():
                cond |= Q(pk=int(digits))
            qs = qs.filter(cond)
        return qs

    def list(self, request, *args, **kwargs):
        from .models import StockTransferItem

        qs = self.filtered()
        count = qs.count()
        # Worth of what each branch accepted, added up in code (the database would divide the GST % as whole numbers).
        # The transfers are matched by a sub-query: a list of their ids passed to the database stops working
        # (SQLite refuses it) once there are tens of thousands of received transfers.
        total = 0
        per_centre = {}
        counted = {}
        rows = StockTransferItem.objects.filter(transfer__in=qs.order_by().values('pk')).values_list(
            'transfer_id', 'transfer__to_center__display_name', 'transfer__to_center__center_name',
            'quantity', 'received_quantity', 'product__price', 'product__gst_percent')
        for tid, display, name, quantity, received, price, gst in rows:
            name = display or name
            base = qty_(quantity if received is None else received) * (price or 0)
            worth = float(base + base * (gst or 0) / 100)
            total += worth
            entry = per_centre.setdefault(name, {'centre': name, 'transfers': 0, 'total': 0.0})
            entry['total'] = round(entry['total'] + worth, 2)
            if (name, tid) not in counted:
                counted[(name, tid)] = True
                entry['transfers'] += 1
        total = round(total, 2)
        with_bill = qs.exclude(bill_file='').exclude(bill_file__isnull=True).count()
        summary = {
            'count': count, 'with_bill': with_bill, 'without_bill': count - with_bill,
            'with_difference': qs.filter(has_difference=True).count(), 'total': float(total),
            'by_centre': sorted(per_centre.values(), key=lambda r: -r['total']),
        }
        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            size = min(max(int(request.query_params.get('page_size', 50)), 1), 200)
        except (TypeError, ValueError):
            page, size = 1, 50
        rows = self.get_serializer(qs[(page - 1) * size: page * size], many=True).data
        return Response({'summary': summary, 'results': rows, 'page': page, 'page_size': size})


class StockRequestViewSet(InventoryBaseViewSet):
    """A branch asks for stock of one category; an approver (anyone ticked
    for Approve Stock Requests at that branch) says yes or no; the stock person
    (Fulfill Stock Requests) then sends it by a Stock Transfer, choosing where
    it comes from. Receiving that transfer fulfils the request."""

    from .models import StockRequest
    from .serializers import StockRequestSerializer
    queryset = (StockRequest.objects.all()
                .select_related('requesting_center', 'created_by', 'decided_by')
                .prefetch_related(_with_products(StockRequestItem), 'transfers').order_by('-created_at', '-id'))
    serializer_class = StockRequestSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    permission_submodule = 'stock_requests'
    permission_action_submodules = {'approve': 'approve_stock_requests',
                                    'reject': 'approve_stock_requests'}
    permission_action_verbs = {'approve': 'update', 'reject': 'update', 'cancel': 'update'}
    center_field = 'requesting_center'

    def perform_create(self, serializer):
        from pos_backend.scoping import can_use_center

        branch = serializer.validated_data.get('requesting_center')
        if branch is None or not can_use_center(self.request.user, branch.pk):
            raise PermissionDenied('You can only request stock for a branch you are assigned to.')
        instance = serializer.save(created_by=self.request.user)
        from inventory.stock_request_flow import open_for_approval
        open_for_approval(instance, self.request.user)

    def perform_update(self, serializer):
        # Only the notes and items of a still-pending request, and only by the person who made it (or an
        # approver); the status moves through approve / reject / cancel.
        from inventory.stock_request_flow import can_cancel
        req = serializer.instance
        if req.status != 'Pending':
            raise DRFValidationError({'detail': f'A {req.status.lower()} request can no longer be changed.'})
        if not can_cancel(self.request.user, req):
            raise PermissionDenied('Only the person who made this request, or an approver, can change it.')
        serializer.save()

    def _decide(self, request, pk, approve):
        from inventory.stock_request_flow import decide
        req = self.get_object()
        decide(req.pk, request.user, approve, note=str(request.data.get('note') or ''), via='pos')
        return Response(self.get_serializer(self.get_object()).data)

    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        return self._decide(request, pk, True)

    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        return self._decide(request, pk, False)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """The branch withdraws a request that has not been sent yet."""
        from django.db import transaction

        with transaction.atomic():
            req = StockRequestViewSet.StockRequest.objects.select_for_update().get(pk=self.get_object().pk)
            from inventory.stock_request_flow import can_cancel
            if not can_cancel(request.user, req):
                raise PermissionDenied('Only the person who made this request, or an approver, can cancel it.')
            if req.status not in ('Pending', 'Approved'):
                raise DRFValidationError({'detail': f'A {req.status.lower()} request cannot be cancelled.'})
            if req.transfers.exists():
                raise DRFValidationError({'detail': 'A transfer was already made for this request.'})
            req.status = 'Cancelled'
            req.save(update_fields=['status'])
            from approvals.models import ApprovalRequest
            from inventory.stock_request_flow import close_shadow
            close_shadow(req.pk, ApprovalRequest.CANCELLED)
        return Response(self.get_serializer(self.get_object()).data)


class StockTransactionViewSet(InventoryBaseViewSet):
    """The stock ledger, newest first.

    Filters (all optional, any combination): `transaction_type` (CHECKOUT, SALE ...), `date` (one day,
    YYYY-MM-DD), `date_from` / `date_to` (days, both included), `product` (a product id). A screen that
    needs only today's checkouts asks for exactly those, instead of downloading the newest ten thousand
    ledger rows of every type and throwing most of them away."""
    # `created_by_name` is on every row: joined here, it is not a query per row.
    queryset = (StockTransaction.objects.all()
                .select_related('product', 'product__center', 'center', 'created_by')
                .order_by('-created_at', '-id'))
    serializer_class = StockTransactionSerializer
    # InventoryBaseViewSet already carries HasModulePermission; this only names
    # which submodule governs the stock ledger.
    permission_module = 'inventory'
    permission_submodule = 'stock_history'
    # The stock ledger is written by sales, checkouts, receipts and transfers,
    # never by hand: through the API it can only be read.
    http_method_names = ['get', 'head', 'options']

    @staticmethod
    def _day(raw, name):
        try:
            return datetime.datetime.strptime(str(raw).strip()[:10], '%Y-%m-%d').date()
        except ValueError:
            raise DRFValidationError({name: 'Use a date like 2026-10-28.'})

    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        ttype = (params.get('transaction_type') or '').strip()
        if ttype:
            queryset = queryset.filter(transaction_type=ttype)
        if params.get('date'):
            day = self._day(params['date'], 'date')
            queryset = queryset.filter(created_at__gte=datetime.datetime.combine(day, datetime.time.min),
                                       created_at__lt=datetime.datetime.combine(day + datetime.timedelta(days=1),
                                                                                datetime.time.min))
        if params.get('date_from'):
            queryset = queryset.filter(
                created_at__gte=datetime.datetime.combine(self._day(params['date_from'], 'date_from'), datetime.time.min))
        if params.get('date_to'):
            day = self._day(params['date_to'], 'date_to')
            queryset = queryset.filter(created_at__lt=datetime.datetime.combine(day + datetime.timedelta(days=1),
                                                                                datetime.time.min))
        if params.get('product'):
            try:
                queryset = queryset.filter(product_id=int(params['product']))
            except ValueError:
                raise DRFValidationError({'product': 'Use a product id.'})
        return queryset


from rest_framework.views import APIView
from django.db.models import F


class LowStockAlertView(APIView):
    # Inventory data: needs inventory read (finding R2 - it was open to any
    # signed-in user). The shell's background poll fails silently without it.
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'inventory'
    permission_submodule = 'products'

    def get(self, request):
        from pos_backend.scoping import scope_to_centers

        center_id = coerce_center_id(request.query_params.get('center_id'))
        qs = Product.objects.filter(current_stock__lte=F('reorder_level')).select_related('center')
        # Shared fail-closed scoping: an all-branches role now sees every
        # branch here too (it used to be limited to "owner").
        qs = scope_to_centers(qs, request.user, requested_center=center_id)

        # X24: the shell only needs a number for its badge, and the list can run
        # to thousands of rows. `count_only` answers with the count; `page` /
        # `page_size` page the list. With neither, the list is capped at 500.
        if request.query_params.get('count_only'):
            # The shell polls this every few seconds from every open till, and
            # comparing two columns cannot use an index, so it scans the table.
            # A 30 second answer per user and branch keeps that off the database.
            from django.core.cache import cache
            from pos_backend.scoping import selection_key, user_center_ids
            # The answer depends only on WHICH branches the person may see, not on who they are: everyone
            # with the same branches shares one count (hundreds of tills, one scan per 30 seconds).
            allowed = user_center_ids(request.user)
            import hashlib
            scope = 'all' if allowed is None else hashlib.md5(
                ','.join(str(i) for i in sorted(allowed)).encode()).hexdigest()
            key = f'lowstock-count:{scope}:{center_id}:{hashlib.md5(selection_key().encode()).hexdigest()}'
            count = cache.get(key)
            if count is None:
                count = qs.count()
                cache.set(key, count, 30)
            return Response({'count': count})
        try:
            page = max(int(request.query_params.get('page', 0)), 0)
            size = min(max(int(request.query_params.get('page_size', 500)), 1), 500)
        except (TypeError, ValueError):
            page, size = 0, 500
        qs = qs.order_by('center_id', 'name')
        qs = qs[(page - 1) * size: page * size] if page else qs[:size]

        data = []
        for p in qs:
            data.append({
                'id': p.id,
                'name': p.name,
                'stock': float(p.current_stock),
                'reorder_level': p.reorder_level,
                'center': p.center.display_name or p.center.center_name if p.center else None
            })
        return Response(data)


