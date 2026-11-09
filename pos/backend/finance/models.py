from django.db import models
from django.conf import settings

class PettyCashCategory(models.Model):
    """What petty cash is spent on (tea, courier, repairs...).

    The list comes from SAP: one category per "Petty-..." expense account in
    the chart of accounts, carrying that account's code (sap_account). It is
    loaded by the "Load from SAP" action, not typed in.
    """
    name = models.CharField(max_length=80, unique=True)
    is_active = models.BooleanField(default=True)
    sap_account = models.CharField(max_length=30, blank=True, default='',
                                   help_text="SAP expense account the entries of this category are debited to.")

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name


class PettyCashEntry(models.Model):
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='petty_cash_entries')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateTimeField(auto_now_add=True, db_index=True)
    description = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    voucher_number = models.CharField(max_length=100, blank=True, null=True)
    comments = models.TextField(blank=True, null=True)
    category = models.ForeignKey('PettyCashCategory', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='entries')
    # Picture of the bill / slip behind the spend. Compulsory for a new entry (the serializer checks it);
    # entries made before it was asked for have none.
    bill_image = models.ImageField(upload_to='petty_cash/', blank=True, null=True)
    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"{self.date.strftime('%Y-%m-%d')} - {self.description} ({self.amount})"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'date'], name='petty_center_date_idx'),
        ]

class DailyClosing(models.Model):
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='daily_closings')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    date = models.DateField(db_index=True)
    
    # Cash section
    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    system_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    todays_expenses = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_in_hand = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    difference = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cash_deposit = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    closing_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Other payment methods
    credit_card = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    upi = models.DecimalField(max_digits=12, decimal_places=2, default=0)   # ADDED: was missing
    paytm = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    bharat_pe = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cheque_netbanking = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    google_pay = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    phone_pe = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    nearbuy = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    # Partner brands' coupons taken as payment: money the brands owe, not cash.
    gift_card = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    other = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('center', 'date')
        indexes = [
            models.Index(fields=['center', 'date'], name='dc_center_date_idx'),
            models.Index(fields=['created_at'], name='dc_created_idx'),
        ]
        
    def __str__(self):
        return f"Closing {self.center.center_name} - {self.date}"

class IncentiveConfig(models.Model):
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='incentive_configs', null=True, blank=True)
    name = models.CharField(max_length=255)
    
    # Types: services, redemptions, products, value_cards, packages
    category = models.CharField(max_length=50) 
    
    use_multiple = models.BooleanField(default=False)  # If True, use salary-multiple tiers
    use_custom_percent = models.BooleanField(default=True)
    custom_percent = models.DecimalField(max_digits=5, decimal_places=2, default=5.00)  # Flat % if not using tiers

    def __str__(self):
        return f"{self.name} - {self.category} ({self.custom_percent}%)"


class IncentiveTier(models.Model):
    """Defines a salary-multiple bracket for incentive calculation.
    
    Example: min_multiple=5.0, incentive_percent=8.0 means:
    If (revenue - salary) / salary >= 5.0, the staff gets 8% of total revenue.
    Center-specific tiers override org-level tiers for the same config.
    """
    config = models.ForeignKey(IncentiveConfig, on_delete=models.CASCADE, related_name='tiers')
    min_multiple = models.DecimalField(max_digits=6, decimal_places=2, default=1.00,
                                       verbose_name="Min Salary Multiple (x)")
    incentive_percent = models.DecimalField(max_digits=5, decimal_places=2, default=5.00,
                                             verbose_name="Incentive % of Revenue")

    class Meta:
        ordering = ['min_multiple']
        unique_together = ('config', 'min_multiple')

    def __str__(self):
        return f"{self.config.name}: ≥{self.min_multiple}× → {self.incentive_percent}%"

class Shift(models.Model):
    STATUS_CHOICES = (
        ('Open', 'Open'),
        ('Closed', 'Closed'),
    )
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='shifts')
    opened_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='opened_shifts')
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='closed_shifts')
    opened_at = models.DateTimeField(auto_now_add=True, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    
    starting_float = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    expected_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    actual_cash = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    variance = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Open')
    notes = models.TextField(blank=True, null=True)
    # Several shifts can run in a day, one open per till at a time.
    till = models.CharField(max_length=40, default='Till 1')

    def __str__(self):
        return f"Shift {self.id} at {self.center.center_name}"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'status'], name='shift_center_status_idx'),
        ]


class IncentiveRule(models.Model):
    FREQUENCY_CHOICES = (
        ('monthly', 'Monthly Basis'),
        ('daily', 'Daily Basis'),
    )
    CATEGORY_CHOICES = (
        ('value_cards', 'Value Cards'),
        ('products', 'Products'),
        ('services', 'Services'),
        ('memberships', 'Memberships'),
        ('packages', 'Packages'),
        ('overall', 'Overall Business'),
        ('daily_business', 'Daily Business Slabs'),
        ('service_addon', 'Specific Service Incentive (Add-on)'),
        ('service_target', 'Specific Service Target (Count Target)'),
    )
    RULE_TYPE_CHOICES = (
        ('slab', 'Fixed Slab / Range Reward'),
        ('multiple', 'Business / Salary Multiplier Tiers'),
        ('percentage', 'Flat Percentage'),
        ('flat', 'Flat Amount'),
        ('service_bonus', 'Per-Service Fixed/Percentage Bonus'),
        ('target_count', 'Service Count / Volume Target'),
    )
    APPLICABLE_ROLE_CHOICES = (
        ('all', 'All Managers and Staff'),
        ('staff', 'Staff / Stylists'),
        ('manager', 'Managers Only'),
        ('lhds_uhds', 'LHDS / UHDS (Leading & Ultimate Hair Designers)'),
        ('mhds_beauty', 'MHDS / Beauty / Therapists'),
        ('pedicurist_k_ambassador', 'Pedicurists & K Ambassadors'),
    )

    name = models.CharField(max_length=255)
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='incentive_rules', null=True, blank=True)
    frequency = models.CharField(max_length=30, default='monthly')
    category = models.CharField(max_length=60, default='services')
    rule_type = models.CharField(max_length=60, default='multiple')
    applicable_role = models.CharField(max_length=100, default='all')
    
    # Dynamic JSON list of tiers / slabs
    tiers = models.JSONField(default=list, blank=True)
    
    # Flat fallback values
    flat_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    flat_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    # Validity & Timers (ensures historical calculations remain unchanged)
    effective_from = models.DateField(null=True, blank=True, help_text="Date from which this rule applies.")
    effective_to = models.DateField(null=True, blank=True, help_text="Date until which this rule applies (optional for ongoing).")
    is_active = models.BooleanField(default=True)

    description = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['center', 'is_active'], name='ir_center_active_idx'),
            models.Index(fields=['is_active', 'category'], name='ir_active_cat_idx'),
            models.Index(fields=['created_at'], name='ir_created_idx'),
            models.Index(fields=['effective_from'], name='ir_effective_from_idx'),
            models.Index(fields=['effective_to'], name='ir_effective_to_idx'),
            models.Index(fields=['is_active', 'effective_from', 'effective_to'], name='ir_active_dates_idx'),
        ]

    def __str__(self):
        center_str = self.center.display_name if self.center else 'All Centers'
        return f"{self.name} ({self.get_category_display()} - {self.get_frequency_display()}) [{center_str}]"

