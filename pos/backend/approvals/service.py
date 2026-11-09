"""Approvals: a cashier asks, an approver decides, the server checks the bill.

Decisions F10-F13 / D11:
  * only a price change and a discount need approval here; everything else is
    permission-based (stock requests are approved by whoever holds the
    Approve Stock Requests tick, see inventory.views.StockRequestViewSet);
  * the free limits are Admin > Settings values;
  * approvers are mapped to branches many-to-many;
  * the cashier waits in the POS; the approver can decide in the POS, or from an
    e-mail / WhatsApp link that is single-use, expires in 15 minutes and is logged;
  * the first decision wins;
  * the server accepts the bill only when the approved figures cover the bill's.

An approval kind is off until its policy is switched on, so an unchanged
system prices and bills exactly as before.
"""

import hashlib
import secrets
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError

from approvals.models import ApprovalApprover, ApprovalPolicy, ApprovalRequest, ApprovalToken
from pos_backend import app_settings
from pos_backend.money import ZERO, money

HUNDRED = Decimal('100')

# `available` is false for a kind whose flow does not use it yet, so the policy
# screen never offers a switch that does nothing.
KINDS = {
    'discount': {
        'label': 'Discount above the free limit',
        'help': 'A discount typed at the till (on a line or on the bill) larger than the free '
                'limit set in Settings > Approvals. Offers and memberships need no approval.',
        'available': True,
    },
    'price_change': {
        'label': 'Price change',
        'help': 'A service, product or package sold at a price other than the list price.',
        'available': True,
    },
}


