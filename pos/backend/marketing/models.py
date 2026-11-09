from django.db import models
from django.core.validators import MaxValueValidator, MinValueValidator
from salon_admin.models import Center
from decimal import Decimal

LEVEL_CHOICES = (
    ('Organisation', 'Organisation Level'),
    ('Center', 'Center Specific'),
)

class WhatsAppCampaign(models.Model):
    """One press of "Send" on the WhatsApp tab.

    It exists so a repeated send of the same text to the same audience can be
    caught, and so a large audience can be prepared by a background worker
    instead of inside the web request.
    """
    STATUS_PREPARING = 'Preparing'
    STATUS_DONE = 'Done'

    center = models.ForeignKey(
        Center, on_delete=models.SET_NULL, null=True, blank=True, related_name='whatsapp_campaigns')
    message = models.TextField()
    # 'all', or the sorted branch ids the audience was drawn from.
    scope_key = models.CharField(max_length=255, db_index=True, default='all')
    created_by = models.ForeignKey(
        'accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='whatsapp_campaigns')
    recipients = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=20, default=STATUS_PREPARING)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f'{self.message[:40]} ({self.recipients})'


class WhatsAppMessage(models.Model):
    campaign = models.ForeignKey(
        WhatsAppCampaign, on_delete=models.SET_NULL, null=True, blank=True, related_name='messages')
    # Nullable, because a customer may not be attached to a branch. The campaign
    # sender used to file those messages under `Center.objects.first()`, which
    # credited an arbitrary branch's marketing report with messages that had
    # nothing to do with it. SET_NULL rather than CASCADE so deleting a branch
    # does not erase the record that its customers were contacted.
    center = models.ForeignKey(
        Center, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='whatsapp_messages')
    date = models.DateField(db_index=True)
    time = models.TimeField()
    client_name = models.CharField(max_length=255)
    client_phone = models.CharField(max_length=50)
    message = models.TextField()
    status = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client_name} - {self.status}"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'created_at'], name='wa_center_date_idx'),
            models.Index(fields=['status'], name='wa_status_idx'),
            models.Index(fields=['created_at'], name='wa_created_idx'),
        ]


class Promotion(models.Model):
    PROMO_TYPES = (
        ('Discount', 'Discount on Bill/items'),
        ('FlatPrice', 'Flat Price on items'),
        ('Trigger', 'Trigger Deals'),
        ('Cashback', 'Cashback Cards'),
        ('Coupon', 'Promotional Coupons'),
    )
    level = models.CharField(max_length=50, choices=LEVEL_CHOICES, default='Organisation')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, null=True, blank=True, related_name='promotions')
    promo_type = models.CharField(max_length=50, choices=PROMO_TYPES, default='Discount')
    name = models.CharField(max_length=255)
    start_date = models.DateField(db_index=True)
    end_date = models.DateField(db_index=True)
    description = models.TextField(blank=True, null=True)
    members_only = models.BooleanField(default=False)
    max_usage_per_client = models.IntegerField(null=True, blank=True, help_text="Limit per client. Leave blank for unlimited.")
    discount_type = models.CharField(max_length=50, choices=(('Percentage', 'Percentage'), ('Flat', 'Flat Amount')), default='Percentage')
    discount_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    config = models.JSONField(default=dict, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active', 'start_date', 'end_date'], name='promo_active_dates_idx'),
            models.Index(fields=['created_at'], name='promo_created_idx'),
        ]


