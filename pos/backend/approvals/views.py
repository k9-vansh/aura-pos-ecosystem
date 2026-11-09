"""Approvals API.

    GET  /approvals/api/policies/               kinds, switches, approvers   (Admin > Approvals)
    PUT  /approvals/api/policies/<kind>/        {"enabled", "approvers": [{"user_id", "center_id"|null}]}
    GET  /approvals/api/inbox/                  requests I may decide (pending first)
    GET  /approvals/api/requests/<id>/          one request - the asker, an approver, or Admin
    POST /approvals/api/requests/<id>/decide/   {"action": "approve"|"reject", "note"}
    POST /approvals/api/requests/<id>/cancel/   the asker withdraws
    GET  /approvals/api/history/                every request (Admin > Approvals)
    GET  /approvals/api/link/<token>/           what an e-mail / WhatsApp link is about
    POST /approvals/api/link/<token>/           {"action"} - decide from the link (single use)

The till asks through `POST /billing/invoices/request_approval/`, which prices the
bill itself and opens a request for exactly what it finds.
"""

from django.core.paginator import EmptyPage, Paginator
from django.db import transaction
from django.db.models import Q
from rest_framework import permissions, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response
from rest_framework.views import APIView

from approvals import service
from approvals.models import ApprovalApprover, ApprovalPolicy, ApprovalRequest
from pos_backend.permissions import HasModulePermission, has_module_permission
from pos_backend.scoping import user_center_ids


def present(req, user=None):
    return {
        'id': req.pk, 'kind': req.kind, 'kind_label': service.kind_label(req.kind),
        'center': req.center_id,
        'center_name': (req.center.display_name or req.center.center_name) if req.center_id else '',
        'requested_by': req.requested_by_id,
        'requested_by_name': (req.requested_by.full_name or req.requested_by.email) if req.requested_by else '',
        'summary': req.summary, 'reason': req.reason, 'subject': req.subject,
        'status': req.status,
        'decided_by_name': (req.decided_by.full_name or req.decided_by.email) if req.decided_by else '',
        'decided_at': req.decided_at, 'decided_via': req.decided_via,
        'decision_note': req.decision_note, 'created_at': req.created_at,
        'used_invoice': req.used_invoice_id,
        'can_decide': bool(user and req.status == ApprovalRequest.PENDING and service.may_decide(user, req)),
    }


def _page(qs, request, size=40):
    try:
        number = max(int(request.query_params.get('page', 1)), 1)
    except (TypeError, ValueError):
        number = 1
    paginator = Paginator(qs, size)
    try:
        return paginator.page(number), paginator.count
    except EmptyPage:
        return [], paginator.count


class _Admin:
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'approvals'
    permission_strict_read = True


class PoliciesView(_Admin, APIView):
    def get(self, request):
        from accounts.models import CustomUser
        from salon_admin.models import Center

        policies = {p.kind: p for p in ApprovalPolicy.objects.prefetch_related('approvers')}
        out = []
        for kind, spec in service.KINDS.items():
            policy = policies.get(kind)
            out.append({
                'kind': kind, 'label': spec['label'], 'help': spec['help'],
                'available': spec['available'], 'enabled': bool(policy and policy.enabled),
                'approvers': [{'user_id': a.user_id, 'center_id': a.center_id}
                              for a in (policy.approvers.all() if policy else [])],
            })
        return Response({
            'policies': out,
            'users': list(CustomUser.objects.filter(is_active=True).order_by('full_name')
                          .values('id', 'full_name', 'email')),
            'centers': [{'id': c.pk, 'name': c.display_name or c.center_name}
                        for c in Center.objects.order_by('center_name')],
        })


class PolicyDetailView(_Admin, APIView):
    def put(self, request, kind):
        spec = service.KINDS.get(kind)
        if spec is None or not spec['available']:
            return Response({'detail': 'That approval is not in use yet.'},
                            status=status.HTTP_404_NOT_FOUND)
        from accounts.models import CustomUser
        from salon_admin.models import Center

        enabled = bool(request.data.get('enabled'))
        rows = request.data.get('approvers') or []
        if not isinstance(rows, list):
            return Response({'approvers': 'A list.'}, status=status.HTTP_400_BAD_REQUEST)
        wanted = set()
        for row in rows:
            try:
                user_id = int(row.get('user_id'))
                center_id = row.get('center_id')
                center_id = int(center_id) if center_id not in (None, '', 'all') else None
            except (AttributeError, TypeError, ValueError):
                return Response({'approvers': 'Each approver needs a user and a branch (or all).'},
                                status=status.HTTP_400_BAD_REQUEST)
            wanted.add((user_id, center_id))
        users = set(CustomUser.objects.filter(pk__in={u for u, _ in wanted}, is_active=True)
                    .values_list('pk', flat=True))
        centers = set(Center.objects.filter(pk__in={c for _, c in wanted if c}).values_list('pk', flat=True))
        if any(u not in users or (c and c not in centers) for u, c in wanted):
            return Response({'approvers': 'An approver or branch does not exist.'},
                            status=status.HTTP_400_BAD_REQUEST)

        with transaction.atomic():
            policy, _ = ApprovalPolicy.objects.get_or_create(kind=kind)
            policy.enabled = enabled
            policy.save()
            for row in list(policy.approvers.all()):
                if (row.user_id, row.center_id) not in wanted:
                    row.delete()
            for user_id, center_id in wanted:
                ApprovalApprover.objects.get_or_create(policy=policy, user_id=user_id,
                                                       center_id=center_id)
        return Response({'kind': kind, 'enabled': policy.enabled,
                         'approvers': [{'user_id': u, 'center_id': c} for u, c in sorted(
                             wanted, key=lambda t: (t[0], t[1] or 0))]})


