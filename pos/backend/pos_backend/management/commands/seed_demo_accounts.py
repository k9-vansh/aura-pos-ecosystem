from django.core.management.base import BaseCommand
from pos_backend.demo_seeder import ensure_demo_accounts

class Command(BaseCommand):
    help = 'Seeds or updates the 4 guaranteed demo accounts for Aura POS Ecosystem'

    def handle(self, *args, **options):
        self.stdout.write("==> Seeding guaranteed demo credentials...")
        result = ensure_demo_accounts()
        if result.get("status") == "success":
            self.stdout.write(self.style.SUCCESS("[OK] POS Till user: admin@aurasalon.io / admin123 (Ready)"))
            self.stdout.write(self.style.SUCCESS("[OK] Owner Dashboard user: 9811122233 / admin123 (Ready)"))
            self.stdout.write(self.style.SUCCESS("[OK] Staff Mobile user: 9000100001 / PIN 1234 (Ready)"))
            self.stdout.write(self.style.SUCCESS("[OK] Client Portal user: 9876543210 / PIN 1234 (Ready)"))
            self.stdout.write(self.style.SUCCESS("==> All 4 demo credentials verified and active!"))
        else:
            self.stdout.write(self.style.ERROR(f"Error seeding demo accounts: {result.get('message')}"))
