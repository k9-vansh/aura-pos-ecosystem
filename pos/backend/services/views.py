import logging
from rest_framework import viewsets, status
from rest_framework.response import Response
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.db.models import Q
from .models import ServiceMaster, CenterService
from .serializers import ServiceMasterSerializer, CenterServiceSerializer
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from pos_backend.permissions import IsOwner, HasModulePermission
from pos_backend.scoping import coerce_center_id

class ServiceMasterViewSet(viewsets.ModelViewSet):
    # `center_overrides` is prefetched in get_queryset(): for a branch request only
    # that branch's overrides are loaded. Loading every branch's override for every
    # service built ~17,000 model objects (and ~1.8 s) to answer one screen.
    queryset = ServiceMaster.objects.all()
    serializer_class = ServiceMasterSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'services'
    # The service catalogue is read by the till, appointments and every report.
    permission_read_also = [('billing', None), ('appointments', None), ('marketing', None), ('finance', None), ('staff', None)]
    # Importing services creates service records.
    permission_action_verbs = {
        'bulk_upload': 'create',
    }
    # The service catalogue is NOT cached at the response level.
    #
    # It was `cache_page(60 * 15)` keyed by URL, varying only on Authorization.
    # But this list is scoped by BRANCH, and the branch comes from the caller's
    # `?center_id=` or their own assignment - neither of which is part of the
    # cache key. So one branch's price list could be served to another, and a
    # price change took up to fifteen minutes to appear at the till while staff
    # quoted the old figure. Caching the rendered response of a per-branch,
    # per-user list cannot be made correct by varying on a header.
    #
    # If this list ever needs caching, cache the QUERYSET per
    # (center_id, updated_at) and invalidate on save - never the response.

    def _with_overrides(self, queryset):
        from django.db.models import Prefetch
        cid = coerce_center_id(self.request.query_params.get('center_id'))
        overrides = CenterService.objects.all()
        if cid:
            overrides = overrides.filter(center_id=cid)
        # `centers` is serialised as a list of ids only. Prefetching whole Center rows
        # built one full model per (service, branch) pair, roughly 17,000 objects.
        from salon_admin.models import Center
        from django.db.models import Count
        return queryset.annotate(recipe_line_count=Count('bom_items', distinct=True)).prefetch_related(
            Prefetch('center_overrides', queryset=overrides),
            Prefetch('centers', queryset=Center.objects.only('id')),
        )

    def get_queryset(self):
        user = self.request.user
        queryset = self._with_overrides(super().get_queryset())

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        is_all_centers = is_owner or perms.get('all_centers', False)

        if is_all_centers:
            center_id = coerce_center_id(self.request.query_params.get('center_id'))
            if center_id:
                return queryset.filter(Q(centers__id=center_id) | Q(level='Organisation')).distinct()
            from pos_backend.scoping import selected_center_ids
            if selected_center_ids():
                return queryset.filter(Q(centers__id__in=list(selected_center_ids())) | Q(level='Organisation')).distinct()
            return queryset

        if user.centers.exists():
            from pos_backend.scoping import selected_center_ids
            mine = user.centers.all()
            if selected_center_ids():
                mine = mine.filter(id__in=list(selected_center_ids()))
            return queryset.filter(Q(centers__in=mine) | Q(level='Organisation')).distinct()
        elif getattr(user, 'center', None):
            return queryset.filter(Q(centers__id=user.center.id) | Q(level='Organisation')).distinct()

        return queryset.none()

    def perform_create(self, serializer):
        user = self.request.user
        perms = getattr(user.role, 'permissions', {}) or {}
        is_owner = IsOwner.check_is_owner(user)
        is_all_centers = is_owner or perms.get('all_centers', False)

        if self.request.data.get('level') == 'Organisation':
            if not is_all_centers:
                raise PermissionDenied("Only owners or users with all-center access can create organisation level services.")
            self._to_sap(serializer.save())
            return

        if not is_all_centers:
            if not user.centers.exists() and not getattr(user, 'center', None):
                raise PermissionDenied("User is not assigned to any center.")

        self._to_sap(serializer.save())

    @staticmethod
    def _to_sap(service):
        """A service made or changed in the POS becomes (or updates) a service item in SAP, queued for the
        background worker; without it a bill for the service cannot be posted."""
        from pos_backend.sap_utils import create_sap_service, run_sap_sync_async
        service.sap_sync_status = False
        service.save(update_fields=['sap_sync_status'])
        run_sap_sync_async(create_sap_service, service)

    def perform_update(self, serializer):
        from rest_framework.exceptions import PermissionDenied
        from pos_backend.scoping import can_use_center, user_center_ids

        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        user = self.request.user
        if center_id and not can_use_center(user, center_id):
            raise PermissionDenied('You are not assigned to this branch.')
        if not center_id and user_center_ids(user) is not None:
            # An edit with no branch changes the CENTRAL record - price and tax
            # for every branch. A branch-level user did that silently whenever
            # the screen had no branch selected (finding R22). Central edits are
            # for the superuser and all-branches roles.
            raise PermissionDenied(
                'Choose a branch to change its price. Changing the central service '
                'for all branches needs an all-branches role.')
        if center_id:
            from .models import CenterService
            # When editing with a center_id, route center-specific fields to CenterService override
            # instead of mutating the global ServiceMaster record.
            center_override_fields = {}

            if 'default_price' in serializer.validated_data:
                center_override_fields['price'] = serializer.validated_data.pop('default_price')

            if 'incentive' in serializer.validated_data:
                # Incentive is per-center — store on CenterService, not globally
                center_override_fields['incentive'] = serializer.validated_data.pop('incentive')

            instance = serializer.save()

            if center_override_fields:
                CenterService.objects.update_or_create(
                    center_id=center_id,
                    service_id=instance.id,
                    defaults=center_override_fields
                )
        else:
            self._to_sap(serializer.save())         # the central record: name, group, tax rate, SAC

    @action(detail=True, methods=['get', 'put'], url_path='recipe')
    def recipe(self, request, pk=None):
        """The products a service uses: GET to read, PUT to replace (Admin > Services > Recipe).

        The recipe is central, like the price: changing it changes every branch, so it
        needs an all-branches role, as a central price edit does."""
        from rest_framework.exceptions import PermissionDenied
        from pos_backend.scoping import user_center_ids
        from . import recipes

        service = self.get_object()
        if request.method == 'GET':
            return Response(recipes.recipe_payload(service))
        if user_center_ids(request.user) is not None:
            raise PermissionDenied('A service recipe is the same for every branch. Changing it '
                                   'needs an all-branches role.')
        return Response(recipes.save_recipe(service, request.data))

    @action(detail=False, methods=['get'], url_path='recipe-products')
    def recipe_products(self, request):
        """What the recipe form can pick: products matching `q` (never retail) and the
        categories an option can offer."""
        from . import recipes
        return Response({'products': recipes.product_choices(request.query_params.get('q', '')),
                         'categories': recipes.recipe_categories()})

    @action(detail=False, methods=['get'], url_path='till-recipes')
    def till_recipes(self, request):
        """What the till offers for each recipe-owning service at a branch: the groups and
        their options with stock, so the person billing picks Colour A, B or C."""
        from rest_framework.exceptions import PermissionDenied
        from pos_backend.scoping import can_use_center
        from salon_admin.models import Center
        from . import recipes

        center_id = coerce_center_id(request.query_params.get('center_id')) or getattr(request.user, 'center_id', None)
        center = Center.objects.filter(pk=center_id).first() if center_id else None
        if center is None:
            return Response({'recipes': {}, 'extras': []})
        if not can_use_center(request.user, center.pk):
            raise PermissionDenied('You are not assigned to this branch.')
        return Response(recipes.till_recipes(center))

    @action(detail=False, methods=['post'])
    def bulk_upload(self, request):
        """Import or update services from a .csv / .xlsx file.

        Fixes in this pass:
          * `center_id` was passed straight to `int()` inside a
            `try/except Center.DoesNotExist`, so 'null' or 'abc' raised
            ValueError and the whole upload failed with a generic message;
          * there was NO permission check on the resolved center. A manager of
            one branch could pass another branch's `center_id` and rewrite that
            branch's service prices;
          * `existing.tax_percentage = tax_percentage` was applied
            unconditionally, and the fallback when the Tax column is absent is
            5%. Re-importing a price list with no Tax column therefore reset
            every matched service's GST rate to 5% - and the tax rate decides
            what customers are charged;
          * a blank Price became 0.0, so a new service was created at zero and
            billed at zero. Blank now means "not stated", and a row that states
            no price for a NEW service is skipped rather than created free;
          * there was no row limit and warnings were truncated to 20 silently.
        """
        from django.db import transaction as db_transaction

        from salon_admin.models import Center as SalonCenter
        from pos_backend.bulk_upload import (
            BulkUploadError, ImportReport, bulk_upload_guard, cell, cell_int,
            cell_percent, read_tabular_file, require_columns,
        )
        from pos_backend.scoping import can_use_center, scope_to_centers

        from .models import CenterService

        RECOGNISED = {
            'sno', 's.no', 'servicecode', 'brand', 'category', 'subcategory',
            'servicename', 'name', 'price', 'defaultprice', 'hsncode', 'hsn',
            'saccode', 'sac', 'tax', 'taxpercentage', 'gst', 'duration',
            'durationmins',
        }
        NAME_COLUMNS = ('servicename', 'name')

        with bulk_upload_guard('services', request) as guard:
            rows = read_tabular_file(request.FILES.get('file'))
            require_columns(rows, RECOGNISED, NAME_COLUMNS, label='service file')

            user = request.user

            # The center this price list belongs to, if any.
            center = None
            center_id = coerce_center_id(
                request.data.get('center_id')
                or request.query_params.get('center_id'))
            if center_id is not None:
                if not can_use_center(user, center_id):
                    raise PermissionDenied('You are not assigned to this center.')
                center = SalonCenter.objects.filter(id=center_id).first()
                if not center:
                    raise BulkUploadError('That center no longer exists.')
            else:
                # No center named. Only assume one when there is exactly one it
                # could be. Taking `.first()` of the caller's accessible centers
                # meant an owner of several branches silently wrote a
                # center-specific override for whichever branch happened to sort
                # first, leaving every other branch on the old price while the
                # response reported success.
                allowed = list(
                    scope_to_centers(SalonCenter.objects.all(), user, field='id')[:2])
                center = allowed[0] if len(allowed) == 1 else None

            # Whether the file states a tax rate at all. Without this the
            # fallback silently became the stored rate.
            tax_columns = {'tax', 'taxpercentage', 'gst'}
            file_states_tax = bool(set(rows[0] or {}) & tax_columns)

            report = ImportReport()
            price_column_present = bool(
                set(rows[0] or {}) & {'price', 'defaultprice'})

            with db_transaction.atomic():
                for index, row in enumerate(rows, start=2):
                    name = cell(row, 'servicename', 'name')
                    if not name:
                        report.skip(index, 'service name is empty.')
                        continue

                    category = cell(row, 'category')
                    sub_category = cell(row, 'subcategory')
                    brand = cell(row, 'brand')
                    service_code = cell(row, 'sno', 's.no', 'servicecode')

                    price = None
                    if price_column_present:
                        from pos_backend.bulk_upload import cell_decimal
                        price = cell_decimal(row, 'price', 'defaultprice')
                        if price is not None and price < 0:
                            report.skip(index, f'price {price} is negative.')
                            continue

                    tax_percentage = None
                    if file_states_tax:
                        tax_percentage = cell_percent(row, 'tax', 'taxpercentage', 'gst')
                        if tax_percentage is None:
                            report.problems.append(
                                f'Row {index}: tax rate not understood; '
                                f'left unchanged.')

                    hsn_code = cell(row, 'hsncode', 'hsn')
                    if hsn_code == '0':
                        hsn_code = ''
                    sac_code = cell(row, 'saccode', 'sac')
                    if sac_code == '0':
                        sac_code = ''

                    duration = cell_int(row, 'duration', 'durationmins')

                    # `__iexact`, like every other importer. A byte-exact
                    # match behaves differently on MySQL (whose default
                    # collation is case-INSENSITIVE) and SQLite, so the same
                    # file would create duplicates on one and update on the
                    # other.
                    existing = ServiceMaster.objects.filter(name__iexact=name).first()

                    if existing:
                        if service_code:
                            existing.service_code = service_code
                        if brand:
                            existing.brand = brand
                        if sub_category:
                            existing.sub_category = sub_category
                        if category:
                            existing.category = category
                        if sac_code:
                            existing.sac_code = sac_code
                        if hsn_code:
                            existing.hsn_code = hsn_code
                        if duration:
                            existing.duration_mins = duration
                        # Only when the file actually said so.
                        if tax_percentage is not None:
                            existing.tax_percentage = tax_percentage

                        if center:
                            existing.centers.add(center)
                            existing.save()
                            # The price for a center-specific import belongs on
                            # CenterService; ServiceMaster.default_price is the
                            # org-wide figure and must not be rewritten by one
                            # branch's price list.
                            override = CenterService.objects.filter(
                                center_id=center.id, service_id=existing.id).first()
                            if price is not None:
                                CenterService.objects.update_or_create(
                                    center_id=center.id, service_id=existing.id,
                                    defaults={'price': price})
                            elif override is None:
                                # No price stated and no override yet: inherit
                                # the org price rather than creating a zero.
                                CenterService.objects.create(
                                    center_id=center.id, service_id=existing.id,
                                    price=existing.default_price)
                        else:
                            if price is not None:
                                existing.default_price = price
                            existing.save()

                        if not existing.sap_sync_status:
                            self._to_sap(existing)
                        report.updated += 1
                    else:
                        if price is None:
                            # Creating a service with no price is what produced
                            # zero-rupee lines on real bills.
                            report.skip(
                                index,
                                f"'{name}' is new and the file states no price, so it "
                                f'was not created. Add a Price column value.')
                            continue

                        service = ServiceMaster.objects.create(
                            service_code=service_code,
                            name=name,
                            brand=brand,
                            category=category,
                            sub_category=sub_category,
                            default_price=price,
                            # A new service with no stated rate keeps the model
                            # default instead of being forced to 5%.
                            **({'tax_percentage': tax_percentage}
                               if tax_percentage is not None else {}),
                            duration_mins=duration or 0,
                            sac_code=sac_code,
                            hsn_code=hsn_code,
                            level='Organisation',
                        )
                        if center:
                            service.centers.add(center)
                            CenterService.objects.update_or_create(
                                center_id=center.id, service_id=service.id,
                                defaults={'price': price})
                        self._to_sap(service)                      # a new service must exist in SAP too
                        report.created += 1

            payload = report.as_dict()
            payload['center'] = (center.display_name or center.center_name) if center else None
            if center is None:
                payload['note'] = (
                    'No branch was named and you have access to more than one, so '
                    'this updated the organisation-wide prices. Pass center_id to '
                    'set one branch’s prices instead.')
            if not file_states_tax:
                payload['tax_note'] = (
                    'The file had no Tax column, so existing GST rates were left '
                    'as they are. Add a Tax column to change them.')
            guard.result = Response(payload)

        return guard.response


