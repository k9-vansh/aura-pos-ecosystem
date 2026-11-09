"""Does a service stop when its Sales-BOM consumables are not in stock?

A service (an SAP Sales BOM parent) uses consumable products. The POS must
refuse the bill when a consumable is short at the branch, and a short consumable
must never be silently skipped. These tests pin down every way a bill can be
raised or completed.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from inventory.models import Product, StockTransaction
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceBOM, ServiceMaster
from staff.models import StaffMember


class ServiceConsumableTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket',
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='O',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client_obj = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist')
        self.gel = Product.objects.create(name='Gel', product_code='GEL', price=Decimal('200'), mrp=Decimal('200'),
                                          gst_percent=Decimal('18'), current_stock=5, center=self.center)
        self.wash = ServiceMaster.objects.create(name='Hair wash', default_price=Decimal('300'),
                                                 tax_percentage=Decimal('18'))
        ServiceBOM.objects.create(service=self.wash, product_code='GEL', quantity=Decimal('2'))

    def bill(self, service=None, qty=1, **extra):
        s = service or self.wash
        payload = {'client': self.client_obj.id, 'center': self.center.id, 'status': 'paid', 'discount': '0.00',
                   'items': [{'content_type': 'services.servicemaster', 'object_id': s.id, 'description': s.name,
                              'unit_price': str(s.default_price), 'discount': '0.00', 'quantity': qty,
                              'tax_percentage': str(s.tax_percentage), 'staff_members': [self.staff.id]}]}
        payload.update(extra)
        return payload

    def stock(self):
        self.gel.refresh_from_db()
        return self.gel.current_stock

    def test_with_enough_consumable_the_service_is_billed_and_stock_drops(self):
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(), Decimal('3'))
        row = StockTransaction.objects.get(transaction_type='BOM_CONSUMPTION')
        self.assertEqual(row.quantity_change, Decimal('-2'))

    def test_no_consumable_in_stock_refuses_the_service(self):
        Product.objects.filter(pk=self.gel.pk).update(current_stock=0)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400, getattr(res, 'data', None))
        self.assertIn('Not enough stock for consumable', str(res.data))
        self.assertEqual(Invoice.objects.exclude(status='draft').count(), 0)

    def test_less_than_one_service_worth_refuses_it(self):
        Product.objects.filter(pk=self.gel.pk).update(current_stock=1)   # needs 2
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.stock(), Decimal('1'))

    def test_two_services_together_need_twice_the_consumable(self):
        res = bill_and_pay(self.api, self.bill(qty=3))     # needs 6, has 5
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.stock(), Decimal('5'))

    def test_a_consumable_the_branch_never_stocked_refuses_the_service(self):
        Product.objects.filter(pk=self.gel.pk).delete()
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400)
        self.assertIn('Not enough stock for consumable', str(res.data))

    def test_stock_at_another_branch_does_not_count(self):
        other = Center.objects.create(center_name='Noida', sap_warehouse_code='WH-NOIDA')
        Product.objects.filter(pk=self.gel.pk).update(current_stock=0)
        Product.objects.create(name='Gel', product_code='GEL', price=Decimal('200'), mrp=Decimal('200'),
                               gst_percent=Decimal('18'), current_stock=50, center=other)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400)

    def test_a_held_bill_is_refused_at_completion_if_the_consumable_ran_out_meanwhile(self):
        held = self.api.post('/billing/invoices/', dict(self.bill(), status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        Product.objects.filter(pk=self.gel.pk).update(current_stock=0)      # used up while the bill waited
        total = held.data['total_amount']
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            dict(self.bill(), payments=[{'payment_method': 'Cash', 'amount': total}]),
                            format='json')
        self.assertEqual(done.status_code, 400, done.data)
        invoice = Invoice.objects.get(pk=held.data['id'])
        self.assertEqual(invoice.status, 'draft')
        self.assertEqual(invoice.payments.count(), 0)          # no payment kept for a refused bill
        self.assertEqual(self.stock(), Decimal('0'))

    def test_a_technical_item_in_a_service_is_deducted_and_checked_like_a_consumable(self):
        Product.objects.filter(pk=self.gel.pk).update(category='Technical', current_stock=0)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400)                      # no stock: refused, like any used item
        Product.objects.filter(pk=self.gel.pk).update(current_stock=10)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertLess(self.stock(), Decimal('10'))
        self.assertTrue(StockTransaction.objects.filter(transaction_type='BOM_CONSUMPTION').exists())

    def test_a_retail_product_is_not_part_of_a_service(self):
        Product.objects.filter(pk=self.gel.pk).update(category='Retail', current_stock=0)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(), Decimal('0'))
        self.assertFalse(StockTransaction.objects.filter(transaction_type='BOM_CONSUMPTION').exists())

    def test_a_consumable_category_is_deducted_and_checked(self):
        Product.objects.filter(pk=self.gel.pk).update(category='Consumable')
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(), Decimal('3'))
        Product.objects.filter(pk=self.gel.pk).update(current_stock=1)
        res = bill_and_pay(self.api, self.bill())
        self.assertEqual(res.status_code, 400)

    def test_a_service_with_no_consumables_recorded_is_not_checked(self):
        """Documents today's behaviour: the check follows the POS's own BOM list."""
        plain = ServiceMaster.objects.create(name='Threading', default_price=Decimal('100'),
                                             tax_percentage=Decimal('18'))
        Product.objects.filter(pk=self.gel.pk).update(current_stock=0)
        res = bill_and_pay(self.api, self.bill(service=plain))
        self.assertEqual(res.status_code, 201, res.data)
