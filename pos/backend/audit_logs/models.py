from django.db import models
from django.conf import settings


class SystemLog(models.Model):
    # Who
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='audit_logs'
    )
    user_name = models.CharField(max_length=255, blank=True)   # snapshot at log time
    user_email = models.CharField(max_length=255, blank=True)
    user_role = models.CharField(max_length=100, blank=True)
    user_id_snapshot = models.IntegerField(null=True, blank=True)

    # Where / Which Centre
    center_id = models.IntegerField(null=True, blank=True)
    center_name = models.CharField(max_length=255, blank=True)

    # What happened
    action = models.CharField(max_length=50)           # LOGIN, CREATE, UPDATE, DELETE, CANCEL, REFUND, LOGOUT
    module = models.CharField(max_length=100, blank=True)  # CLIENTS, STAFF, BILLING, USERS, etc.
    entity_type = models.CharField(max_length=100, blank=True)  # client, staff member, invoice, etc.
    entity_id = models.CharField(max_length=50, blank=True)     # ID of the object affected
    human_description = models.TextField(blank=True)   # "Admin created user John Doe"

    # Technical details
    path = models.CharField(max_length=255)
    description = models.TextField(blank=True)         # sanitised JSON payload
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    device_info = models.TextField(blank=True)   # raw User-Agent
    device_type = models.CharField(max_length=30, blank=True)   # Desktop / Laptop / Mobile / Tablet
    browser = models.CharField(max_length=100, blank=True)       # Chrome 120, Firefox 125 …
    os_info = models.CharField(max_length=100, blank=True)       # Windows 11, macOS 14, iOS 17 …
    geo_city = models.CharField(max_length=100, blank=True)
    geo_region = models.CharField(max_length=100, blank=True)
    geo_country = models.CharField(max_length=100, blank=True)
    geo_country_code = models.CharField(max_length=4, blank=True)
    # How it ended. The log used to record a refused or failed request exactly as
    # if it had worked ("nobody@example.com logged in" for a wrong password).
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    # Exact position the browser reported at sign-in (with the person's permission).
    geo_lat = models.FloatField(null=True, blank=True)
    geo_lng = models.FloatField(null=True, blank=True)
    geo_accuracy = models.FloatField(null=True, blank=True)  # metres
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['-timestamp']),
            models.Index(fields=['action']),
            models.Index(fields=['module']),
            models.Index(fields=['center_id']),
            models.Index(fields=['user_email']),
            # Composite index for the most common filter: module + timestamp for date-range queries
            models.Index(fields=['module', 'action'], name='syslog_module_action_idx'),
            # Composite for center + date filtering
            models.Index(fields=['center_id', 'timestamp'], name='syslog_center_ts_idx'),

            # ── Filter + sort, which is what the screen actually does ──────
            #
            # Every list on the Logs screen filters on something and then sorts
            # by -timestamp. An index on the filter column alone cannot serve
            # the sort, so the database gathers every matching row and sorts
            # them. These carry the sort column, so it can walk the index and
            # stop after one page.
            models.Index(fields=['action', '-timestamp'], name='syslog_action_ts_idx'),
            models.Index(fields=['module', '-timestamp'], name='syslog_module_ts_idx'),
            models.Index(fields=['user_email', '-timestamp'], name='syslog_email_ts_idx'),

            # "Click a person to see their actions" is the log's main
            # drill-down, and it matches `user_id OR user_id_snapshot`.
            # `user_id_snapshot` was in no index at all, so half of that OR was
            # a full table scan every time.
            models.Index(fields=['user_id', '-timestamp'], name='syslog_user_ts_idx'),
            models.Index(fields=['user_id_snapshot', '-timestamp'],
                         name='syslog_usersnap_ts_idx'),

            # The facets endpoint groups on these. Three of them appeared in no
            # index, so each dropdown cost a full scan.
            models.Index(fields=['entity_type'], name='syslog_entity_type_idx'),
            models.Index(fields=['user_role'], name='syslog_user_role_idx'),
            models.Index(fields=['device_type'], name='syslog_device_type_idx'),

            # Finding every action against one record - "who touched invoice
            # 1234" - which is the question an audit log exists to answer.
            models.Index(fields=['entity_type', 'entity_id'], name='syslog_entity_idx'),
            models.Index(fields=['status_code', '-timestamp'], name='syslog_status_ts_idx'),
        ]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} | {self.user_email or 'Anon'} | {self.action} | {self.module}"