class CenterServiceViewSet(viewsets.ModelViewSet):
    serializer_class = CenterServiceSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'services'
    # Per-branch service prices are read wherever a service price is shown.
    permission_read_also = [('billing', None), ('appointments', None), ('finance', None)]
    # An override changes a price that already exists.
    permission_action_verbs = {
        'override': 'update',
    }
    def get_queryset(self):
        user = self.request.user
        queryset = CenterService.objects.all()

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        is_all_centers = is_owner or perms.get('all_centers', False)

        if is_all_centers:
            center_id = coerce_center_id(self.request.query_params.get('center_id'))
            if center_id:
                return queryset.filter(center_id=center_id)
            from pos_backend.scoping import narrow_to_selection
            return narrow_to_selection(queryset, 'center')

        if user.centers.exists():
            from pos_backend.scoping import narrow_to_selection
            return narrow_to_selection(queryset.filter(center__in=user.centers.all()), 'center')
        elif getattr(user, 'center', None):
            return queryset.filter(center=user.center)

        return queryset.none()

    @action(detail=False, methods=['post'])
    def override(self, request):
        user = request.user
        center_id = request.data.get('center_id')

        service_id = request.data.get('service_id')
        price = request.data.get('price')
        is_active = request.data.get('is_active', True)

        if not center_id or not service_id:
            return Response({"error": "Missing center_id or service_id"}, status=status.HTTP_400_BAD_REQUEST)

        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(user.role, 'permissions', {}) or {}
        is_all_centers = is_owner or perms.get('all_centers', False)

        if not is_all_centers:
            try:
                center_id_int = int(center_id)
                if user.centers.exists():
                    if not user.centers.filter(id=center_id_int).exists():
                        raise PermissionDenied("You do not have permission to override prices at this center.")
                elif getattr(user, 'center', None):
                    if user.center.id != center_id_int:
                        raise PermissionDenied("You do not have permission to override prices at this center.")
                else:
                    raise PermissionDenied("You are not assigned to any center.")
            except ValueError:
                return Response({"error": "Invalid center_id"}, status=status.HTTP_400_BAD_REQUEST)

        # Check what was sent before it reaches the database: a price of "abc" used to be stored
        # as it was, and then every bill for that service at that branch crashed reading it.
        from decimal import Decimal, InvalidOperation
        from salon_admin.models import Center
        try:
            center_id, service_id = int(center_id), int(service_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid center_id or service_id"}, status=status.HTTP_400_BAD_REQUEST)
        if not Center.objects.filter(pk=center_id).exists() or not ServiceMaster.objects.filter(pk=service_id).exists():
            return Response({"error": "That centre or service does not exist."}, status=status.HTTP_400_BAD_REQUEST)
        if price in (None, ''):
            price = None                                   # no override: the catalogue price applies
        else:
            try:
                price = Decimal(str(price)).quantize(Decimal('0.01'))
            except (InvalidOperation, ValueError):
                return Response({"price": "Enter the price as a number."}, status=status.HTTP_400_BAD_REQUEST)
            if not price.is_finite() or price < 0 or price > Decimal('99999999.99'):
                return Response({"price": "Enter a price from 0 up to 99,999,999.99."}, status=status.HTTP_400_BAD_REQUEST)
        if isinstance(is_active, str):
            is_active = is_active.strip().lower() not in ('false', '0', 'no', 'off', '')

        obj, created = CenterService.objects.update_or_create(
            center_id=center_id,
            service_id=service_id,
            defaults={'price': price, 'is_active': bool(is_active)}
        )
        return Response(CenterServiceSerializer(obj).data)
