import os
import json
from django.http import JsonResponse
from .demo_seeder import ensure_demo_accounts

def health_check_view(request):
    """Liveness & Readiness probe.
    Ensures all 4 demo users exist and reports status.
    """
    seed_result = ensure_demo_accounts()
    
    from accounts.models import CustomUser
    from owner_api.models import OwnerAccount
    from staff.models import StaffMember
    from clients.models import Client

    data = {
        "status": "healthy",
        "service": "aura-pos-backend",
        "deployment_ready": True,
        "seed_result": seed_result,
        "accounts": {
            "pos_till": CustomUser.objects.filter(email="admin@aurasalon.io", is_active=True).exists(),
            "owner": OwnerAccount.objects.filter(phone="9811122233", is_active=True).exists(),
            "staff": StaffMember.objects.filter(phone="9000100001", is_active=True).exists(),
            "client": Client.objects.filter(phone="9876543210", is_active=True).exists()
        }
    }
    return JsonResponse(data, status=200)
