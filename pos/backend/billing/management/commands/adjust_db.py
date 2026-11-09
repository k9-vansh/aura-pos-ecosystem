import random
from django.core.management.base import BaseCommand
from django.db import transaction
from faker import Faker
from inventory.models import Vendor, Product
from services.models import ServiceMaster
from salon_admin.models import Center

fake = Faker('en_IN')

class Command(BaseCommand):
    help = 'Adjusts services to exactly 1000 and adds 950 detailed products.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("Adjusting Database counts..."))
        
        # 1. Trim Services to exactly 1000
        service_count = ServiceMaster.objects.count()
        if service_count > 1000:
            excess = service_count - 1000
            self.stdout.write(f"Found {service_count} Services. Deleting {excess} to keep exactly 1000.")
            # Get IDs of excess services to delete
            excess_ids = list(ServiceMaster.objects.order_by('-id').values_list('id', flat=True)[:excess])
            ServiceMaster.objects.filter(id__in=excess_ids).delete()
            self.stdout.write(self.style.SUCCESS(f"Trimmed services. Current count: {ServiceMaster.objects.count()}"))
        elif service_count < 1000:
            self.stdout.write(f"Only found {service_count} services. Not deleting any.")

        # 2. Add 950 detailed products
        centers = list(Center.objects.all())
        if not centers:
            self.stdout.write(self.style.ERROR("No centers found."))
            return

        vendors = list(Vendor.objects.all())
        if not vendors:
            self.stdout.write(self.style.ERROR("No vendors found."))
            return

        self.stdout.write("Generating 950 detailed products...")
        products = []
        with transaction.atomic():
            for i in range(950):
                p = Product(
                    product_code=f"DET-PRD-{random.randint(100000, 999999)}",
                    name=f"Premium {fake.word().title()} {random.choice(['Serum', 'Lotion', 'Oil', 'Cream', 'Wax'])}",
                    brand=fake.company(),
                    category=random.choice(['Skin Care', 'Hair Care', 'Body Care', 'Fragrance']),
                    sub_category=random.choice(['Organic', 'Synthetic', 'Luxury', 'Everyday']),
                    vendor_name=random.choice(vendors).name,
                    price=random.uniform(999.0, 15999.0),
                    gst_percent=random.choice([12, 18, 28]),
                    barcode=fake.ean13(),
                    sac_code=f"SAC{random.randint(1000,9999)}",
                    is_active=True,
                    reorder_level=random.randint(10, 50),
                    reorder_quantity=random.randint(50, 200),
                    current_stock=random.randint(100, 1000),
                    center=random.choice(centers),
                    incentive=random.choice([0, 5, 10, 15])
                )
                products.append(p)
            Product.objects.bulk_create(products)
            
        self.stdout.write(self.style.SUCCESS("950 Detailed Products added successfully!"))
