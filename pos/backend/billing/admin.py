from django.contrib import admin
from .models import Invoice, InvoiceItem, AdvancePayment, Payment, BillChangeLog


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'total_amount', 'paid_amount', 'status', 'created_at')
    list_filter = ('status', 'center', 'created_at')
    search_fields = ('client__first_name', 'client__phone', 'id')
    readonly_fields = ('created_at', 'updated_at')


@admin.register(InvoiceItem)
class InvoiceItemAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'description', 'unit_price', 'quantity', 'total_price', 'staff')
    search_fields = ('description', 'invoice__id')


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'payment_method', 'amount', 'value_card_id', 'created_at')
    list_filter = ('payment_method',)
    readonly_fields = ('created_at',)


@admin.register(AdvancePayment)
class AdvancePaymentAdmin(admin.ModelAdmin):
    list_display = ('id', 'client', 'amount', 'staff', 'created_at')
    list_filter = ('created_at',)
    readonly_fields = ('created_at',)


@admin.register(BillChangeLog)
class BillChangeLogAdmin(admin.ModelAdmin):
    list_display = ('id', 'invoice', 'action', 'user', 'center', 'created_at')
    list_filter = ('action', 'center', 'created_at')
    readonly_fields = ('created_at',)

