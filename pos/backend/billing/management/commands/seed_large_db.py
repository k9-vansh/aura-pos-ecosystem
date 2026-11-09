import random
from django.core.management.base import BaseCommand
from django.db import transaction
from faker import Faker
from salon_admin.models import Center
from inventory.models import Vendor, Product
from services.models import ServiceMaster
from staff.models import Designation, StaffMember
from clients.models import Client
from billing.models import Invoice, InvoiceItem

fake = Faker('en_IN')

class Command(BaseCommand):
    help = 'Seeds the database with a massive amount of highly detailed fake data for load testing.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("Starting massive database seeding..."))
        
        # 1. Create Centers
        centers = list(Center.objects.all())
        if len(centers) < 5:
            self.stdout.write("Generating Centers...")
            for _ in range(5):
                c = Center.objects.create(
                    center_name=f"{fake.company()} Branch",
                    display_name=fake.company_suffix(),
                    address=fake.address(),
                    region=fake.city(),
                    phone=fake.phone_number(),
                    center_email=fake.email(),
                )
                centers.append(c)

        # 2. Create Designations
        self.stdout.write("Generating Designations...")
        designations = list(Designation.objects.all())
        if not designations:
            for title in ['Senior Stylist', 'Junior Stylist', 'Massage Therapist', 'Manager', 'Receptionist', 'Makeup Artist']:
                d = Designation.objects.create(name=title)
                designations.append(d)

        # 3. Create Staff
        self.stdout.write("Generating 200 Staff Members...")
        staff_list = []
        with transaction.atomic():
            for _ in range(200):
                staff = StaffMember(
                    first_name=fake.first_name(),
                    last_name=fake.last_name(),
                    phone=fake.phone_number()[:15],
                    email=fake.email(),
                    gender=random.choice(['Male', 'Female', 'Other']),
                    designation=random.choice(designations).name,
                    designation_fk=random.choice(designations),
                    center=random.choice(centers),
                    salary=random.randint(15000, 50000),
                    is_active=True
                )
                staff_list.append(staff)
            StaffMember.objects.bulk_create(staff_list)
        all_staff = list(StaffMember.objects.all())

        # 4. Create Vendors
        self.stdout.write("Generating 50 Vendors...")
        vendors = []
        with transaction.atomic():
            for _ in range(50):
                v = Vendor(
                    name=fake.company(),
                    email=fake.email(),
                    phone=fake.phone_number()[:15],
                    address=fake.address(),
                    city=fake.city(),
                    center=random.choice(centers)
                )
                vendors.append(v)
            Vendor.objects.bulk_create(vendors)
        vendors = list(Vendor.objects.all())

        # 5. Create Products
        self.stdout.write("Generating 1,000 Products...")
        products = []
        with transaction.atomic():
            for i in range(1000):
                p = Product(
                    product_code=f"PRD{random.randint(10000, 99999)}",
                    name=f"{fake.word().title()} {fake.word().title()} Treatment",
                    brand=fake.company(),
                    category=random.choice(['Hair Care', 'Skin Care', 'Nails', 'Makeup']),
                    vendor_name=random.choice(vendors).name,
                    price=random.uniform(500.0, 5000.0),
                    gst_percent=random.choice([0, 5, 12, 18]),
                    barcode=fake.ean13(),
                    is_active=True,
                    current_stock=random.randint(10, 500),
                    center=random.choice(centers)
                )
                products.append(p)
            Product.objects.bulk_create(products)
        products = list(Product.objects.all())

        # 6. Create Services
        self.stdout.write("Generating 1,000 Services...")
        services = []
        with transaction.atomic():
            for i in range(1000):
                s = ServiceMaster(
                    name=f"{fake.word().title()} {fake.word().title()} Service",
                    category=random.choice(['Haircut', 'Coloring', 'Spa', 'Facial', 'Bridal']),
                    duration_mins=random.choice([15, 30, 45, 60, 90, 120]),
                    default_price=random.uniform(300.0, 15000.0),
                    tax_percentage=random.choice([0, 18])
                )
                services.append(s)
            ServiceMaster.objects.bulk_create(services)
        services = list(ServiceMaster.objects.all())

        # 7. Create Clients
        self.stdout.write("Generating 10,000 Clients (This might take a minute)...")
        clients = []
        batch_size = 2000
        for i in range(0, 10000, batch_size):
            batch = []
            for _ in range(batch_size):
                c = Client(
                    first_name=fake.first_name(),
                    last_name=fake.last_name(),
                    phone=f"{random.randint(6000000000, 9999999999)}",
                    gender=random.choice(['Male', 'Female']),
                    center=random.choice(centers)
                )
                batch.append(c)
            with transaction.atomic():
                Client.objects.bulk_create(batch)
            self.stdout.write(f"  -> Inserted {i + batch_size}/10000 clients")
        
        all_clients = list(Client.objects.all().order_by('?')[:5000]) # Sample for invoices

        # 8. Create Invoices
        self.stdout.write("Generating 15,000 Invoices...")
        for i in range(0, 15000, 1000):
            invoice_batch = []
            for _ in range(1000):
                inv = Invoice(
                    center=random.choice(centers),
                    client=random.choice(all_clients) if random.random() > 0.1 else None,
                    status=random.choice(['paid', 'paid', 'paid', 'draft']), # mostly paid
                    subtotal=random.uniform(500.0, 10000.0),
                    discount=0,
                    cgst=0,
                    sgst=0,
                    total_amount=0, # we will simplify
                    created_at=fake.date_time_between(start_date="-1y", end_date="now")
                )
                invoice_batch.append(inv)
            
            with transaction.atomic():
                Invoice.objects.bulk_create(invoice_batch)
            self.stdout.write(f"  -> Inserted {i + 1000}/15000 invoices")

        self.stdout.write(self.style.SUCCESS("Massive Database Seeding completed successfully!"))
