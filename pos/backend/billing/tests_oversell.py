"""
Nothing can be sold without stock (owner decisions #52 and #18).

This module used to assert the opposite policy - "overselling is allowed, record
the shortfall" - which the owner has ruled out: a sale that needs more stock
than the branch holds is refused, the message names the item and the shortfall,
and nothing is billed or moved.

Prices: a product's MRP is GST-inclusive (decision F22), so the customer pays
exactly MRP x quantity; the GST is carried inside that amount, not added on top.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from inventory.models import Product, StockTransaction
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class NoSaleWithoutStockTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('300.00'), gst_percent=Decimal('18.00'),
            current_stock=2, center=self.center)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def sell(self, quantity):
        return self.api.post('/billing/invoices/', {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'paid', 'discount': '0',
            'items': [{
                'content_type': 'inventory.product',
                'object_id': self.product.id, 'quantity': quantity,
                'unit_price': '300.00', 'discount': '0',
                'tax_percentage': '18.00', 'description': 'Shampoo',
            }],
            # MRP is GST-inclusive: the customer pays exactly MRP x quantity.
            'payments': [{'payment_method': 'Cash',
                          'amount': str(Decimal('300.00') * quantity)}],
        }, format='json')

    def transactions(self):
        return list(StockTransaction.objects.filter(product=self.product).order_by('id'))

    # ── within stock: sold, deducted once ─────────────────────────────────

    def test_a_sale_within_stock_deducts_and_records_once(self):
        response = self.sell(2)
        self.assertEqual(response.status_code, 201, response.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 0)

        rows = self.transactions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].transaction_type, 'SALE')
        self.assertEqual(rows[0].quantity_change, -2)
        self.assertEqual(rows[0].center, self.center)

    # ── beyond stock: refused, nothing happens ────────────────────────────

    def test_selling_more_than_in_stock_is_refused(self):
        response = self.sell(5)
        self.assertEqual(response.status_code, 400, response.data)

    def test_the_refusal_names_the_item_and_the_shortfall(self):
        message = str(self.sell(5).data)
        self.assertIn('Shampoo', message)
        self.assertIn('Required: 5', message)
        self.assertIn('Available: 2', message)

    def test_a_refused_sale_bills_nothing_and_moves_no_stock(self):
        self.sell(5)
        self.assertFalse(Invoice.objects.exists())
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 2)
        self.assertEqual(self.transactions(), [])

    def test_selling_from_zero_stock_is_refused(self):
        self.product.current_stock = 0
        self.product.save(update_fields=['current_stock'])

        response = self.sell(1)
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('Available: 0', str(response.data))
        self.assertEqual(self.transactions(), [])
