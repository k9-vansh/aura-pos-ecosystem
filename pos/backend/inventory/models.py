from django.db import models
from salon_admin.models import Center

class Vendor(models.Model):
    name = models.CharField(max_length=255, verbose_name="Vendor Name")
    short_name = models.CharField(max_length=100, blank=True, null=True, verbose_name="Short Name")
    email = models.EmailField(blank=True, null=True, verbose_name="Email Address")
    phone = models.CharField(max_length=50, blank=True, null=True, verbose_name="Phone Number")
    cst_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="CST Number")
    pan_number = models.CharField(max_length=100, blank=True, null=True, verbose_name="PAN Number")
    address = models.TextField(blank=True, null=True)
    city = models.CharField(max_length=100, blank=True, null=True)
    state = models.CharField(max_length=100, blank=True, null=True)
    pin_code = models.CharField(max_length=20, blank=True, null=True)
    vendor_code = models.CharField(max_length=100, blank=True, null=True, verbose_name="Vendor Code")
    mapped_products = models.ManyToManyField('Product', blank=True, related_name='mapped_vendors')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='vendors')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sap_sync_status = models.BooleanField(null=True, blank=True)
    sap_sync_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['center'], name='vendor_center_idx'),
            models.Index(fields=['created_at'], name='vendor_created_idx'),
        ]


class Product(models.Model):
    product_id_str = models.CharField(max_length=100, blank=True, null=True, verbose_name="Product ID")
    product_code = models.CharField(max_length=100, blank=True, null=True, verbose_name="Product Code")
    name = models.CharField(max_length=255)
    brand = models.CharField(max_length=100, blank=True, null=True)
    category = models.CharField(max_length=100, blank=True, null=True)
    sub_category = models.CharField(max_length=100, blank=True, null=True)
    vendor_name = models.CharField(max_length=255, blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="Purchase Price (excl. tax)", null=True, blank=True)
    mrp = models.DecimalField(max_digits=10, decimal_places=2, verbose_name="MRP / Selling Price (incl. tax)", null=True, blank=True)
    gst_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name="GST %")
    barcode = models.CharField(max_length=100, blank=True, null=True)
    sac_code = models.CharField(max_length=100, blank=True, null=True, verbose_name="SAC Code")
    is_active = models.BooleanField(default=True)
    reorder_level = models.IntegerField(default=0)
    reorder_quantity = models.IntegerField(default=0)
    # Quantities are decimal (X8): a consumable measured in ml or g used to be
    # truncated to whole units. `unit` says what the number counts.
    current_stock = models.DecimalField(max_digits=14, decimal_places=3, default=0, verbose_name="Current Stock")
    unit = models.CharField(max_length=20, default='pcs', help_text='Unit of measure: pcs, ml, g, ...')
    # What one stock unit holds ("1 pcs = 100 ml"), so a recipe can use it in ml or g.
    pack_size = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    pack_unit = models.CharField(max_length=20, blank=True, default='')
    center =models.ForeignKey(Center, on_delete=models.CASCADE, related_name='products')
    incentive = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, verbose_name="Incentive %")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)

    def __str__(self):
        return self.name

    class Meta:
        ordering = ['name']
        indexes = [
            models.Index(fields=['center', 'is_active'], name='product_center_active_idx'),
            models.Index(fields=['category'], name='product_category_idx'),
            models.Index(fields=['product_code'], name='product_code_idx'),
            models.Index(fields=['created_at'], name='product_created_idx'),
        ]


class ProductLot(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='lots')
    lot_number = models.CharField(max_length=100)
    net_price = models.DecimalField(max_digits=10, decimal_places=2)
    mrp = models.DecimalField(max_digits=10, decimal_places=2)
    expiry_date = models.DateField(blank=True, null=True)
    manufacturing_date = models.DateField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.product.name} - {self.lot_number}"

    class Meta:
        indexes = [
            models.Index(fields=['product'], name='lot_product_idx'),
            models.Index(fields=['created_at'], name='lot_created_idx'),
        ]


