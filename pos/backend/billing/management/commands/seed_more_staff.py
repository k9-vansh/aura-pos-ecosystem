import random
from django.core.management.base import BaseCommand
from django.db import transaction
from faker import Faker
from salon_admin.models import Center
from staff.models import Designation, StaffMember

fake = Faker('en_IN')

class Command(BaseCommand):
    help = 'Seeds an additional 5000 staff members into the database.'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS("Starting massive staff seeding..."))
        
        centers = list(Center.objects.all())
        if not centers:
            self.stdout.write(self.style.ERROR("No centers found. Please seed centers first."))
            return

        designations = list(Designation.objects.all())
        if not designations:
            self.stdout.write(self.style.ERROR("No designations found. Please seed designations first."))
            return

        self.stdout.write("Generating 5,000 Staff Members...")
        
        batch_size = 1000
        for i in range(0, 5000, batch_size):
            staff_batch = []
            for _ in range(batch_size):
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
                staff_batch.append(staff)
                
            with transaction.atomic():
                StaffMember.objects.bulk_create(staff_batch)
            self.stdout.write(f"  -> Inserted {i + batch_size}/5000 staff members")

        self.stdout.write(self.style.SUCCESS("5,000 Staff Members seeded successfully!"))
