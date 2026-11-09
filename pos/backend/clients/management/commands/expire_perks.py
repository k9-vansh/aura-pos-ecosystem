from django.core.management.base import BaseCommand
from clients.models import ClientMembership, ClientPackage, ClientValueCard
import datetime

class Command(BaseCommand):
    help = 'Mark expired memberships, packages and value cards as inactive'

    def handle(self, *args, **options):
        today = datetime.date.today()
        expired_m = ClientMembership.objects.filter(is_active=True, expiry_date__lt=today).update(is_active=False)
        expired_p = ClientPackage.objects.filter(is_active=True, expiry_date__lt=today).update(is_active=False)
        expired_v = ClientValueCard.objects.filter(is_active=True, expiry_date__lt=today).update(is_active=False)
        self.stdout.write(f"Expired: {expired_m} memberships, {expired_p} packages, {expired_v} value cards")