class PurchaseOrder(models.Model):
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('Finalized', 'Finalized'),
        ('Approved', 'Approved'),
        ('Ordered', 'Ordered'),
        ('Shipped', 'Shipped'),
        ('Partially Received', 'Partially Received'),
        ('Delivered', 'Delivered'),
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.CASCADE, related_name='purchase_orders')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='purchase_orders')
    invoice_number = models.CharField(max_length=100, blank=True, null=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Draft')
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_po_doc_entry = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"PO-{self.id} ({self.vendor.name})"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'status'], name='po_center_status_idx'),
            models.Index(fields=['status', 'created_at'], name='po_status_date_idx'),
            models.Index(fields=['created_at'], name='po_created_idx'),
            # One branch's orders, newest first (the Purchase Orders list).
            models.Index(fields=['center', 'created_at'], name='po_center_date_idx'),
        ]


class PurchaseOrderItem(models.Model):
    purchase_order = models.ForeignKey(PurchaseOrder, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    received_quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    batch_number = models.CharField(max_length=100, blank=True, null=True)
    mfg_date = models.DateField(blank=True, null=True)
    expiry_date = models.DateField(blank=True, null=True)
    rate = models.DecimalField(max_digits=10, decimal_places=2)
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    tax_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.product.name} - PO-{self.purchase_order.id}"


class StockTransaction(models.Model):
    TRANSACTION_TYPES = (
        ('CHECKOUT', 'Checkout for salon use'),
        ('AUDIT', 'Stock Audit Adjustment'),
        ('PO_RECEIPT', 'Purchase Order Receipt'),
        ('SALE', 'Retail Sale'),
        ('REFUND', 'Refunded Sale'),
        ('TRANSFER_OUT', 'Stock transfer out'),
        ('TRANSFER_IN', 'Stock transfer in'),
        ('BOM_CONSUMPTION', 'Service consumables'),
        ('SERVICE_USE', 'Items used by a service'),
        ('SAP_SYNC', 'Changed in SAP'),
    )
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='transactions')
    center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='stock_transactions')
    transaction_type = models.CharField(max_length=50, choices=TRANSACTION_TYPES)
    quantity_change = models.DecimalField(max_digits=14, decimal_places=3)
    lot = models.ForeignKey(ProductLot, on_delete=models.SET_NULL, null=True, blank=True, related_name='stock_transactions')
    batch = models.ForeignKey('StockBatch', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True)
    # The bill a sale or consumption belongs to, so a cancelled bill gives each
    # batch back exactly what it took (the notes text cannot be trusted to find it).
    invoice = models.ForeignKey('billing.Invoice', on_delete=models.SET_NULL, null=True, blank=True,
                                related_name='stock_transactions')

    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True)

    def __str__(self):
        return f"{self.product.name} ({self.quantity_change}) - {self.transaction_type}"

    class Meta:
        indexes = [
            models.Index(fields=['center', 'created_at'], name='stock_center_date_idx'),
            models.Index(fields=['product', 'created_at'], name='stock_product_date_idx'),
            models.Index(fields=['transaction_type'], name='stock_type_idx'),
            # The ledger across all branches, newest first, and "what moved after this day" (stock history):
            # without these two the database reads and sorts the whole ledger for each of them.
            models.Index(fields=['created_at'], name='stock_created_idx'),
            models.Index(fields=['transaction_type', 'created_at'], name='stock_type_date_idx'),
        ]

