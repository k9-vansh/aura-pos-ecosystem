from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.conf import settings

from pos_backend.money import D, money, ZERO

# Marker historically used in item descriptions to flag a package redemption.
# Business logic no longer depends on it - InvoiceItem.is_redemption does - but
# it is kept here so the display label and the legacy-data migration agree.
REDEEM_MARKER = '\U0001F381 [Redeem]'  # "🎁 [Redeem]"


class InvoiceNumberSequence(models.Model):
    """Per-center, per-financial-year consecutive invoice numbering.

    Indian GST requires a consecutive serial number series per place of
    business. Before this, invoices were identified by primary key and the
    "invoice number" search parsed digits out of a display string.

    Allocation takes a row lock, so numbers are gap-free even under concurrent
    checkouts at the same center.
    """
    center = models.ForeignKey('salon_admin.Center', on_delete=models.CASCADE, related_name='invoice_sequences')
    fiscal_year = models.CharField(max_length=9, help_text='e.g. 2026-27')
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        unique_together = ('center', 'fiscal_year')
        verbose_name = 'Invoice number sequence'

    def __str__(self):
        return f'{self.center_id}/{self.fiscal_year} -> {self.last_number}'

    @staticmethod
    def fiscal_year_for(date_obj):
        """Indian financial year (April-March) as 'YYYY-YY'."""
        year = date_obj.year
        if date_obj.month < 4:
            year -= 1
        return f'{year}-{str(year + 1)[-2:]}'

    @staticmethod
    def series_for(date_obj):
        """The financial year as the number shows it: 2026-27 -> '2627'."""
        year = date_obj.year if date_obj.month >= 4 else date_obj.year - 1
        return f'{str(year)[-2:]}{str(year + 1)[-2:]}'

    @classmethod
    def _next_number(cls, center_id, series):
        """Take the next number for (branch, year) in ONE statement.

        The old get_or_create + select_for_update deadlocked when two tills at
        a branch billed for the first time in a new financial year (or the
        first bills of a new branch): both tried to create the row, and
        MySQL aborted one of them with error 1213. An upsert creates the row or
        bumps it atomically, and leaves it locked until this transaction ends,
        so concurrent bills queue instead of colliding.
        """
        from django.db import connection

        table = cls._meta.db_table
        with connection.cursor() as cursor:
            if connection.vendor == 'mysql':
                cursor.execute(
                    f'INSERT INTO {table} (center_id, fiscal_year, last_number) VALUES (%s, %s, 1) '
                    'ON DUPLICATE KEY UPDATE last_number = last_number + 1',
                    [center_id, series])
            else:
                cursor.execute(
                    f'INSERT INTO {table} (center_id, fiscal_year, last_number) VALUES (%s, %s, 1) '
                    'ON CONFLICT (center_id, fiscal_year) DO UPDATE SET last_number = last_number + 1',
                    [center_id, series])
            cursor.execute(
                f'SELECT last_number FROM {table} WHERE center_id = %s AND fiscal_year = %s',
                [center_id, series])
            return cursor.fetchone()[0]

    @classmethod
    def allocate(cls, center_id, date_obj, prefix=None):
        """Reserve and return the next invoice number for a center.

        Format DL01-2627-00045: branch code, financial year, sequence (owner
        decisions F20 / D12) - at most 16 characters, as GST allows. This is a
        new series, so it starts at 00001; numbers already issued in the old
        C58/2026-27/00037 form are never changed. Its counter is kept under
        the short year ('2627'), apart from the old series' '2026-27'.
        """
        from salon_admin.models import Center

        series = cls.series_for(date_obj)
        with transaction.atomic():
            n = cls._next_number(center_id, series)
        head = prefix
        if not head:
            center = Center.all_objects.filter(pk=center_id).first()
            if center is not None and not center.branch_code:
                center.save()          # assigns its code
            head = center.branch_code if center is not None else f'B{center_id}'
        return f'{head}-{series}-{n:05d}'


