from django.core.management.base import BaseCommand
from finance.views import _get_filtered_invoices, _compute_revenue_breakdown
from billing.models import Invoice

class Command(BaseCommand):
    def handle(self, *args, **kwargs):
        invoices = Invoice.objects.filter(status__in=['paid', 'partial'])
        print(f"Total invoices: {invoices.count()}")
        print("Computing revenue breakdown...")
        try:
            res = _compute_revenue_breakdown(invoices)
            print(res)
            print("SUCCESS")
        except Exception as e:
            import traceback
            traceback.print_exc()
