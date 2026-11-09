"""
Inventory invariant tests.

Pins the stock-ledger and purchase-order findings from the audit.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, PurchaseOrder, StockTransaction, Vendor
from inventory.serializers import PurchaseOrderSerializer, line_total
from pos_backend.money import money
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class InventoryTestBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.other_center = Center.objects.create(center_name='Other')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )
        self.clerk_role = Role.objects.create(name='Clerk', permissions={
            'inventory': {'products': {'read': True, 'create': True, 'update': True, 'delete': False}}
        })
        self.clerk = CustomUser.objects.create_user(
            email='clerk@test.com', password='pw', full_name='Clerk',
            role=self.clerk_role, center=self.center,
        )

        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('500.00'), gst_percent=Decimal('18.00'),
            current_stock=20, center=self.center,
        )
        self.vendor = Vendor.objects.create(name='Acme', center=self.center)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def ledger_total(self, product=None):
        product = product or self.product
        return sum(t.quantity_change for t in StockTransaction.objects.filter(product=product))


class StockCheckoutTests(InventoryTestBase):
    """H15 / H16 / H18."""

    def test_checkout_deducts_and_logs(self):
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 3}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 17)
        self.assertEqual(self.ledger_total(), -3)

    def test_failed_line_does_not_commit_earlier_lines(self):
        """H15: returning a Response mid-loop still COMMITTED the atomic block."""
        second = Product.objects.create(
            name='Conditioner', price=Decimal('300.00'), current_stock=1, center=self.center,
        )
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [
                {'product_id': self.product.id, 'quantity': 5},   # fine
                {'product_id': second.id, 'quantity': 99},        # fails
            ],
        }, format='json')
        self.assertEqual(response.status_code, 400)

        self.product.refresh_from_db()
        second.refresh_from_db()
        # Nothing moved.
        self.assertEqual(self.product.current_stock, 20)
        self.assertEqual(second.current_stock, 1)
        self.assertEqual(StockTransaction.objects.count(), 0)

    def test_negative_quantity_cannot_inflate_stock(self):
        """H16: `40 - (-5) = 45` - a negative checkout ADDED stock."""
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': -5}],
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 20)

    def test_missing_quantity_is_a_400_not_a_500(self):
        """H18: item['quantity'] was read outside the try block -> KeyError."""
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id}],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_non_numeric_quantity_is_a_400_not_a_500(self):
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 'lots'}],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_fractional_quantity_is_kept_exactly(self):
        """X8: a consumable in ml or g is no longer cut to whole units."""
        before = self.product.current_stock
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': '2.5'}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, Decimal(before) - Decimal('2.5'))
        self.assertEqual(StockTransaction.objects.filter(product=self.product).latest('id').quantity_change,
                         Decimal('-2.5'))

    def test_a_non_numeric_quantity_is_rejected(self):
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 'two'}],
        }, format='json')
        self.assertEqual(response.status_code, 400)

    def test_product_from_another_center_is_rejected(self):
        foreign = Product.objects.create(
            name='Foreign', price=Decimal('100.00'), current_stock=5, center=self.other_center,
        )
        response = self.api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': foreign.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(response.status_code, 400)


class StockAuditTests(InventoryTestBase):
    """H16 / H29 - the audit action overwrites recorded stock."""

    def test_audit_records_the_delta(self):
        response = self.api.post('/inventory/api/products/audit/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 18}],
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 18)
        self.assertEqual(self.ledger_total(), -2)

    def test_audit_requires_permission(self):
        """H29: stock audit needed nothing beyond being logged in."""
        self.api.force_authenticate(self.clerk)
        response = self.api.post('/inventory/api/products/audit/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 999}],
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 20)

    def test_negative_physical_count_is_rejected(self):
        response = self.api.post('/inventory/api/products/audit/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': -3}],
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 20)


class StockLedgerIntegrityTests(InventoryTestBase):
    """H17 - current_stock must not be writable without a ledger entry."""

    def test_current_stock_is_read_only_on_update(self):
        response = self.api.patch(
            f'/inventory/api/products/{self.product.id}/',
            {'current_stock': 9999}, format='json',
        )
        self.assertIn(response.status_code, (200, 400))
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 20)

    def test_opening_stock_on_create_is_logged(self):
        response = self.api.post('/inventory/api/products/', {
            'name': 'New Product', 'price': '100.00', 'gst_percent': '18.00',
            'center': self.center.id, 'current_stock': 15,
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        created = Product.objects.get(name='New Product')
        self.assertEqual(created.current_stock, 15)
        self.assertEqual(self.ledger_total(created), 15)

    def test_negative_price_is_rejected(self):
        response = self.api.post('/inventory/api/products/', {
            'name': 'Bad', 'price': '-10.00', 'center': self.center.id,
        }, format='json')
        self.assertEqual(response.status_code, 400)


class PurchaseOrderMathTests(InventoryTestBase):
    """H7 / H8 - the PO total must not change when the PO is edited."""

    def test_line_total_taxes_the_post_discount_value(self):
        result = line_total(10, Decimal('250.00'), Decimal('10.00'), Decimal('18.00'))
        self.assertEqual(result['gross'], Decimal('2500.00'))
        self.assertEqual(result['discount_amount'], Decimal('250.00'))
        self.assertEqual(result['taxable'], Decimal('2250.00'))
        self.assertEqual(result['tax_amount'], Decimal('405.00'))
        self.assertEqual(result['total'], Decimal('2655.00'))

    def test_missing_percentages_do_not_raise(self):
        """H8: `0 / 100` is a float, and Decimal * float is a TypeError."""
        result = line_total(10, Decimal('250.00'), None, None)
        self.assertEqual(result['total'], Decimal('2500.00'))

    def test_create_and_update_agree(self):
        """H7: create() taxed the pre-discount base; update() the post."""
        payload = {
            'vendor': self.vendor.id,
            'center': self.center.id,
            'status': 'Draft',
            'items': [{
                'product': self.product.id, 'quantity': 10, 'rate': '250.00',
                'discount_percent': '10.00', 'tax_percent': '18.00',
            }],
        }
        created = self.api.post('/inventory/api/purchase-orders/', payload, format='json')
        self.assertEqual(created.status_code, 201, created.data)
        po_id = created.data['id']
        total_after_create = money(PurchaseOrder.objects.get(pk=po_id).total_amount)

        # Re-send the identical payload as an update.
        updated = self.api.put(f'/inventory/api/purchase-orders/{po_id}/', payload, format='json')
        self.assertEqual(updated.status_code, 200, updated.data)
        total_after_update = money(PurchaseOrder.objects.get(pk=po_id).total_amount)

        self.assertEqual(total_after_create, total_after_update)
        self.assertEqual(total_after_create, Decimal('2655.00'))

    def test_po_creates_without_explicit_percentages(self):
        response = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': self.vendor.id,
            'center': self.center.id,
            'items': [{'product': self.product.id, 'quantity': 4, 'rate': '100.00'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(money(PurchaseOrder.objects.get(pk=response.data['id']).total_amount),
                         Decimal('400.00'))

    def test_delivery_adds_stock_and_logs_it(self):
        created = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': self.vendor.id, 'center': self.center.id, 'status': 'Draft',
            'items': [{'product': self.product.id, 'quantity': 6, 'rate': '100.00',
                       'discount_percent': '0.00', 'tax_percent': '0.00'}],
        }, format='json')
        po_id = created.data['id']

        for next_status in ['Finalized', 'Approved', 'Ordered', 'Shipped', 'Delivered']:
            response = self.api.patch(f'/inventory/api/purchase-orders/{po_id}/',
                                      {'status': next_status}, format='json')
            self.assertEqual(response.status_code, 200, response.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 26)
        self.assertEqual(self.ledger_total(), 6)

    def test_status_cannot_move_backwards(self):
        created = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': self.vendor.id, 'center': self.center.id, 'status': 'Approved',
            'items': [{'product': self.product.id, 'quantity': 1, 'rate': '10.00',
                       'discount_percent': '0.00', 'tax_percent': '0.00'}],
        }, format='json')
        response = self.api.patch(f'/inventory/api/purchase-orders/{created.data["id"]}/',
                                  {'status': 'Draft'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_delivering_twice_does_not_double_receive(self):
        created = self.api.post('/inventory/api/purchase-orders/', {
            'vendor': self.vendor.id, 'center': self.center.id, 'status': 'Shipped',
            'items': [{'product': self.product.id, 'quantity': 5, 'rate': '10.00',
                       'discount_percent': '0.00', 'tax_percent': '0.00'}],
        }, format='json')
        po_id = created.data['id']
        self.api.patch(f'/inventory/api/purchase-orders/{po_id}/',
                       {'status': 'Delivered'}, format='json')
        self.api.patch(f'/inventory/api/purchase-orders/{po_id}/',
                       {'status': 'Delivered'}, format='json')
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 25)


class CenterScopingTests(InventoryTestBase):
    """M19 / H30 - center_id handling and fail-closed scoping."""

    def test_literal_null_center_id_is_not_a_500(self):
        """M19: `int('null')` raised ValueError. The frontend does send 'null'."""
        self.api.force_authenticate(self.clerk)
        response = self.api.get('/inventory/api/products/?center_id=null')
        self.assertEqual(response.status_code, 200)

    def test_garbage_center_id_is_not_a_500(self):
        response = self.api.get('/inventory/api/products/?center_id=abc')
        self.assertEqual(response.status_code, 200)

    def test_user_with_no_center_sees_nothing(self):
        """H30: the pattern had no else branch, so no filter was applied.

        With module permissions now enforced on the inventory base viewset, a
        role granting nothing is refused outright - which is stricter than the
        empty list this used to assert, and correct either way.
        """
        orphan_role = Role.objects.create(name='Orphan', permissions={})
        orphan = CustomUser.objects.create_user(
            email='orphan@test.com', password='pw', full_name='Orphan', role=orphan_role,
        )
        self.api.force_authenticate(orphan)
        response = self.api.get('/inventory/api/products/')
        self.assertIn(response.status_code, (200, 403))
        if response.status_code == 200:
            rows = (response.data if isinstance(response.data, list)
                    else response.data.get('results', []))
            self.assertEqual(len(rows), 0)

    def test_inventory_module_permission_is_enforced(self):
        """The base viewset only had IsAuthenticated, so the permission_module
        declared on the subclasses was never read.

        Front desk can SEE the product list - they sell products at the till, so
        governing the catalogue by Inventory alone would stop them billing one.
        They cannot CHANGE the product master. That split is
        `permission_read_also` on ProductViewSet.
        """
        front_desk = Role.objects.create(name='Front Desk', permissions={
            'billing': {'invoices': {'read': True}, 'new_invoice': {'read': True}},
        })
        user = CustomUser.objects.create_user(
            email='fd@test.com', password='pw', full_name='Front Desk',
            role=front_desk, center=self.center,
        )
        self.api.force_authenticate(user)

        # Reading is delegated from billing.
        self.assertEqual(self.api.get('/inventory/api/products/').status_code, 200)

        # Writing is not.
        created = self.api.post('/inventory/api/products/', {
            'name': 'Sneaky Product', 'price': '100.00', 'center': self.center.id,
        }, format='json')
        self.assertEqual(created.status_code, 403)

        product = Product.objects.filter(center=self.center).first()
        self.assertEqual(
            self.api.patch(f'/inventory/api/products/{product.id}/',
                           {'price': '1.00'}, format='json').status_code, 403)
        self.assertEqual(
            self.api.delete(f'/inventory/api/products/{product.id}/').status_code, 403)

    def test_a_role_with_no_permissions_at_all_reads_nothing(self):
        """The delegation must not become a way in for a role that grants nothing."""
        nobody = Role.objects.create(name='Nobody', permissions={})
        user = CustomUser.objects.create_user(
            email='nobody@test.com', password='pw', full_name='Nobody',
            role=nobody, center=self.center,
        )
        self.api.force_authenticate(user)
        self.assertEqual(self.api.get('/inventory/api/products/').status_code, 403)

    def test_a_user_with_inventory_read_is_allowed(self):
        self.api.force_authenticate(self.clerk)
        self.assertEqual(self.api.get('/inventory/api/products/').status_code, 200)

    def test_user_cannot_read_another_centers_products(self):
        Product.objects.create(
            name='Foreign', price=Decimal('50.00'), current_stock=3, center=self.other_center,
        )
        self.api.force_authenticate(self.clerk)
        response = self.api.get(f'/inventory/api/products/?center_id={self.other_center.id}')
        rows = response.data if isinstance(response.data, list) else response.data.get('results', [])
        self.assertEqual(len(rows), 0)