class PromotionUsage(models.Model):
    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name='usages')
    # Nullable: a walk-in bill can have no center, and a non-nullable FK here
    # raised IntegrityError mid-checkout.
    center = models.ForeignKey(Center, on_delete=models.SET_NULL, null=True, blank=True)
    client = models.ForeignKey('clients.Client', on_delete=models.CASCADE, null=True, blank=True, related_name='promo_usages')
    # Which invoice this usage belongs to. This is what makes promotion
    # application idempotent - without it, apply_promo could be called
    # repeatedly, stacking the discount toward zero.
    invoice = models.ForeignKey('billing.Invoice', on_delete=models.CASCADE, null=True, blank=True,
                               related_name='promotion_usages')
    date = models.DateField(auto_now_add=True, db_index=True)
    bill_amount_before = models.DecimalField(max_digits=10, decimal_places=2)
    bill_amount_after = models.DecimalField(max_digits=10, decimal_places=2)
    revenue_generated = models.DecimalField(max_digits=10, decimal_places=2)
    cashback_awarded = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # ── Reversal ──────────────────────────────────────────────────────────
    #
    # Cancelling or refunding a bill used to DELETE this row. That made a
    # one-per-customer coupon farmable - bill, refund, bill again, forever - and
    # erased the refunded bill from the marketing usage and ROI report.
    #
    # The row now survives. `releases_allowance` says whether the customer gets
    # their entitlement back: cancelling an unfulfilled bill releases it,
    # refunding a bill whose discount was already honoured does not.
    is_reversed = models.BooleanField(default=False, db_index=True)
    releases_allowance = models.BooleanField(
        default=False,
        help_text='The customer may use this promotion again despite this row.')
    reversal_reason = models.CharField(max_length=255, blank=True, default='')

    #: Rows that still consume a customer's per-client allowance.
    @classmethod
    def counting_toward_limit(cls, **filters):
        return cls.objects.filter(**filters).exclude(releases_allowance=True)

    def __str__(self):
        return f"{self.promotion.name} at {self.center}"

    class Meta:
        constraints = [
            # No `condition`: MySQL cannot create conditional unique
            # constraints and silently skipped this one. Both MySQL and SQLite
            # treat NULLs as distinct in a unique index, so the plain
            # constraint gives the same guarantee - one row per
            # (promotion, invoice), any number of rows without an invoice.
            models.UniqueConstraint(
                fields=['promotion', 'invoice'],
                name='uniq_promo_per_invoice',
            ),
        ]
        indexes = [
            models.Index(fields=['promotion', 'center'], name='promo_usage_center_idx'),
            models.Index(fields=['client'], name='promo_usage_client_idx'),
            models.Index(fields=['invoice'], name='promo_usage_invoice_idx'),
        ]


class CouponCode(models.Model):
    """One brand coupon: a code that pays part of a bill, once.

    A "Promotional Coupons" promotion holds the codes a partner brand (Blinkit,
    say) gave the business, or that the business made for it. At the till the
    code is typed under the Gift Card payment method; it is valid only while it
    is unused, not withdrawn, and its promotion is on, in date and at the branch.
    The brand pays the business later, so the money is a receivable, not cash.
    """
    SOURCE_IMPORTED = 'imported'
    SOURCE_GENERATED = 'generated'

    promotion = models.ForeignKey(Promotion, on_delete=models.CASCADE, related_name='codes')
    # As the brand wrote it / as we printed it (BLK-7KQ2-M9XP).
    code = models.CharField(max_length=40)
    # Upper case letters and digits only: what is looked up, so "blk 7kq2-m9xp"
    # finds it. Unique across every promotion - one code, one meaning.
    code_key = models.CharField(max_length=40, unique=True)
    # A rupee value this code carries on its own (a list that mixes Rs 100 and
    # Rs 500 coupons); empty means the promotion's value applies.
    value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=12, default=SOURCE_IMPORTED)
    # Withdrawn by the business (the brand recalled it): never accepted again.
    is_void = models.BooleanField(default=False)
    is_used = models.BooleanField(default=False, db_index=True)
    used_at = models.DateTimeField(null=True, blank=True)
    invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='coupon_codes')
    payment = models.ForeignKey('billing.Payment', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='coupon_codes')
    client = models.ForeignKey('clients.Client', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='coupon_codes')
    center = models.ForeignKey(Center, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='coupon_codes')
    # What the coupon actually paid on its bill (less than its worth when the
    # bill was smaller).
    amount_applied = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Cancelling the bill puts the code back; how often that happened.
    times_released = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.code

    @property
    def status(self):
        if self.is_void:
            return 'Void'
        return 'Used' if self.is_used else 'Unused'

    class Meta:
        ordering = ['id']
        indexes = [
            models.Index(fields=['promotion', 'is_used'], name='coupon_promo_used_idx'),
        ]


