import datetime
from rest_framework import viewsets, status
from django.db import transaction
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import MethodNotAllowed, PermissionDenied, ValidationError
from django.db.models import Q
from salon_admin.models import Center
from .models import WhatsAppMessage, Promotion, ValueCard, Membership, Package, PromotionUsage, CouponCode
from . import coupons
from .serializers import (
    WhatsAppMessageSerializer, PromotionSerializer,
    ValueCardSerializer, MembershipSerializer, PackageSerializer
)
from django.db.models import Count
from rest_framework.decorators import action
from rest_framework.response import Response
from django.contrib.contenttypes.models import ContentType
from billing.models import InvoiceItem
from clients.models import ClientMembership, ClientValueCard
from pos_backend.permissions import IsOwner, HasModulePermission, has_module_permission
from pos_backend.scoping import (
    assert_can_write_center, coerce_center_id, safe_date_param,
    selected_center_ids, user_center_ids,
)

# A campaign body longer than this is a mistake, and it is written once per
# recipient.
MAX_CAMPAIGN_MESSAGE_CHARS = 4000

# Actions that edit one existing row. They must be able to reach a row that is
# switched off (to switch it back on) or past its dates (to extend it).
ROW_ACTIONS = ('retrieve', 'update', 'partial_update', 'destroy', 'toggle_status',
               'codes', 'export_codes', 'add_codes', 'generate_codes', 'void_code')


def timezone_text(value):
    """A stored moment as local 'YYYY-MM-DD HH:MM', or '' when there is none."""
    from django.utils import timezone

    if not value:
        return ''
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return value.strftime('%Y-%m-%d %H:%M')


class MarketingBaseViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]

    # ── who is asking ─────────────────────────────────────────────────────
    def _has_all_centers(self):
        user = self.request.user
        perms = getattr(getattr(user, 'role', None), 'permissions', None) or {}
        return IsOwner.check_is_owner(user) or bool(perms.get('all_centers', False))

    def _can_manage(self):
        """May this caller see switched-off rows? Anyone who can change them:
        otherwise a branch manager could switch an offer off and never get it
        back (the row vanished from their screen and returned 404)."""
        if self._has_all_centers():
            return True
        sub = getattr(self, 'permission_submodule', None)
        return bool(sub) and has_module_permission(self.request.user, 'marketing', 'update', sub)

    # ── what they may see ─────────────────────────────────────────────────
    def scope_queryset(self, include_inactive=False):
        """Rows in the caller's branches (plus organisation-level ones)."""
        user = self.request.user
        queryset = super().get_queryset()
        model = self.serializer_class.Meta.model
        has_level = hasattr(model, 'level')
        is_all_centers = self._has_all_centers()

        if not include_inactive and hasattr(model, 'is_active'):
            queryset = queryset.filter(is_active=True)

        def within(ids=None, centers=None):
            if centers is not None:
                cond = Q(center__in=centers)
            else:
                cond = Q(center_id__in=ids)
            return queryset.filter(cond | Q(level='Organisation')) if has_level else queryset.filter(cond)

        if is_all_centers:
            # `coerce_center_id` already returns an int or None.
            center_id = coerce_center_id(self.request.query_params.get('center_id'))
            if center_id is not None:
                # Explicit filter: that branch's own rows AND the organisation-level ones
                # (WhatsApp messages have no 'level').
                return within(ids=[center_id])
            picked = selected_center_ids()
            if picked:
                return within(ids=list(picked))
            return queryset

        # Everyone else sees their own branches' rows (and organisation-level ones).
        allowed = user_center_ids(user)
        if allowed:
            return within(ids=list(allowed))
        # No branch and not an owner: nothing.
        return queryset.none()

    def get_queryset(self):
        wants_inactive = self.request.query_params.get('show_inactive', 'false').lower() == 'true'
        include_inactive = (wants_inactive and self._can_manage()) or self.action in ROW_ACTIONS
        return self.scope_queryset(include_inactive=include_inactive)

    # ── deleting ──────────────────────────────────────────────────────────
    def _in_use_message(self, instance):
        """Why this row must not be deleted, or None.

        A used coupon takes its usage history (and the ROI report) with it, and a
        sold card, membership or package leaves invoice lines pointing at
        nothing. Switching it off keeps all of that."""
        label = getattr(instance, 'name', None) or getattr(instance, 'title', '')
        if hasattr(instance, 'usages') and instance.usages.exists():
            return (f'"{label}" has already been used on customers\' bills, and deleting it would erase '
                    f'that history from the usage report.')
        if isinstance(instance, Promotion) and instance.codes.filter(is_used=True).exists():
            return (f'Coupons of "{label}" have already been used on customers\' bills, and deleting it '
                    f'would erase them and what the brand owes.')
        if not isinstance(instance, Promotion):
            sold = InvoiceItem.objects.filter(
                content_type=ContentType.objects.get_for_model(instance), object_id=instance.pk).exists()
            if sold:
                return (f'"{label}" has already been sold on bills, and deleting it would leave those '
                        f'bills pointing at nothing.')
        return None

    def destroy(self, request, *args, **kwargs):
        """Refuse to delete something customers are holding or have used.

        Deleting a ValueCard, Package or Membership template used to CASCADE to
        every customer's issued card, stripping their prepaid balance and their
        remaining entitlement across all branches, with no audit row and nothing
        to restore from. The list view hides inactive rows, which actively
        encouraged deleting an expired offer rather than deactivating it.

        The FKs are PROTECT now, so the database refuses. This turns that into an
        answer an operator can act on instead of a 500.
        """
        from django.db.models.deletion import ProtectedError

        instance = self.get_object()
        blocked = self._in_use_message(instance)
        if blocked:
            return Response({
                'error': blocked,
                'what_to_do': 'Switch it off instead - set it inactive. It stops being offered and the history stays.',
            }, status=status.HTTP_409_CONFLICT)
        try:
            with transaction.atomic():
                self.perform_destroy(instance)
        except ProtectedError:
            label = getattr(instance, 'name', None) or getattr(instance, 'title', '')
            return Response({
                'error': (f'"{label}" cannot be deleted because customers are '
                          f'holding it. Their balances and entitlements would go '
                          f'with it.'),
                'what_to_do': ('Switch it off instead - set it inactive. It will '
                               'stop being sold and existing holders keep what '
                               'they paid for.'),
            }, status=status.HTTP_409_CONFLICT)
        return Response(status=status.HTTP_204_NO_CONTENT)

    # ── writing ───────────────────────────────────────────────────────────
    def perform_create(self, serializer):
        user = self.request.user
        is_all_centers = self._has_all_centers()
        data = serializer.validated_data
        model = self.serializer_class.Meta.model
        if not hasattr(model, 'level'):
            serializer.save()
            return

        level = data.get('level')
        if level is None:
            # Nothing said: a branch user's item belongs to their branch.
            level = 'Organisation' if (is_all_centers and not data.get('center')) else 'Center'

        if level == 'Organisation':
            if not is_all_centers:
                raise PermissionDenied("Only owners or users with all-center access can create organisation level items.")
            serializer.save(level='Organisation', center=None)
            return

        center = data.get('center')
        if is_all_centers:
            if center is None:
                raise ValidationError({'center': 'Choose the branch this belongs to.'})
        else:
            if center is None:
                # A branch user who does not pick one gets their own.
                if user.centers.exists():
                    center = user.centers.first()
                elif getattr(user, 'center', None):
                    center = user.center
                else:
                    raise PermissionDenied("User is not assigned to any center.")
            # ...and cannot write to a branch that is not theirs.
            assert_can_write_center(user, center)
        serializer.save(level='Center', center=center)

    def perform_update(self, serializer):
        user = self.request.user
        is_all_centers = self._has_all_centers()
        instance = serializer.instance
        data = serializer.validated_data

        if not hasattr(instance, 'level'):
            serializer.save()
            return

        # The check used to look only at the row's CURRENT level, so a branch user
        # could PATCH their own item to `level=Organisation` (live in every
        # branch) or point `center` at a branch that is not theirs.
        new_level = data.get('level', instance.level)
        if (instance.level == 'Organisation' or new_level == 'Organisation') and not is_all_centers:
            raise PermissionDenied("Only owners or users with all-center access can create, modify or delete organisation level items.")

        if 'level' not in data and 'center' not in data:
            serializer.save()
            return

        if new_level == 'Organisation':
            serializer.save(level='Organisation', center=None)
            return
        center = data['center'] if 'center' in data else instance.center
        if center is None:
            raise ValidationError({'center': 'Choose the branch this belongs to.'})
        if not is_all_centers:
            assert_can_write_center(user, center)
        serializer.save(level='Center', center=center)

    def perform_destroy(self, instance):
        if hasattr(instance, 'level') and instance.level == 'Organisation' and not self._has_all_centers():
            raise PermissionDenied("Only owners or users with all-center access can delete organisation level items.")

        instance.delete()

    @action(detail=True, methods=['patch'], url_path='toggle-status')
    def toggle_status(self, request, pk=None):
        """Dedicated endpoint to toggle is_active. Reaches switched-off and expired rows too."""
        obj = self.get_object()
        if not hasattr(obj, 'is_active'):
            raise MethodNotAllowed(request.method)
        if hasattr(obj, 'level') and obj.level == 'Organisation' and not self._has_all_centers():
            raise PermissionDenied("Only owners or users with all-center access can modify organisation level items.")
        obj.is_active = not obj.is_active
        obj.save(update_fields=['is_active'])
        return Response({'id': obj.id, 'is_active': obj.is_active})


