"""Phase 3: bill integrity.

Pins the blueprint's Phase 3 billing rules so they cannot come back:
  * R19 / X1 - the browser cannot set a bill's status; it follows the money;
  * X2 - a completed bill's payments are not edited (only drafts are);
  * R20 - SAP sync fields are not writable;
  * X3 - a coupon's discount is in the bill once, and never above the offer;
  * #39 / #40 - no tips, no Nearbuy;
  * #45 / F19 - no refunds; cancel the same day only;
  * #47 - a bill whose sold card / package / membership / advance has been
    used cannot be cancelled; the advance it sold is taken back on cancel;
  * #23 - cancelling a bill cancels its appointment;
  * D13 / #42 - drafts can be deleted and expire after their day.
"""

import datetime
from decimal import Decimal

from django.utils import timezone

from appointments.models import Appointment
from billing.models import AdvancePayment, Invoice, InvoiceItem, Payment
from billing.tests import BillingTestBase
from clients.models import ClientMembership, ClientPackage, ClientValueCard
from marketing.models import Membership, Package, Promotion, PromotionUsage, ValueCard
from pos_backend.money import money


class StatusIsTheServersTests(BillingTestBase):
    def test_completing_without_payment_is_refused(self):
        """It used to store a 'paid' bill nobody paid for."""
        response = self.api.post('/billing/invoices/', self.build_payload(payments=[]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Take the payment', str(response.data))
        self.assertFalse(Invoice.objects.exists())

    def test_a_held_bill_takes_no_payment(self):
        response = self.api.post('/billing/invoices/', self.build_payload(
            status='draft', payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_status_follows_the_money(self):
        held = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        self.assertEqual(held.data['status'], 'draft')
        part = self.api.post('/billing/invoices/', self.build_payload(
            payments=[{'payment_method': 'Cash', 'amount': '400.00'}]), format='json')
        self.assertEqual(part.status_code, 400)          # paid in full (#37)
        full = self.create_invoice(self.build_payload())
        self.assertEqual(full.data['status'], 'paid')

    def test_a_patch_cannot_cancel_or_reopen_a_paid_bill(self):
        """PATCH {status: cancelled} cancelled with no stock or wallet reversal."""
        bill = self.create_invoice(self.build_payload(items=[self.product_line(qty=2)]))
        self.product.refresh_from_db()
        stock = self.product.current_stock
        for status_value in ('cancelled', 'draft', 'refunded'):
            result = self.api.patch(f"/billing/invoices/{bill.data['id']}/",
                                    {'status': status_value}, format='json')
            self.assertEqual(result.status_code, 400, status_value)
        self.assertEqual(Invoice.objects.get(pk=bill.data['id']).status, 'paid')
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, stock)

    def test_sap_sync_fields_cannot_be_written(self):
        held = self.api.post('/billing/invoices/', self.build_payload(
            status='draft', sap_sync_status=True, sap_sync_message='forged'), format='json')
        invoice = Invoice.objects.get(pk=held.data['id'])
        self.assertFalse(invoice.sap_sync_status)
        self.assertNotEqual(invoice.sap_sync_message, 'forged')

    def test_a_completed_bill_s_payments_cannot_be_edited(self):
        """X2: editing deleted payment rows without returning wallet money."""
        bill = self.create_invoice(self.build_payload())
        result = self.api.put(f"/billing/invoices/{bill.data['id']}/", self.build_payload(
            payments=[{'payment_method': 'Cash', 'amount': '1.00'}]), format='json')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(money(Invoice.objects.get(pk=bill.data['id']).paid_amount), Decimal('1050.00'))

    def test_nearbuy_is_refused(self):
        response = self.api.post('/billing/invoices/', self.build_payload(
            payments=[{'payment_method': 'NearBuy', 'amount': '1050.00'}]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('Nearbuy', str(response.data))


class CouponDiscountTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.promo = Promotion.objects.create(
            name='10% off', promo_type='Discount', discount_type='Percentage',
            discount_value=Decimal('10'), is_active=True,
            start_date=datetime.date.today() - datetime.timedelta(days=1),
            end_date=datetime.date.today() + datetime.timedelta(days=30))

    def test_the_coupon_discount_is_in_the_bill_once(self):
        """X3: the till's discount was applied, then the promotion added it again."""
        bill = self.create_invoice(self.build_payload(discount='100.00', promo_id=self.promo.id))
        invoice = Invoice.objects.get(pk=bill.data['id'])
        self.assertEqual(money(invoice.discount), Decimal('100.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('945.00'))   # (1000-100) * 1.05
        self.assertEqual(money(invoice.paid_amount), money(invoice.total_amount))
        self.assertTrue(PromotionUsage.objects.filter(promotion=self.promo, invoice=invoice).exists())

    def test_the_coupon_discount_is_worked_out_by_the_server(self):
        """Phase 6: the till sends the coupon, the server works out its value;
        a discount figure sent with it is ignored (X3 for good)."""
        response = self.api.post('/billing/invoices/', self.build_payload(
            discount='500.00', promo_id=self.promo.id,
            payments=[{'payment_method': 'Cash', 'amount': '945.00'}]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual((invoice.discount, invoice.total_amount, invoice.promotion_id),
                         (Decimal('100.00'), Decimal('945.00'), self.promo.id))

    def test_coupons_count_services_only(self):
        """#29 / D1: a product in the basket does not raise the coupon's value,
        and none of the discount lands on the product."""
        response = self.api.post('/billing/invoices/', self.build_payload(
            items=[self.service_line(), self.product_line()], discount='150.00',
            promo_id=self.promo.id, payments=[{'payment_method': 'Cash', 'amount': '1445.00'}]),
            format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.discount, Decimal('100.00'))
        shares = {i.content_type.model: i.bill_discount_share for i in invoice.items.all()}
        self.assertEqual(shares, {'servicemaster': Decimal('100.00'), 'product': Decimal('0.00')})

    def test_a_membership_discount_is_capped_and_recorded(self):
        gold = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'),
                                         discount_percent=Decimal('20'), expiry_days=30)
        cm = ClientMembership.objects.create(
            client=self.client_obj, membership=gold,
            expiry_date=datetime.date.today() + datetime.timedelta(days=30))
        # A claimed Rs 300 is ignored: 20% of the Rs 1000 service is Rs 200,
        # capped before any payment is written (X10).
        bill = self.create_invoice(self.build_payload(discount='300.00', membership_id=cm.id))
        invoice = Invoice.objects.get(pk=bill.data['id'])
        self.assertEqual((invoice.discount, invoice.total_amount, invoice.membership_used_id),
                         (Decimal('200.00'), Decimal('840.00'), cm.id))


class CancelRulesTests(BillingTestBase):
    def cancel(self, invoice_id):
        return self.api.post(f'/billing/invoices/{invoice_id}/cancel/', {'reason': 'error'}, format='json')

    def test_refund_is_refused(self):
        bill = self.create_invoice(self.build_payload())
        result = self.api.post(f"/billing/invoices/{bill.data['id']}/refund/", {'reason': 'x'}, format='json')
        self.assertEqual(result.status_code, 400)
        self.assertEqual(Invoice.objects.get(pk=bill.data['id']).status, 'paid')

    def test_the_cancellations_report_exports(self):
        """X40: the Excel export read an undefined variable and crashed."""
        bill = self.create_invoice(self.build_payload())
        self.cancel(bill.data['id'])
        result = self.api.get('/finance/api/refunds/?export=true')
        self.assertEqual(result.status_code, 200)

    def test_a_bill_from_an_earlier_day_cannot_be_cancelled(self):
        bill = self.create_invoice(self.build_payload())
        yesterday = timezone.now() - datetime.timedelta(days=1)
        Invoice.objects.filter(pk=bill.data['id']).update(finalized_at=yesterday, created_at=yesterday)
        result = self.cancel(bill.data['id'])
        self.assertEqual(result.status_code, 400)
        self.assertIn('only on the day it was made', str(result.data))

    def test_cancelling_cancels_the_appointment(self):
        appt = Appointment.objects.create(center=self.center, client=self.client_obj,
                                          client_phone='9990000001', client_name='Asha',
                                          date=datetime.date.today(), start_time=datetime.time(10, 0))
        bill = self.create_invoice(self.build_payload(appointment=appt.id))
        self.assertEqual(self.cancel(bill.data['id']).status_code, 200)
        appt.refresh_from_db()
        self.assertEqual(appt.status, 'Cancelled')

    def test_a_used_value_card_blocks_cancelling_the_bill_that_sold_it(self):
        bill = self.create_invoice(self.build_payload())
        card = ValueCard.objects.create(title='Card', pre_tax_price=Decimal('1000'), value=Decimal('1500'),
                                        expiry_days=90)
        sold = ClientValueCard.objects.create(
            client=self.client_obj, value_card=card, source_invoice_id=bill.data['id'],
            balance=Decimal('1000'), expiry_date=datetime.date.today() + datetime.timedelta(days=90))
        other = self.create_invoice(self.build_payload())
        Payment.objects.create(invoice_id=other.data['id'], amount=Decimal('100'),
                               payment_method='Value Card', value_card_id=sold.id)
        result = self.cancel(bill.data['id'])
        self.assertEqual(result.status_code, 400)
        self.assertIn('value card', str(result.data))

    def test_a_used_package_blocks_cancelling_the_bill_that_sold_it(self):
        bill = self.create_invoice(self.build_payload())
        pkg = Package.objects.create(name='5 cuts', price=Decimal('4000'), validity_days=90,
                                     services_json=[{'id': self.service.id, 'pkgQty': 5}])
        sold = ClientPackage.objects.create(
            client=self.client_obj, package=pkg, source_invoice_id=bill.data['id'],
            services_remaining={str(self.service.id): 4},
            expiry_date=datetime.date.today() + datetime.timedelta(days=90))
        other = self.create_invoice(self.build_payload())
        InvoiceItem.objects.create(invoice_id=other.data['id'], description='Haircut (redeemed)',
                                   unit_price=0, total_price=0, quantity=1, is_redemption=True,
                                   redeemed_from_package=sold)
        self.assertEqual(self.cancel(bill.data['id']).status_code, 400)

    def test_a_used_membership_blocks_cancelling_the_bill_that_sold_it(self):
        bill = self.create_invoice(self.build_payload())
        gold = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'),
                                         discount_percent=Decimal('20'), expiry_days=30)
        sold = ClientMembership.objects.create(
            client=self.client_obj, membership=gold, source_invoice_id=bill.data['id'],
            expiry_date=datetime.date.today() + datetime.timedelta(days=30))
        self.create_invoice(self.build_payload(discount='200.00', membership_id=sold.id))
        self.assertEqual(self.cancel(bill.data['id']).status_code, 400)

    def advance_line(self, amount='2000.00'):
        return {'content_type': 'billing.advancepayment', 'object_id': None,
                'description': 'Advance Payment', 'unit_price': amount, 'quantity': 1,
                'tax_percentage': '0', 'staff_members': [self.staff.id]}

    def test_cancelling_takes_back_the_advance_the_bill_sold(self):
        """The client kept the advance AND got the money back."""
        bill = self.create_invoice(self.build_payload(items=[self.advance_line()]))
        from billing.wallets import advance_balance
        self.assertEqual(advance_balance(self.client_obj.id), Decimal('2000.00'))
        self.assertEqual(self.cancel(bill.data['id']).status_code, 200)
        self.assertEqual(advance_balance(self.client_obj.id), Decimal('0.00'))

    def test_a_spent_advance_blocks_the_cancel(self):
        bill = self.create_invoice(self.build_payload(items=[self.advance_line()]))
        AdvancePayment.objects.create(client=self.client_obj, amount=Decimal('-1500.00'),
                                      notes='spent elsewhere')
        result = self.cancel(bill.data['id'])
        self.assertEqual(result.status_code, 400)
        self.assertIn('advance', str(result.data))


class StaffSplitTests(BillingTestBase):
    """#49: the contribution % chosen at the till drives each share."""

    def setUp(self):
        super().setUp()
        from staff.models import StaffMember
        self.second = StaffMember.objects.create(center=self.center, first_name='Sita',
                                                 designation='Stylist')

    def line(self, allocations):
        line = self.service_line()
        line['staff_members'] = [self.staff.id, self.second.id]
        line['staff_allocations'] = allocations
        return line

    def test_commission_follows_the_chosen_split(self):
        from staff.models import ServiceLog
        bill = self.create_invoice(self.build_payload(
            items=[self.line({str(self.staff.id): 70, str(self.second.id): 30})]))
        logs = {l.staff_id: money(l.price) for l in ServiceLog.all_objects.filter(invoice_id=bill.data['id'])}
        self.assertEqual(logs, {self.staff.id: Decimal('700.00'), self.second.id: Decimal('300.00')})
        item = Invoice.objects.get(pk=bill.data['id']).items.first()
        self.assertEqual(item.staff_allocations, {str(self.staff.id): '70.00', str(self.second.id): '30.00'})

    def test_a_split_that_is_not_100_percent_is_refused(self):
        response = self.api.post('/billing/invoices/', self.build_payload(
            status='draft', items=[self.line({str(self.staff.id): 70, str(self.second.id): 20})]),
            format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('90.00%, not 100%', str(response.data))

    def test_a_split_naming_someone_not_on_the_line_is_refused(self):
        response = self.api.post('/billing/invoices/', self.build_payload(
            status='draft', items=[self.line({str(self.staff.id): 50, '999999': 50})]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_thirds_are_accepted_and_no_paisa_is_lost(self):
        from staff.models import ServiceLog, StaffMember
        third = StaffMember.objects.create(center=self.center, first_name='Meena', designation='Stylist')
        line = self.line({})
        line['staff_members'] = [self.staff.id, self.second.id, third.id]
        line['staff_allocations'] = {str(self.staff.id): 33.33, str(self.second.id): 33.33,
                                     str(third.id): 33.33}
        bill = self.create_invoice(self.build_payload(items=[line]))
        total = sum(money(l.price) for l in ServiceLog.all_objects.filter(invoice_id=bill.data['id']))
        self.assertEqual(total, Decimal('1000.00'))


class StockGuardTests(BillingTestBase):
    """Over-receipt and never-negative stock (#52)."""

    def test_receiving_more_than_was_ordered_is_refused(self):
        from inventory.models import PurchaseOrder, PurchaseOrderItem, Vendor
        vendor = Vendor.objects.create(name='Loreal', center=self.center)
        po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Ordered')
        item = PurchaseOrderItem.objects.create(purchase_order=po, product=self.product, quantity=10,
                                                rate=Decimal('100'), total_price=Decimal('1000'))
        url = f'/inventory/api/purchase-orders/{po.id}/receive_grpo/'
        self.assertEqual(self.api.post(url, {'items': [{'id': item.id, 'received_quantity': 6}]},
                                       format='json').status_code, 200)
        over = self.api.post(url, {'items': [{'id': item.id, 'received_quantity': 5}]}, format='json')
        self.assertEqual(over.status_code, 400)
        self.assertIn('only 4', str(over.data))
        item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(item.received_quantity, 6)
        self.assertEqual(self.product.current_stock, 26)          # 20 + 6, nothing more

    def test_a_transfer_cannot_take_stock_below_zero(self):
        from inventory.models import StockTransfer, StockTransferItem
        transfer = StockTransfer.objects.create(from_center=self.center, to_center=self.other_center,
                                                status='Pending')
        StockTransferItem.objects.create(transfer=transfer, product=self.product, quantity=25)
        result = self.api.patch(f'/inventory/api/stock_transfers/{transfer.id}/',
                                {'status': 'In Transit'}, format='json')
        self.assertEqual(result.status_code, 400)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, 20)


class ServerOnlyFieldsTests(BillingTestBase):
    """R20: the browser cannot set the SAP sync result or a shift's close."""

    def test_a_product_cannot_be_marked_synced_from_the_browser(self):
        self.api.patch(f'/inventory/api/products/{self.product.id}/',
                       {'sap_sync_status': True, 'sap_sync_message': 'forged', 'name': 'Shampoo XL'},
                       format='json')
        self.product.refresh_from_db()
        self.assertEqual(self.product.name, 'Shampoo XL')       # ordinary fields still save
        self.assertFalse(self.product.sap_sync_status)
        self.assertNotEqual(self.product.sap_sync_message, 'forged')

    def test_a_shift_cannot_be_closed_or_rebalanced_by_an_edit(self):
        from finance.models import Shift
        shift = Shift.objects.create(center=self.center, starting_float=Decimal('1000'))
        self.api.patch(f'/finance/api/shifts/{shift.id}/',
                       {'status': 'Closed', 'variance': '0', 'expected_cash': '99999'}, format='json')
        shift.refresh_from_db()
        self.assertEqual(shift.status, 'Open')
        self.assertEqual(money(shift.expected_cash), Decimal('0.00'))


class DraftTests(BillingTestBase):
    def test_a_held_bill_can_be_deleted_and_a_completed_one_cannot(self):
        held = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        self.assertEqual(self.api.delete(f"/billing/invoices/{held.data['id']}/").status_code, 204)
        self.assertFalse(Invoice.objects.filter(pk=held.data['id']).exists())
        done = self.create_invoice(self.build_payload())
        self.assertEqual(self.api.delete(f"/billing/invoices/{done.data['id']}/").status_code, 405)

    def test_yesterday_s_drafts_expire_and_today_s_stay(self):
        from billing.background_jobs import expire_drafts
        old = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        new = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        Invoice.objects.filter(pk=old.data['id']).update(
            created_at=timezone.now() - datetime.timedelta(days=1))
        self.assertEqual(expire_drafts(), 1)
        self.assertFalse(Invoice.objects.filter(pk=old.data['id']).exists())
        self.assertTrue(Invoice.objects.filter(pk=new.data['id']).exists())

    def test_a_cashier_who_can_raise_bills_can_complete_a_held_one(self):
        """Found live: completing a held bill needed Edit, which cashiers lack."""
        from accounts.models import CustomUser
        from rest_framework.test import APIClient
        from salon_admin.models import Role
        role = Role.objects.create(name='Cashier', permissions={'billing': {
            'new_invoice': {'read': True, 'create': True}, 'invoices': {'read': True}}})
        cashier = CustomUser.objects.create_user(email='c@t.com', password='StrongPass!234',
                                                 full_name='C', role=role, center=self.center)
        cashier.centers.add(self.center)
        api = APIClient()
        api.force_authenticate(cashier)
        held = api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        done = api.put(f"/billing/invoices/{held.data['id']}/", self.build_payload(
            payments=[{'payment_method': 'Cash', 'amount': held.data['total_amount']}]), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data['status'], 'paid')
        # ...but a completed bill is still not editable.
        again = api.patch(f"/billing/invoices/{held.data['id']}/", {'notes': 'x'}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_completing_a_held_bill_finalises_it(self):
        held = self.api.post('/billing/invoices/', self.build_payload(
            status='draft', items=[self.product_line(qty=2)]), format='json')
        self.product.refresh_from_db()
        before = self.product.current_stock          # a draft moves no stock
        total = held.data['total_amount']
        done = self.api.put(f"/billing/invoices/{held.data['id']}/", self.build_payload(
            items=[self.product_line(qty=2)], payments=[{'payment_method': 'Cash', 'amount': total}]),
            format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(done.data['status'], 'paid')
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, before - 2)
