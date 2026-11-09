"""Who may decide a stock request, and what deciding does.

One path for every way of deciding, so the Approve button in the POS and the
link in an approver's e-mail can never disagree:

  * a person may decide a request when their role ticks Inventory > Approve
    Stock Requests and they can act on the requesting branch (no role name is
    ever looked at);
  * the first decision wins;
  * deciding tells the person who asked and, on approval, the people who send stock.

An e-mail / WhatsApp link is an approvals.ApprovalToken on a small "shadow"
ApprovalRequest (kind `stock_request`) that points at the StockRequest. The
shadow row only carries the link: the StockRequest holds the truth, and
`decide` closes the shadow row in the same transaction.
"""


from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

KIND = 'stock_request'
APPROVE_TICK = 'approve_stock_requests'
SEND_TICK = 'fulfill_stock_requests'


def holders(tick, center_id):
    """Active users whose role ticks `tick` (update) and who can act on `center_id`."""
    from accounts.models import CustomUser
    from pos_backend.permissions import has_module_permission
    from pos_backend.scoping import can_use_center

    return [u for u in CustomUser.objects.filter(is_active=True)
            if has_module_permission(u, 'inventory', 'update', tick) and can_use_center(u, center_id)]


def can_approve(user, center_id):
    from pos_backend.permissions import has_module_permission
    from pos_backend.scoping import can_use_center

    return (bool(user and getattr(user, 'is_active', True))
            and has_module_permission(user, 'inventory', 'update', APPROVE_TICK)
            and can_use_center(user, center_id))


def own_request_ok(user, stock_request):
    """Nobody approves their own request. The owner (superuser) may: someone has to be able to,
    when the owner is the only person there is."""
    if getattr(user, 'is_superuser', False):
        return True
    return not (stock_request.created_by_id and stock_request.created_by_id == getattr(user, 'pk', None))


def can_cancel(user, stock_request):
    """A request is withdrawn by the person who made it, or by someone who can approve it."""
    if getattr(user, 'is_superuser', False):
        return True
    if stock_request.created_by_id and stock_request.created_by_id == getattr(user, 'pk', None):
        return True
    return can_approve(user, stock_request.requesting_center_id)


def summary_of(stock_request):
    names = ', '.join(f'{i.quantity.normalize():f} x {i.product.name}'
                      for i in stock_request.items.select_related('product')[:5])
    where = stock_request.requesting_center.display_name or stock_request.requesting_center.center_name
    return (f'Stock request REQ-{stock_request.pk} ({stock_request.stock_type}) for {where}: {names}')[:300]


def _shadow_requests(stock_request_id, status=None):
    from approvals.models import ApprovalRequest

    qs = ApprovalRequest.objects.filter(kind=KIND, subject__stock_request=stock_request_id)
    return qs.filter(status=status) if status else qs


def open_for_approval(stock_request, asker):
    """Tell the approvers: a bell row each, and an e-mail / WhatsApp message with
    their own single-use Approve / Reject link."""
    from approvals import service as approvals
    from notifications.service import notify

    center = stock_request.requesting_center
    # The asker is not told to approve their own request (unless they are the owner).
    approvers = [u for u in holders(APPROVE_TICK, center.pk) if own_request_ok(u, stock_request)]
    summary = summary_of(stock_request)
    where = center.display_name or center.center_name
    who = (asker.full_name or asker.email) if asker else 'Someone'
    headline = f'{who} asked for {stock_request.stock_type} stock for {where} (REQ-{stock_request.pk}). It needs your approval.'

    raw_by_user = approvals.open_shadow_request(
        stock_request, asker, approvers, summary, days=int(approvals.stock_link_days()))

    def with_link(approver):
        raw = raw_by_user.get(approver.pk)
        if not raw:
            return headline
        return (f'{headline}\n{summary}\nApprove or reject from this link (works once, for '
                f'{approvals.stock_link_days()} days): {approvals.link_for(raw)}')

    notify('stock.request_raised', title=f'Stock request from {where}', body=headline,
           url='/inventory', center=center, users=approvers, data={'stock_request_id': stock_request.pk},
           email_body=with_link, whatsapp_body=with_link)


def decide(stock_request_id, user, approve, note='', via='pos', token=None):
    """Approve or reject a stock request. Raises when it cannot be decided."""
    from approvals.models import ApprovalRequest, ApprovalToken
    from inventory.models import StockRequest
    from notifications.service import notify

    with transaction.atomic():
        req = StockRequest.objects.select_for_update().select_related(
            'requesting_center', 'created_by', 'decided_by').get(pk=stock_request_id)
        if req.status != 'Pending':
            by = f' by {req.decided_by.full_name or req.decided_by.email}' if req.decided_by_id else ''
            raise ValidationError({'detail': f'This request is already {req.status.lower()}{by}.'})
        if not can_approve(user, req.requesting_center_id):
            raise PermissionDenied('You are not an approver for this branch.')
        if not own_request_ok(user, req):
            raise PermissionDenied('You made this request, so someone else has to decide it.')

        req.status = 'Approved' if approve else 'Rejected'
        req.decided_by, req.decided_at = user, timezone.now()
        req.decision_note = (note or '')[:300]
        req.save(update_fields=['status', 'decided_by', 'decided_at', 'decision_note'])

        shadow = _shadow_requests(req.pk, ApprovalRequest.PENDING)
        shadow_ids = list(shadow.values_list('pk', flat=True))
        shadow.update(status=ApprovalRequest.APPROVED if approve else ApprovalRequest.REJECTED,
                      decided_by=user, decided_at=req.decided_at, decided_via=via,
                      decision_note=req.decision_note)
        ApprovalToken.objects.filter(request_id__in=shadow_ids, used_at__isnull=True).update(
            used_at=timezone.now(), used_action='closed')
        if token is not None:
            ApprovalToken.objects.filter(pk=token.pk).update(used_action='approve' if approve else 'reject')

        where = req.requesting_center.display_name or req.requesting_center.center_name
        who = user.full_name or user.email
        word = 'approved' if approve else 'rejected'
        if req.created_by_id:
            notify('approval.decided', title=f'Your stock request was {word}',
                   body=f'{who} {word} REQ-{req.pk} for {where}.', url='/inventory',
                   center=req.requesting_center, users=[req.created_by],
                   data={'stock_request_id': req.pk, 'status': req.status})
        if approve:
            notify('stock.request_approved', title=f'Stock request to send - {where}',
                   body=f'REQ-{req.pk} ({req.stock_type}) for {where} was approved by {who}. '
                        f'Open it in Stock Requests, choose where to send from, and make the transfer.',
                   url='/inventory', center=req.requesting_center,
                   users=holders(SEND_TICK, req.requesting_center_id),
                   data={'stock_request_id': req.pk})
    return req


def close_shadow(stock_request_id, status):
    """The request ended another way (withdrawn): its e-mail links stop working."""
    from approvals.models import ApprovalRequest, ApprovalToken

    shadow = _shadow_requests(stock_request_id, ApprovalRequest.PENDING)
    ids = list(shadow.values_list('pk', flat=True))
    shadow.update(status=status)
    ApprovalToken.objects.filter(request_id__in=ids, used_at__isnull=True).update(
        used_at=timezone.now(), used_action='closed')