class ApprovalRequired(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'This bill needs approval.'
    default_code = 'approval_required'


class ApprovalPending(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = 'Waiting for approval.'
    default_code = 'approval_pending'


# ─── Policies and approvers ─────────────────────────────────────────────────

def policy_enabled(kind):
    return ApprovalPolicy.objects.filter(kind=kind, enabled=True).exists()


def approvers_for(kind, center_id):
    """Users who may decide `kind` at the branch. With none mapped, the
    superusers - so a switched-on policy can never leave a bill stuck."""
    from accounts.models import CustomUser

    scopes = ApprovalApprover.objects.filter(policy__kind=kind).filter(
        models_q_center(center_id)).select_related('user')
    users = {s.user.pk: s.user for s in scopes if s.user.is_active}
    if not users:
        users = {u.pk: u for u in CustomUser.objects.filter(is_superuser=True, is_active=True)}
    return list(users.values())


def models_q_center(center_id):
    from django.db.models import Q
    return Q(center__isnull=True) | Q(center_id=center_id)


STOCK_REQUEST = 'stock_request'


def kind_label(kind):
    if kind == STOCK_REQUEST:
        return 'Stock request'
    return KINDS.get(kind, {}).get('label', kind)


def may_decide(user, req):
    if req.kind == STOCK_REQUEST:
        # Decided by whoever ticks Approve Stock Requests at that branch: no
        # approver list and no superuser shortcut here.
        from inventory.stock_request_flow import can_approve
        return can_approve(user, req.center_id)
    if user.is_superuser:
        return True
    return any(u.pk == user.pk for u in approvers_for(req.kind, req.center_id))


# ─── What a bill needs ──────────────────────────────────────────────────────

def _free(key, center):
    return Decimal(str(app_settings.get(key, center=center)))


def requirements(center, lines, header_discount=ZERO):
    """What, if anything, this bill needs approved. Pure arithmetic over `lines`
    (dicts with ct, object_id, name, unit_price, list_price, resolved, discount,
    quantity, redemption, card), whether they come from a quote or a saved bill.

    Only kinds whose policy is switched on are returned."""
    needed = []
    live = [l for l in lines if not l['redemption']]
    gross = money(sum((Decimal(l['unit_price']) * Decimal(l['quantity']) for l in live), ZERO))

    if policy_enabled('discount') and gross > 0:
        manual = money(sum((Decimal(l['discount']) for l in live if not l['card']), ZERO)
                       + Decimal(header_discount))
        percent = (manual / gross * HUNDRED).quantize(Decimal('0.01'))
        free = _free('approvals.discount_free_percent', center)
        if manual > 0 and percent > free:
            needed.append({'kind': 'discount', 'amount': str(manual), 'percent': str(percent),
                           'gross': str(gross), 'free_percent': str(free)})

    if policy_enabled('price_change'):
        free = _free('approvals.price_change_free_percent', center)
        changed = []
        for l in live:
            if not l['resolved'] or l['card']:
                continue
            listed, price = money(l['list_price']), money(l['unit_price'])
            if listed <= 0 or price == listed:
                continue
            percent = (abs(price - listed) / listed * HUNDRED).quantize(Decimal('0.01'))
            if percent > free:
                changed.append({'content_type': l['ct'], 'object_id': l['object_id'],
                                'name': l['name'], 'unit_price': str(price),
                                'list_price': str(listed)})
        if changed:
            needed.append({'kind': 'price_change', 'lines': changed, 'free_percent': str(free)})
    return needed


def lines_from_quote(quote):
    out = []
    for l in quote.lines:
        cat = l.catalog
        out.append({
            'ct': l.ct_label, 'object_id': l.object_id,
            'name': (l.description or '')[:80],
            'unit_price': l.unit_price, 'discount': l.discount, 'quantity': l.quantity,
            'list_price': cat.list_price if cat is not None else ZERO,
            'resolved': bool(cat is not None and cat.resolved),
            'redemption': bool(l.is_redemption), 'card': bool(l.value_card),
        })
    return out


def header_manual_discount(quote):
    """The bill-level discount typed at the till (not an offer's, not a card's)."""
    if quote.promotion is not None or quote.membership is not None or quote.value_card is not None:
        return ZERO
    return money(quote.discount)


def quote_requirements(center, quote):
    return requirements(center, lines_from_quote(quote), header_manual_discount(quote))


def invoice_requirements(invoice):
    """The same, from a saved (held) bill's stored lines."""
    from billing import pricing

    lines = []
    for item in invoice.items.select_related('content_type'):
        label = pricing.content_type_label(item.content_type)
        cat = pricing.resolve(label, item.object_id, center=invoice.center)
        lines.append({
            'ct': label, 'object_id': item.object_id, 'name': (item.description or '')[:80],
            'unit_price': item.unit_price, 'discount': item.discount, 'quantity': item.quantity,
            'list_price': cat.list_price, 'resolved': cat.resolved,
            'redemption': bool(item.is_redemption), 'card': bool(item.value_card_id),
        })
    manual_header = ZERO
    if not (invoice.promotion_id or invoice.membership_used_id):
        manual_header = money(invoice.discount)
    return requirements(invoice.center, lines, manual_header)


# ─── Does an approval cover the bill ────────────────────────────────────────

def covers(approved, need):
    """True when the approved figures cover what the bill now needs."""
    if need['kind'] == 'discount':
        return money(need['amount']) <= money(approved.get('amount', 0))
    if need['kind'] == 'price_change':
        granted = {}
        for line in approved.get('lines', []):
            key = (line['content_type'], str(line['object_id']), str(money(line['unit_price'])))
            granted[key] = granted.get(key, 0) + 1
        for line in need['lines']:
            key = (line['content_type'], str(line['object_id']), str(money(line['unit_price'])))
            if granted.get(key, 0) <= 0:
                return False
            granted[key] -= 1
        return True
    return False


def _valid_for_use(req, user, center_id):
    now = timezone.now()
    ttl = timedelta(minutes=int(app_settings.get('approvals.valid_minutes')))
    return (req.status == ApprovalRequest.APPROVED and req.used_invoice_id is None
            and req.center_id == center_id and req.requested_by_id == user.pk
            and req.decided_at is not None and now <= req.decided_at + ttl)


def enforce(*, user, center, needed, approval_ids):
    """Raise ApprovalRequired unless approved requests cover every `needed` item.
    Returns the requests that cover it, to be consumed once the bill is saved."""
    if not needed:
        return []
    center_id = getattr(center, 'pk', center)
    ids = [int(i) for i in (approval_ids or []) if str(i).isdigit()]
    pool = list(ApprovalRequest.objects.filter(pk__in=ids)) if (ids and user is not None) else []
    usable = [r for r in pool if _valid_for_use(r, user, center_id)]

    chosen, missing = [], []
    for need in needed:
        match = next((r for r in usable if r.kind == need['kind'] and covers(r.subject, need)
                      and r not in chosen), None)
        if match is None:
            missing.append(need)
        else:
            chosen.append(match)
    if missing:
        raise ApprovalRequired({
            'code': 'approval_required',
            'detail': 'This bill needs approval: ' + '; '.join(describe(n) for n in missing) + '.',
            'required': missing,
        })
    return chosen


def consume(requests, invoice):
    """Spend the approvals on this bill, once. A race for the same approval fails the bill."""
    for req in requests:
        updated = ApprovalRequest.objects.filter(
            pk=req.pk, status=ApprovalRequest.APPROVED, used_invoice__isnull=True,
        ).update(status=ApprovalRequest.USED, used_invoice=invoice)
        if not updated:
            raise ValidationError({'detail': 'That approval has already been used on another bill.'})


def describe(need):
    if need['kind'] == 'discount':
        return (f"a discount of Rs {need['amount']} ({need['percent']}%; free up to "
                f"{need['free_percent']}%)")
    names = ', '.join(f"{l['name'] or 'item'} at Rs {l['unit_price']} (list Rs {l['list_price']})"
                      for l in need['lines'])
    return f'a price change: {names}'


# ─── Asking ─────────────────────────────────────────────────────────────────

def _link(raw_token):
    base = getattr(settings, 'APP_BASE_URL', 'http://localhost:4200').rstrip('/')
    return f'{base}/approve/{raw_token}'


link_for = _link


def stock_link_days():
    return int(app_settings.get('approvals.stock_link_days'))


def open_shadow_request(stock_request, asker, approvers, summary, days):
    """The e-mail / WhatsApp side of a stock request: one pending ApprovalRequest
    that points at it, and a single-use link for each approver. Returns
    {user pk: raw link secret}. Stock requests wait for people, so these links
    live for days and `expire_stale` never closes them."""
    expires = timezone.now() + timedelta(days=days)
    req = ApprovalRequest.objects.create(
        kind=STOCK_REQUEST, center=stock_request.requesting_center, requested_by=asker,
        subject={'stock_request': stock_request.pk}, summary=summary[:300],
        reason=(stock_request.notes or '')[:300])
    raw_by_user = {}
    for approver in approvers:
        raw = secrets.token_urlsafe(32)
        raw_by_user[approver.pk] = raw
        ApprovalToken.objects.create(request=req, approver=approver, token_hash=_hash(raw), expires_at=expires)
    return raw_by_user


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def expire_stale():
    """Pending requests nobody answered lapse (the cashier asks again)."""
    minutes = int(app_settings.get('approvals.request_minutes'))
    cutoff = timezone.now() - timedelta(minutes=minutes)
    return ApprovalRequest.objects.filter(
        status=ApprovalRequest.PENDING, created_at__lt=cutoff).exclude(
        kind=STOCK_REQUEST).update(status=ApprovalRequest.EXPIRED)


def create_request(user, center, need, reason=''):
    """Open a request for one `need` (as returned by `requirements`) and tell the approvers."""
    from notifications.service import notify

    kind = need['kind']
    if not KINDS.get(kind, {}).get('available'):
        raise ValidationError({'kind': 'That approval is not in use yet.'})
    if not policy_enabled(kind):
        raise ValidationError({'kind': 'This does not need approval.'})
    expire_stale()
    subject = {k: v for k, v in need.items() if k != 'kind'}

    with transaction.atomic():
        existing = ApprovalRequest.objects.filter(
            kind=kind, center=center, requested_by=user, status=ApprovalRequest.PENDING,
            subject=subject).first()
        if existing:
            return existing
        req = ApprovalRequest.objects.create(
            kind=kind, center=center, requested_by=user, subject=subject,
            summary=describe(need)[:300], reason=(reason or '')[:300])

        approvers = approvers_for(kind, center.pk)
        raw_by_user = {}
        expires = timezone.now() + timedelta(minutes=int(app_settings.get('approvals.link_minutes')))
        for approver in approvers:
            raw = secrets.token_urlsafe(32)
            raw_by_user[approver.pk] = raw
            ApprovalToken.objects.create(request=req, approver=approver, token_hash=_hash(raw),
                                         expires_at=expires)

        where = center.display_name or center.center_name or 'a branch'
        who = user.full_name or user.email
        headline = f'{who} at {where} needs approval for {describe(need)}.'
        if reason:
            headline += f' Reason: {reason}'
        minutes = int(app_settings.get('approvals.link_minutes'))

        def with_link(approver):
            return (f'{headline}\nApprove or reject (link works once, for {minutes} minutes): '
                    f'{_link(raw_by_user[approver.pk])}')

        notify('approval.requested', title=f'Approval needed - {where}', body=headline,
               url='/approvals', center=center, users=approvers,
               data={'request_id': req.pk, 'kind': kind},
               email_body=with_link, whatsapp_body=with_link)
    return req


# ─── Deciding ───────────────────────────────────────────────────────────────

def decide(req_id, user, action, note='', via='pos', token=None):
    """Approve or reject. The first decision wins; later ones are refused."""
    from notifications.service import notify, push_to_user

    if action not in ('approve', 'reject'):
        raise ValidationError({'action': 'Approve or reject.'})
    expire_stale()          # outside the block: a refusal below must not undo it
    shadow = ApprovalRequest.objects.filter(pk=req_id, kind=STOCK_REQUEST).first()
    if shadow is not None:
        # A stock request is decided by inventory's own code, so the POS button
        # and the e-mail link give the same answer.
        from inventory.stock_request_flow import decide as decide_stock_request
        decide_stock_request(shadow.subject['stock_request'], user, action == 'approve',
                             note=note, via=via, token=token)
        return ApprovalRequest.objects.get(pk=shadow.pk)
    with transaction.atomic():
        req = ApprovalRequest.objects.select_for_update().select_related(
            'center', 'requested_by').get(pk=req_id)
        if req.status != ApprovalRequest.PENDING:
            raise ValidationError({'detail': f'This request is already {req.status}'
                                             + (f' by {req.decided_by.full_name}.' if req.decided_by else '.')})
        if not may_decide(user, req):
            raise PermissionDenied('You are not an approver for this branch.')
        req.status = ApprovalRequest.APPROVED if action == 'approve' else ApprovalRequest.REJECTED
        req.decided_by, req.decided_at = user, timezone.now()
        req.decided_via, req.decision_note = via, (note or '')[:300]
        req.save()
        ApprovalToken.objects.filter(request=req, used_at__isnull=True).update(
            used_at=timezone.now(), used_action='closed')
        if token is not None:
            ApprovalToken.objects.filter(pk=token.pk).update(used_action=action)

        word = 'approved' if action == 'approve' else 'rejected'
        if req.requested_by_id:
            notify('approval.decided', title=f'Your request was {word}',
                   body=f'{user.full_name or user.email} {word} {req.summary}.',
                   center=req.center, users=[req.requested_by],
                   data={'request_id': req.pk, 'status': req.status})
            push_to_user(req.requested_by_id, {'type': 'approval_update', 'request_id': req.pk,
                                               'status': req.status})
    return req


def resolve_token(raw):
    """(token, request) for a live link, else a clear error."""
    token = ApprovalToken.objects.select_related(
        'request', 'request__center', 'request__requested_by', 'approver').filter(
        token_hash=_hash(raw or '')).first()
    if token is None:
        raise ValidationError({'detail': 'This link is not valid.'})
    if token.used_at is not None:
        raise ValidationError({'detail': 'This link has already been used.'})
    if token.expires_at < timezone.now():
        raise ValidationError({'detail': 'This link has expired. Open the POS to decide.'})
    return token, token.request


def decide_by_token(raw, action, note=''):
    token, req = resolve_token(raw)
    with transaction.atomic():
        locked = ApprovalToken.objects.select_for_update().get(pk=token.pk)
        if locked.used_at is not None:
            raise ValidationError({'detail': 'This link has already been used.'})
        ApprovalToken.objects.filter(pk=locked.pk).update(used_at=timezone.now(), used_action=action)
        return decide(req.pk, token.approver, action, note=note, via='link', token=token)


def cancel(req, user):
    if req.requested_by_id != user.pk and not user.is_superuser:
        raise PermissionDenied('Only the person who asked can withdraw a request.')
    updated = ApprovalRequest.objects.filter(pk=req.pk, status=ApprovalRequest.PENDING).update(
        status=ApprovalRequest.CANCELLED)
    return bool(updated)
