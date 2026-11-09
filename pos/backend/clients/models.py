import datetime

from django.db import models


class Client(models.Model):
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True, on_delete=models.SET_NULL)
    phone = models.CharField(max_length=20)
    app_pin = models.CharField(max_length=128, blank=True, null=True)        # the old, unused PIN column
    # The client's 4-digit PIN, sealed (see clients/pins.py): needed to use their cards, memberships and advances.
    pin_sealed = models.TextField(blank=True, default='')
    pin_set_at = models.DateTimeField(null=True, blank=True)
    first_name = models.CharField(max_length=100)
    last_name = models.CharField(max_length=100, blank=True, null=True)
    email = models.EmailField(blank=True, null=True)
    birthday = models.DateField(blank=True, null=True)
    gst_number = models.CharField(max_length=50, blank=True, null=True)
    # A B2B client's billing address and PIN: printed on their tax invoice and
    # required by the e-invoice portal (Q4b).
    address = models.TextField(blank=True, default='')
    pin_code = models.CharField(max_length=6, blank=True, default='')
    notes = models.TextField(blank=True, null=True)
    # Normalised phone, unique across the business when present (F2). Kept
    # by save(); a NULL key (no phone) may repeat.
    phone_key = models.CharField(max_length=20, null=True, blank=True, unique=True, editable=False)
    # The phone written backwards, kept by save(): "the last digits of the number" is then a prefix lookup on an
    # index (clients/search.py) instead of a scan of every customer.
    phone_reversed = models.CharField(max_length=20, blank=True, default='', editable=False)
    # Promotional messages only go to a client who has not opted out; DND
    # is still respected on top (blueprint B5.3).
    whatsapp_consent = models.BooleanField(default=True)
    sms_consent = models.BooleanField(default=True)
    dnd_status = models.CharField(max_length=50, default='NOT ON DND')
    gender = models.CharField(max_length=20, default='female', blank=True, null=True)
    
    is_active = models.BooleanField(default=True)
    is_blacklisted = models.BooleanField(default=False)
    blacklist_reason = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)

    @property
    def full_name(self):
        """Safe concatenation that handles nullable last_name."""
        return f"{self.first_name} {self.last_name or ''}".strip()

    @property
    def advance_balance(self):
        """
        Advance balance = sum of all AdvancePayment rows for this client.
        Positive rows = advances received.
        Negative rows = advances redeemed (created in finalize_invoice).
        We do NOT also look at Payment rows because that would double-subtract.

        Deliberately does NOT swallow exceptions. This value is used as the cap
        on how much a customer may redeem; returning Rs 0.00 on a transient
        database error silently reported an empty wallet to a paying customer.
        """
        from django.db.models import Sum
        from billing.models import AdvancePayment
        from pos_backend.money import money

        total = AdvancePayment.objects.filter(client=self).aggregate(
            total=Sum('amount')
        )['total']
        return money(total or 0)

    @property
    def cashback_balance(self):
        """
        Cashback balance = sum of all CashbackTransaction rows for this client.
        Positive rows = cashback earned.
        Negative rows = cashback redeemed.
        """
        from django.db.models import Sum
        from billing.models import CashbackTransaction
        from pos_backend.money import money

        total = CashbackTransaction.objects.filter(client=self).aggregate(
            total=Sum('amount')
        )['total']
        return money(total or 0)

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        # The phone is stored normalised and its key enforces one client per
        # phone across the whole business (F2). A legacy record without a
        # phone has no key; any number of those may exist (Q1).
        from clients.identity import normalise_phone, phone_key
        self.phone = normalise_phone(self.phone)
        self.phone_key = phone_key(self.phone)
        self.phone_reversed = self.phone[::-1]
        if self.gst_number:
            self.gst_number = self.gst_number.strip().upper()
        update_fields = kwargs.get('update_fields')
        if update_fields is not None and 'phone' in update_fields:
            kwargs['update_fields'] = set(update_fields) | {'phone_key', 'phone_reversed'}
        is_new = self.pk is None
        if is_new and self.phone and not self.pin_sealed:
            # Every client with a phone has a PIN from the day they are created (clients/pins.py); it is sent to them
            # when they are added at the counter, and when they first buy a card, advance or membership.
            from django.utils import timezone
            from clients import pins
            self.pin_sealed = pins.seal(pins.new_pin())
            self.pin_set_at = timezone.now()
            if update_fields is not None:
                kwargs['update_fields'] = set(kwargs['update_fields']) | {'pin_sealed', 'pin_set_at'}
        super().save(*args, **kwargs)
        # The welcome e-mail used to be sent from here on a thread, branded
        # "Chilli Potato" and carrying a client-app PIN; the app is gone (#3).
        # It is now a background job, switched in Admin > Settings.
        if is_new and self.email:
            from pos_backend import app_settings
            if app_settings.get('clients.welcome_email'):
                from pos_backend.jobs import enqueue
                enqueue('clients.welcome_email', {'client_id': self.pk},
                        dedupe_key=f'welcome:{self.pk}')

    class Meta:
        ordering = ['first_name']
        indexes = [
            models.Index(fields=['phone'], name='client_phone_idx'),
            models.Index(fields=['center'], name='client_center_idx'),
            models.Index(fields=['created_at'], name='client_created_idx'),
            models.Index(fields=['center', 'created_at'], name='client_center_date_idx'),
            models.Index(fields=['gender'], name='client_gender_idx'),
            # The till finds a customer by the start of the first name, last name, GSTIN, or the first / last digits
            # of the phone (clients/search.py).
            models.Index(fields=['first_name'], name='client_first_name_idx'),
            models.Index(fields=['last_name'], name='client_last_name_idx'),
            models.Index(fields=['gst_number'], name='client_gst_idx'),
            models.Index(fields=['phone_reversed'], name='client_phone_rev_idx'),
        ]

    def delete(self, *args, **kwargs):
        """Soft delete: disable the client instead of cascading deletions."""
        self.is_active = False
        self.save(update_fields=['is_active'])