class Invoice(models.Model):
    STATUS_DRAFT = 'draft'
    STATUS_PAID = 'paid'
    STATUS_PARTIAL = 'partial'
    STATUS_CANCELLED = 'cancelled'
    STATUS_REFUNDED = 'refunded'

    STATUS_CHOICES = (
        (STATUS_DRAFT, 'Draft'),
        (STATUS_PAID, 'Paid'),
        (STATUS_PARTIAL, 'Partial'),
        (STATUS_CANCELLED, 'Cancelled'),
        (STATUS_REFUNDED, 'Refunded'),
    )

    # ─── What counts as money ─────────────────────────────────────────────
    #
    # Twelve places wrote this filter out by hand and they did not agree. Most
    # used `status__in=['paid', 'partial']`. The marketing usage report used
    # `['paid', 'partial', 'Paid', 'Partial', 'completed', 'Completed',
    #   'draft', 'Draft']` - which counts unfinished bills still sitting in the
    # till as sales, and lists four values that are not valid statuses at all
    # ('completed' has never been one). So the Usage screen and the Finance
    # reports disagreed about the same month, every month, and an abandoned
    # draft inflated Usage permanently.
    #
    # Use these constants. Do not retype the lists.

    #: Settled, wholly or in part. This is revenue.
    REVENUE_STATUSES = frozenset({STATUS_PAID, STATUS_PARTIAL})

    #: Reversed after the fact. Never revenue, and commission must be undone.
    VOID_STATUSES = frozenset({STATUS_CANCELLED, STATUS_REFUNDED})

    #: Started but not settled. Not money yet, and may never be.
    OPEN_STATUSES = frozenset({STATUS_DRAFT})

    # PROTECTED: client FK must not cascade-delete invoices (invoices are financial records)
    # Use PROTECT so Django raises an error if someone tries to delete a client with invoices.
    # To deactivate/anonymise a client, set is_blacklisted=True instead of deleting.
    client = models.ForeignKey('clients.Client', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    staff = models.ForeignKey('staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL)
    appointment = models.ForeignKey('appointments.Appointment', null=True, blank=True, on_delete=models.SET_NULL, related_name='invoices')
    # The membership whose discount this bill used. Nothing recorded this, so
    # the bill that SOLD a membership could be cancelled (and the membership
    # withdrawn) after the client had already enjoyed its discount (#47).
    membership_used = models.ForeignKey('clients.ClientMembership', null=True, blank=True,
                                        on_delete=models.SET_NULL, related_name='bills')

    # The promotion the bill used (one offer per bill, #29). The discount it
    # gives is worked out by the server from this, never taken from the till.
    promotion = models.ForeignKey('marketing.Promotion', null=True, blank=True,
                                  on_delete=models.SET_NULL, related_name='bills')

    # GST serial number, allocated once when the invoice leaves draft.
    invoice_number = models.CharField(max_length=40, unique=True, null=True, blank=True, db_index=True)
    finalized_at = models.DateTimeField(null=True, blank=True)

    # Who raised the bill - the "manager" of the discount reports (#83) - and
    # the till shift it was taken on, so each cashier is accountable.
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='invoices_created')
    shift = models.ForeignKey('finance.Shift', null=True, blank=True,
                              on_delete=models.SET_NULL, related_name='invoices')

    # ─── Snapshots, taken when the bill is completed ──────────────────────
    # A tax invoice must print the same way for ever. The receipt used to read
    # the client's and the branch's CURRENT details, so renaming a branch or
    # correcting a phone number rewrote every old bill.
    DOC_TAX_INVOICE = 'tax_invoice'
    DOC_BILL_OF_SUPPLY = 'bill_of_supply'
    DOC_TYPE_CHOICES = ((DOC_TAX_INVOICE, 'Tax Invoice'), (DOC_BILL_OF_SUPPLY, 'Bill of Supply'))
    document_type = models.CharField(max_length=20, choices=DOC_TYPE_CHOICES, blank=True, default='')
    customer_name = models.CharField(max_length=255, blank=True, default='')
    customer_phone = models.CharField(max_length=30, blank=True, default='')
    customer_gstin = models.CharField(max_length=15, blank=True, default='')
    customer_address = models.TextField(blank=True, default='')
    branch_name = models.CharField(max_length=255, blank=True, default='')
    branch_legal_name = models.CharField(max_length=255, blank=True, default='')
    branch_gstin = models.CharField(max_length=15, blank=True, default='')
    branch_address = models.TextField(blank=True, default='')
    branch_phone = models.CharField(max_length=30, blank=True, default='')
    # "07-Delhi". Salon services are supplied where they are performed and
    # counter sales where they are handed over, so this is the branch's state.
    place_of_supply = models.CharField(max_length=60, blank=True, default='')

    # ─── E-invoice (B2B), filled by the IRP when it is set up (Q4b) ───────
    EINV_NOT_NEEDED = ''
    EINV_PENDING = 'pending'
    EINV_NOT_CONFIGURED = 'not_configured'
    EINV_DONE = 'done'
    EINV_FAILED = 'failed'
    einvoice_status = models.CharField(max_length=20, blank=True, default='')
    einvoice_error = models.TextField(blank=True, default='')
    irn = models.CharField(max_length=64, blank=True, default='')
    irn_ack_no = models.CharField(max_length=30, blank=True, default='')
    irn_ack_date = models.DateTimeField(null=True, blank=True)
    irn_signed_qr = models.TextField(blank=True, default='')

    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    cgst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    sgst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # A POS bill is always intra-state (services where performed, counter goods
    # where handed over), so this stays 0; it is here so the tax total and the
    # e-invoice have one place for it.
    igst = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    rounding = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tip_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True, help_text="SAP Internal Invoice ID (DocEntry)")

    def save(self, *args, **kwargs):
        if D(self.total_amount) < 0:
            raise ValidationError({'total_amount': 'Invoice total amount cannot be negative.'})
        if D(self.paid_amount) < 0:
            raise ValidationError({'paid_amount': 'Paid amount cannot be negative.'})
        if D(self.tip_amount) < 0:
            raise ValidationError({'tip_amount': 'Tip amount cannot be negative.'})
        super().save(*args, **kwargs)

    def assign_invoice_number(self):
        """Allocate a GST serial number if this invoice does not have one yet."""
        if self.invoice_number or not self.center_id:
            return self.invoice_number
        date_obj = (self.created_at.date() if self.created_at
                    else __import__('datetime').date.today())
        self.invoice_number = InvoiceNumberSequence.allocate(self.center_id, date_obj)
        self.save(update_fields=['invoice_number'])
        return self.invoice_number

    @property
    def display_number(self):
        return self.invoice_number or f'DRAFT-{self.pk}'

    @property
    def amount_due(self):
        """What is still owed on the bill.

        `tip_amount` is only ever populated from tip money actually received,
        so it is already collected and must not be added back here.
        """
        due = money(D(self.total_amount) - D(self.paid_amount))
        return ZERO if due < 0 else due

    @property
    def collectable_total(self):
        """Everything the customer hands over: bill plus tip."""
        return money(D(self.total_amount) + D(self.tip_amount))

    def __str__(self):
        return f"Invoice {self.display_number} - {self.client} ({self.total_amount})"

    class Meta:
        indexes = [
            models.Index(fields=['status', 'created_at'], name='inv_status_date_idx'),
            models.Index(fields=['center', 'created_at'], name='inv_center_date_idx'),
            models.Index(fields=['center', 'status', 'created_at'], name='inv_center_status_date_idx'),
            models.Index(fields=['client'], name='inv_client_idx'),
            models.Index(fields=['client', 'status'], name='inv_client_status_idx'),
            # All-branches lists and reports read a date range across every branch,
            # newest first: with only (status, date) and (branch, date) indexes
            # that was a full scan plus a sort of ~200k rows a year. The second
            # index carries the columns the revenue reports add up, so they
            # are read from the index alone.
            models.Index(fields=['created_at'], name='inv_created_idx'),
            models.Index(fields=['status', 'created_at', 'center', 'total_amount', 'cgst', 'sgst', 'discount'],
                         name='inv_rev_cover_idx'),
            # The Usage Report counts a year of bills by customer, centre and month, and tells a bill that
            # used an offer or membership by its two columns. With only the indexes above MySQL walked the
            # whole table by customer and read every row (about 3.5 s a question on 400,000 bills); with
            # these columns in one index each question is answered from the index alone (under 0.5 s).
            models.Index(fields=['status', 'created_at', 'client', 'center', 'promotion', 'membership_used',
                                 'subtotal', 'discount'], name='inv_usage_cover_idx'),
            # Found on a 324-lakh-bill database (4 lakh bills in every one of 81 centres). "Is this customer new?" read every bill of
            # each customer in the period through the customer index and then each bill's row (47-62 s for one centre's month on the
            # Overview); this index answers it from the index alone.
            models.Index(fields=['client', 'status', 'created_at', 'center'], name='inv_client_first_idx'),
            # One centre's dashboard adds up its bills' money columns over a month or a year. With the centre only third in the
            # indexes above, MySQL read all centres' rows of the date range or looked up every bill's row (25 s for one centre's
            # year of 2 lakh bills); with the centre first and the columns in the index, it is one short range read.
            models.Index(fields=['center', 'status', 'created_at', 'client', 'total_amount', 'paid_amount', 'subtotal', 'discount',
                                 'cgst', 'sgst', 'igst', 'rounding'], name='inv_center_cover_idx'),
        ]


