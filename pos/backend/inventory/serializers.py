
"""
Inventory serializers.

Fixes from the audit:
  H7  PurchaseOrderSerializer.create() taxed the PRE-discount base while
      update() taxed the POST-discount subtotal. The same PO therefore had two
      different totals depending on whether it had been edited - Rs 2700 on
      create, Rs 2655 on update for the same inputs. update() was the correct
      one: GST applies to the discounted taxable value.
  H8  `item_data.get('discount_percent', 0)` returned the int 0 when the field
      was absent, and `0 / 100` is a float, so `Decimal * float` raised
      TypeError and PO creation 500'd unless every item spelled out both
      percentages.
  H17 ProductSerializer used fields='__all__', so current_stock was writable.
      A direct PATCH moved stock with no StockTransaction, which desynchronised
      the ledger and made stock_history (derived from the ledger) wrong.
"""

from rest_framework import serializers

from pos_backend.serializer_guards import ServerFieldsReadOnly

from pos_backend.money import D, money, ZERO
from inventory.quantities import qty as qty_, plain as plain_qty
from .models import Vendor, Product, PurchaseOrder, PurchaseOrderItem, ProductLot, StockTransaction, StockTransfer, StockTransferItem


def line_total(quantity, rate, discount_percent, tax_percent):
    """Compute a purchase-order line total.

    One implementation, used by both create() and update(), so a PO's total
    cannot change just because it was edited.

    GST is charged on the taxable value AFTER the trade discount, which is the
    correct treatment and what update() already did.
    """
    qty = D(quantity if quantity is not None else 1)
    rate = D(rate)
    discount_pct = D(discount_percent)
    tax_pct = D(tax_percent)

    gross = qty * rate
    discount_amount = money(gross * discount_pct / D('100'))
    taxable = money(gross - discount_amount)
    if taxable < 0:
        taxable = ZERO
    tax_amount = money(taxable * tax_pct / D('100'))
    return {
        'gross': money(gross),
        'discount_amount': discount_amount,
        'taxable': taxable,
        'tax_amount': tax_amount,
        'total': money(taxable + tax_amount),
    }



MAX_STOCK_QUANTITY = D('1000000')
MAX_ORDER_LINE_TOTAL = D('9999999999.99')           # PurchaseOrderItem.total_price holds 12 digits


def _quantity_field(**kw):
    """Stock quantities go to the screens as numbers, not strings (they are summed
    and compared there), whatever the project-wide decimal setting says."""
    # No stock line is a million units or more; a typo like 99999999999 used to be saved and
    # then overflowed the order total.
    kw.setdefault('max_value', MAX_STOCK_QUANTITY)
    return serializers.DecimalField(max_digits=14, decimal_places=3, coerce_to_string=False, **kw)


class VendorSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    class Meta:
        model = Vendor
        fields = '__all__'

    def validate_phone(self, value):
        import re
        value = (value or '').strip()
        if not value:
            return value or None
        digits = re.sub(r'[\s+\-().]', '', value)
        if not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise serializers.ValidationError('Enter a phone number with 8 to 15 digits.')
        return value

    def validate_pan_number(self, value):
        import re
        value = (value or '').strip().upper()
        if value and not re.fullmatch(r'[A-Z]{5}\d{4}[A-Z]', value):
            raise serializers.ValidationError('A PAN is 10 characters, like ABCDE1234F.')
        return value or None

    def validate_cst_number(self, value):
        """The box is "GST / CST": an old CST number is any short text, but anything that is
        15 characters long is meant as a GSTIN and must be one (it feeds input tax credit)."""
        from clients.identity import gstin_problem
        value = (value or '').strip().upper()
        if len(value) == 15:
            problem = gstin_problem(value)
            if problem:
                raise serializers.ValidationError(problem)
        return value or None


class ProductLotSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProductLot
        fields = '__all__'
        read_only_fields = ('product',)


class StockTransactionSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_brand = serializers.CharField(source='product.brand', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    quantity_change = _quantity_field(read_only=True)

    class Meta:
        model = StockTransaction
        fields = '__all__'
        read_only_fields = ('created_at', 'created_by')

    def get_created_by_name(self, obj):
        return getattr(obj.created_by, 'full_name', '') or ''


class ProductSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    lots = ProductLotSerializer(many=True, read_only=True)
    # Exposed for display; changed only through checkout / audit / billing, each
    # of which writes a matching StockTransaction.
    current_stock = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True,
                                             coerce_to_string=False)
    is_low_stock = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = '__all__'

    def get_is_low_stock(self, obj):
        level = int(obj.reorder_level or 0)
        return level > 0 and qty_(obj.current_stock) <= level

    def validate_price(self, value):
        if value is not None and D(value) < 0:
            raise serializers.ValidationError('Price cannot be negative.')
        return value

    def validate_mrp(self, value):
        if value is not None and D(value) < 0:
            raise serializers.ValidationError('MRP cannot be negative.')
        return value

    def validate_gst_percent(self, value):
        pct = D(value)
        if pct < 0 or pct > 100:
            raise serializers.ValidationError('GST percent must be between 0 and 100.')
        return value

    def validate_pack_size(self, value):
        if value is not None and not (0 < D(value) < 10_000_000):
            raise serializers.ValidationError('What one unit holds must be more than zero.')
        return value

    def validate(self, attrs):
        if 'pack_size' in attrs or 'pack_unit' in attrs:
            from services import units
            size = attrs['pack_size'] if 'pack_size' in attrs else getattr(self.instance, 'pack_size', None)
            unit = units.norm(attrs['pack_unit'] if 'pack_unit' in attrs else getattr(self.instance, 'pack_unit', ''))
            if size and units.family(unit) not in ('volume', 'weight'):
                raise serializers.ValidationError({'pack_unit': 'Pick ml, l, g or kg for what one unit holds.'})
            if not size and unit:
                raise serializers.ValidationError({'pack_size': 'Say how much one unit holds.'})
            attrs['pack_unit'] = unit if size else ''
        # Selling below what it costs is a typo, not a price.
        cost = attrs['price'] if 'price' in attrs else getattr(self.instance, 'price', None)
        mrp = attrs['mrp'] if 'mrp' in attrs else getattr(self.instance, 'mrp', None)
        if (('price' in attrs or 'mrp' in attrs) and cost is not None and mrp is not None
                and D(mrp) > 0 and D(cost) > D(mrp)):
            raise serializers.ValidationError({'mrp': 'The MRP cannot be lower than the purchase price.'})
        # One code, one product, per centre: a transfer finds the destination's
        # product by code, so a duplicate sent stock to whichever came first.
        code = attrs.get('product_code') if 'product_code' in attrs else getattr(self.instance, 'product_code', None)
        center = attrs.get('center') if 'center' in attrs else getattr(self.instance, 'center', None)
        if code and center is not None and ('product_code' in attrs or 'center' in attrs):
            clash = Product.objects.filter(center=center, product_code__iexact=str(code).strip())
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError(
                    {'product_code': f'Another product at this centre already uses the code "{code}".'})
        return super().validate(attrs)


class ProductStockCreateSerializer(ProductSerializer):
    """Only used at creation time, where an opening stock figure is legitimate."""
    current_stock = serializers.DecimalField(max_digits=14, decimal_places=3, required=False,
                                             min_value=0, coerce_to_string=False)


class PurchaseOrderItemSerializer(serializers.ModelSerializer):
    quantity = _quantity_field()
    received_quantity = _quantity_field(required=False)
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_barcode = serializers.CharField(source='product.barcode', read_only=True)
    product_sac = serializers.CharField(source='product.sac_code', read_only=True)

    class Meta:
        model = PurchaseOrderItem
        fields = '__all__'
        # Derived from quantity / rate / discount / tax.
        read_only_fields = ('purchase_order', 'total_price')

    def validate_quantity(self, value):
        if qty_(value) <= 0:
            raise serializers.ValidationError('Quantity must be greater than zero.')
        return value

    def validate_rate(self, value):
        if D(value) < 0:
            raise serializers.ValidationError('Rate cannot be negative.')
        return value

    def validate_discount_percent(self, value):
        pct = D(value)
        if pct < 0 or pct > 100:
            raise serializers.ValidationError('Discount percent must be between 0 and 100.')
        return value

    def validate_tax_percent(self, value):
        pct = D(value)
        if pct < 0 or pct > 100:
            raise serializers.ValidationError('Tax percent must be between 0 and 100.')
        return value

    def validate(self, attrs):
        if all(k in attrs for k in ('quantity', 'rate')):
            total = line_total(attrs['quantity'], attrs['rate'],
                               attrs.get('discount_percent', 0), attrs.get('tax_percent', 0))
            if total['total'] > MAX_ORDER_LINE_TOTAL:
                raise serializers.ValidationError(
                    {'quantity': 'This line comes to too large an amount. Check the quantity and rate.'})
        return attrs


class PurchaseOrderSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    items = PurchaseOrderItemSerializer(many=True, required=False)
    vendor_details = VendorSerializer(source='vendor', read_only=True)
    # Derived from the items.
    total_amount = serializers.DecimalField(max_digits=12, decimal_places=2, read_only=True)
    taxable_amount = serializers.SerializerMethodField()
    tax_amount = serializers.SerializerMethodField()

    STATUS_ORDER = ['Draft', 'Finalized', 'Approved', 'Ordered', 'Shipped', 'Partially Received', 'Delivered']

    class Meta:
        model = PurchaseOrder
        fields = '__all__'

    def get_taxable_amount(self, obj):
        total = ZERO
        for item in obj.items.all():
            total += line_total(item.quantity, item.rate,
                               item.discount_percent, item.tax_percent)['taxable']
        return money(total)

    def get_tax_amount(self, obj):
        total = ZERO
        for item in obj.items.all():
            total += line_total(item.quantity, item.rate,
                               item.discount_percent, item.tax_percent)['tax_amount']
        return money(total)

    def validate(self, attrs):
        # A branch orders from its own vendors only.
        vendor = attrs.get('vendor', getattr(self.instance, 'vendor', None))
        center = attrs.get('center', getattr(self.instance, 'center', None))
        if (('vendor' in attrs or 'center' in attrs) and vendor is not None and center is not None
                and getattr(vendor, 'center_id', None) and vendor.center_id != center.id):
            raise serializers.ValidationError({'vendor': 'That vendor belongs to another center.'})
        return super().validate(attrs)

    def _write_items(self, purchase_order, items_data):
        """Replace the PO's items and return the recomputed total."""
        total = ZERO
        for item_data in items_data:
            product = item_data.get('product')
            if product and product.center_id != purchase_order.center_id:
                raise serializers.ValidationError({
                    'items': f'Product {product.name} does not belong to this '
                             f"purchase order's center."
                })
            amounts = line_total(
                item_data.get('quantity'),
                item_data.get('rate'),
                item_data.get('discount_percent'),
                item_data.get('tax_percent'),
            )
            payload = dict(item_data)
            payload['total_price'] = amounts['total']
            PurchaseOrderItem.objects.create(purchase_order=purchase_order, **payload)
            total += amounts['total']
        return money(total)

    def create(self, validated_data):
        items_data = validated_data.pop('items', [])
        purchase_order = PurchaseOrder.objects.create(**validated_data)
        purchase_order.total_amount = self._write_items(purchase_order, items_data)
        purchase_order.save(update_fields=['total_amount'])
        return purchase_order

    def update(self, instance, validated_data):
        from django.db import transaction

        items_data = validated_data.pop('items', None)
        old_status = instance.status
        new_status = validated_data.get('status', old_status)

        if old_status in self.STATUS_ORDER and new_status in self.STATUS_ORDER:
            if self.STATUS_ORDER.index(new_status) < self.STATUS_ORDER.index(old_status):
                raise serializers.ValidationError(
                    {'status': 'A purchase order cannot be moved back to an earlier status. '
                               'Raise a stock adjustment instead.'}
                )

        if old_status == 'Delivered' and items_data is not None:
            raise serializers.ValidationError(
                {'items': 'Cannot modify the items of a delivered purchase order.'}
            )

        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            if items_data is not None:
                instance.items.all().delete()
                instance.total_amount = self._write_items(instance, items_data)
                instance.save(update_fields=['total_amount'])

            if old_status != 'Delivered' and new_status == 'Delivered':
                self._receive_stock(instance)

        return instance

    @staticmethod
    def _receive_stock(purchase_order):
        """Add received quantities to stock and log the ledger entries."""
        from inventory.models import Product

        for item in purchase_order.items.select_related('product').all():
            quantity = qty_(item.quantity)
            if quantity <= 0:
                continue

            # Calculate the remaining unreceived balance
            remaining_to_receive = quantity - qty_(item.received_quantity)
            if remaining_to_receive <= 0:
                continue
                
            received = remaining_to_receive
            
            # Update item's received_quantity to show it's fully received now
            item.received_quantity = quantity
            item.save(update_fields=['received_quantity'])
            if received <= 0:
                continue
                
            product = Product.objects.select_for_update().filter(pk=item.product_id).first()
            if not product:
                continue

            product.current_stock = qty_(product.current_stock) + received
            product.save(update_fields=['current_stock'])

            from inventory.stock import add_batch
            batch_row = add_batch(product, received, item.batch_number or '', item.expiry_date,
                                  source='receipt')
            StockTransaction.objects.create(
                product=product,
                center=purchase_order.center,
                transaction_type='PO_RECEIPT',
                batch=batch_row,
                quantity_change=received,
                notes=f'Receipt from PO-{purchase_order.id}',
            )

        from pos_backend.sap_utils import create_sap_purchase_delivery_note, run_sap_sync_async
        run_sap_sync_async(create_sap_purchase_delivery_note, purchase_order)