class ValueCard(models.Model):
    level = models.CharField(max_length=50, choices=LEVEL_CHOICES, default='Organisation')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, null=True, blank=True, related_name='value_cards')
    title = models.CharField(max_length=255)
    pre_tax_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    post_tax_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    benefit_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # The GST rate, stated rather than inferred.
    #
    # There was no rate column, so `billing/pricing.py` inferred one from the
    # gap between pre_tax_price and post_tax_price. And the create/edit forms
    # wrote that pair with a hardcoded 1.05 in BOTH directions - typing in
    # either field rewrote the other to a 5% relationship - so 18% was
    # physically unreachable through the UI and every membership and value card
    # was invoiced at 5%. Beauty and personal-care services under SAC 9997
    # attract 18%.
    #
    # 18 is the default for anything created from now on. Existing rows are
    # backfilled from whatever their price pair already implied, so no bill
    # changes retroactively.
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('18.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='GST rate charged when this is sold.')

    incentive = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    expiry_days = models.IntegerField(validators=[MinValueValidator(1)], help_text="Must be at least 1 day.")
    description = models.TextField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title

    class Meta:
        indexes = [
            models.Index(fields=['is_active'], name='vc_active_idx'),
            models.Index(fields=['created_at'], name='vc_created_idx'),
        ]


class Membership(models.Model):
    level = models.CharField(max_length=50, choices=LEVEL_CHOICES, default='Organisation')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, null=True, blank=True, related_name='memberships')
    name = models.CharField(max_length=255)
    pre_tax_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    post_tax_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # The GST rate, stated rather than inferred.
    #
    # There was no rate column, so `billing/pricing.py` inferred one from the
    # gap between pre_tax_price and post_tax_price. And the create/edit forms
    # wrote that pair with a hardcoded 1.05 in BOTH directions - typing in
    # either field rewrote the other to a 5% relationship - so 18% was
    # physically unreachable through the UI and every membership and value card
    # was invoiced at 5%. Beauty and personal-care services under SAC 9997
    # attract 18%.
    #
    # 18 is the default for anything created from now on. Existing rows are
    # backfilled from whatever their price pair already implied, so no bill
    # changes retroactively.
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('18.00'),
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='GST rate charged when this is sold.')

    incentive = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    expiry_days = models.IntegerField(validators=[MinValueValidator(1)], help_text="Must be at least 1 day.")
    description = models.TextField(blank=True, null=True)
    is_vip = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active'], name='mbr_active_idx'),
            models.Index(fields=['created_at'], name='mbr_created_idx'),
        ]


class Package(models.Model):
    level = models.CharField(max_length=50, choices=LEVEL_CHOICES, default='Organisation')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, null=True, blank=True, related_name='packages')
    name = models.CharField(max_length=255)
    service_name = models.CharField(max_length=255, blank=True, null=True)  # Legacy placeholder
    services_json = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    # GST charged when the package is sold. Empty = the business-wide rate in
    # Admin > Settings (GST on package sales), which is what every package paid
    # until now.
    gst_percent = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal('0')), MaxValueValidator(Decimal('100'))],
        help_text='GST rate charged when this is sold. Leave empty for the default in Settings.')
    validity_days = models.IntegerField(validators=[MinValueValidator(1)], help_text="Must be at least 1 day.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['is_active'], name='pkg_active_idx'),
            models.Index(fields=['created_at'], name='pkg_created_idx'),
        ]
