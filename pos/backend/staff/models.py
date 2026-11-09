from django.db import models
from salon_admin.models import Center

class StaffMember(models.Model):
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='staff_members')
    first_name = models.CharField(max_length=255)
    last_name = models.CharField(max_length=255, blank=True, null=True)
    gender = models.CharField(max_length=20, choices=(('Male', 'Male'), ('Female', 'Female'), ('Other', 'Other')), default='Female')
    designation = models.CharField(max_length=255)
    # FK link to Designation master (nullable so existing data is unaffected)
    designation_fk = models.ForeignKey(
        'Designation',
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name='staff_members',
        verbose_name='Designation (Master)'
    )
    joining_date = models.DateField(blank=True, null=True, db_index=True)
    staff_code = models.CharField(max_length=100, blank=True, null=True, verbose_name="Staff Code")
    email = models.EmailField(blank=True, null=True)
    aadhar_number = models.CharField(max_length=50, blank=True, null=True)
    phone = models.CharField(max_length=50, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pin_code = models.CharField(max_length=20, blank=True, null=True)
    # Holds a password HASH (make_password), so it needs Django's 128 characters.
    # At 50, SQLite stored the hash anyway (it ignores lengths) but MySQL
    # rejects it - setting any staff PIN failed with "Data too long".
    app_password = models.CharField(max_length=128, blank=True, null=True)
    image = models.ImageField(upload_to='staff_images/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    product_commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    allocated_points = models.DecimalField(max_digits=10, decimal_places=2, default=0.0, verbose_name="Perk Points")
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        name = self.first_name
        if self.last_name:
            name += f" {self.last_name}"
        return name

    class Meta:
        indexes = [
            models.Index(fields=['center'], name='staff_center_idx'),
            models.Index(fields=['is_active'], name='staff_active_idx'),
            models.Index(fields=['center', 'is_active'], name='staff_center_active_idx'),
            models.Index(fields=['created_at'], name='staff_created_idx'),
        ]

    def delete(self, *args, **kwargs):
        """Soft delete: disable the staff member instead of cascading deletions."""
        self.is_active = False
        self.save(update_fields=['is_active'])

class Designation(models.Model):
    name = models.CharField(max_length=255, unique=True)
    salary = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    product_commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        indexes = [
            models.Index(fields=['created_at'], name='desig_created_idx'),
        ]

class SafeServiceLogManager(models.Manager):
    """Default manager that hides commission rows for reversed invoices.

    Written explicitly rather than as `.exclude(invoice__status__in=[...])` so
    the treatment of rows with no invoice is unambiguous: a manually recorded
    service log has `invoice=None` and MUST still count toward commission.

    Every commission query used to add `invoice__status__in=Invoice.REVENUE_STATUSES`
    on top of this, which silently dropped every manually recorded log - so
    those services earned nothing. That filter is now redundant: this manager
    already excludes reversed invoices.
    """

    def get_queryset(self):
        from django.db.models import Q

        from billing.models import Invoice
        return super().get_queryset().filter(
            Q(invoice__isnull=True)
            | ~Q(invoice__status__in=Invoice.VOID_STATUSES)
        )

class ServiceLog(models.Model):
    # Default manager safe against refunded commissions
    objects = SafeServiceLogManager()
    all_objects = models.Manager()
    TYPE_CHOICES = (
        ('Service', 'Service'),
        ('Package', 'Package'),
        ('Membership', 'Membership'),
        ('Value Card', 'Value Card'),
        ('Product', 'Product'),
    )

    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='service_logs')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='service_logs')
    client_name = models.CharField(max_length=255)
    service_name = models.CharField(max_length=255)
    service_type = models.CharField(max_length=50, choices=TYPE_CHOICES, default='Service')
    price = models.DecimalField(max_digits=10, decimal_places=2)
    date = models.DateField()
    time = models.TimeField()
    invoice = models.ForeignKey('billing.Invoice', null=True, blank=True, on_delete=models.SET_NULL, related_name='service_logs')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.service_name} by {self.staff.first_name}"

    class Meta:
        indexes = [
            models.Index(fields=['staff', 'date'], name='slog_staff_date_idx'),
            models.Index(fields=['center', 'date'], name='slog_center_date_idx'),
            models.Index(fields=['center', 'date', 'staff'], name='slog_center_date_staff_idx'),
            models.Index(fields=['center'], name='slog_center_idx'),
            models.Index(fields=['invoice'], name='slog_invoice_idx'),
            models.Index(fields=['date'], name='slog_date_idx'),
            models.Index(fields=['created_at'], name='slog_created_idx'),
        ]

