"""Notification APIs.

    GET  /notifications/api/bell/?unread=1&page=      my bell (any signed-in user)
    POST /notifications/api/bell/read/                {"ids": [..]} or {"all": true}
    GET  /notifications/api/rules/                    events with their rule
    PUT  /notifications/api/rules/                    {"event", "enabled", "bell", "email", "whatsapp", "role_ids", "user_ids"}
    GET  /notifications/api/integrations/             the three services, secrets masked
    PUT  /notifications/api/integrations/<key>/       {"enabled", "values": {...}}
    POST /notifications/api/integrations/<key>/test/  {"to": "address or number"}
    GET  /notifications/api/deliveries/?status=&page= sent / failed / skipped messages

The bell is everyone's own. The rest is Admin > Integrations (the test button
is its own tick).
"""

from django.core.paginator import EmptyPage, Paginator
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from notifications import integrations
from notifications.events import CLIENT_EVENTS, EVENTS
from notifications.models import DeliveryLog, Notification, NotificationRule
from notifications.service import rule_for, serialise
from pos_backend.permissions import HasModulePermission, require_action

PAGE = 30


def _page(qs, request, size=PAGE):
    try:
        number = max(int(request.query_params.get('page', 1)), 1)
    except (TypeError, ValueError):
        number = 1
    paginator = Paginator(qs, size)
    try:
        return paginator.page(number), paginator.count
    except EmptyPage:
        return None, paginator.count


class BellView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        mine = Notification.objects.filter(user=request.user)
        unread = mine.filter(read_at__isnull=True).count()
        qs = mine.filter(read_at__isnull=True) if request.query_params.get('unread') else mine
        page, count = _page(qs, request)
        return Response({'unread': unread, 'count': count,
                         'results': [serialise(n) for n in (page or [])]})


class BellReadView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        qs = Notification.objects.filter(user=request.user, read_at__isnull=True)
        if not request.data.get('all'):
            ids = [i for i in (request.data.get('ids') or []) if str(i).isdigit()]
            qs = qs.filter(pk__in=ids)
        qs.update(read_at=timezone.now())
        return Response({'unread': Notification.objects.filter(
            user=request.user, read_at__isnull=True).count()})


class _AdminIntegrations:
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'integrations'
    permission_strict_read = True


class RulesView(_AdminIntegrations, APIView):
    def get(self, request):
        from accounts.models import CustomUser
        from salon_admin.models import Role

        out = []
        for key, spec in EVENTS.items():
            rule = rule_for(key)
            out.append({
                'event': key, 'label': spec['label'], 'help': spec['help'],
                'people': spec['people'], 'enabled': rule.enabled, 'bell': rule.bell,
                'email': rule.email, 'whatsapp': rule.whatsapp,
                'role_ids': rule.role_ids, 'user_ids': rule.user_ids,
            })
        return Response({
            'events': out,
            'client_events': [{'event': k, 'label': v} for k, v in CLIENT_EVENTS.items()],
            'roles': list(Role.objects.order_by('name').values('id', 'name')),
            'users': list(CustomUser.objects.filter(is_active=True).order_by('full_name')
                          .values('id', 'full_name', 'email')),
        })

    def put(self, request):
        event = request.data.get('event')
        if event not in EVENTS:
            return Response({'event': 'Unknown event.'}, status=status.HTTP_400_BAD_REQUEST)
        row, _ = NotificationRule.objects.get_or_create(event=event)
        for flag in ('enabled', 'bell', 'email', 'whatsapp'):
            if flag in request.data:
                setattr(row, flag, bool(request.data[flag]))
        for name in ('role_ids', 'user_ids'):
            if name in request.data:
                row_ids = request.data[name]
                if not isinstance(row_ids, list) or any(not str(i).isdigit() for i in row_ids):
                    return Response({name: 'A list of ids.'}, status=status.HTTP_400_BAD_REQUEST)
                setattr(row, name, sorted({int(i) for i in row_ids}))
        row.save()
        return Response({'event': event, 'enabled': row.enabled, 'bell': row.bell,
                         'email': row.email, 'whatsapp': row.whatsapp,
                         'role_ids': row.role_ids, 'user_ids': row.user_ids})


class IntegrationsView(_AdminIntegrations, APIView):
    def get(self, request):
        return Response([integrations.describe(key) for key in integrations.SERVICES])


class IntegrationDetailView(_AdminIntegrations, APIView):
    def put(self, request, key):
        if key not in integrations.SERVICES:
            return Response({'detail': 'Unknown service.'}, status=status.HTTP_404_NOT_FOUND)
        values = request.data.get('values')
        if not isinstance(values, dict):
            return Response({'values': 'Send the fields as an object.'},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            integrations.save(key, bool(request.data.get('enabled')), values, request.user)
        except ValueError as exc:
            return Response({'detail': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(integrations.describe(key))


class IntegrationTestView(_AdminIntegrations, APIView):
    def post(self, request, key):
        require_action(request.user, 'admin', 'integrations', 'send_test')
        if key not in integrations.SERVICES:
            return Response({'detail': 'Unknown service.'}, status=status.HTTP_404_NOT_FOUND)
        to = str(request.data.get('to') or '').strip()
        if not to:
            return Response({'to': 'Enter an address or number to test with.'},
                            status=status.HTTP_400_BAD_REQUEST)
        row, _ = integrations.load(key)
        ok, message = True, 'Test message sent.'
        try:
            if key == 'whatsapp':
                integrations.send_whatsapp(to, 'Test message from your POS.')
            else:
                from notifications.service import _designed
                text = ('This is a test message from your POS.\n\nIf you can read it, the mail set-up works: '
                        'approvals, stock requests and alerts will reach people the same way.')
                integrations.send_email(to, 'POS test message', text, only=key,
                                        html=_designed('test', 'POS test message', text) or None)
        except integrations.NotConfigured as exc:
            ok, message = False, str(exc)
        except Exception as exc:                               # noqa: BLE001 - shown to the admin
            ok, message = False, str(exc)[:300]
        if row is not None:
            row.last_test_at, row.last_test_ok, row.last_test_message = timezone.now(), ok, message[:300]
            row.save(update_fields=['last_test_at', 'last_test_ok', 'last_test_message'])
        return Response({'ok': ok, 'message': message},
                        status=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST)


class DeliveriesView(_AdminIntegrations, APIView):
    def get(self, request):
        qs = DeliveryLog.objects.select_related('user', 'client', 'center')
        state = request.query_params.get('status')
        if state in (DeliveryLog.SENT, DeliveryLog.FAILED, DeliveryLog.SKIPPED, DeliveryLog.QUEUED):
            qs = qs.filter(status=state)
        channel = request.query_params.get('channel')
        if channel in ('email', 'whatsapp'):
            qs = qs.filter(channel=channel)
        page, count = _page(qs, request, 50)
        rows = [{
            'id': d.pk, 'event': d.event, 'channel': d.channel, 'recipient': d.recipient,
            'who': (d.user.full_name if d.user else (d.client.full_name if d.client else '')),
            'status': d.status, 'error': d.error, 'subject': d.subject,
            'created_at': d.created_at, 'sent_at': d.sent_at,
        } for d in (page or [])]
        counts = {s: DeliveryLog.objects.filter(status=s).count()
                  for s in (DeliveryLog.SENT, DeliveryLog.FAILED, DeliveryLog.SKIPPED, DeliveryLog.QUEUED)}
        return Response({'count': count, 'counts': counts, 'results': rows})