def line_value(quantity, product):
    """(value before GST, GST, total) of `quantity` of a product at its rate."""
    base = money(qty_(quantity) * D(product.price or 0))
    gst = money(base * D(product.gst_percent or 0) / D(100))
    return base, gst, base + gst


def lines_value(lines):
    """Sum of line_value over [(quantity, product)] as three plain numbers."""
    base = gst = total = ZERO
    for quantity, product in lines:
        b, g, t = line_value(quantity, product)
        base, gst, total = base + b, gst + g, total + t
    return {'value': float(base), 'gst': float(gst), 'total': float(total)}


def refuse_repeated_products(items):
    """A product may be on a request or transfer once; to want more of it, raise its quantity."""
    seen = set()
    for item in items or []:
        product = item.get('product')
        pid = getattr(product, 'pk', product)
        if pid in seen:
            name = getattr(product, 'name', 'This product')
            raise serializers.ValidationError(f'{name} is on the list twice. Put the whole quantity on one line.')
        seen.add(pid)
    return items


class StockTransferItemSerializer(serializers.ModelSerializer):
    quantity = _quantity_field()
    received_quantity = serializers.DecimalField(max_digits=14, decimal_places=3, read_only=True,
                                                 coerce_to_string=False)
    scanned_quantity = _quantity_field(required=False, allow_null=True, min_value=0)
    manual_quantity = _quantity_field(required=False, allow_null=True, min_value=0)
    received_scanned_quantity = _quantity_field(required=False, allow_null=True, min_value=0)
    received_manual_quantity = _quantity_field(required=False, allow_null=True, min_value=0)
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_barcode = serializers.CharField(source='product.barcode', read_only=True)
    # What a printed transfer needs about each product.
    product_code = serializers.CharField(source='product.product_code', read_only=True)
    product_hsn = serializers.CharField(source='product.sac_code', read_only=True)
    product_unit = serializers.CharField(source='product.unit', read_only=True)
    product_gst = serializers.DecimalField(source='product.gst_percent', max_digits=5, decimal_places=2,
                                           read_only=True, coerce_to_string=False)
    product_price = serializers.DecimalField(source='product.price', max_digits=10, decimal_places=2,
                                             read_only=True, coerce_to_string=False)
    sent_check = serializers.SerializerMethodField()
    received_check = serializers.SerializerMethodField()

    class Meta:
        model = StockTransferItem
        fields = '__all__'
        read_only_fields = ('transfer',)

    @staticmethod
    def _check(quantity, scanned, manual):
        """'none' (nothing counted yet), 'ok' (scanned + typed = the quantity), 'short' or 'over'."""
        if scanned is None and manual is None:
            return 'none'
        counted = qty_(scanned or 0) + qty_(manual or 0)
        want = qty_(quantity)
        return 'ok' if counted == want else ('short' if counted < want else 'over')

    def get_sent_check(self, obj):
        return self._check(obj.quantity, obj.scanned_quantity, obj.manual_quantity)

    def get_received_check(self, obj):
        return self._check(obj.quantity, obj.received_scanned_quantity, obj.received_manual_quantity)

    def validate_quantity(self, value):
        if int(value or 0) <= 0:
            raise serializers.ValidationError('Quantity must be greater than zero.')
        return value


class StockTransferSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    items = StockTransferItemSerializer(many=True, required=False)
    from_center_name = serializers.CharField(source='from_center.center_name', read_only=True)
    to_center_name = serializers.CharField(source='to_center.center_name', read_only=True)
    # For the printed transfer: who sends, who receives, and whether it crosses a state line (IGST).
    from_info = serializers.SerializerMethodField()
    to_info = serializers.SerializerMethodField()
    transfer_kind = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    dispatched_by_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()

    @staticmethod
    def _info(center):
        from .places import center_state
        return {'name': center.display_name or center.center_name, 'legal_name': center.center_name,
                'address': center.address or '', 'pin_code': center.pin_code or '', 'gstin': center.gst_number or '',
                'phone': center.phone or '', 'state': center_state(center)}

    def get_from_info(self, obj):
        return self._info(obj.from_center)

    def get_to_info(self, obj):
        return self._info(obj.to_center)

    def get_transfer_kind(self, obj):
        from .places import transfer_kind
        return transfer_kind(obj.from_center, obj.to_center)

    @staticmethod
    def _who(user):
        return (user.full_name or user.email) if user else ''

    def get_created_by_name(self, obj):
        return self._who(obj.created_by)

    def get_dispatched_by_name(self, obj):
        return self._who(obj.dispatched_by)

    def get_received_by_name(self, obj):
        return self._who(obj.received_by)

    bill_url = serializers.SerializerMethodField()
    amounts = serializers.SerializerMethodField()

    def get_bill_url(self, obj):
        return obj.bill_file.url if obj.bill_file else ''

    def get_amounts(self, obj):
        """What the transfer is worth at the products' rates (sent), and what was accepted."""
        items = list(obj.items.all())
        sent = lines_value([(i.quantity, i.product) for i in items])
        sent['accepted_total'] = lines_value(
            [(i.quantity if i.received_quantity is None else i.received_quantity, i.product) for i in items])['total']
        return sent

    def validate_items(self, items):
        return refuse_repeated_products(items)

    class Meta:
        model = StockTransfer
        fields = '__all__'
        # The receipt (bill, note, difference) is written only by the receive action; the SAP invoices and the tax
        # figures are SAP's, written only by the posting.
        read_only_fields = ('bill_file', 'received_note', 'has_difference', 'supply_kind', 'sap_ar_doc_entry',
                            'sap_ar_doc_num', 'sap_ap_doc_entry', 'sap_ap_doc_num', 'sap_taxable_value',
                            'sap_tax_amount', 'sap_invoice_total', 'sap_branch_message')

    def validate(self, attrs):
        source = attrs.get('from_center', getattr(self.instance, 'from_center', None))
        target = attrs.get('to_center', getattr(self.instance, 'to_center', None))
        if source is not None and target is not None and source.pk == target.pk:
            raise serializers.ValidationError(
                {'to_center': 'A transfer must go to a different centre than it leaves from.'})
        request_obj = attrs.get('stock_request')
        if request_obj is not None and (self.instance is None or request_obj != self.instance.stock_request):
            if request_obj.status != 'Approved':
                raise serializers.ValidationError(
                    {'stock_request': f'REQ-{request_obj.pk} is {request_obj.status.lower()}; only an approved request can be sent.'})
            if target is not None and target.pk != request_obj.requesting_center_id:
                raise serializers.ValidationError(
                    {'to_center': f'REQ-{request_obj.pk} was asked for {request_obj.requesting_center}; send it there.'})
            if request_obj.transfers.exists():
                raise serializers.ValidationError(
                    {'stock_request': f'REQ-{request_obj.pk} already has a transfer.'})
        return super().validate(attrs)

    def _write_items(self, transfer, items_data):
        for item_data in items_data:
            product = item_data.get('product')
            if product and product.center_id != transfer.from_center_id:
                raise serializers.ValidationError({
                    'items': f'Product {product.name} does not belong to the source center.'
                })
            payload = dict(item_data)
            StockTransferItem.objects.create(transfer=transfer, **payload)

    def create(self, validated_data):
        from django.db import transaction

        items_data = validated_data.pop('items', [])
        if not items_data:
            raise serializers.ValidationError({'items': 'Add at least one product to transfer.'})
        # One transaction: a refused item must not leave an empty draft behind.
        with transaction.atomic():
            transfer = StockTransfer.objects.create(**validated_data)
            self._write_items(transfer, items_data)
        return transfer

    def update(self, instance, validated_data):
        """Draft -> In Transit (dispatch) -> Completed (receive).

        Dispatch takes the stock out of the source branch (earliest-expiring
        batches first, never below zero); receiving puts it into the
        destination. Going straight from Draft to Completed does both, as
        before. A branch's stock changes only when its own side of the move
        happens, so goods on the road are in neither branch.
        """
        from django.db import transaction

        items_data = validated_data.pop('items', None)
        old_status = instance.status
        new_status = validated_data.get('status', old_status)

        if old_status == 'Completed':
            raise serializers.ValidationError({'status': 'Cannot modify a completed stock transfer.'})
        if old_status == 'In Transit' and new_status == 'Draft':
            raise serializers.ValidationError({'status': 'Stock already left the source branch; receive it.'})
        if old_status == 'In Transit' and items_data is not None:
            raise serializers.ValidationError({'items': 'A dispatched transfer cannot change its items.'})

        dispatching = old_status != 'In Transit' and new_status in ('In Transit', 'Completed')
        receiving = new_status == 'Completed'
        request = self.context.get('request')
        user = getattr(request, 'user', None)

        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            if items_data is not None:
                instance.items.all().delete()
                self._write_items(instance, items_data)

            if dispatching:
                self._dispatch(instance, user)
            if receiving:
                self._receive(instance, user)
                # The request this transfer was sent for is now fulfilled.
                if instance.stock_request_id:
                    StockRequest.objects.filter(pk=instance.stock_request_id, status='Approved').update(
                        status='Fulfilled')

        return instance

    def _dispatch(self, transfer, user):
        """Stock leaves the source branch, taken from the earliest-expiring batches first."""
        from django.utils import timezone
        from inventory.models import Product, StockTransaction
        from inventory.stock import take_fefo

        actor = user if getattr(user, 'is_authenticated', False) else None
        for item in transfer.items.select_related('product'):
            product = Product.objects.select_for_update().get(pk=item.product_id)
            quantity = qty_(item.quantity)
            available = qty_(product.current_stock)
            if quantity > available:
                raise serializers.ValidationError({
                    'items': f'{product.name}: cannot transfer {plain_qty(quantity)} - only '
                             f'{plain_qty(available)} in stock at {transfer.from_center}.'})
            product.current_stock = available - quantity
            product.save(update_fields=['current_stock'])

            carried = []
            for batch, taken in take_fefo(product, quantity):
                StockTransaction.objects.create(
                    product=product, center=transfer.from_center, transaction_type='TRANSFER_OUT',
                    quantity_change=-taken, batch=batch, created_by=actor,
                    notes=f'Stock Transfer Out to {transfer.to_center.center_name} (ST-{transfer.pk})')
                carried.append({
                    'batch_number': batch.batch_number if batch else '',
                    'expiry_date': batch.expiry_date.isoformat() if batch and batch.expiry_date else None,
                    'quantity': plain_qty(taken)})
            item.batches = carried
            item.save(update_fields=['batches'])
        transfer.dispatched_at = timezone.now()
        transfer.dispatched_by = actor
        transfer.save(update_fields=['dispatched_at', 'dispatched_by'])

        # Goods leaving for another branch (another GSTIN) go with a tax invoice: SAP gets the A/R invoice now, and
        # works out the IGST. For two centres of one branch this queued job finds nothing to do.
        from pos_backend.sap_branch_transfer import create_sap_branch_sale
        from pos_backend.sap_utils import run_sap_sync_async
        run_sap_sync_async(create_sap_branch_sale, transfer)

    def _receive(self, transfer, user):
        """Stock arrives at the destination, in the batches it left in."""
        import datetime
        from django.utils import timezone
        from inventory.models import Product, StockTransaction
        from inventory.stock import add_batch

        actor = user if getattr(user, 'is_authenticated', False) else None
        for item in transfer.items.select_related('product'):
            src_product = item.product
            matches = Product.objects.filter(center=transfer.to_center)
            # A blank code would match any uncoded product there, so fall back to the name.
            matches = (matches.filter(product_code=src_product.product_code) if src_product.product_code
                       else matches.filter(name=src_product.name))
            tgt_product = matches.first()
            if not tgt_product:
                # The destination has never stocked it: create its row there.
                tgt_product = Product.objects.get(id=src_product.id)
                tgt_product.pk = None
                tgt_product.center = transfer.to_center
                tgt_product.current_stock = 0
                tgt_product.save()
                from pos_backend.sap_utils import create_sap_item, run_sap_sync_async
                run_sap_sync_async(create_sap_item, tgt_product)
            tgt_product = Product.objects.select_for_update().get(pk=tgt_product.pk)
            # What the branch accepted: the whole line unless it signed in less.
            quantity = qty_(item.quantity if item.received_quantity is None else item.received_quantity)
            if quantity <= 0:
                continue
            tgt_product.current_stock = qty_(tgt_product.current_stock) + quantity
            tgt_product.save(update_fields=['current_stock'])

            lines = item.batches or [{'batch_number': '', 'expiry_date': None,
                                      'quantity': plain_qty(quantity)}]
            left = quantity
            for line in lines:
                expiry = datetime.date.fromisoformat(line['expiry_date']) if line.get('expiry_date') else None
                part = min(qty_(line['quantity']), left)
                left -= part
                if part <= 0:
                    continue
                batch = add_batch(tgt_product, part, line.get('batch_number') or '', expiry,
                                  source='transfer_in')
                StockTransaction.objects.create(
                    product=tgt_product, center=transfer.to_center, transaction_type='TRANSFER_IN',
                    quantity_change=part, batch=batch, created_by=actor,
                    notes=f'Stock Transfer In from {transfer.from_center.center_name} (ST-{transfer.pk})')
        transfer.received_at = timezone.now()
        transfer.received_by = actor
        transfer.save(update_fields=['received_at', 'received_by'])

        from pos_backend.sap_utils import create_sap_stock_transfer, run_sap_sync_async
        run_sap_sync_async(create_sap_stock_transfer, transfer)


