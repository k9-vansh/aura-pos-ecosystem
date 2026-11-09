import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'pos_backend.settings')
django.setup()

from salon_admin.models import Center
from accounts.models import CustomUser
from owner_api.models import OwnerAccount
from staff.models import StaffMember
from clients.models import Client
from clients import pins
from django.contrib.auth.hashers import make_password

print("Starting database migration & cleanup for Aura Salon & Spa...")

# 1. Update Centers
centers_mapping = {
    'Ambience Mall': ('Downtown Flagship', 'Downtown Flagship', 'Connaught Place, Central Boulevard', '110001'),
    'Ardee Mall': ('Metropolis Branch', 'Metropolis Branch', 'Galleria Towers, Sector 28', '122002'),
    'General Warehouse': ('Central Hub & Warehouse', 'Central Hub & Warehouse', 'Industrial Park Area, Unit 4B', '110020')
}

for old_name, (new_name, display_name, address, pin_code) in centers_mapping.items():
    center = Center.objects.filter(center_name=old_name).first()
    if center:
        center.center_name = new_name
        center.display_name = display_name
        center.address = address
        center.pin_code = pin_code
        center.save()
        print(f"Updated Center: {old_name} -> {new_name}")
    else:
        c, created = Center.objects.get_or_create(
            center_name=new_name,
            defaults={
                'display_name': display_name,
                'address': address,
                'pin_code': pin_code,
                'is_active': True
            }
        )
        if created:
            print(f"Created Center: {new_name}")

# 2. Update Admin Users
admin_user, _ = CustomUser.objects.get_or_create(
    email='admin@aurasalon.io',
    defaults={'full_name': 'Aura Admin', 'is_superuser': True, 'is_staff': True, 'is_active': True}
)
admin_user.full_name = 'Aura Admin'
admin_user.set_password('admin123')
admin_user.is_superuser = True
admin_user.is_staff = True
admin_user.is_active = True
admin_user.save()
print("Configured Superuser: admin@aurasalon.io / admin123")

# Also keep hello@gmail.com for convenience if desired, but with Aura details
hello_user = CustomUser.objects.filter(email='hello@gmail.com').first()
if hello_user:
    hello_user.full_name = 'Aura Director'
    hello_user.set_password('admin123')
    hello_user.save()
    print("Updated hello@gmail.com / admin123")

# 3. Update / Create Owner Account for owner-app
owner_acc = OwnerAccount.objects.filter(phone='9811122233').first() or OwnerAccount(phone='9811122233')
owner_acc.name = 'Aura Executive'
owner_acc.set_password('admin123')
owner_acc.is_active = True
owner_acc.all_salons = True
owner_acc.save()
print("Configured Owner Account: 9811122233 / admin123")

# 4. Update / Create Test Client for clients-app
c_first = Center.objects.filter(center_name='Downtown Flagship').first() or Center.objects.first()
client, _ = Client.objects.get_or_create(
    phone='9876543210',
    defaults={
        'first_name': 'Priya',
        'last_name': 'Sharma',
        'email': 'priya.sharma@example.com',
        'center': c_first,
        'is_active': True
    }
)
client.first_name = 'Priya'
client.last_name = 'Sharma'
client.email = 'priya.sharma@example.com'
client.center = c_first
client.save()
pins.set_pin(client, '1234')
print("Configured Client Account: 9876543210 / PIN 1234 (Priya Sharma)")

# 5. Update Staff Members for staff-app
for staff in StaffMember.objects.all():
    staff.app_password = make_password('1234')
    staff.save()

s_first = StaffMember.objects.first()
if s_first:
    print(f"Configured Staff Account: {s_first.phone} / PIN 1234 ({s_first.first_name})")

print("Database update finished successfully!")
