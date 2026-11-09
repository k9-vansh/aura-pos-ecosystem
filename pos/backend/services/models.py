from django.db import models
from salon_admin.models import Center

class ServiceMaster(models.Model):
    service_code = models.CharField(max_length=50, blank=True, null=True)
    name = models.CharField(max_length=200)
    brand = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=100)
    sub_category = models.CharField(max_length=100, blank=True, null=True)
    sac_code = models.CharField(max_length=50, blank=True, null=True)
    hsn_code = models.CharField(max_length=50, blank=True, null=True)
    default_price = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=5.00)
    duration_mins = models.IntegerField(default=0)
    level = models.CharField(max_length=20, choices=(('Organisation', 'Organisation'), ('Center', 'Center Specific')), default='Organisation')
    incentive = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name="Incentive %")
    centers = models.ManyToManyField(Center, related_name='assigned_services', blank=True)
    # Who owns the recipe (the products the service uses): the SAP Sales BOM that
    # `pull_from_sap` keeps in step, or the POS (Admin > Services > Recipe), which
    # SAP never overwrites.
    recipe_source = models.CharField(max_length=10, choices=(('sap', 'SAP'), ('pos', 'POS')), default='sap')
    # Is the service item in SAP (so a bill for it can be posted)? Services that came from SAP are;
    # one made or changed in the POS is sent by the background worker.
    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['category'], name='svc_category_idx'),
            models.Index(fields=['created_at'], name='svc_created_idx'),
        ]

class CenterService(models.Model):
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='services')
    service = models.ForeignKey(ServiceMaster, on_delete=models.CASCADE, related_name='center_overrides')
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Per-center incentive % override. If set, overrides ServiceMaster.incentive for this center.
    incentive = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
                                    verbose_name='Incentive % (Center Override)')
    is_active = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('center', 'service')
        indexes = [
            models.Index(fields=['center', 'is_active'], name='cs_center_active_idx'),
            models.Index(fields=['service'], name='cs_service_idx'),
            models.Index(fields=['created_at'], name='cs_created_idx'),
        ]

    def __str__(self):
        return f"{self.service.name} at {self.center.center_name}"

class ServiceRecipeGroup(models.Model):
    """A "pick one" slot of a service's recipe, e.g. Colour or Shampoo.

    The person billing the service chooses one of the group's options for each
    client. `required` groups must be chosen before the bill is completed."""

    service = models.ForeignKey(ServiceMaster, on_delete=models.CASCADE, related_name='recipe_groups')
    name = models.CharField(max_length=100)
    required = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ['sort_order', 'id']
        unique_together = ('service', 'name')

    def __str__(self):
        return f"{self.name} for {self.service.name}"


class ServiceBOM(models.Model):
    """One line of a service's recipe: a product the service uses.

    * `group` empty: a FIXED item, used every time (scissors, a towel).
    * `group` set: one OPTION of that pick-one group. The option is either a
      product (`product_code`) or every product of a category (`category`), so a
      new colour added to the category shows up without editing the service.

    Services whose recipe comes from SAP (`ServiceMaster.recipe_source == 'sap'`)
    only ever hold fixed lines: the SAP Sales BOM pulled by `pull_from_sap`.
    """
    service = models.ForeignKey(ServiceMaster, on_delete=models.CASCADE, related_name='bom_items')
    product_code = models.CharField(max_length=100, blank=True, default='',
                                    help_text="SAP ItemCode of the consumable product")
    quantity = models.DecimalField(max_digits=10, decimal_places=4, help_text="Quantity to deduct per 1 unit of service")
    unit = models.CharField(max_length=20, blank=True, default='',
                            help_text="What `quantity` counts (ml, g, pcs...); empty = the product's own stock unit")
    group =models.ForeignKey(ServiceRecipeGroup, null=True, blank=True, on_delete=models.CASCADE,
                              related_name='options')
    category = models.CharField(max_length=100, blank=True, default='',
                                help_text='Option only: every product of this category is offered')
    is_default = models.BooleanField(default=False, help_text='Option only: chosen unless the till picks another')

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['service', 'group', 'product_code', 'category'],
                                    name='uniq_recipe_line'),
        ]

    def __str__(self):
        what = self.product_code or f'any {self.category}'
        return f"{self.quantity}x {what} for {self.service.name}"


def bom_role(category):
    """What a product's Category means inside a service.

    Retail is for selling over the counter and is never used in a service.
    Anything else (Consumable, Technical, no category) is used up by the
    service and is deducted. (Technical used to be listed but not deducted;
    the owner asked on 26 Oct 2026 for it to be deducted like the rest.)
    """
    c = (category or '').strip().lower()
    if c == 'retail':
        return 'retail'
    return 'consumable'


def deducting_boms(service_id, center):
    """The BOM lines of a service whose stock is deducted at `center`.

    A line whose product the centre never stocked still counts, so the
    shortage is reported rather than silently skipped.
    """
    from inventory.models import Product
    boms = list(ServiceBOM.objects.filter(service_id=service_id, group__isnull=True))
    if not boms:
        return []
    cats = dict(Product.objects.filter(
        center=center, product_code__in=[b.product_code for b in boms]
    ).values_list('product_code', 'category'))
    return [b for b in boms
            if b.product_code not in cats or bom_role(cats[b.product_code]) == 'consumable']