class WhatsAppMessageViewSet(MarketingBaseViewSet):
    """The message log. Read-only over the API: rows are written by the sender
    and settled by delivery updates, so nobody can forge or delete a line."""
    queryset = WhatsAppMessage.objects.all().select_related('center').order_by('-created_at')
    serializer_class = WhatsAppMessageSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'marketing'
    permission_submodule = 'whatsapp'
    # Customer messages: only WhatsApp reads them, not "any Marketing access" (QA M11).
    permission_strict_read = True
    # Sending a campaign creates message records.
    permission_action_verbs = {
        'send_campaign': 'create',
        'campaign_preview': 'create',
    }

    def create(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    def update(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    def destroy(self, request, *args, **kwargs):
        raise MethodNotAllowed(request.method)

    def _campaign_scope(self, request):
        """(branch ids to draw the audience from or None for everyone, error response)."""
        user = request.user
        raw_center = request.data.get('center_id')
        wants_all = raw_center is None or str(raw_center).strip().lower() in ('', 'all')
        requested = None if wants_all else coerce_center_id(raw_center)
        if not wants_all and requested is None:
            return None, Response({'error': 'That branch could not be identified.'},
                                  status=status.HTTP_400_BAD_REQUEST)

        allowed = user_center_ids(user)          # None = every branch
        if allowed is not None and not allowed:
            return None, Response({'error': 'You do not have access to any centers.'},
                                  status=status.HTTP_403_FORBIDDEN)
        if requested is not None:
            if allowed is not None and requested not in allowed:
                return [], None                  # a branch that is not theirs reaches nobody
            return [requested], None
        return (None if allowed is None else sorted(allowed)), None

    def _campaign_message(self, request):
        raw = request.data.get('message')
        if raw is not None and not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
            return None, Response({'error': 'The message must be text.'}, status=status.HTTP_400_BAD_REQUEST)
        message = ('' if raw is None else str(raw)).strip()
        if not message:
            return None, Response({'error': 'Message is required'}, status=status.HTTP_400_BAD_REQUEST)
        if len(message) > MAX_CAMPAIGN_MESSAGE_CHARS:
            return None, Response({
                'error': f'That message is {len(message)} characters, over the '
                         f'{MAX_CAMPAIGN_MESSAGE_CHARS} character limit.',
            }, status=status.HTTP_400_BAD_REQUEST)
        return message, None

    NOBODY = ('No eligible customers found. Everyone in the selected branch is on DND, has not '
              'agreed to WhatsApp, is blacklisted, inactive, or has no phone number on file.')

    @action(detail=False, methods=['post'])
    def campaign_preview(self, request):
        """How many customers a broadcast would reach - before anything is sent."""
        from notifications import integrations
        from . import campaigns

        center_ids, error = self._campaign_scope(request)
        if error:
            return error
        message = (request.data.get('message') or '').strip()
        counts = campaigns.count_audience(center_ids)
        counts.update({
            'gateway_configured': integrations.is_ready('whatsapp'),
            'background': counts['eligible'] > campaigns.INLINE_LIMIT,
            'duplicate': bool(message) and campaigns.recent_duplicate(message, center_ids) is not None,
        })
        return Response(counts)

    @action(detail=False, methods=['post'])
    def send_campaign(self, request):
        # Who may send is the Marketing > WhatsApp "create" tick (HasModulePermission),
        # not a role's name.
        from notifications import integrations
        from . import campaigns
        from .models import WhatsAppCampaign

        # `center_id` was passed straight into `filter(center_id=...)`, so a junk
        # value raised ValueError - a 500 on a marketing screen.
        center_ids, error = self._campaign_scope(request)
        if error:
            return error
        message, error = self._campaign_message(request)
        if error:
            return error

        # Who may be contacted: active, not blacklisted, a phone, not on DND, and
        # - first of all - consent: a client who has not agreed to WhatsApp is
        # never messaged, whatever the branch wants to say (B5.7).
        counts = campaigns.count_audience(center_ids)
        if counts['eligible'] == 0:
            return Response({'error': self.NOBODY}, status=status.HTTP_404_NOT_FOUND)

        # The same text to the same people a moment ago is a double-click, or a
        # second operator doing the same job. It needs a deliberate yes.
        repeat = str(request.data.get('confirm_repeat', '')).lower() in ('1', 'true', 'yes')
        earlier = campaigns.recent_duplicate(message, center_ids)
        if earlier is not None and not repeat:
            return Response({
                'error': ('This exact message was already sent to the same customers a few minutes ago. '
                          'Send it again only if you really mean to.'),
                'duplicate': True,
                'sent_at': earlier.created_at,
            }, status=status.HTTP_409_CONFLICT)

        ready = integrations.is_ready('whatsapp')
        status_text = 'Queued' if ready else 'Not sent - WhatsApp not set up'
        campaign = WhatsAppCampaign.objects.create(
            message=message, scope_key=campaigns.scope_key(center_ids),
            center_id=(center_ids[0] if center_ids is not None and len(center_ids) == 1 else None),
            created_by=request.user, recipients=counts['eligible'])

        background = counts['eligible'] > campaigns.INLINE_LIMIT
        if background:
            from pos_backend.jobs import enqueue
            enqueue('marketing.send_campaign',
                    {'campaign_id': campaign.pk, 'center_ids': center_ids},
                    dedupe_key=f'marketing:campaign:{campaign.pk}')
            queued = counts['eligible']
        else:
            queued = campaigns.run_campaign(campaign.pk, center_ids)

        if background:
            text = (f'{queued} message(s) are being prepared in the background and will appear in the '
                    f'list shortly.' + ('' if ready else ' Nothing will be transmitted: WhatsApp is not set up yet (Admin > Integrations).'))
        elif ready:
            text = f'{queued} message(s) queued for WhatsApp.'
        else:
            text = (f'{queued} message(s) recorded, but nothing was transmitted: WhatsApp is not set up '
                    f'yet (Admin > Integrations).')
        payload = {
            'message': text,
            'count': queued,
            'queued': queued,
            'delivered': 0,
            'gateway_configured': ready,
            'background': background,
            'campaign_id': campaign.pk,
            'status': status_text,
            'skipped_no_consent': counts['skipped_no_consent'],
            'skipped_dnd': counts['skipped_dnd'],
        }
        if counts['without_branch']:
            payload['without_branch'] = counts['without_branch']
        return Response(payload)

class PromotionViewSet(MarketingBaseViewSet):
    queryset = Promotion.objects.all().select_related('center').order_by('-created_at')
    serializer_class = PromotionSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'marketing'
    permission_submodule = 'promotions'
    # Loading, making or withdrawing codes changes an existing promotion.
    permission_action_verbs = {'add_codes': 'update', 'generate_codes': 'update', 'void_code': 'update'}
    # The codes are money: only the Promotions tick shows them. The till reads the
    # offers list, but a cashier (or a role that can read some other marketing tab)
    # must not be able to list or download every coupon code.
    CODE_ACTIONS = ('codes', 'export_codes')

    @property
    def permission_read_also(self):
        return [] if self.action in self.CODE_ACTIONS else [('billing', None)]

    @property
    def permission_strict_read(self):
        return self.action in self.CODE_ACTIONS

    # ── Promotional coupons: the codes ────────────────────────────────────
    def create(self, request, *args, **kwargs):
        if request.data.get('promo_type') != 'Coupon':
            return super().create(request, *args, **kwargs)
        # The promotion and its codes are saved together: a coupon promotion that
        # could not get its codes is not left behind empty.
        with transaction.atomic():
            response = super().create(request, *args, **kwargs)
            promo = Promotion.objects.get(pk=response.data['id'])
            response.data['codes_load'] = self._load_codes(promo, request.data)
            response.data['codes_summary'] = coupons.counts(promo)
        return response

    def _load_codes(self, promo, data):
        source = str(data.get('code_source') or '')
        try:
            if source == 'list':
                text = str(data.get('codes_text') or '')
                if not text.strip():
                    raise ValidationError({'codes_text': "Paste the brand's codes (one on each line)."})
                summary = coupons.import_codes(promo, text)
                if summary['added'] == 0:
                    raise ValidationError({'codes_text': 'None of those codes could be added: '
                                           f"{summary['already_held']} already held, "
                                           f"{summary['repeated_in_list']} repeated, "
                                           f"{summary['invalid']} not valid."})
                return summary
            if source == 'generate':
                return coupons.generate_codes(promo, data.get('quantity'), data.get('code_prefix') or '')
        except coupons.CouponError as exc:
            raise ValidationError({'codes': str(exc)})
        raise ValidationError({'code_source': "Choose how the codes come: the brand's list, or make them here."})

    def _coupon_promotion(self):
        promo = self.get_object()
        if promo.promo_type != 'Coupon':
            raise ValidationError({'detail': 'Only a Promotional Coupons promotion holds codes.'})
        return promo

    def _codes_in_scope(self, promo):
        """The promotion's codes, as this caller may see them: a branch user does
        not see which customer of another branch used a code."""
        qs = promo.codes.select_related('invoice', 'client', 'center')
        allowed = user_center_ids(self.request.user)
        if allowed is not None:
            qs = qs.filter(Q(is_used=False) | Q(center_id__in=list(allowed)))
        return qs

    @staticmethod
    def _code_row(c):
        return {
            'id': c.id, 'code': c.code, 'status': c.status,
            'value': str(c.value) if c.value is not None else None,
            'source': c.source, 'created_at': c.created_at,
            'used_at': c.used_at,
            'bill': (c.invoice.display_number if c.invoice_id else ''),
            'client': (f'{c.client.first_name or ""} {c.client.last_name or ""}'.strip() if c.client_id else ''),
            'client_phone': (c.client.phone if c.client_id else ''),
            'branch': ((c.center.display_name or c.center.center_name) if c.center_id else ''),
            'amount_applied': str(c.amount_applied) if c.is_used else None,
            'times_released': c.times_released,
        }

    def _filtered_codes(self, promo):
        qs = self._codes_in_scope(promo)
        wanted = (self.request.query_params.get('status') or '').lower()
        if wanted == 'unused':
            qs = qs.filter(is_used=False, is_void=False)
        elif wanted == 'used':
            qs = qs.filter(is_used=True)
        elif wanted == 'void':
            qs = qs.filter(is_void=True)
        text = (self.request.query_params.get('q') or '').strip()
        if text:
            key = coupons.normalise(text)
            cond = Q(client__phone__contains=text) | Q(client__first_name__icontains=text)
            if key:
                cond |= Q(code_key__contains=key)
            qs = qs.filter(cond)
        return qs.order_by('-used_at', 'id') if wanted == 'used' else qs.order_by('id')

    @action(detail=True, methods=['get'])
    def codes(self, request, pk=None):
        """The promotion's coupon codes: which are unused, used (and where), withdrawn."""
        promo = self._coupon_promotion()
        qs = self._filtered_codes(promo)
        try:
            page = max(int(request.query_params.get('page') or 1), 1)
            size = min(max(int(request.query_params.get('page_size') or 50), 1), 200)
        except (TypeError, ValueError):
            page, size = 1, 50
        total = qs.count()
        rows = list(qs[(page - 1) * size: page * size])
        return Response({
            'summary': coupons.counts(promo), 'count': total, 'page': page, 'page_size': size,
            'results': [self._code_row(c) for c in rows],
        })

    @action(detail=True, methods=['get'])
    def export_codes(self, request, pk=None):
        """All the codes (for the filter in force) as a CSV, to hand to the brand."""
        import csv
        from django.http import HttpResponse

        promo = self._coupon_promotion()
        response = HttpResponse(content_type='text/csv; charset=utf-8')
        slug = ''.join(ch if ch.isalnum() else '-' for ch in promo.name).strip('-')[:40] or 'coupons'
        response['Content-Disposition'] = f'attachment; filename="coupon-codes-{slug}.csv"'
        response.write('﻿')

        def safe(text):
            # A spreadsheet runs a cell that starts with = + - @ as a formula.
            text = '' if text is None else str(text)
            return "'" + text if text[:1] in ('=', '+', '-', '@') else text

        writer = csv.writer(response)
        writer.writerow(['Code', 'Status', 'Own value (Rs)', 'Made by', 'Used on', 'Bill',
                         'Customer', 'Phone', 'Branch', 'Paid on the bill (Rs)'])
        for c in self._filtered_codes(promo).iterator(chunk_size=1000):
            row = self._code_row(c)
            writer.writerow([safe(row['code']), row['status'], row['value'] or '', row['source'],
                             timezone_text(row['used_at']), safe(row['bill']), safe(row['client']),
                             safe(row['client_phone']), safe(row['branch']), row['amount_applied'] or ''])
        return response

    @action(detail=True, methods=['post'])
    def add_codes(self, request, pk=None):
        """Add more of the brand's codes (a pasted list) to this promotion."""
        promo = self._coupon_promotion()
        text = str(request.data.get('codes_text') or '')
        if not text.strip():
            raise ValidationError({'codes_text': "Paste the brand's codes (one on each line)."})
        try:
            summary = coupons.import_codes(promo, text)
        except coupons.CouponError as exc:
            raise ValidationError({'codes_text': str(exc)})
        return Response({'load': summary, 'codes_summary': coupons.counts(promo)})

    @action(detail=True, methods=['post'])
    def generate_codes(self, request, pk=None):
        """Make more random codes for this promotion."""
        promo = self._coupon_promotion()
        try:
            summary = coupons.generate_codes(promo, request.data.get('quantity'),
                                             request.data.get('code_prefix') or '')
        except coupons.CouponError as exc:
            raise ValidationError({'quantity': str(exc)})
        return Response({'load': summary, 'codes_summary': coupons.counts(promo)})

    @action(detail=True, methods=['post'])
    def void_code(self, request, pk=None):
        """Withdraw an unused code (the brand recalled it), or bring it back."""
        promo = self._coupon_promotion()
        code_id = coerce_center_id(request.data.get('code_id'))      # an id as an int, or None for rubbish
        with transaction.atomic():
            code = (CouponCode.objects.select_for_update()
                    .filter(promotion=promo, pk=code_id).first()) if code_id is not None else None
            if code is None:
                raise ValidationError({'code_id': 'That code is not on this promotion.'})
            if code.is_used:
                raise ValidationError({'code_id': 'This code has been used on a bill, so it cannot be withdrawn.'})
            code.is_void = bool(request.data.get('void', True))
            code.save(update_fields=['is_void'])
        return Response({'code': self._code_row(code), 'codes_summary': coupons.counts(promo)})

    def get_queryset(self):
        qs = super().get_queryset()
        today = datetime.date.today()
        show_expired = self.request.query_params.get('show_expired', 'false').lower() == 'true'

        # Only the LIST hides finished offers. Editing, switching on or deleting
        # one goes by id and must reach it - otherwise an expired offer could
        # never be extended to run again.
        if self.action == 'list' and not show_expired:
            qs = qs.filter(end_date__gte=today)

        # The till's list: only offers that can be used at its branch today.
        usable_at = coerce_center_id(self.request.query_params.get('usable_at'))
        if usable_at is not None:
            qs = (qs.filter(is_active=True, start_date__lte=today, end_date__gte=today)
                  .exclude(promo_type='Coupon')          # coupons are paid by code, not picked
                  .filter(Q(center_id=usable_at) | Q(center__isnull=True, level='Organisation')))
        return qs.order_by('-created_at')

    @action(detail=False, methods=['get'])
    def usage_report(self, request):
        # The Usage tab is its own tick (Marketing > Usage). Reading the offers
        # list, or any other Marketing tab, is not enough to see sales figures.
        if not has_module_permission(request.user, 'marketing', 'read', 'usage'):
            raise PermissionDenied('The usage report needs the Marketing > Usage permission.')
        return self._usage_report(request)

    def _usage_report(self, request):
        """Sales and redemptions of every perk type, per branch.

        The screen is called Usage, and the intent is that a card, membership,
        package or coupon appears here when a customer BUYS it or USES it. It
        only ever showed purchases:

          * memberships, packages and value cards were counted by looking for
            InvoiceItem rows whose content_type is the perk - that is the sale;
          * a package redemption writes an InvoiceItem for the SERVICE with
            `is_redemption=True`, so it was invisible here;
          * paying with a value card writes a Payment row, not an InvoiceItem,
            so that was invisible too.

        So a package sold once and drawn down eight times read as "1", and the
        screen could not answer the question it exists to answer: are these
        things actually being used, or just sold?

        Three other defects in the same handler:

          * the status filter was
            ['paid','partial','Paid','Partial','completed','Completed','draft',
             'Draft'] - it counted unfinished bills still open in the till, and
            four of those values are not valid invoice statuses at all. Every
            other report in the POS counts ['paid','partial'], so Usage and
            Finance disagreed about the same month, every month, and an
            abandoned draft inflated Usage permanently;
          * the branch filter read a `Center-Id` HTTP header, which nothing
            sends - the frontend passes `center_id` as a query parameter - so
            selecting a branch did nothing;
          * a non-owner was scoped by `user.center` alone, ignoring the
            `centers` many-to-many, so a manager of two branches saw one;
          * `Count('id')` counted invoice LINES, so selling three memberships on
            one line counted as one.
        """
        from django.db.models import IntegerField, Sum, Value
        from django.db.models.functions import Coalesce

        from billing.models import Invoice, Payment
        from pos_backend.scoping import scope_to_centers

        start_date = safe_date_param(request.query_params.get('start_date'))
        end_date = safe_date_param(request.query_params.get('end_date'))
        requested_center = coerce_center_id(request.query_params.get('center_id'))

        user = request.user

        # ── Which branches may this caller see, and which did they ask for? ──
        allowed_centers = scope_to_centers(
            Center.objects.all(), user, field='id',
            requested_center=requested_center)
        allowed_ids = set(allowed_centers.values_list('id', flat=True))
        # The money figures are narrowed to `allowed_ids` whenever the caller is
        # limited to some branches OR asked for one. This used to be
        # `if allowed_ids:`, so a request for a branch the caller may not see (or a
        # caller with no branch) left an EMPTY set that switched the filter off -
        # and returned every branch's sales on organisation-level perks.
        restrict = (user_center_ids(user) is not None) or requested_center is not None
        today = datetime.date.today()

        def branch_name(display, legal):
            return display or legal or 'Organisation / All Centers'

        def perk_name(obj):
            # ValueCard calls it `title`; Membership, Package and Promotion call
            # it `name`. Reading `.name` unconditionally would 500 the screen.
            return getattr(obj, 'name', None) or getattr(obj, 'title', None) or ''

        # ── The catalog of perks, scoped the same way for all four types ─────
        def scoped(queryset):
            # Organisation-level perks belong to every branch.
            qs = queryset.filter(
                Q(level='Organisation') | Q(center_id__in=allowed_ids)
                | Q(center__isnull=True))
            if requested_center is not None:
                qs = qs.filter(
                    Q(level='Organisation') | Q(center_id=requested_center)
                    | Q(center__isnull=True))
            return qs

        # Every offer in the caller's scope, switched off or finished or not: a
        # report on last month must still show the offer that ended last week.
        # The list screen hides those; this one cannot.
        promotions = self.scope_queryset(include_inactive=True)
        if start_date or end_date:
            overlaps = Q()
            if start_date:
                overlaps &= Q(end_date__gte=start_date)
            if end_date:
                overlaps &= Q(start_date__lte=end_date)
            promotions = promotions.filter(overlaps | Q(usages__isnull=False)).distinct()
        memberships = scoped(Membership.objects.all().select_related('center'))
        packages = scoped(Package.objects.all().select_related('center'))
        cards = scoped(ValueCard.objects.all().select_related('center'))

        data_map = {}

        def entry(key, name, type_str, obj=None, start=None, end=None,
                  level=None, active=True):
            if key not in data_map:
                data_map[key] = {
                    'id': key,
                    'name': name,
                    'type': type_str,
                    'start_date': start,
                    'end_date': end,
                    'level': level,
                    'status': ('Inactive' if not active
                               else 'Expired' if end and end < today else 'Active'),
                    # `count` stays for any caller that already reads it, and is
                    # now sold + used rather than lines-of-sale.
                    'count': 0,
                    'sold': 0,
                    'used': 0,
                    'revenue': 0,
                    'center_breakdown': [],
                }
            return data_map[key]

        def add(key, center_name, sold=0, used=0, revenue=0):
            row = data_map.get(key)
            if row is None:
                return
            row['sold'] += sold
            row['used'] += used
            row['count'] += sold + used
            row['revenue'] += revenue

            for existing in row['center_breakdown']:
                if existing['center_name'] == center_name:
                    existing['sold'] += sold
                    existing['used'] += used
                    existing['count'] += sold + used
                    existing['revenue'] += revenue
                    return
            row['center_breakdown'].append({
                'center_name': center_name,
                'count': sold + used,
                'sold': sold,
                'used': used,
                'revenue': revenue,
            })

        # ── Seed an entry per perk so an unused one still appears ────────────
        for p in promotions:
            entry(f'promo_{p.id}', perk_name(p), 'Coupon' if p.promo_type == 'Coupon' else 'Promotion', start=p.start_date,
                  end=p.end_date, level=p.level, active=p.is_active)
        for m in memberships:
            entry(f'membership_{m.id}', perk_name(m), 'Membership',
                  start=getattr(m, 'created_at', None) and m.created_at.date(),
                  level=m.level, active=getattr(m, 'is_active', True))
        for pk in packages:
            entry(f'package_{pk.id}', perk_name(pk), 'Package',
                  start=getattr(pk, 'created_at', None) and pk.created_at.date(),
                  level=pk.level, active=getattr(pk, 'is_active', True))
        for c in cards:
            entry(f'valuecard_{c.id}', perk_name(c), 'Value Card',
                  start=getattr(c, 'created_at', None) and c.created_at.date(),
                  level=c.level, active=getattr(c, 'is_active', True))

        # ── Coupons: PromotionUsage already records a use ────────────────────
        usage_qs = PromotionUsage.objects.filter(promotion__in=promotions)
        if start_date:
            usage_qs = usage_qs.filter(date__gte=start_date)
        if end_date:
            usage_qs = usage_qs.filter(date__lte=end_date)
        if restrict:
            usage_qs = usage_qs.filter(center_id__in=allowed_ids)

        for row in usage_qs.values(
            'promotion', 'center__center_name', 'center__display_name'
        ).annotate(uses=Count('id'), revenue=Sum('revenue_generated')):
            add(f"promo_{row['promotion']}",
                branch_name(row['center__display_name'], row['center__center_name']),
                used=row['uses'], revenue=row['revenue'] or 0)

        # ── Brand coupons: each code used is one use; the money is what the
        # code paid on the bill (what the brand owes the business) ───────────
        coupon_qs = CouponCode.objects.filter(promotion__in=promotions, is_used=True,
                                              invoice__status__in=Invoice.REVENUE_STATUSES)
        if start_date:
            coupon_qs = coupon_qs.filter(used_at__date__gte=start_date)
        if end_date:
            coupon_qs = coupon_qs.filter(used_at__date__lte=end_date)
        if restrict:
            coupon_qs = coupon_qs.filter(center_id__in=allowed_ids)
        for row in coupon_qs.values(
            'promotion', 'center__center_name', 'center__display_name'
        ).annotate(uses=Count('id'), paid=Sum('amount_applied')):
            add(f"promo_{row['promotion']}",
                branch_name(row['center__display_name'], row['center__center_name']),
                used=row['uses'], revenue=row['paid'] or 0)

        # ── Sales of memberships, packages and value cards ───────────────────
        mem_ct = ContentType.objects.get_for_model(Membership)
        pkg_ct = ContentType.objects.get_for_model(Package)
        card_ct = ContentType.objects.get_for_model(ValueCard)
        ct_to_prefix = {
            mem_ct.id: 'membership', pkg_ct.id: 'package', card_ct.id: 'valuecard',
        }

        sales = InvoiceItem.objects.filter(
            invoice__status__in=Invoice.REVENUE_STATUSES,
            content_type_id__in=list(ct_to_prefix),
            is_redemption=False,
        )
        if restrict:
            sales = sales.filter(invoice__center_id__in=allowed_ids)
        if start_date:
            sales = sales.filter(invoice__created_at__date__gte=start_date)
        if end_date:
            sales = sales.filter(invoice__created_at__date__lte=end_date)

        for row in sales.values(
            'content_type_id', 'object_id',
            'invoice__center__center_name', 'invoice__center__display_name',
        ).annotate(
            # Quantity, not line count: three memberships on one line is three.
            units=Coalesce(Sum('quantity'), Value(0), output_field=IntegerField()),
            revenue=Sum('total_price'),
        ):
            prefix = ct_to_prefix.get(row['content_type_id'])
            if not prefix:
                continue
            add(f"{prefix}_{row['object_id']}",
                branch_name(row['invoice__center__display_name'],
                            row['invoice__center__center_name']),
                sold=row['units'] or 0, revenue=row['revenue'] or 0)

        # ── Package REDEMPTIONS, credited to the package they came from ──────
        #
        # A redemption line references the service, not the package, which is
        # why this needs `redeemed_from_package`. That link exists precisely so
        # a drawdown is attributable.
        redemptions = InvoiceItem.objects.filter(
            invoice__status__in=Invoice.REVENUE_STATUSES,
            is_redemption=True,
            redeemed_from_package__isnull=False,
        )
        if restrict:
            redemptions = redemptions.filter(invoice__center_id__in=allowed_ids)
        if start_date:
            redemptions = redemptions.filter(invoice__created_at__date__gte=start_date)
        if end_date:
            redemptions = redemptions.filter(invoice__created_at__date__lte=end_date)

        for row in redemptions.values(
            'redeemed_from_package__package_id',
            'invoice__center__center_name', 'invoice__center__display_name',
        ).annotate(
            units=Coalesce(Sum('quantity'), Value(0), output_field=IntegerField()),
        ):
            package_id = row['redeemed_from_package__package_id']
            if not package_id:
                # A custom package with no catalog row behind it.
                continue
            add(f'package_{package_id}',
                branch_name(row['invoice__center__display_name'],
                            row['invoice__center__center_name']),
                used=row['units'] or 0)

        # ── Value card SPEND, credited to the card that was debited ──────────
        card_payments = Payment.objects.filter(
            invoice__status__in=Invoice.REVENUE_STATUSES,
            value_card_id__isnull=False,
        )
        if restrict:
            card_payments = card_payments.filter(invoice__center_id__in=allowed_ids)
        if start_date:
            card_payments = card_payments.filter(invoice__created_at__date__gte=start_date)
        if end_date:
            card_payments = card_payments.filter(invoice__created_at__date__lte=end_date)

        # `Payment.value_card_id` points at a ClientValueCard; the catalog row is
        # the ValueCard behind it.
        card_of_client_card = dict(
            ClientValueCard.objects
            .filter(id__in=card_payments.values_list('value_card_id', flat=True))
            .values_list('id', 'value_card_id')
        )

        for row in card_payments.values(
            'value_card_id',
            'invoice__center__center_name', 'invoice__center__display_name',
        ).annotate(uses=Count('id')):
            catalog_id = card_of_client_card.get(row['value_card_id'])
            if not catalog_id:
                continue
            add(f'valuecard_{catalog_id}',
                branch_name(row['invoice__center__display_name'],
                            row['invoice__center__center_name']),
                used=row['uses'])

        # ── Membership USE: invoices where the membership gave a discount ────
        memberships_used = ClientMembership.objects.filter(
            membership__in=memberships)
        if restrict:
            memberships_used = memberships_used.filter(center_id__in=allowed_ids)
        if start_date:
            memberships_used = memberships_used.filter(start_date__gte=start_date)
        if end_date:
            memberships_used = memberships_used.filter(start_date__lte=end_date)

        for row in memberships_used.values('membership_id').annotate(
            holders=Count('id')
        ):
            row_entry = data_map.get(f"membership_{row['membership_id']}")
            if row_entry is not None:
                # Active holders is the honest measure of a membership being in
                # use; a per-invoice discount is not recorded against the
                # membership that granted it.
                row_entry['holders'] = row['holders']

        return Response(list(data_map.values()))

class ValueCardViewSet(MarketingBaseViewSet):
    queryset = ValueCard.objects.all().select_related('center').order_by('-created_at')
    serializer_class = ValueCardSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'marketing'
    permission_submodule = 'cards'
    # A value card is sold and spent at the till.
    permission_read_also = [('billing', None)]
class MembershipViewSet(MarketingBaseViewSet):
    queryset = Membership.objects.all().select_related('center').order_by('-created_at')
    serializer_class = MembershipSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'marketing'
    permission_submodule = 'memberships'
    # A membership is sold at the till.
    permission_read_also = [('billing', None)]
class PackageViewSet(MarketingBaseViewSet):
    queryset = Package.objects.all().select_related('center').order_by('-created_at')
    serializer_class = PackageSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'marketing'
    permission_submodule = 'packages'
    # A package is sold and redeemed at the till.
    permission_read_also = [('billing', None)]