def _stock_request_q(user):
    """Stock requests the user may decide: they tick Approve Stock Requests and can
    act on the branch. Not a list of approvers - the tick is the whole rule."""
    from inventory.stock_request_flow import APPROVE_TICK

    if not has_module_permission(user, 'inventory', 'update', APPROVE_TICK):
        return None
    centers = user_center_ids(user)
    if centers is None:
        return Q(kind=service.STOCK_REQUEST)
    return Q(kind=service.STOCK_REQUEST, center_id__in=list(centers))


def _visible(request, qs):
    """Requests the user may see: their own, ones they may decide, or all for Admin > Approvals."""
    user = request.user
    if user.is_superuser or has_module_permission(user, 'admin', 'read', 'approvals'):
        centers = user_center_ids(user)
        return qs if centers is None else qs.filter(center_id__in=list(centers))
    decidable = ApprovalApprover.objects.filter(user=user).values_list('policy__kind', 'center_id')
    q = Q(requested_by=user)
    for kind, center_id in decidable:
        q |= Q(kind=kind) if center_id is None else Q(kind=kind, center_id=center_id)
    stock_q = _stock_request_q(user)
    if stock_q is not None:
        q |= stock_q
    return qs.filter(q)


class InboxView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        service.expire_stale()
        user = request.user
        base = ApprovalRequest.objects.select_related('center', 'requested_by', 'decided_by')
        if user.is_superuser:
            mine = base
        else:
            q = Q()
            for kind, center_id in ApprovalApprover.objects.filter(user=user).values_list(
                    'policy__kind', 'center_id'):
                q |= Q(kind=kind) if center_id is None else Q(kind=kind, center_id=center_id)
            stock_q = _stock_request_q(user)
            if stock_q is not None:
                q |= stock_q
            mine = base.filter(q) if q else base.none()
        pending = [present(r, user) for r in mine.filter(status=ApprovalRequest.PENDING)]
        recent = [present(r, user) for r in mine.exclude(status=ApprovalRequest.PENDING)[:20]]
        return Response({'pending': pending, 'recent': recent, 'pending_count': len(pending)})


class RequestDetailView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, pk):
        service.expire_stale()
        req = _visible(request, ApprovalRequest.objects.select_related(
            'center', 'requested_by', 'decided_by')).filter(pk=pk).first()
        if req is None:
            raise PermissionDenied('That request is not one you can see.')
        return Response(present(req, request.user))


class DecideView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        req = service.decide(pk, request.user, request.data.get('action'),
                             note=str(request.data.get('note') or ''), via='pos')
        return Response(present(ApprovalRequest.objects.select_related(
            'center', 'requested_by', 'decided_by').get(pk=req.pk), request.user))


class CancelView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        req = ApprovalRequest.objects.filter(pk=pk).first()
        if req is None:
            raise PermissionDenied('That request is not one you can see.')
        return Response({'cancelled': service.cancel(req, request.user)})


class HistoryView(_Admin, APIView):
    def get(self, request):
        qs = _visible(request, ApprovalRequest.objects.select_related(
            'center', 'requested_by', 'decided_by'))
        for field, param in (('status', 'status'), ('kind', 'kind')):
            value = request.query_params.get(param)
            if value:
                qs = qs.filter(**{field: value})
        center = request.query_params.get('center_id')
        if center and center.isdigit():
            qs = qs.filter(center_id=int(center))
        page, count = _page(qs, request)
        return Response({'count': count, 'results': [present(r, request.user) for r in page]})


class LinkView(APIView):
    """The e-mail / WhatsApp link. Opening it changes nothing (mail scanners open
    links); only the POST decides, and it works once."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, token):
        _, req = service.resolve_token(token)
        return Response({
            'kind': req.kind, 'summary': req.summary, 'reason': req.reason,
            'branch': (req.center.display_name or req.center.center_name),
            'requested_by': (req.requested_by.full_name or req.requested_by.email) if req.requested_by else '',
            'status': req.status, 'created_at': req.created_at,
        })

    def post(self, request, token):
        req = service.decide_by_token(token, request.data.get('action'),
                                      note=str(request.data.get('note') or ''))
        return Response({'status': req.status, 'summary': req.summary})