class StaffConsumptionLog(models.Model):
    PAYMENT_CHOICES = (
        ('Points', 'Allocated Points'),
        ('Money', 'Own Money'),
    )

    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='consumption_logs')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='staff_consumptions')
    service_name = models.CharField(max_length=255)
    date = models.DateField()
    time = models.TimeField()
    payment_method = models.CharField(max_length=50, choices=PAYMENT_CHOICES, default='Points')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.staff.first_name} consumed {self.service_name}"

    class Meta:
        indexes = [
            models.Index(fields=['staff', 'date'], name='scon_staff_date_idx'),
            models.Index(fields=['center', 'date'], name='scon_center_date_idx'),
            models.Index(fields=['created_at'], name='scon_created_idx'),
        ]

class StaffTransfer(models.Model):
    TRANSFER_CHOICES = (
        ('Temporary', 'Temporary'),
        ('Permanent', 'Permanent'),
    )
    STATUS_CHOICES = (
        ('Active', 'Active'),
        ('Completed', 'Completed'),
        ('Cancelled', 'Cancelled'),
    )
    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='transfers')
    from_center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='transfers_out')
    to_center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='transfers_in')
    transfer_type = models.CharField(max_length=50, choices=TRANSFER_CHOICES, default='Permanent')
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(blank=True, null=True, db_index=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Active')
    # True once the person has actually been moved to `to_center`. A transfer
    # dated in the future is scheduled (Active, not applied) and moves them on
    # its start date; one that has not moved anybody must not "send back" anybody.
    applied = models.BooleanField(default=False)
    reason = models.TextField(default='')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.staff.first_name} -> {self.to_center.center_name}"

    class Meta:
        indexes = [
            models.Index(fields=['staff'], name='stransfer_staff_idx'),
            models.Index(fields=['status'], name='stransfer_status_idx'),
            models.Index(fields=['created_at'], name='stransfer_created_idx'),
            models.Index(fields=['start_date'], name='stransfer_start_idx'),
            models.Index(fields=['status', 'start_date'], name='stransfer_status_date_idx'),
        ]

class PayrollRecord(models.Model):
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('Locked', 'Locked'),
        ('Paid', 'Paid'),
    )
    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='payrolls')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='payrolls')
    month = models.IntegerField()
    year = models.IntegerField()
    base_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    service_commission = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    product_commission = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Value-card, add-on, target and business-slab incentives. The rule engine
    # computes these but the payroll record had no column for them, so its
    # output could not be recorded anywhere.
    other_incentive = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                          verbose_name='Other Incentives')
    deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    net_pay = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Per-category breakdown and the rules that produced it, snapshotted at
    # generation time so a later rule edit cannot rewrite settled payroll.
    breakdown = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(null=True, blank=True,
                                        help_text='When this record was generated from the incentive engine')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Draft')
    locked_at = models.DateTimeField(null=True, blank=True)
    locked_by = models.ForeignKey('accounts.CustomUser', null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name='locked_payrolls')
    paid_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def computed_net_pay(self):
        from pos_backend.money import D, money
        return money(
            D(self.base_salary) + D(self.service_commission)
            + D(self.product_commission) + D(self.other_incentive)
            - D(self.deductions)
        )

    @property
    def is_editable(self):
        return self.status == 'Draft'

    class Meta:
        unique_together = ('staff', 'month', 'year')
        indexes = [
            models.Index(fields=['center', 'month', 'year'], name='payroll_center_month_idx'),
            # The app asks "payroll for March 2026", often across every branch -
            # payroll generation and the payroll list both do. A composite
            # index is only usable from its LEFTMOST column, so the one above
            # cannot serve a query that does not name a branch. Year first,
            # because that is the coarser bucket.
            models.Index(fields=['year', 'month'], name='payroll_year_month_idx'),
            # Finding one person's payroll history.
            models.Index(fields=['staff', 'year', 'month'], name='payroll_staff_period_idx'),
            models.Index(fields=['status'], name='payroll_status_idx'),
            models.Index(fields=['created_at'], name='payroll_created_idx'),
        ]

    def __str__(self):
        return f"{self.staff.first_name} - {self.month}/{self.year} ({self.status})"

class StaffToolTracker(models.Model):
    STATUS_CHOICES = (
        ('Taken', 'Taken'),
        ('Returned', 'Returned')
    )
    staff = models.ForeignKey(StaffMember, on_delete=models.CASCADE, related_name='tool_trackers')
    tool_name = models.CharField(max_length=255)
    details = models.TextField(blank=True, null=True, verbose_name="Item Details")
    amount = models.IntegerField(default=1, verbose_name="Quantity/Amount")
    date_taken = models.DateField(db_index=True)
    expected_return_date = models.DateField(blank=True, null=True, db_index=True)
    actual_return_date = models.DateField(blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Taken')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.tool_name} - {self.staff.first_name}"

    class Meta:
        indexes = [
            models.Index(fields=['staff'], name='tool_staff_idx'),
            models.Index(fields=['status'], name='tool_status_idx'),
            models.Index(fields=['created_at'], name='tool_created_idx'),
        ]