class Payment(models.Model):
    # Canonical payment methods. Classification elsewhere matches on the exact
    # normalised string, never a substring - 'cash' is a substring of
    # 'cashback wallet', which used to book wallet redemptions as cash.
    CASH = 'Cash'
    CREDIT_CARD = 'Credit Card'
    DEBIT_CARD = 'Debit Card'
    UPI = 'UPI'
    GOOGLE_PAY = 'Google Pay'
    PHONEPE = 'PhonePe'
    PAYTM = 'Paytm'
    BHARATPE = 'BharatPe'
    CHEQUE = 'Cheque'
    NET_BANKING = 'Net Banking'
    NEARBUY = 'NearBuy'
    ADVANCE = 'Advance'
    VALUE_CARD = 'Value Card'
    CASHBACK_WALLET = 'Cashback Wallet'
    # A partner brand's coupon, entered by its code. Not cash and not a balance the
    # client holds: the brand pays the business later.
    GIFT_CARD = 'Gift Card'
    OTHER = 'Other'

    PAYMENT_METHOD_CHOICES = (
        (CASH, 'Cash'),
        (CREDIT_CARD, 'Credit Card'),
        (DEBIT_CARD, 'Debit Card'),
        (UPI, 'UPI'),
        (GOOGLE_PAY, 'Google Pay'),
        (PHONEPE, 'PhonePe'),
        (PAYTM, 'Paytm'),
        (BHARATPE, 'BharatPe'),
        (CHEQUE, 'Cheque'),
        (NET_BANKING, 'Net Banking'),
        (NEARBUY, 'NearBuy'),
        (ADVANCE, 'Advance'),
        (VALUE_CARD, 'Value Card'),
        (CASHBACK_WALLET, 'Cashback Wallet'),
        (GIFT_CARD, 'Gift Card'),
        (OTHER, 'Other'),
    )

    # Methods that draw down an existing liability rather than bringing in new
    # money. Reports must not add these to "cash collected".
    LIABILITY_METHODS = frozenset({ADVANCE, VALUE_CARD, CASHBACK_WALLET})

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=50, choices=PAYMENT_METHOD_CHOICES, default=CASH)
    # Store value card ID directly when method involves a card, avoids fragile string parsing
    # Indexed so the Usage Report's value-card check finds the few card payments without reading every bill.
    value_card_id = models.IntegerField(null=True, blank=True, db_index=True)
    # Face value taken off the card for this payment. `amount` is what the
    # card settled on the bill (taxable + the card's GST); the face value is
    # larger by the card's benefit. Null on payments made before Phase 6.
    card_face = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # UPI transaction id, card slip number or last four digits, cheque number.
    reference = models.CharField(max_length=64, blank=True, default='')
    # Cash handed over; the change given back is tendered - amount. Only the
    # bill amount is ever recorded as paid (#38).
    tendered = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    # Portion of this payment that is a tip rather than payment against the bill.
    is_tip = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        if D(self.amount) <= 0:
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})
        super().save(*args, **kwargs)

    @property
    def is_liability_drawdown(self):
        return self.payment_method in self.LIABILITY_METHODS

    @property
    def change_given(self):
        if self.tendered is None:
            return ZERO
        change = money(D(self.tendered) - D(self.amount))
        return change if change > 0 else ZERO

    def __str__(self):
        return f"{self.payment_method} - {self.amount}"

    class Meta:
        indexes = [
            models.Index(fields=['invoice'], name='pay_invoice_idx'),
            models.Index(fields=['created_at'], name='pay_created_idx'),
            models.Index(fields=['payment_method', 'created_at'], name='pay_method_date_idx'),
        ]


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='items')

    # Generic relation to Service/Product/Membership/Package/Card
    content_type = models.ForeignKey(ContentType, on_delete=models.CASCADE, null=True, blank=True)
    object_id = models.PositiveIntegerField(null=True, blank=True)
    content_object = GenericForeignKey('content_type', 'object_id')

    description = models.CharField(max_length=255, blank=True)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Portion of `discount` that required manager approval. This is a BREAKDOWN
    # of discount, not an additional deduction - it used to be subtracted a
    # second time, taking the stored line total below what the customer paid.
    manager_discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    quantity = models.PositiveIntegerField(default=1)
    tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)  # From service/product master
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)   # taxable_value x tax_percentage / 100
    # Tax-EXCLUSIVE line total: (unit_price x quantity) - discount.
    # Invoice.subtotal is the sum of these, so sum(items) == subtotal holds.
    # Tax lives in tax_amount; the tax-inclusive figure is `gross_total`.
    total_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    is_complimentary = models.BooleanField(default=False, help_text='Flags 0-value items intentionally given for free or redeemed')
    # Explicit redemption flag. Previously inferred from the emoji marker in
    # `description`, which meant a label change silently stopped depleting
    # customers' packages.
    is_redemption = models.BooleanField(default=False, db_index=True,
                                        help_text='This line was redeemed from a client package, not sold')
    redeemed_from_package = models.ForeignKey(
        'clients.ClientPackage', null=True, blank=True, on_delete=models.SET_NULL,
        related_name='redemption_items',
        help_text='Which client package this redemption was debited from')
    staff = models.ForeignKey('staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL)
    staff_members = models.ManyToManyField('staff.StaffMember', related_name='invoice_items_multi', blank=True)
    staff_allocations = models.JSONField(default=dict, blank=True, help_text='Dictionary mapping staff ID to allocated amount in Rs')

    # What the till chose from the service's recipe ({choices: {group id: {product_code,
    # quantity?}}, extras: [...]}) and what that came to when the bill was completed
    # (services.recipes.resolve): the products the service used, which stock, the
    # ledger and the items listed under the service on the SAP invoice are all written from.
    recipe_choices = models.JSONField(default=dict, blank=True)
    consumables = models.JSONField(default=list, blank=True)

    # Snapshot of the item at the time of sale, so a reprint never changes.
    item_code = models.CharField(max_length=100, blank=True, default='')
    hsn_sac = models.CharField(max_length=20, blank=True, default='')

    # This line's share of the bill-level discount (a coupon or membership on
    # the services, or a manual bill discount). It is taxed away from this line
    # and posted to SAP on it, so SAP's lines and tax follow the bill. Each
    # share is rounded to the paisa on its own, as the till does.
    bill_discount_share = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # ─── Value-card cover (F5) ────────────────────────────────────────────
    # The part of this line the client's value card paid for. `card_face` of
    # face value buys `card_taxable` (face x the card's pre-tax/face ratio),
    # taxed at the card's own rate. The difference is `card_benefit`, which is
    # also inside `discount`, so total_price = unit_price x qty - discount
    # still holds. Any rest of the line is billed normally.
    value_card = models.ForeignKey('clients.ClientValueCard', null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name='covered_items')
    card_face = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_benefit = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_taxable = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    card_tax_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0)

    @property
    def card_settled(self):
        """What the card paid on this line: its taxable part plus its GST."""
        return money(D(self.card_taxable) + D(self.card_tax))

    @property
    def taxable_value(self):
        """Tax-exclusive amount this line is taxed on."""
        return money(self.total_price)

    @property
    def gross_total(self):
        """Tax-inclusive amount for this line."""
        return money(D(self.total_price) + D(self.tax_amount))

    def recalculate(self):
        """Derive total_price and tax_amount from unit_price/quantity/discount.

        Single place the line arithmetic lives, so the serializer, admin and
        any data migration all agree.
        """
        if self.is_redemption:
            self.total_price = ZERO
            self.tax_amount = ZERO
            return
        gross = D(self.unit_price) * D(self.quantity or 1)
        taxable = gross - D(self.discount)
        if taxable < 0:
            taxable = ZERO
        self.total_price = money(taxable)
        # The card-covered part is taxed at the card's rate; the rest, less
        # the line's share of the bill discount, at the line's own rate.
        rest = money(taxable - D(self.card_taxable) - D(self.bill_discount_share))
        if rest < 0:
            rest = ZERO
        self.tax_amount = money(D(self.card_tax) + rest * D(self.tax_percentage) / D('100'))

    def save(self, *args, **kwargs):
        # A manager discount is part of `discount`, never on top of it.
        if D(self.manager_discount) > D(self.discount):
            self.manager_discount = money(self.discount)
        if D(self.total_price) < 0:
            self.total_price = ZERO
        if D(self.tax_amount) < 0:
            self.tax_amount = ZERO
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.description} x {self.quantity} ({self.total_price})"

    class Meta:
        indexes = [
            # The GenericForeignKey pair. `object_id` was in no index at all,
            # so every revenue report that groups by (content_type, object_id)
            # - which is how services, products, memberships and packages are
            # told apart - scanned the whole line-item table. This also serves
            # the `content_type=X, object_id__in=[...]` drill-downs, and
            # subsumes the automatic index on content_type alone.
            models.Index(fields=['content_type', 'object_id'],
                         name='invitem_ct_object_idx'),
            models.Index(fields=['invoice', 'is_redemption'], name='item_inv_redeem_idx'),
            # The Usage Report asks "which bills hold a package session / a value card" across a whole
            # period; these two let the database list those lines without reading every line item.
            models.Index(fields=['is_redemption', 'invoice'], name='item_redeem_invoice_idx'),
            models.Index(fields=['card_face'], name='item_card_face_idx'),
        ]


