import random
import string
from locust import HttpUser, task, between

def random_string(length=10):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def random_phone():
    return ''.join(random.choices(string.digits, k=10))

class POSUserBehavior(HttpUser):
    # Wait between 1 and 3 seconds between tasks to simulate real user behavior
    wait_time = between(1, 3)

    def on_start(self):
        """
        Executed when a virtual user starts. 
        We would ideally log in here and get a JWT token.
        For this test, assuming basic auth or token-based auth isn't strictly required 
        or we bypass it, but usually you'd do a login request here.
        """
        # If your API requires auth, you would do it here:
        # response = self.client.post("/api/token/", {"username": "admin", "password": "password"})
        # self.token = response.json()["access"]
        # self.client.headers.update({"Authorization": f"Bearer {self.token}"})
        self.center_id = None
        self.client_id = None
        
    @task(1)
    def full_flow(self):
        """Simulate the entire flow from center creation to billing"""
        
        # 1. Admin Flow - Create Center
        center_name = f"Center_{random_string()}"
        res = self.client.post("/salon_admin/api/centers/", json={
            "center_name": center_name,
            "phone": random_phone(),
            "address": "123 Load Test St"
        })
        if res.status_code in [200, 201]:
            # Try to get the center ID to use in subsequent requests
            data = res.json()
            if "id" in data:
                self.center_id = data["id"]
            elif "data" in data and "id" in data["data"]:
                self.center_id = data["data"]["id"]
        
        # If we couldn't create a center, we use a fallback ID (e.g. 1) to keep the flow moving
        center = self.center_id or 1
        
        # 2. Staff Flow - Create Designation & Staff
        self.client.post("/staff/api/designations/", json={
            "name": f"Stylist_{random_string(5)}",
            "category": "Hair"
        })
        self.client.post("/staff/api/members/", json={
            "first_name": "Test",
            "last_name": random_string(5),
            "phone": random_phone(),
            "designation": f"Stylist_{random_string(5)}",
            "center": center,
            "gender": "Male",
            "salary": 50000,
            "is_active": True
        })

        # 3. Services Flow - Create Service Master
        self.client.post("/services/api/master/", json={
            "name": f"Haircut_{random_string(5)}",
            "category": "Hair",
            "sub_category": "Men",
            "duration_mins": 30,
            "default_price": 500,
            "is_active": True
        })

        # 4. Clients Flow - Create Client
        res = self.client.post("/clients/api/clients/", json={
            "first_name": "Load",
            "last_name": f"User_{random_string(5)}",
            "phone": random_phone(),
            "gender": "Female",
            "center": center
        })
        if res.status_code in [200, 201]:
            data = res.json()
            if "id" in data:
                self.client_id = data["id"]
            elif "data" in data and "id" in data["data"]:
                self.client_id = data["data"]["id"]
                
        client = self.client_id or 1

        # 5. Inventory Flow - Create Vendor & Product
        self.client.post("/inventory/api/vendors/", json={
            "name": f"Vendor_{random_string(5)}",
            "contact_person": "Bob",
            "phone": random_phone(),
            "center": center
        })
        
        self.client.post("/inventory/api/products/", json={
            "name": f"Product_{random_string(5)}",
            "brand": "Loreal",
            "category": "Hair Care",
            "price": 200,
            "current_stock": 50,
            "center": center
        })

        # 6. Finance Flow - Add Petty Cash
        self.client.post("/finance/api/petty_cash/", json={
            "center": center,
            "amount": 1000,
            "type": "in",
            "category": "topup",
            "description": "Initial Topup"
        })

        # 7. Marketing Flow - Create Promotion
        self.client.post("/marketing/api/promotions/", json={
            "title": f"Promo_{random_string(5)}",
            "description": "Test",
            "promo_type": "discount",
            "level": "Global",
            "is_active": True,
            "config": {"discount_type": "percent", "discount_value": 10},
            "center": center
        })

        # 8. Appointments Flow
        self.client.post("/appointments/api/appointments/", json={
            "center": center,
            "client_name": "Appt Client",
            "client_phone": random_phone(),
            "date": "2026-08-10",
            "start_time": "10:00",
            "status": "scheduled"
        })

        # 9. Billing Flow - Create Invoice
        self.client.post("/billing/api/invoices/", json={
            "center": center,
            "client": client,
            "status": "unpaid",
            "subtotal": 100,
            "total_amount": 100,
            "payment_status": "unpaid"
        })
        
    @task(3)
    def read_heavy_flow(self):
        """Simulate a user just browsing the dashboard and reports (more frequent than full flow)"""
        center = self.center_id or 1
        
        # Hit dashboard endpoints
        self.client.get(f"/salon_admin/api/centers/{center}/")
        self.client.get(f"/staff/api/members/?center={center}")
        self.client.get(f"/inventory/api/products/?center={center}")
        self.client.get(f"/billing/api/invoices/?center={center}")
        self.client.get(f"/appointments/api/appointments/?center={center}")