class TransferBillSerializer(serializers.ModelSerializer):
    """One received transfer as a row of the Transfer Bills screen."""
    from_name = serializers.SerializerMethodField()
    to_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()
    dispatched_by_name = serializers.SerializerMethodField()
    bill_url = serializers.SerializerMethodField()
    item_count = serializers.SerializerMethodField()
    amounts = serializers.SerializerMethodField()
    transfer_kind = serializers.SerializerMethodField()

    class Meta:
        model = StockTransfer
        fields = ('id', 'from_name', 'to_name', 'transfer_kind', 'dispatched_at', 'dispatched_by_name', 'received_at',
                  'received_by_name', 'bill_url', 'has_difference', 'received_note', 'item_count', 'amounts',
                  'stock_request', 'sap_sync_status')

    @staticmethod
    def _name(center):
        return center.display_name or center.center_name

    def get_from_name(self, obj):
        return self._name(obj.from_center)

    def get_to_name(self, obj):
        return self._name(obj.to_center)

    def get_received_by_name(self, obj):
        return (obj.received_by.full_name or obj.received_by.email) if obj.received_by_id else ''

    def get_dispatched_by_name(self, obj):
        return (obj.dispatched_by.full_name or obj.dispatched_by.email) if obj.dispatched_by_id else ''

    def get_bill_url(self, obj):
        return obj.bill_file.url if obj.bill_file else ''

    def get_item_count(self, obj):
        return len(obj.items.all())

    def get_transfer_kind(self, obj):
        from .places import transfer_kind
        return transfer_kind(obj.from_center, obj.to_center)

    def get_amounts(self, obj):
        items = list(obj.items.all())
        sent = lines_value([(i.quantity, i.product) for i in items])
        sent['accepted_total'] = lines_value(
            [(i.quantity if i.received_quantity is None else i.received_quantity, i.product) for i in items])['total']
        return sent