class ClientMembership(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='memberships')
    # PROTECT: deleting a Membership template used to erase every holder's
    # membership.
    membership = models.ForeignKey('marketing.Membership', on_delete=models.PROTECT)
    source_invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True)
    # The selling branch: a membership is used only there (#36).
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='client_memberships')
    start_date = models.DateField(default=datetime.date.today)
    expiry_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client.first_name} - {self.membership.name}"

    class Meta:
        indexes = [
            models.Index(fields=['client'], name='cm_client_idx'),
            models.Index(fields=['is_active'], name='cm_active_idx'),
            models.Index(fields=['created_at'], name='cm_created_idx'),
            models.Index(fields=['expiry_date'], name='cm_expiry_idx'),
            models.Index(fields=['is_active', 'expiry_date'], name='cm_active_expiry_idx'),
        ]


class ClientPackage(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='packages')
    # FIXED: null=True allows custom packages that have no pre-defined Package object
    # PROTECT: deleting a Package template used to erase every customer's
    # remaining entitlement.
    package = models.ForeignKey('marketing.Package', on_delete=models.PROTECT, null=True, blank=True)
    source_invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True)
    # The selling branch: a package is redeemed only there (#32).
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='client_packages')
    services_remaining = models.JSONField(default=dict, help_text="Maps service_id to remaining quantity")
    start_date = models.DateField(default=datetime.date.today)
    expiry_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        pkg_name = self.package.name if self.package else 'Custom Package'
        return f"{self.client.first_name} - {pkg_name}"

    class Meta:
        indexes = [
            models.Index(fields=['client'], name='cpkg_client_idx'),
            models.Index(fields=['is_active'], name='cpkg_active_idx'),
            models.Index(fields=['created_at'], name='cpkg_created_idx'),
            models.Index(fields=['expiry_date'], name='cpkg_expiry_idx'),
            models.Index(fields=['is_active', 'expiry_date'], name='cpkg_active_expiry_idx'),
        ]


