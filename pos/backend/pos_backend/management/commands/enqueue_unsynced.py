from django.core.management.base import BaseCommand
from django.db import transaction

from pos_backend import sap_jobs
from clients.models import Client
from inventory.models import Product, Vendor, PurchaseOrder, StockTransfer, StockTransaction
from billing.models import Invoice
from salon_admin.models import Center
from services.models import ServiceMaster


class Command(BaseCommand):
    help = 'Queue every record not yet in SAP for posting (Admin > SAP Sync shows progress)'

    def _enqueue(self, label, object_type, queryset):
        count = 0
        for obj in queryset.iterator():
            with transaction.atomic():
                row = sap_jobs.enqueue(object_type, obj.id, obj=obj)
            count += 1 if row else 0
        self.stdout.write(self.style.SUCCESS(f"Queued {count} unsynced {label}."))

    def handle(self, *args, **options):
        self.stdout.write("Finding unsynced records across all entity types...")

        self._enqueue('Clients',           'client',            Client.objects.filter(sap_sync_status=False))
        self._enqueue('Products',          'product',           Product.objects.filter(sap_sync_status=False))
        self._enqueue('Services',          'service',           ServiceMaster.objects.filter(sap_sync_status=False))
        self._enqueue('Vendors',           'vendor',            Vendor.objects.filter(sap_sync_status=False))
        self._enqueue('Invoices',          'invoice',           Invoice.objects.filter(sap_sync_status=False, status__in=['paid', 'partial']))
        self._enqueue('Purchase Orders',   'purchase_order',    PurchaseOrder.objects.filter(sap_sync_status=False, status='Delivered'))
        self._enqueue('Stock Transfers',   'stock_transfer',    StockTransfer.objects.filter(sap_sync_status=False, status='Completed'))
        self._enqueue('Stock Transactions','stock_transaction',  StockTransaction.objects.filter(sap_sync_status=False))
        self._enqueue('Centers',           'center',            Center.all_objects.filter(sap_sync_status=False))

        self.stdout.write(self.style.SUCCESS(
            "Done. The background worker (run_workers) posts them; already-posted ones are not sent twice."))
