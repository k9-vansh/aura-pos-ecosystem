"""The fast product list sends exactly what the serializer would."""
import datetime
import json
from decimal import Decimal

from django.test import TestCase
from rest_framework.renderers import JSONRenderer
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.fast_lists import product_rows
from inventory.models import Product, ProductLot
from inventory.serializers import ProductSerializer
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

D = Decimal


def as_json(data):
    return json.loads(JSONRenderer().render(data))


class FastProductListTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket', gst_number='07AAAAA0000A1Z5')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='O', role=role,
                                                   center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        full = Product.objects.create(
            name='Shampoo', product_code='SH1', brand='Loreal', category='Consumable', sub_category='Hair',
            vendor_name='V', price=D('120.5'), mrp=D('199'), gst_percent=D('18'), barcode='890', sac_code='33059090',
            reorder_level=5, reorder_quantity=10, current_stock=D('3.250'), unit='pcs', pack_size=D('250'),
            pack_unit='ml', center=self.center, incentive=D('2.5'), sap_sync_status=True, sap_sync_message='ok')
        Product.objects.create(name='Bare', center=self.center)                   # everything optional left empty
        ProductLot.objects.create(product=full, lot_number='L1', net_price=D('100'), mrp=D('199'),
                                  expiry_date=datetime.date(2027, 5, 1), manufacturing_date=datetime.date(2026, 1, 1))
        ProductLot.objects.create(product=full, lot_number='L2', net_price=D('110.55'), mrp=D('210'))

    def test_every_field_matches_the_serializer(self):
        qs = Product.objects.filter(center=self.center).order_by('id')
        fast, _ = product_rows(qs, 100)
        slow = as_json(ProductSerializer(qs.select_related('center').prefetch_related('lots'), many=True).data)
        self.assertEqual(len(fast), 2)
        for a, b in zip(as_json(fast), slow):
            self.assertEqual(a, b)

    def test_the_endpoint_uses_it_and_still_answers_as_before(self):
        res = self.api.get(f'/inventory/api/products/?center_id={self.center.id}')
        self.assertEqual(res.status_code, 200)
        self.assertEqual({p['name'] for p in res.json()}, {'Shampoo', 'Bare'})
        paged = self.api.get(f'/inventory/api/products/?center_id={self.center.id}&page=1&page_size=1').json()
        self.assertEqual(paged['count'], 2)

    def test_a_branch_user_sees_only_their_own_branch(self):
        other = Center.objects.create(center_name='Noida', display_name='Noida', gst_number='09CCCCC0000C1Z5')
        Product.objects.create(name='Elsewhere', center=other)
        role = Role.objects.create(name='Clerk', permissions={'inventory': {'products': {'read': True}}})
        clerk = CustomUser.objects.create_user(email='c@test.com', password='pw', full_name='C', role=role,
                                               center=self.center)
        api = APIClient()
        api.force_authenticate(clerk)
        names = {p['name'] for p in api.get('/inventory/api/products/').json()}
        self.assertNotIn('Elsewhere', names)

    def test_the_cap_is_reported(self):
        rows, truncated = product_rows(Product.objects.filter(center=self.center), 1)
        self.assertEqual((len(rows), truncated), (1, True))