class ClientValueCard(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name='value_cards')
    # PROTECT, not CASCADE. Deleting a ValueCard TEMPLATE used to cascade
    # away every customer's issued card - and with it their entire prepaid
    # balance, across all branches, with no audit row and nothing to restore
    # from. Tidying up an expired offer listing is a normal end-of-season
    # action; it must not be able to destroy customer money.
    value_card = models.ForeignKey('marketing.ValueCard', on_delete=models.PROTECT)
    source_invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True)
    # Face value left on the card.
    balance = models.DecimalField(max_digits=10, decimal_places=2)
    # Fixed when the card is sold (F5): the face value loaded, the pre-tax
    # price paid for it and the card's GST rate. Each rupee of face value buys
    # pre_tax_price / face_value rupees of taxable service, taxed at gst_percent.
    face_value = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    pre_tax_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    # The selling branch: the card is used only there (#36).
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='client_value_cards')
    # Unused balance is forfeited at expiry (F7); what was lost is kept here.
    forfeited_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    forfeited_on = models.DateField(null=True, blank=True)
    start_date = models.DateField(default=datetime.date.today)
    expiry_date = models.DateField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.client.first_name} - {self.value_card.title}"

    @property
    def number(self):
        return f'VC-{self.pk:06d}' if self.pk else 'VC-NEW'

    def terms(self):
        """(face_value, pre_tax_price, gst_percent) as Decimals.

        Cards issued before the terms were stored read them from the template
        they were sold from."""
        from pos_backend.money import D, HUNDRED, money
        tpl = self.value_card
        face = D(self.face_value) if self.face_value is not None else D(getattr(tpl, 'value', None))
        rate = D(self.gst_percent) if self.gst_percent is not None else D(getattr(tpl, 'gst_percent', None))
        if self.pre_tax_price is not None:
            pre = D(self.pre_tax_price)
        else:
            pre = D(getattr(tpl, 'pre_tax_price', None))
            if pre <= 0 and D(getattr(tpl, 'post_tax_price', None)) > 0:
                pre = money(D(tpl.post_tax_price) * HUNDRED / (HUNDRED + rate))
        return face, pre, rate

    class Meta:
        indexes = [
            models.Index(fields=['client'], name='cvc_client_idx'),
            models.Index(fields=['is_active'], name='cvc_active_idx'),
            models.Index(fields=['created_at'], name='cvc_created_idx'),
            models.Index(fields=['expiry_date'], name='cvc_expiry_idx'),
            models.Index(fields=['is_active', 'expiry_date'], name='cvc_active_expiry_idx'),
        ]


class MasterPin(models.Model):
    """One branch's master PIN for one day. A new random one is made for each day the first time it is asked for."""
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='master_pins')
    date = models.DateField()
    sealed = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [('center', 'date')]
        ordering = ['-date']


class PinAttempt(models.Model):
    """Every PIN tried (client PIN, master PIN, "send me my PIN"), right or wrong: it limits guessing and is the trail
    of who used a master PIN for whom."""
    KINDS = (('client', 'Client PIN'), ('master', 'Master PIN'), ('send', 'PIN sent'), ('login', 'Client app sign-in'))
    kind = models.CharField(max_length=10, choices=KINDS)
    ok = models.BooleanField(default=False)
    client = models.ForeignKey('clients.Client', null=True, blank=True, on_delete=models.CASCADE, related_name='pin_attempts')
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True, on_delete=models.CASCADE)
    user_id = models.IntegerField(null=True, blank=True)
    via = models.CharField(max_length=30, blank=True, default='')
    at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=['kind', 'client', 'at'], name='pinatt_client_idx'),
                   models.Index(fields=['kind', 'center', 'at'], name='pinatt_center_idx')]
        ordering = ['-at']
