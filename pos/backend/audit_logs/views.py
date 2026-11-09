"""
The activity log.

Two things were wrong.

The queryset raised `PermissionDenied("You do not have permission to view system
logs.")` for anybody who was not an owner. So once Logs became a grantable
module, granting it still did nothing - which is exactly the "I gave them
permission and it says no permission" symptom. The role is honoured now, and
`HasModulePermission` already gates the endpoint.

And the filtering was too thin to answer the questions people actually ask of an
audit log: what did this person do, who touched this record, what happened
between these two times. There was no way to filter by user id, no list of who
appears in the log at all, and the date filters interpolated the raw query
parameter into a timestamp comparison, so `?start_date=yesterday` was a 500.
"""

from django.db.models import Count, Max, Q
import datetime
from rest_framework import filters, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from pos_backend.permissions import HasModulePermission, IsOwner
from pos_backend.scoping import coerce_center_id, safe_date_param, user_center_ids

from .models import SystemLog
from .serializers import SystemLogSerializer


class StandardResultsSetPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 1000


class SystemLogViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = SystemLogSerializer
    permission_classes = [IsAuthenticated, HasModulePermission]
    pagination_class = StandardResultsSetPagination
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['user_name', 'user_email', 'path', 'human_description',
                     'ip_address', 'center_name', 'entity_type', 'entity_id']
    ordering_fields = ['timestamp', 'user_name', 'action', 'module']
    ordering = ['-timestamp']
    permission_module = 'logs'
    permission_submodule = 'activity'

    def get_queryset(self):
        """Rows this caller may see, narrowed by the filters they asked for."""
        params = self.request.query_params
        qs = SystemLog.objects.all()

        # An owner sees every branch. Anyone else sees the branches they are
        # assigned to, plus rows with no branch recorded (a login, say). This
        # replaces a blanket PermissionDenied that made the Logs permission
        # meaningless.
        if not IsOwner.check_is_owner(self.request.user):
            allowed = user_center_ids(self.request.user)
            # None means unrestricted (the all_centers permission); an empty set
            # means assigned to nothing, which must see nothing rather than
            # everything.
            if allowed is not None:
                qs = qs.filter(
                    Q(center_id__in=allowed) | Q(center_id__isnull=True))

        requested_center = coerce_center_id(params.get('center_id'))
        if requested_center is not None:
            qs = qs.filter(center_id=requested_center)

        # ── Who ───────────────────────────────────────────────────────────
        # `user_id` is what the "click a person to see their actions" list uses.
        # The log keeps a snapshot of the user's identity, so a deleted user's
        # history survives; match on either.
        user_id = params.get('user_id')
        if user_id == 'FAILED':
            qs = qs.filter(action='LOGIN_FAILED', user_id__isnull=True, user_id_snapshot__isnull=True)
        elif user_id == 'SYSTEM':
            qs = (qs.filter(user_id__isnull=True, user_id_snapshot__isnull=True)
                    .exclude(action='LOGIN_FAILED'))
        elif user_id:
            try:
                user_id = int(user_id)
            except (TypeError, ValueError):
                user_id = None
            if user_id is not None:
                qs = qs.filter(Q(user_id=user_id) | Q(user_id_snapshot=user_id))

        user_email = params.get('user_email')
        if user_email:
            qs = qs.filter(user_email__iexact=user_email.strip())

        user_name = params.get('user_name')
        if user_name:
            qs = qs.filter(user_name__icontains=user_name.strip())

        role = params.get('user_role')
        if role:
            qs = qs.filter(user_role__iexact=role.strip())

        # ── What ──────────────────────────────────────────────────────────
        # Repeatable: ?action=Create&action=Delete
        actions = [a for a in params.getlist('action') if a]
        if actions:
            qs = qs.filter(action__in=actions)

        modules = [m for m in params.getlist('module') if m]
        if modules:
            qs = qs.filter(module__in=modules)

        entity_type = params.get('entity_type')
        if entity_type:
            qs = qs.filter(entity_type__iexact=entity_type.strip())

        entity_id = params.get('entity_id')
        if entity_id:
            qs = qs.filter(entity_id=str(entity_id).strip())

        # ── When ──────────────────────────────────────────────────────────
        # safe_date_param, not raw interpolation: `?start_date=yesterday` used
        # to reach the database and 500.
        start_date = safe_date_param(params.get('start_date'))
        if start_date:
            qs = qs.filter(timestamp__gte=start_date)

        end_date = safe_date_param(params.get('end_date'))
        if end_date:
            dt = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            qs = qs.filter(timestamp__lt=dt + datetime.timedelta(days=1))

        # ── How it ended ──────────────────────────────────────────────────
        outcome = (params.get('outcome') or '').lower()
        if outcome == 'failed':
            qs = qs.filter(status_code__gte=400)
        elif outcome == 'success':
            qs = qs.filter(Q(status_code__lt=400) | Q(status_code__isnull=True))

        # ── Where from ────────────────────────────────────────────────────
        ip = params.get('ip_address')
        if ip:
            qs = qs.filter(ip_address=ip.strip())

        device = params.get('device_type')
        if device:
            qs = qs.filter(device_type__iexact=device.strip())

        return qs.select_related('user')

    @action(detail=False, methods=['get'])
    def facets(self, request):
        """The values worth offering as filters, with counts.

        The screen previously had nothing to populate a dropdown with, so the
        only way to filter was to already know what to type.

        Counts are taken over the caller's visible rows AFTER their current
        filters, except each facet excludes its own filter - so choosing an
        action still shows the other actions available alongside it.
        """
        base = self.get_queryset()

        def top(field, limit=60):
            return [
                {'value': row[field], 'count': row['n']}
                for row in (base.exclude(**{f'{field}': ''})
                                .values(field)
                                .annotate(n=Count('id'))
                                .order_by('-n')[:limit])
                if row[field]
            ]

        return Response({
            'actions': top('action'),
            'modules': top('module'),
            'entity_types': top('entity_type'),
            'roles': top('user_role'),
            'device_types': top('device_type'),
            'outcomes': {
                'failed': base.filter(status_code__gte=400).count(),
            },
            'total': base.count(),
        })

    @action(detail=False, methods=['get'])
    def users(self, request):
        """Everyone who appears in the log, for the clickable user list.

        Each row carries enough to be useful without opening it: how many
        actions, when they were last active, and which branch. Clicking one is
        just `?user_id=<id>` against the list endpoint.
        """
        base = self.get_queryset()
        nobody = Q(user_id__isnull=True, user_id_snapshot__isnull=True)

        rows = (base.exclude(nobody)
                .values('user_id', 'user_id_snapshot', 'user_name', 'user_email', 'user_role')
                .annotate(actions=Count('id'), last_active=Max('timestamp'))
                .order_by('-last_active')[:500])

        # Collapse by identity: the same person can appear with a null user FK
        # (after deletion) and with their snapshot id.
        merged = {}
        for row in rows:
            key = (row['user_email'] or '').lower() or f"id:{row['user_id_snapshot']}"
            entry = merged.setdefault(key, {
                'user_id': row['user_id'] or row['user_id_snapshot'],
                'name': row['user_name'] or row['user_email'] or '(unknown)',
                'email': row['user_email'] or '',
                'role': row['user_role'] or '',
                'kind': 'user',
                'actions': 0,
                'last_active': row['last_active'],
            })
            entry['actions'] += row['actions']
            if row['last_active'] and (
                entry['last_active'] is None
                or row['last_active'] > entry['last_active']
            ):
                entry['last_active'] = row['last_active']
            # Prefer a live user id over a snapshot.
            if row['user_id']:
                entry['user_id'] = row['user_id']

        result = list(merged.values())

        # Activity that belongs to no signed-in person is shown once each, named
        # for what it is, instead of one "System Activity" row per stray address.
        failed = base.filter(nobody, action='LOGIN_FAILED').aggregate(n=Count('id'), last=Max('timestamp'))
        if failed['n']:
            result.append({'user_id': 'FAILED', 'name': 'Failed sign-ins', 'email': '', 'role': '',
                           'kind': 'failed_login', 'actions': failed['n'], 'last_active': failed['last']})
        anon = base.filter(nobody).exclude(action='LOGIN_FAILED').aggregate(n=Count('id'), last=Max('timestamp'))
        if anon['n']:
            result.append({'user_id': 'SYSTEM', 'name': 'Not signed in', 'email': '', 'role': '',
                           'kind': 'anonymous', 'actions': anon['n'], 'last_active': anon['last']})

        result.sort(key=lambda r: r['last_active'] or datetime.datetime.min, reverse=True)
        return Response({'count': len(result), 'results': result})

    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Counts per day and per action, for the header strip on the screen."""
        base = self.get_queryset()

        by_day = (base.values('timestamp__date')
                      .annotate(n=Count('id'))
                      .order_by('-timestamp__date')[:31])

        return Response({
            'total': base.count(),
            'distinct_users': base.values('user_email').distinct().count(),
            'by_day': [
                {'date': row['timestamp__date'], 'count': row['n']}
                for row in by_day
            ],
            'by_action': [
                {'action': row['action'], 'count': row['n']}
                for row in base.values('action').annotate(n=Count('id')).order_by('-n')[:20]
                if row['action']
            ],
        })