class AdvancePayment(models.Model):
    """Client advance/wallet ledger. Positive = topped up, negative = spent.

    Append-only: rows are never deleted, because the balance is Sum(amount) and
    deleting a negative row silently gifts the client money. Reversals are new
    positive rows.
    """
    client = models.ForeignKey('clients.Client', on_delete=models.CASCADE, related_name='advances')
    invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='advance_payments'
    )
    staff = models.ForeignKey('staff.StaffMember', null=True, blank=True, on_delete=models.SET_NULL)
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='advance_payments',
                              help_text='Center that took or applied this advance')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    gst_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # True when `amount` is what the client paid, GST included (Rs 21,000 for a
    # Rs 20,000 advance + Rs 1,000 GST), as the SAP team's advance scenarios
    # have it: the whole 21,000 can be spent and the GST comes off the
    # advance-GST accounts when it is. Rows from before this was introduced
    # hold the amount without GST and keep that meaning.
    gst_inclusive = models.BooleanField(default=False)
    payment_method = models.CharField(
        max_length=50,
        choices=Payment.PAYMENT_METHOD_CHOICES,
        default=Payment.CASH
    )
    notes = models.TextField(blank=True, null=True)
    # Set on a row that reverses an earlier one, so cancellations are auditable
    # instead of deleting history.
    reverses = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name='reversed_by')
    created_at = models.DateTimeField(auto_now_add=True)
    
    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return f"Advance {self.id} - {self.client} ({self.amount})"

    class Meta:
        indexes = [
            models.Index(fields=['client', 'created_at'], name='adv_client_date_idx'),
            models.Index(fields=['center', 'created_at'], name='adv_center_date_idx'),
        ]


