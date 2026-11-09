from django.core.validators import RegexValidator
from django.db import models

class ActiveCenterManager(models.Manager):
    """Default manager that hides disabled centers.

    `Center.delete()` is a soft delete that sets `is_active = False`, but no
    query anywhere read the flag - so a "deleted" center still appeared in
    every dropdown, dashboard aggregate and the create-for-all-centers loop.

    `Center.objects` now excludes disabled centers. `Center.all_objects` is the
    unfiltered manager, for admin screens that need to see and re-enable them.
    """

    def get_queryset(self):
        return super().get_queryset().filter(is_active=True)


class Center(models.Model):
    center_name = models.CharField(max_length=255, verbose_name="Center / Legal Name")
    display_name = models.CharField(max_length=255, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    # Printed on the receipt and needed by the e-invoice portal (Q4b).
    pin_code = models.CharField(max_length=6, blank=True, default='')
    region = models.CharField(max_length=100, blank=True, null=True)
    
    # Tax Details
    cst_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="CST Number")
    gst_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="GST Number")
    pan_number = models.CharField(max_length=50, blank=True, null=True, verbose_name="Pan Number")
    
    # Financial
    monthly_target = models.DecimalField(max_digits=12, decimal_places=2, blank=True, null=True)
    monthly_targets_history = models.JSONField(default=dict, blank=True)
    closing_sms_recipients = models.JSONField(default=list, blank=True)
    credit_limit = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    gst_enabled = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)    
    # Leads every invoice number: DL01-2627-00045 (owner decisions F20, D12).
    # 2-5 capitals/digits so the number stays within GST's 16 characters.
    branch_code = models.CharField(
        max_length=5, unique=True, null=True, blank=True,
        validators=[RegexValidator(r'^[A-Z0-9]{2,5}$',
                                   'Use 2 to 5 capital letters or digits, e.g. DL01.')],
        help_text='Starts every invoice number of this branch, e.g. DL01.')
    sap_warehouse_code = models.CharField(max_length=50, blank=True, null=True, verbose_name="SAP Warehouse Code")
    sap_branch_name = models.CharField(max_length=100, blank=True, null=True, verbose_name="SAP Branch")
    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)
    # Contact
    phone = models.CharField(max_length=20, blank=True, null=True)
    landline_1 = models.CharField(max_length=20, blank=True, null=True)
    landline_2 = models.CharField(max_length=20, blank=True, null=True)
    center_email = models.EmailField(blank=True, null=True)
    
    # Owner Details
    owner_name = models.CharField(max_length=255, blank=True, null=True)
    owner_phone = models.CharField(max_length=20, blank=True, null=True)
    owner_email_1 = models.EmailField(blank=True, null=True)
    
    owner_name_2 = models.CharField(max_length=255, blank=True, null=True)
    owner_phone_2 = models.CharField(max_length=20, blank=True, null=True)
    owner_email_2 = models.EmailField(blank=True, null=True)
    
    owner_name_3 = models.CharField(max_length=255, blank=True, null=True)
    owner_phone_3 = models.CharField(max_length=20, blank=True, null=True)
    owner_email_3 = models.EmailField(blank=True, null=True)
    
    # Accountant Details
    accountant_name_1 = models.CharField(max_length=255, blank=True, null=True)
    accountant_phone_1 = models.CharField(max_length=20, blank=True, null=True)
    accountant_email_1 = models.EmailField(blank=True, null=True)
    
    accountant_name_2 = models.CharField(max_length=255, blank=True, null=True)
    accountant_phone_2 = models.CharField(max_length=20, blank=True, null=True)
    accountant_email_2 = models.EmailField(blank=True, null=True)
    
    launched_on = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = ActiveCenterManager()
    all_objects = models.Manager()

    def __str__(self):
        return self.display_name or self.center_name

    def save(self, *args, **kwargs):
        # Every branch needs a code to number its invoices; one is suggested
        # from its GSTIN state when none was entered.
        if not self.branch_code:
            from salon_admin.branch_codes import suggest
            self.branch_code = suggest(self)
            update_fields = kwargs.get('update_fields')
            if update_fields is not None:
                kwargs['update_fields'] = set(update_fields) | {'branch_code'}
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Soft delete: disable the center instead of cascading deletions."""
        self.is_active = False
        self.save(update_fields=['is_active'])

    def reactivate(self):
        self.is_active = True
        self.save(update_fields=['is_active'])

    @property
    def charges_gst(self):
        """Whether invoices at this center carry GST.

        `gst_enabled` was declared on the model and read by nothing, so a
        non-GST center was still taxed. Billing consults this.
        """
        return bool(self.gst_enabled)

    class Meta:
        # `base_manager_name` keeps related-object traversal (invoice.center,
        # etc.) working for disabled centers - otherwise a historical invoice
        # at a closed center would fail to resolve its own center.
        base_manager_name = 'all_objects'
        indexes = [
            models.Index(fields=['created_at'], name='center_created_idx'),
            models.Index(fields=['is_active'], name='center_active_idx'),
        ]

class Role(models.Model):
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True, null=True)
    permissions = models.JSONField(default=dict, blank=True)
    
    def __str__(self):
        return self.name