from .models import StockRequest, StockRequestItem

class StockRequestItemSerializer(serializers.ModelSerializer):
    quantity = _quantity_field()
    product_name = serializers.CharField(source='product.name', read_only=True)
    product_code = serializers.CharField(source='product.product_code', read_only=True)
    product_price = serializers.DecimalField(source='product.price', max_digits=10, decimal_places=2,
                                             read_only=True, coerce_to_string=False)
    product_gst = serializers.DecimalField(source='product.gst_percent', max_digits=5, decimal_places=2,
                                           read_only=True, coerce_to_string=False)

    class Meta:
        model = StockRequestItem
        fields = ['id', 'product', 'product_name', 'product_code', 'product_price', 'product_gst', 'quantity']

    def validate_quantity(self, value):
        if D(value) <= 0:
            raise serializers.ValidationError('Quantity must be greater than zero.')
        return value


UNCATEGORISED = 'Uncategorised'


def product_category(product):
    """A product's category as the screens show it; a blank one is 'Uncategorised'."""
    return (product.category or '').strip() or UNCATEGORISED


def request_group(category):
    """A request is either Retail (goods sold to clients) or Technical (everything else the salon uses)."""
    return 'Retail' if (category or '').strip().lower() == 'retail' else 'Technical'


def product_matches_stock_type(product, stock_type):
    """A request is for Retail or Technical stock; its products must all be in it. A request made before
    that split, for some other category, still means exactly that category."""
    wanted = (stock_type or '').strip().lower()
    if wanted in ('retail', 'technical'):
        return request_group(product.category).lower() == wanted
    return product_category(product).lower() == wanted