class StockTransfer(models.Model):
    STATUS_CHOICES = (
        ('Draft', 'Draft'),
        ('In Transit', 'In Transit'),
        ('Completed', 'Completed'),
    )
    from_center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='stock_transfers_out')
    to_center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='stock_transfers_in')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Draft')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    # The approved stock request this transfer sends; receiving it fulfils the request.
    stock_request = models.ForeignKey('inventory.StockRequest', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='transfers')

    # The receiving branch's sign-off: the bill / challan that came with the goods, and a note that is
    # required when what arrived differs from what was sent.
    bill_file = models.FileField(upload_to='transfer_bills/', null=True, blank=True)
    received_note = models.CharField(max_length=300, blank=True, default='')
    has_difference = models.BooleanField(default=False)

    sap_sync_status = models.BooleanField(default=False)
    sap_sync_message = models.TextField(blank=True, null=True)
    sap_doc_entry = models.IntegerField(null=True, blank=True)

    # Stock moving between two business places (two GSTINs) is a supply: SAP gets an A/R invoice from the sending
    # branch when the goods leave and an A/P invoice at the receiving branch when they arrive, with IGST worked out
    # by SAP. Same GSTIN = a plain Inventory Transfer (`sap_doc_entry` above). The figures below are SAP's own.
    supply_kind = models.CharField(max_length=12, blank=True, default='')            # plain / igst / cgst_sgst
    sap_ar_doc_entry = models.IntegerField(null=True, blank=True)
    sap_ar_doc_num = models.IntegerField(null=True, blank=True)
    sap_ap_doc_entry = models.IntegerField(null=True, blank=True)
    sap_ap_doc_num = models.IntegerField(null=True, blank=True)
    sap_taxable_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sap_tax_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sap_invoice_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    sap_branch_message = models.TextField(blank=True, default='')

    def __str__(self):
        return f"ST-{self.id} ({self.from_center} -> {self.to_center})"

    class Meta:
        indexes = [
            # A branch's transfers out and in, newest first; and the Transfer Bills screen (received, by date).
            models.Index(fields=['from_center', 'created_at'], name='transfer_from_date_idx'),
            models.Index(fields=['to_center', 'created_at'], name='transfer_to_date_idx'),
            models.Index(fields=['status', 'received_at'], name='transfer_status_recv_idx'),
        ]

class StockTransferItem(models.Model):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)
    # Which batches left the source at dispatch, so they arrive as the same batches.
    batches = models.JSONField(default=list, blank=True)
    # Counted by hand against the goods: how many were scanned with the barcode reader and how many were
    # typed in, when packing (sent) and when unpacking (received). Together they should equal `quantity`.
    scanned_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    manual_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    received_scanned_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    received_manual_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)
    # What the receiving branch accepted (blank = all of `quantity`); never more than was sent.
    received_quantity = models.DecimalField(max_digits=14, decimal_places=3, null=True, blank=True)

    def __str__(self):
        return f"{self.product.name} (Qty: {self.quantity})"

class StockRequest(models.Model):
    # Pending = waiting for an approver; Approved = the stock person may now
    # send it by a Stock Transfer; Fulfilled = that transfer was received.
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
        ('Fulfilled', 'Fulfilled'),
        ('Cancelled', 'Cancelled'),
    )
    requesting_center = models.ForeignKey(Center, on_delete=models.CASCADE, related_name='stock_requests')
    # The product category this request is for (Retail, Technical, Consumable,
    # or any category added later): stock of different categories is taxed and
    # handled differently, so a request offers and accepts one category only.
    stock_type = models.CharField(max_length=100, default='')
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='Pending')
    decided_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True, default='')
    notes = models.TextField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey('accounts.CustomUser', on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"REQ-{self.id} ({self.requesting_center})"

    class Meta:
        indexes = [
            models.Index(fields=['requesting_center', 'created_at'], name='request_center_date_idx'),
        ]

class StockRequestItem(models.Model):
    stock_request = models.ForeignKey(StockRequest, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=1)

    def __str__(self):
        return f"{self.product.name} (Qty: {self.quantity})"


class StockBatch(models.Model):
    """What is left of one received batch of a product (F18, #68).

    A sale, a checkout or a transfer out takes from the batch that expires
    first, automatically. Stock that has no batch (everything before batches
    existed, goods received without a batch number) is the *unbatched*
    remainder: `Product.current_stock` minus the sum of its batches. Batches are
    always used up first, so legacy stock is never silently skipped.
    """
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='batches')
    batch_number = models.CharField(max_length=100, blank=True, default='')
    expiry_date = models.DateField(null=True, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3, default=0)
    received_on = models.DateTimeField(auto_now_add=True)
    source = models.CharField(max_length=20, default='receipt')

    class Meta:
        ordering = ['expiry_date', 'received_on', 'id']
        indexes = [models.Index(fields=['product', 'expiry_date'], name='batch_product_expiry_idx')]

    def __str__(self):
        return f'{self.product} batch {self.batch_number or self.pk}'