class BillChangeLog(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='change_logs')
    center = models.ForeignKey('salon_admin.Center', on_delete=models.SET_NULL, null=True, blank=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=50)
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.action} on Invoice {self.invoice_id}'

    class Meta:
        indexes = [
            models.Index(fields=['center', 'created_at'], name='changelog_center_date_idx'),
            models.Index(fields=['invoice'], name='changelog_invoice_idx'),
        ]


class CashbackTransaction(models.Model):
    """Cashback wallet ledger. Positive = earned, negative = spent. Append-only."""
    client = models.ForeignKey('clients.Client', on_delete=models.CASCADE, related_name='cashback_transactions')
    invoice = models.ForeignKey(
        'billing.Invoice',
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='cashback_transactions'
    )
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='cashback_transactions')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    notes = models.TextField(blank=True, null=True)
    reverses = models.ForeignKey('self', null=True, blank=True, on_delete=models.SET_NULL,
                                related_name='reversed_by')
    created_at = models.DateTimeField(auto_now_add=True)

    # ─── Expiry (#35) ─────────────────────────────────────────────────────
    # A credit expires on the date its campaign set (null: never). Spending
    # takes the credits that expire soonest first; `remaining` is what is
    # left of a credit, and `consumed` on a spend row records which credits it
    # took, so a cancelled bill gives back to the same credits.
    KIND_EARNED = 'earned'
    KIND_SPENT = 'spent'
    KIND_EXPIRED = 'expired'
    KIND_REVERSAL = 'reversal'
    kind = models.CharField(max_length=10, blank=True, default='')
    expires_on = models.DateField(null=True, blank=True, db_index=True)
    remaining = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    consumed = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"Cashback {self.id} - {self.client} ({self.amount})"

    class Meta:
        indexes = [
            models.Index(fields=['client', 'created_at'], name='cb_client_date_idx'),
            models.Index(fields=['center', 'created_at'], name='cb_center_date_idx'),
        ]


