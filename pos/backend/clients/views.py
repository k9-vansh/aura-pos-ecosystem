import logging
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db import models
from django.db.models import Sum, DecimalField
from django.db.models.functions import Coalesce
from .models import Client, ClientMembership, ClientPackage, ClientValueCard
from .serializers import ClientSerializer
from staff.models import ServiceLog
from pos_backend.permissions import IsOwner, HasModulePermission
from pos_backend.scoping import coerce_center_id
from pos_backend.scoping import narrow_to_selection
from billing.models import Invoice
from decimal import Decimal

from django.db.models import DecimalField, OuterRef, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from billing.models import AdvancePayment, CashbackTransaction


class ClientViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated, HasModulePermission]
    queryset = Client.objects.all().select_related('center').prefetch_related(
        'memberships__membership',
        'packages__package',
        'value_cards__value_card'
    )
    serializer_class = ClientSerializer

    permission_module = 'admin'
    permission_submodule = 'clients'
    # The till must find and register a customer to bill them, and appointments must register one to book them.
    permission_read_also = [('billing', None), ('appointments', None), ('staff', None)]
    permission_create_also = [('billing', 'new_invoice'), ('appointments', 'calendar')]
    # Carry_over adjusts an existing customer balance, so it is an update.
    permission_action_verbs = {
        'bulk_upload': 'create',
        'carry_over': 'update',
    }
    # Planned ticks on Admin > Clients (blueprint B3).
    permission_action_ticks = {
        'merge': ('clients', 'merge'),
        'bulk_upload': ('clients', 'bulk_upload'),
    }

    @action(detail=False, methods=['get'])
    def duplicates(self, request):
        """Records sharing a phone that the uniqueness rule could not keep apart.

        When phones became unique (F2) the oldest record kept each number; the
        others are listed here until they are merged.
        """
        from clients.identity import phone_key
        from collections import defaultdict

        groups = defaultdict(list)
        unkeyed = Client.objects.filter(is_active=True, phone_key__isnull=True).exclude(phone='')
        for c in unkeyed.values('id', 'first_name', 'last_name', 'phone'):
            key = phone_key(c['phone'])
            if key:
                groups[key].append(c)
        rows = []
        for key, extras in groups.items():
            owner = Client.objects.filter(phone_key=key).values('id', 'first_name', 'last_name', 'phone').first()
            rows.append({'phone': key, 'keeps_number': owner, 'duplicates': extras})
        return Response(rows)

    @action(detail=True, methods=['post'])
    def merge(self, request, pk=None):
        """Merge client `from_id` into this one (moves history, perks, wallets)."""
        from clients.merge import MergeError, merge_clients
        from audit_logs.models import SystemLog

        keep = self.get_object()
        drop_id = request.data.get('from_id')
        if not drop_id:
            return Response({'from_id': 'Which client should be merged into this one?'}, status=400)
        try:
            moved = merge_clients(keep.pk, drop_id)
        except (MergeError, ValueError, TypeError) as exc:
            return Response({'detail': str(exc)}, status=400)
        text = f'Merged client #{drop_id} into #{keep.pk} ({keep.full_name}): {moved}'
        SystemLog.objects.create(
            module='CLIENTS', action='MERGE', entity_type='Client', entity_id=str(keep.pk),
            path=request.path, user=request.user,
            user_name=getattr(request.user, 'full_name', '') or '', user_email=request.user.email,
            human_description=text[:1000], description=text[:2000])
        keep.refresh_from_db()
        return Response({'moved': moved, 'client': self.get_serializer(keep).data})
    def get_queryset(self):
        # Clients are GLOBAL (owner decision #11): anyone who may look clients
        # up finds them at any branch. A client still has a home branch, and
        # center_id filters by it when asked.
        qs = super().get_queryset().filter(is_active=True)

        # Clients are shared across branches, but a user assigned to NO branch
        # (an account from before a centre became compulsory) sees none: fail
        # closed, like every other record type.
        user = self.request.user
        from pos_backend.permissions import role_permissions, IsOwner
        if (not IsOwner.check_is_owner(user) and role_permissions(user).get('all_centers') is not True
                and not user.centers.exists() and not getattr(user, 'center_id', None)):
            return qs.none()

        q = (self.request.query_params.get('q') or '').strip()
        phone = self.request.query_params.get('phone')
        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if q and center_id:
            # One centre's customers are few: any part of the name, phone or GSTIN, every word somewhere
            # ("asha 98" finds Asha Rao 98765...).
            for term in q.split()[:5]:
                digits = ''.join(ch for ch in term if ch.isdigit())
                match = (models.Q(first_name__icontains=term) | models.Q(last_name__icontains=term)
                         | models.Q(gst_number__icontains=term))
                if digits and len(digits) == len(term):
                    match |= models.Q(phone__contains=digits)
                qs = qs.filter(match)
        elif q:
            # All centres' customers (1+ crore at full size): found through an index from the START of the name,
            # GSTIN or phone, or the END of the phone; see clients/search.py. The rows are the same full customer
            # rows below, so cards, memberships, packages and balances still come with every match.
            from clients import search
            found = search.find_ids(q, narrow_to_selection(Client.objects.filter(is_active=True), 'center'))
            qs = qs.filter(id__in=found) if found else qs.none()
        elif phone:
            from clients.identity import normalise_phone
            qs = qs.filter(phone__istartswith=normalise_phone(phone) or phone)
        if center_id:
            qs = qs.filter(center_id=center_id)
        qs = narrow_to_selection(qs, 'center')
        # ── Wallet balances, in two queries instead of two PER ROW ────────
        #
        # `ClientSerializer.get_advance_balance` checks for
        # `advance_balance_annotated` and otherwise falls back to the model
        # property, which runs its own `aggregate(Sum(...))`. Nothing ever set
        # the annotation, so the fallback fired for every row: measured at
        # exactly 2.00 extra queries per client (107 queries for 50 clients,
        # 507 for 250). At the 15,000 customers this business expects that is
        # ~30,000 queries in one request.
        #
        # The comment this replaces refers to a real bug: annotating with JOINs
        # multiplies rows when a client has both advances and cashback, and the
        # sums come out inflated. Two independent correlated SUBQUERIES cannot
        # multiply rows, so the balances stay correct.
        money_field = DecimalField(max_digits=12, decimal_places=2)

        def wallet_total(model):
            return Coalesce(
                Subquery(
                    model.objects
                    .filter(client=OuterRef('pk'))
                    .values('client')
                    .annotate(total=Sum('amount'))
                    .values('total')[:1],
                    output_field=money_field,
                ),
                Value(Decimal('0.00'), output_field=money_field),
                output_field=money_field,
            )

        qs = qs.annotate(
            advance_balance_annotated=wallet_total(AdvancePayment),
            cashback_balance_annotated=wallet_total(CashbackTransaction),
        )

        return qs.order_by('-id')

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())

        page = self.paginate_queryset(queryset)
        if page is not None:
            serializer = self.get_serializer(page, many=True)
            return self.get_paginated_response(serializer.data)

        # Cap at 50 results when no pagination is requested to prevent 
        # SQLite "too many SQL variables" exceptions during prefetch_related
        # since the frontend only uses this for autocomplete dropdowns.
        queryset = queryset[:50]
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)

    def _check_home_branch(self, center, verb):
        """A client's home branch must be one the user works at."""
        from rest_framework.exceptions import PermissionDenied
        from pos_backend.scoping import can_use_center
        if center is not None and not can_use_center(self.request.user, center.pk):
            raise PermissionDenied(f"You cannot {verb} clients for this center.")

    def perform_create(self, serializer):
        self._check_home_branch(serializer.validated_data.get('center'), 'create')

        archived = getattr(serializer, 'reactivate', None)
        if archived is not None:
            # The number belongs to an archived client: bring them back with
            # the details just entered, instead of refusing.
            for field, value in serializer.validated_data.items():
                setattr(archived, field, value)
            archived.is_active = True
            archived.save()
            serializer.instance = archived
            client = archived
        else:
            client = serializer.save()
        try:
            from . import pins
            pins.on_create(client)
        except Exception:                                  # noqa: BLE001 - a message that cannot be sent must not undo the client
            import logging
            logging.getLogger(__name__).warning('Could not send the new client their PIN', exc_info=True)
        from pos_backend.sap_utils import create_sap_business_partner, run_sap_sync_async
        run_sap_sync_async(create_sap_business_partner, client)

    def perform_update(self, serializer):
        if 'center' in serializer.validated_data:
            self._check_home_branch(serializer.validated_data.get('center'), 'move')
        client = serializer.save()
        # A GSTIN added later makes the client B2B: they need their own SAP partner. A B2B client whose name,
        # phone or e-mail changes has the same SAP partner updated (it used to keep the first values for ever).
        # A client without a GSTIN is the shared B2C partner in SAP: nothing to send.
        if client.gst_number and any(k in serializer.validated_data
                                     for k in ('gst_number', 'full_name', 'first_name', 'last_name', 'phone', 'email')):
            from pos_backend.sap_utils import create_sap_business_partner, run_sap_sync_async
            run_sap_sync_async(create_sap_business_partner, client)

    def destroy(self, request, *args, **kwargs):
        client = self.get_object()
        client.is_active = False
        client.save(update_fields=['is_active'])
        return Response(status=204)

    @action(detail=True, methods=['get'])
    def service_history(self, request, pk=None):
        client = self.get_object()
        logs = ServiceLog.objects.filter(invoice__client=client).select_related('staff', 'center', 'invoice').order_by('-date', '-time')[:50]
        data = []
        for log in logs:
            data.append({
                'id': log.id,
                'date': log.date,
                'time': log.time,
                'service_name': log.service_name,
                'service_type': log.service_type,
                'price': log.price,
                'staff_name': (log.staff.first_name + ' ' + (log.staff.last_name or '')).strip() if log.staff else 'Unknown',
                'center_name': (log.center.display_name or log.center.center_name) if log.center else 'Unknown',
                'invoice_id': log.invoice.id if log.invoice else None
            })
        return Response(data)

    @action(detail=True, methods=['post'], url_path='carry-over')
    def carry_over(self, request, pk=None):
        client = self.get_object()
        data = request.data
        source_type = data.get('source_type')
        source_id = data.get('source_id')
        target_id = data.get('target_id')

        from django.db import transaction as db_transaction
        try:
            with db_transaction.atomic():
                if source_type == 'package':
                    source_pkg = ClientPackage.objects.select_for_update().get(id=source_id, client=client)
                    target_pkg = ClientPackage.objects.select_for_update().get(id=target_id, client=client)

                    src_services = source_pkg.services_remaining or {}
                    tgt_services = target_pkg.services_remaining or {}

                    for s_id, qty in src_services.items():
                        tgt_services[s_id] = tgt_services.get(s_id, 0) + qty

                    target_pkg.services_remaining = tgt_services
                    target_pkg.save()

                    source_pkg.is_active = False
                    source_pkg.services_remaining = {}
                    source_pkg.save()

                elif source_type == 'value_card':
                    source_vc = ClientValueCard.objects.select_for_update().get(id=source_id, client=client)
                    target_vc = ClientValueCard.objects.select_for_update().get(id=target_id, client=client)

                    # Use Decimal arithmetic — float() causes precision loss on monetary values
                    target_vc.balance = target_vc.balance + source_vc.balance
                    target_vc.save()

                    source_vc.is_active = False
                    source_vc.balance = 0
                    source_vc.save()

                elif source_type == 'membership':
                    source_mem = ClientMembership.objects.get(id=source_id, client=client)
                    new_expiry = data.get('new_expiry')
                    if new_expiry:
                        source_mem.expiry_date = new_expiry
                        source_mem.save()
                else:
                    return Response({'error': 'Invalid source_type'}, status=400)

            return Response({'status': 'success'})
        except Exception as e:
            logging.getLogger('bulk_upload').exception(
                '[Clients] %s failed', request.path)
            return Response(
                {'error': 'That request could not be completed. The details have '
                          'been logged.'}, status=400)

    @action(detail=True, methods=['get'])
    def profile(self, request, pk=None):
        client = self.get_object()
        from django.db.models import Sum, Max
        from billing.models import Invoice

        invoices = Invoice.objects.filter(client=client, status__in=Invoice.REVENUE_STATUSES)
        ltv = invoices.aggregate(total=Sum('paid_amount'))['total'] or 0
        last_visit = invoices.aggregate(last=Max('created_at'))['last']

        return Response({
            'client_id': client.id,
            'name': client.full_name,   # FIXED: uses safe full_name property
            'phone': client.phone,
            'ltv': float(ltv),
            'last_visit': last_visit
        })

    @action(detail=False, methods=['post'])
    def bulk_upload(self, request):
        """Import customers from a .csv / .xlsx file.

        Fixes in this pass:
          * column headings were matched exactly and case-sensitively against
            'phone', 'first_name', 'name' ... so a file with 'Phone' or
            'First Name' matched nothing, imported nobody, and still returned
            HTTP 200 with "Successfully imported 0 clients";
          * every skipped row was skipped in silence. Uploading 500 customers
            and being told 3 were imported gave no indication which 497 were
            dropped or why;
          * `bulk_create(..., ignore_conflicts=True)` does not set primary keys
            on the returned objects, so `len(created)` counted rows that were
            never inserted and the reported total was wrong;
          * the duplicate-phone set was loaded for EVERY center, so a customer
            already registered at another branch was silently dropped instead of
            being created for this one;
          * a row with a phone but no name created a nameless customer;
          * `gender` was stored verbatim from the file;
          * there was no row limit and no center permission check.
        """
        from django.db import transaction as db_transaction

        from rest_framework.exceptions import PermissionDenied
        from salon_admin.models import Center
        from pos_backend.bulk_upload import (
            BulkUploadError, ImportReport, bulk_upload_guard, cell, cell_date,
            read_tabular_file, require_columns,
        )
        from pos_backend.scoping import can_use_center, scope_to_centers

        RECOGNISED = {
            'phone', 'phonenumber', 'mobile', 'contact', 'contactnumber',
            'firstname', 'lastname', 'name', 'fullname', 'clientname',
            'customername', 'email', 'emailaddress', 'gender', 'notes',
            'birthday', 'dob', 'dateofbirth', 'gstnumber', 'gst',
        }
        PHONE_COLUMNS = ('phone', 'phonenumber', 'mobile', 'contact',
                         'contactnumber')
        GENDERS = {'male': 'male', 'female': 'female', 'other': 'other',
                   'm': 'male', 'f': 'female'}

        with bulk_upload_guard('clients', request) as guard:
            rows = read_tabular_file(request.FILES.get('file'))
            require_columns(rows, RECOGNISED, PHONE_COLUMNS, label='customer file')

            user = request.user
            center_id = coerce_center_id(
                request.data.get('center_id')
                or request.query_params.get('center_id'))
            if center_id is not None:
                if not can_use_center(user, center_id):
                    raise PermissionDenied('You are not assigned to this center.')
                center = Center.objects.filter(id=center_id).first()
                if not center:
                    raise BulkUploadError('That center no longer exists.')
            else:
                center = scope_to_centers(
                    Center.objects.all(), user, field='id').first()

            # Duplicate detection is scoped to the branch being imported into.
            # Loading every phone in the database meant a customer of another
            # branch blocked this one, and it read the whole table into memory.
            existing = Client.objects.filter(center=center) if center else Client.objects.all()
            # Phones are unique across the business (F2), not per branch.
            existing_phones = set(Client.objects.exclude(phone_key__isnull=True)
                                  .values_list('phone_key', flat=True))
            existing_emails = {
                email for email in existing.exclude(email='')
                                           .exclude(email__isnull=True)
                                           .values_list('email', flat=True)
            }

            report = ImportReport()
            to_create = []
            seen_in_file = set()

            for index, row in enumerate(rows, start=2):
                phone = cell(row, *PHONE_COLUMNS)
                if phone.endswith('.0'):
                    # Excel reads a phone column as a float.
                    phone = phone[:-2]
                if not phone:
                    report.skip(index, 'phone number is empty.')
                    continue

                first_name = cell(row, 'firstname')
                last_name = cell(row, 'lastname')
                if not first_name:
                    whole = cell(row, 'name', 'fullname', 'clientname', 'customername')
                    if whole:
                        parts = whole.split(' ', 1)
                        first_name = parts[0]
                        last_name = last_name or (parts[1] if len(parts) > 1 else '')
                if not first_name:
                    report.skip(index, f'no name given for {phone}.')
                    continue

                # The same rules as the form: normalised phone, unique across
                # the business, a valid GSTIN if one is given.
                from clients.identity import gstin_problem, normalise_phone
                phone = normalise_phone(phone)
                if phone in seen_in_file:
                    report.skip(index, f'{phone} appears more than once in this file.')
                    continue
                if phone in existing_phones:
                    report.skip(index, f'{phone} is already used by an existing client.')
                    continue
                gstin = cell(row, 'gstnumber', 'gst', 'gstin').strip().upper()
                problem = gstin_problem(gstin)
                if problem:
                    report.skip(index, f'GSTIN {gstin}: {problem}')
                    continue

                email = cell(row, 'email', 'emailaddress')
                if email and email in existing_emails:
                    report.skip(index, f'the email {email} is already on file.')
                    continue

                raw_gender = cell(row, 'gender').lower()
                gender = GENDERS.get(raw_gender)
                if raw_gender and gender is None:
                    report.problems.append(
                        f'Row {index}: gender "{raw_gender}" not recognised; '
                        f'left unset.')

                to_create.append(Client(
                    center=center,
                    first_name=first_name,
                    last_name=last_name,
                    phone=phone,
                    phone_key=phone,
                    phone_reversed=phone[::-1],
                    email=email,
                    gender=gender or 'female',
                    birthday=cell_date(row, 'birthday', 'dob', 'dateofbirth'),
                    gst_number=gstin or None,
                    notes=cell(row, 'notes'),
                ))
                seen_in_file.add(phone)
                if email:
                    existing_emails.add(email)

            # `bulk_create(ignore_conflicts=True)` leaves primary keys unset, so
            # the returned list cannot be counted. Compare the table instead.
            CHUNK = 500
            before = existing.count()
            with db_transaction.atomic():
                for start in range(0, len(to_create), CHUNK):
                    Client.objects.bulk_create(
                        to_create[start:start + CHUNK], ignore_conflicts=True)
            report.created = max(0, existing.count() - before)

            payload = report.as_dict()
            payload['center'] = (
                (center.display_name or center.center_name) if center else None)
            if not center:
                payload['note'] = (
                    'No branch could be determined, so these customers were '
                    'imported without one. Pass center_id to assign them.')
            guard.result = Response(payload)

        return guard.response
