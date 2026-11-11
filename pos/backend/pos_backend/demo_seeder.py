import logging
from django.utils import timezone
from django.contrib.auth.hashers import make_password

logger = logging.getLogger(__name__)

def ensure_demo_accounts():
    """Guarantees the existence of all 4 demo users with the exact demo credentials:
    1. POS Till: admin@aurasalon.io / admin123
    2. Owner App: 9811122233 / admin123
    3. Staff App: 9000100001 / PIN 1234
    4. Client App: 9876543210 / PIN 1234
    """
    try:
        from accounts.models import CustomUser
        from salon_admin.models import Center, Role
        from owner_api.models import OwnerAccount
        from staff.models import StaffMember, Designation
        from clients.models import Client
        from clients.pins import set_pin

        # 1. Primary Center
        center = Center.objects.filter(is_active=True).first()
        if not center:
            center = Center.objects.create(
                center_name="Aura Flagship",
                display_name="Aura Salon & Spa (Flagship)",
                code="AURA-01",
                is_active=True
            )

        # 2. Super Admin Role
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

        # 3. POS Till: admin@aurasalon.io / admin123
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

        # 4. Owner: 9811122233 / admin123
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

        # 5. Staff: 9000100001 / PIN 1234
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
                designation="Senior Stylist & Art Director",
                designation_fk=designation,
                app_password=make_password("1234"),
                is_active=True
            )
        else:
            staff.first_name = "Rahul"
            staff.last_name = "Kapoor"
            staff.center = center
            staff.designation = "Senior Stylist & Art Director"
            staff.designation_fk = designation
            staff.app_password = make_password("1234")
            staff.is_active = True
            staff.save()

        # 6. Client: 9876543210 / PIN 1234
        client = Client.objects.filter(phone="9876543210").first()
        if not client:
            client = Client.objects.create(
                phone="9876543210",
                first_name="Ananya",
                last_name="Sen",
                email="ananya.sen@example.com",
                center=center,
                is_active=True
            )
        else:
            client.first_name = "Ananya"
            client.last_name = "Sen"
            client.email = "ananya.sen@example.com"
            client.center = center
            client.is_active = True
            client.save()
        set_pin(client, "1234")

        return {
            "status": "success",
            "pos_user": user.email,
            "owner_phone": owner.phone,
            "staff_phone": staff.phone,
            "client_phone": client.phone
        }
    except Exception as e:
        logger.exception("Failed in ensure_demo_accounts: %s", e)
        return {"status": "error", "message": str(e)}
