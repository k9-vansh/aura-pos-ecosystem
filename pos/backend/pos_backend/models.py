from django.db import models
from django.utils import timezone

class SapSyncQueue(models.Model):
    """Dead letter queue for SAP syncs that failed."""
    OBJECT_TYPE_CHOICES = (
        ('client',            'Client (Business Partner)'),
        ('product',           'Product (Item)'),
        ('vendor',            'Vendor (Supplier BP)'),
        ('invoice',           'Invoice (A/R Invoice)'),
        ('credit_note',       'Credit Note (A/R Credit Memo)'),
        ('purchase_order',    'Purchase Order (GRPO)'),
        ('stock_transfer',    'Stock Transfer (Inventory Transfer)'),
        ('stock_transaction', 'Stock Transaction (Goods Issue/Receipt)'),
        ('center',            'Center (Warehouse)'),
        ('incoming_payment',  'Incoming Payment'),
        ('advance_je',        'Advance (Journal Entry)'),
        ('marketing_je',      'Card / membership / package sale (Journal Entry)'),
    )
    object_type = models.CharField(max_length=20, choices=OBJECT_TYPE_CHOICES)
    object_id = models.IntegerField()
    action = models.CharField(max_length=50, default='create') # create, update

    retry_count = models.IntegerField(default=0)
    last_error = models.TextField(blank=True)
    next_retry_at = models.DateTimeField(default=timezone.now)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'pos_backend_sap_sync_queue'
        indexes = [
            models.Index(fields=['object_type', 'object_id']),
            models.Index(fields=['next_retry_at']),
        ]

    def __str__(self):
        return f"SAP Sync Retry: {self.object_type} {self.object_id} ({self.retry_count} retries)"


class SapPosting(models.Model):
    """One row per document the POS posts to SAP, keyed by what it is.

    The retry queue re-runs a whole sync function. Without a record of which
    documents already went through, a retry after a timeout (SAP accepted the
    document but the reply was lost) or after a half-finished run (the A/R
    payment posted, the value-card journal did not) posted the same document
    again. Every posting goes through `sap_ledger.post_once`, which checks
    this table first and asks SAP before trying again.

    The row keyed `lease:sap-retry-worker` is not a document: it is the lease
    that lets only one server process run the retry worker at a time.
    """
    PENDING = 'pending'
    DONE = 'done'
    STATUS_CHOICES = ((PENDING, 'Pending'), (DONE, 'Posted'))

    key = models.CharField(max_length=120, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING)
    doc_entry = models.CharField(max_length=40, blank=True)
    # What this posting moved, and for incoming payments which POS payments it
    # covered, so a later part-payment posts only what is new.
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    detail = models.JSONField(default=dict, blank=True)
    attempts = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pos_backend_sap_posting'

    def __str__(self):
        return f"{self.key} [{self.status}] {self.doc_entry}"


class BackgroundJob(models.Model):
    """One unit of background work - the outbox.

    Business code enqueues a job inside the SAME database transaction as the
    change it describes (a bill, a stock move), so a job exists if and only if
    the change committed. Worker processes (`manage.py run_workers`) claim due
    jobs, run the registered handler and record the outcome. See jobs.py.
    """
    QUEUED = 'queued'
    RUNNING = 'running'
    DONE = 'done'
    FAILED = 'failed'      # will be retried at run_after
    DEAD = 'dead'          # gave up after max_attempts; needs a person
    STATUS_CHOICES = (
        (QUEUED, 'Queued'), (RUNNING, 'Running'), (DONE, 'Done'),
        (FAILED, 'Failed - retrying'), (DEAD, 'Dead - needs attention'),
    )

    kind = models.CharField(max_length=80, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    # Optional: at most one live (not done) job per key - e.g. 'sap:invoice:42'.
    dedupe_key = models.CharField(max_length=160, null=True, blank=True, unique=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=QUEUED)
    priority = models.SmallIntegerField(default=100, help_text='Lower runs first.')
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=8)
    run_after = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    locked_by = models.CharField(max_length=120, blank=True)
    last_error = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'pos_backend_background_job'
        indexes = [
            models.Index(fields=['status', 'run_after', 'priority'], name='job_due_idx'),
            models.Index(fields=['kind', 'status'], name='job_kind_status_idx'),
        ]

    def __str__(self):
        return f"{self.kind} #{self.pk} [{self.status}]"


class AppSetting(models.Model):
    """A value set in Admin > Settings (owner decision: nothing hard-coded).

    One row per (setting, scope). `scope` is 'global' or 'center:<id>'; a
    branch row overrides the global one for that branch. Which settings exist,
    their type, limits and default live in pos_backend/app_settings.py.
    """
    GLOBAL = 'global'

    key = models.CharField(max_length=80)
    scope = models.CharField(max_length=40, default=GLOBAL)
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True,
                               on_delete=models.CASCADE, related_name='app_settings')
    value = models.JSONField(null=True, blank=True)
    updated_by = models.ForeignKey('accounts.CustomUser', null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='+')
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'pos_backend_app_setting'
        constraints = [
            models.UniqueConstraint(fields=['key', 'scope'], name='uniq_app_setting_scope'),
        ]

    def __str__(self):
        return f'{self.key} [{self.scope}] = {self.value!r}'


class JobSchedule(models.Model):
    """Last run of each periodic task, so only one worker runs it per period."""
    name = models.CharField(max_length=80, primary_key=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        db_table = 'pos_backend_job_schedule'

    def __str__(self):
        return f"{self.name} (last {self.last_run_at})"
