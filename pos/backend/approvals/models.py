from django.conf import settings
from django.db import models


class ApprovalPolicy(models.Model):
    """Switches an approval kind on. With no row, or `enabled` false, that kind
    needs no approval - an unchanged system works exactly as before."""

    kind = models.CharField(max_length=30, unique=True)
    enabled = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.kind


class ApprovalApprover(models.Model):
    """Who may decide a kind at a branch. `center` empty = every branch.
    One person can cover several branches; a branch can have several approvers."""

    policy = models.ForeignKey(ApprovalPolicy, on_delete=models.CASCADE, related_name='approvers')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='approval_scopes')
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True,
                               on_delete=models.CASCADE, related_name='+')

    class Meta:
        constraints = [models.UniqueConstraint(fields=['policy', 'user', 'center'],
                                               name='uniq_approver_scope')]


class ApprovalRequest(models.Model):
    PENDING, APPROVED, REJECTED, CANCELLED, EXPIRED, USED = (
        'pending', 'approved', 'rejected', 'cancelled', 'expired', 'used')

    kind = models.CharField(max_length=30)
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='+')
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
                                     related_name='approval_requests')
    # What was asked for, in exact figures: the server accepts the bill only
    # when it matches (see service.covers).
    subject = models.JSONField(default=dict)
    summary = models.CharField(max_length=300, blank=True, default='')
    reason = models.CharField(max_length=300, blank=True, default='')
    status = models.CharField(max_length=10, default=PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_via = models.CharField(max_length=12, blank=True, default='')
    decision_note = models.CharField(max_length=300, blank=True, default='')
    used_invoice = models.ForeignKey('billing.Invoice', null=True, blank=True,
                                     on_delete=models.SET_NULL, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at', '-id']
        indexes = [models.Index(fields=['status', 'center', '-created_at'], name='apr_status_idx')]


class ApprovalToken(models.Model):
    """The single-use link in an approver's e-mail or WhatsApp. Only the hash is
    stored; the link carries the secret."""

    request = models.ForeignKey(ApprovalRequest, on_delete=models.CASCADE, related_name='tokens')
    approver = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='+')
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)
    used_action = models.CharField(max_length=10, blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