class StockRequestSerializer(serializers.ModelSerializer):
    items = StockRequestItemSerializer(many=True, required=False)
    requesting_center_name = serializers.CharField(source='requesting_center.center_name', read_only=True)
    created_by_name = serializers.SerializerMethodField()
    decided_by_name = serializers.SerializerMethodField()
    transfer_id = serializers.SerializerMethodField()
    transfer_status = serializers.SerializerMethodField()
    amounts = serializers.SerializerMethodField()

    def get_amounts(self, obj):
        return lines_value([(i.quantity, i.product) for i in obj.items.all()])

    def validate_items(self, items):
        return refuse_repeated_products(items)

    class Meta:
        model = StockRequest
        fields = '__all__'
        # Approving, rejecting, cancelling and fulfilling go through their own
        # actions, so a status can never be typed in.
        read_only_fields = ['status', 'decided_by', 'decided_at', 'decision_note', 'created_by']

    def get_created_by_name(self, obj):
        return (obj.created_by.full_name or obj.created_by.email) if obj.created_by_id else ''

    def get_decided_by_name(self, obj):
        return (obj.decided_by.full_name or obj.decided_by.email) if obj.decided_by_id else ''

    def _transfer(self, obj):
        # Newest transfer first; prefetched with the request list.
        transfers = sorted(obj.transfers.all(), key=lambda t: -t.pk)
        return transfers[0] if transfers else None

    def get_transfer_id(self, obj):
        t = self._transfer(obj)
        return t.pk if t else None

    def get_transfer_status(self, obj):
        t = self._transfer(obj)
        return t.status if t else None

    def _write_items(self, request_obj, items_data):
        for item_data in items_data:
            product = item_data.get('product')
            if product and product.center_id != request_obj.requesting_center_id:
                raise serializers.ValidationError({
                    'items': f'Product {product.name} must belong to the requesting center.'
                })
            if product and not product.is_active:
                raise serializers.ValidationError({
                    'items': f'{product.name} is switched off in the item master, so it cannot be requested.'
                })
            if product and not product_matches_stock_type(product, request_obj.stock_type):
                raise serializers.ValidationError({
                    'items': f'{product.name} is not a {request_obj.stock_type} product.'
                })
            payload = dict(item_data)
            StockRequestItem.objects.create(stock_request=request_obj, **payload)

    def create(self, validated_data):
        from django.db import transaction

        items_data = validated_data.pop('items', [])
        if not (validated_data.get('stock_type') or '').strip():
            raise serializers.ValidationError({'stock_type': 'Choose the category of stock you are asking for.'})
        validated_data['stock_type'] = validated_data['stock_type'].strip()
        if not items_data:
            raise serializers.ValidationError({'items': 'Add at least one product to request.'})
        # One transaction: a refused item must not leave an empty request behind.
        with transaction.atomic():
            request_obj = StockRequest.objects.create(**validated_data)
            self._write_items(request_obj, items_data)
        return request_obj

    def update(self, instance, validated_data):
        from django.db import transaction

        items_data = validated_data.pop('items', None)
        if instance.status != 'Pending':
            raise serializers.ValidationError(
                {'status': f'A {instance.status.lower()} stock request can no longer be edited.'})
        validated_data.pop('stock_type', None)       # the kind is fixed once asked

        with transaction.atomic():
            for attr, value in validated_data.items():
                setattr(instance, attr, value)
            instance.save()

            if items_data is not None:
                instance.items.all().delete()
                self._write_items(instance, items_data)

        return instance
