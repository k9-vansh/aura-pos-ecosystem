from django.core.management.base import BaseCommand
from finance.views import _get_filtered_invoices
from billing.models import Invoice, InvoiceItem
from django.db.models.functions import ExtractYear, ExtractMonth
from django.db.models import Sum, Count, Q
from django.contrib.contenttypes.models import ContentType
from services.models import ServiceMaster
from inventory.models import Product
from marketing.models import Membership, Package, ValueCard

class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        invoices = Invoice.objects.all()
        print(f"Total invoices: {invoices.count()}")
        
        ct_map = ContentType.objects.get_for_models(ServiceMaster, Product, Membership, Package, ValueCard)
        service_ct = ct_map[ServiceMaster]
        product_ct = ct_map[Product]
        membership_ct = ct_map[Membership]
        package_ct = ct_map[Package]
        valuecard_ct = ct_map[ValueCard]

        print("Testing Monthly Aggregation...")
        try:
            monthly = list(
                invoices
                .annotate(year=ExtractYear('created_at'), month_num=ExtractMonth('created_at'))
                .values('year', 'month_num')
                .annotate(
                    total_amount=Sum('total_amount'),
                    total_cgst=Sum('cgst'),
                    total_sgst=Sum('sgst'),
                    total_discount=Sum('discount'),
                    invoice_count=Count('id'),
                )
                .order_by('-year', '-month_num')
            )
            print(f"Monthly output size: {len(monthly)}")

            print("Testing item monthly...")
            item_monthly = list(
                InvoiceItem.objects.filter(invoice__in=invoices)
                .annotate(year=ExtractYear('invoice__created_at'), month_num=ExtractMonth('invoice__created_at'))
                .values('year', 'month_num')
                .annotate(
                    services=Sum('total_price', filter=Q(content_type=service_ct)),
                    products=Sum('total_price', filter=Q(content_type=product_ct)),
                    memberships=Sum('total_price', filter=Q(content_type=membership_ct)),
                    packages=Sum('total_price', filter=Q(content_type=package_ct)),
                    value_cards=Sum('total_price', filter=Q(content_type=valuecard_ct)),
                    other=Sum('total_price', filter=Q(content_type__isnull=True))
                )
                .order_by('-year', '-month_num')
            )
            print(f"Item Monthly output size: {len(item_monthly)}")

            print("SUCCESS")
        except Exception as e:
            import traceback
            traceback.print_exc()
