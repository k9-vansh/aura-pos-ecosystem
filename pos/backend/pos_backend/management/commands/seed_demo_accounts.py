import re
from django.core.management.base import BaseCommand
from django.contrib.auth.hashers import make_password
from django.utils import timezone

class Command(BaseCommand):
    help = 'Seeds or updates the 4 guaranteed demo accounts for Aura POS Ecosystem'

    def handle(self, *args, **options):
        from accounts.models import CustomUser
        from salon_admin.models import Center, Role
        from owner_api.models import OwnerAccount
        from staff.models import StaffMember, Designation
        from clients.models import Client
        from clients.pins import set_pin

        self.stdout.write("==> Seeding guaranteed demo credentials...")

        # 1. Get or create primary Center
        center = Center.objects.filter(is_active=True).first()
        if not center:
            center = Center.objects.create(
                center_name="Aura Flagship",
                display_name="Aura Salon & Spa (Flagship)",
                code="AURA-01",
                is_active=True
            )
            self.stdout.write(f"Created fallback center: {center}")

        # 2. Get or create Super Admin Role with full permissions across all modules
        role, _ = Role.objects.get_or_create(
            name="Super Admin",
            defaults={
                "description": "Full enterprise administrator with all modules",
                "permissions": {}
            }
        )

        role.permissions = {
            "owner_app": {
                "today": {"read": True},
                "month": {"read": True},
                "invoices": {"read": True},
                "staff": {"read": True},
                "ai_report": {"read": True},
                "settings": {"read": True}
            },
            "billing": {"invoices": {"read": True, "create": True, "update": True, "delete": True}, "new_invoice": {"create": True}},
            "inventory": {"products": {"read": True, "create": True, "update": True, "delete": True}, "stock_requests": {"read": True, "create": True, "update": True, "delete": True}},
            "finance": {"register_summary": {"read": True, "create": True, "update": True, "delete": True}, "monthly_sales": {"read": True}},
            "staff": {"directory": {"read": True, "create": True, "update": True, "delete": True}, "payrolls": {"read": True, "update": True}},
            "appointments": {"calendar": {"read": True, "create": True, "update": True, "delete": True}},
            "clients": {"clients": {"read": True, "create": True, "update": True, "delete": True}},
            "marketing": {"promotions": {"read": True, "create": True, "update": True, "delete": True}},
            "salon_admin": {"centers": {"read": True, "create": True, "update": True, "delete": True}, "users": {"read": True, "create": True, "update": True, "delete": True}, "roles": {"read": True, "create": True, "update": True, "delete": True}},
            "admin": {"users": {"read": True, "create": True, "update": True, "delete": True}, "roles": {"read": True, "create": True, "update": True, "delete": True}, "centers": {"read": True, "create": True, "update": True, "delete": True}}
        }
        role.save()

        # 3. Demo User 1: POS Till & Back-Office
        # Email: admin@aurasalon.io / Password: admin123
        user = CustomUser.objects.filter(email="admin@aurasalon.io").first()
        if not user:
            user = CustomUser.objects.create_user(
                email="admin@aurasalon.io",
                password="admin123",
                full_name="Aura Executive Admin",
                is_superuser=True,
                is_staff=True,
                is_active=True,
                center=center,
                role=role,
                designation="Executive Director"
            )
        else:
            user.set_password("admin123")
            user.is_superuser = True
            user.is_staff = True
            user.is_active = True
            user.center = center
            user.role = role
            user.save()
        user.centers.set(Center.objects.filter(is_active=True))
        self.stdout.write(self.style.SUCCESS("[OK] POS Till user: admin@aurasalon.io / admin123 (Ready)"))

        # 4. Demo User 2: Owner Analytics Dashboard
        # Phone: 9811122233 / Password: admin123
        owner = OwnerAccount.objects.filter(phone="9811122233").first()
        if not owner:
            owner = OwnerAccount.objects.create(
                phone="9811122233",
                name="Vikramaditya Oberoi",
                email="owner@aurasalon.io",
                role=role,
                is_active=True
            )
        owner.name = "Vikramaditya Oberoi"
        owner.set_password("admin123")
        owner.role = role
        owner.is_active = True
        owner.save()
        owner.centers.set(Center.objects.filter(is_active=True))
        self.stdout.write(self.style.SUCCESS("[OK] Owner Dashboard user: 9811122233 / admin123 (Ready)"))

        # 5. Demo User 3: Staff Mobile Portal
        # Phone: 9000100001 / PIN: 1234
        designation, _ = Designation.objects.get_or_create(
            name="Senior Stylist & Art Director",
            defaults={"salary": 45000, "commission_percentage": 15.0}
        )
        staff = StaffMember.objects.filter(phone="9000100001").first()
        if not staff:
            staff = StaffMember.objects.create(
                phone="9000100001",
                first_name="Rahul",
                last_name="Kapoor",
                center=center,
                designation=designation.name,
                designation_fk=designation,
                staff_code="STF-001",
                is_active=True,
                salary=45000,
                commission_percentage=15.0
            )
        staff.first_name = "Rahul"
        staff.last_name = "Kapoor"
        staff.app_password = make_password("1234")
        staff.is_active = True
        staff.center = center
        staff.save()
        self.stdout.write(self.style.SUCCESS("[OK] Staff Mobile user: 9000100001 / PIN 1234 (Ready)"))

        # 6. Demo User 4: Client Self-Service App
        # Phone: 9876543210 / PIN: 1234
        client = Client.objects.filter(phone__contains="9876543210").first()
        if not client:
            client = Client.objects.create(
                phone="9876543210",
                first_name="Ananya",
                last_name="Sen",
                email="ananya.sen@example.com",
                center=center,
                is_active=True
            )
        client.first_name = "Ananya"
        client.last_name = "Sen"
        client.center = center
        client.is_active = True
        client.save()
        set_pin(client, "1234")
        self.stdout.write(self.style.SUCCESS("[OK] Client Portal user: 9876543210 / PIN 1234 (Ready)"))

        self.stdout.write(self.style.SUCCESS("==> All 4 demo credentials verified and active!"))