class InvoiceRefund(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='refunds')
    center = models.ForeignKey('salon_admin.Center', null=True, blank=True, on_delete=models.SET_NULL,
                              related_name='refunds')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    reason = models.CharField(max_length=255, blank=True)
    # Date the refund was issued. Reports keyed off the invoice's created_at put
    # a March refund of a January bill into January.
    refunded_on = models.DateField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Refund {self.id} for Invoice {self.invoice_id} ({self.amount})'

    class Meta:
        indexes = [
            models.Index(fields=['center', 'refunded_on'], name='refund_center_date_idx'),
        ]


class CheckoutIdempotency(models.Model):
    """One row per attempted checkout, so a retry cannot become a second bill.

    The guard this replaces was `cache.add(key, True, timeout=30)` against the
    default cache - which is LocMemCache, and LocMemCache is per PROCESS. With
    more than one worker two identical submissions land on different workers and
    neither sees the other's key, so both complete: two invoices, two GST serial
    numbers, stock deducted twice and the customer's wallet debited twice. It
    also culls a third of its entries once it holds 300, so the key could be
    evicted mid-checkout even with a single worker.

    A unique index is the only thing that is genuinely atomic across processes,
    survives a restart, and does not need Redis to be correct.

    It also does better than rejecting the retry. If the first attempt finished,
    its invoice id is recorded here, so a till that retries after a network
    timeout is handed THE SAME BILL rather than an error - which is what stops
    staff billing the customer a second time by hand.
    """

    #: sha256 of (user, request body). Unique: that is the whole mechanism.
    key = models.CharField(max_length=64, unique=True, db_index=True)

    #: Set once the checkout succeeds, so a retry can be answered with it.
    invoice = models.ForeignKey(
        'billing.Invoice', on_delete=models.CASCADE, null=True, blank=True,
        related_name='idempotency_records')

    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name_plural = 'Checkout idempotency records'
        indexes = [
            # For the periodic tidy-up of rows nobody will retry.
            models.Index(fields=['created_at'], name='checkout_idem_created_idx'),
        ]

    def __str__(self):
        return f'{self.key[:12]}... -> invoice {self.invoice_id or "(in flight)"}'
