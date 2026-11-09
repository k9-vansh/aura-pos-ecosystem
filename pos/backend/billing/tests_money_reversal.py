"""
Money that was being lost on cancel, refund and overpayment.

Every case here was found by probing the running code, not by reading it, and
each one loses real money at a real till.

  BILL-01 (critical) A bill settled with TWO wallet payments returned only the
                     first on cancel. The customer permanently lost the rest of
                     their own prepaid balance, and the cancel returned 200.
  BILL-03            A cash payment larger than the bill was stored as
                     paid_amount, so the register could never be reconciled and
                     cancelling refunded the whole tendered amount.
  BILL-05            Deleting a ValueCard/Package/Membership TEMPLATE cascaded
                     away every customer's issued card, balance and entitlement.
  BILL-06            Cancel/refund DELETED the PromotionUsage row, so a
                     one-per-customer coupon could be farmed: bill, refund,
                     bill again, forever.
  BILL-11            Clawing back cashback the customer had already spent drove
                     their wallet balance negative, silently confiscating their
                     future earnings.
"""

import datetime
from pos_backend.testing import bill_and_pay
from decimal import Decimal

from django.db.models import Sum
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, CashbackTransaction, Invoice, InvoiceRefund
from clients.models import Client, ClientValueCard
from marketing.models import Promotion, PromotionUsage, ValueCard
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class MoneyReversalTestBase(TestCase):
    def setUp(self):
        from pos_backend.testing import pin_gate_off
        pin_gate_off()                      # these tests spend wallets without caring about the PIN
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('1000.00'),
            tax_percentage=Decimal('5.00'))
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def payload(self, payments):
        return {
            'client': self.customer.id,
            'center': self.center.id,
            'status': 'paid',
            'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id,
                'quantity': 1,
                'unit_price': '1000.00',
                'discount': '0',
                'tax_percentage': '5.00',
                'description': 'Haircut',
                'staff': self.staff.id,
            }],
            'payments': payments,
        }

    def advance_balance(self):
        return AdvancePayment.objects.filter(client=self.customer).aggregate(
            t=Sum('amount'))['t'] or Decimal('0')

    def cashback_balance(self):
        return CashbackTransaction.objects.filter(client=self.customer).aggregate(
            t=Sum('amount'))['t'] or Decimal('0')


class SplitWalletReversalTests(MoneyReversalTestBase):
    """BILL-01: the critical one."""

    def setUp(self):
        super().setUp()
        AdvancePayment.objects.create(
            client=self.customer, center=self.center,
            amount=Decimal('5000.00'), notes='top up')

    def test_a_bill_settled_with_two_wallet_payments_is_fully_returned(self):
        """The customer used to lose the second payment forever.

        `reverses` is set on the REVERSAL row, never on the debit, so the old
        `.filter(reverses__isnull=True).first()` kept finding the same debit and
        the second payment hit the "already reversed" branch.
        """
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Advance', 'amount': '500.00'},
            {'payment_method': 'Advance', 'amount': '550.00'},
        ]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice_id = response.data['id']
        self.assertEqual(self.advance_balance(), Decimal('3950.00'))

        cancelled = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                                  {'reason': 'customer changed mind'}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)

        # Was 4450: Rs 550 of the customer's own money destroyed.
        self.assertEqual(self.advance_balance(), Decimal('5000.00'))

    def test_three_wallet_payments_are_all_returned(self):
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Advance', 'amount': '300.00'},
            {'payment_method': 'Advance', 'amount': '300.00'},
            {'payment_method': 'Advance', 'amount': '450.00'},
        ]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.api.post(f'/billing/invoices/{response.data["id"]}/cancel/',
                      {'reason': 'x'}, format='json')
        self.assertEqual(self.advance_balance(), Decimal('5000.00'))

    def test_cancelling_twice_does_not_gift_the_balance_back_twice(self):
        """The protection the old code was reaching for must survive the fix."""
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Advance', 'amount': '1050.00'},
        ]), format='json')
        invoice_id = response.data['id']

        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'first'}, format='json')
        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'second'}, format='json')

        self.assertEqual(self.advance_balance(), Decimal('5000.00'))

    def test_the_debit_rows_are_never_deleted(self):
        """The ledger is append-only; a reversal is a new row."""
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Advance', 'amount': '1050.00'},
        ]), format='json')
        self.api.post(f'/billing/invoices/{response.data["id"]}/cancel/',
                      {'reason': 'x'}, format='json')

        rows = AdvancePayment.objects.filter(client=self.customer).order_by('id')
        self.assertEqual(rows.count(), 3)  # top-up, debit, reversal
        self.assertEqual([r.amount for r in rows],
                         [Decimal('5000.00'), Decimal('-1050.00'), Decimal('1050.00')])


class OverpaymentTests(MoneyReversalTestBase):
    """BILL-03."""

    def test_a_payment_larger_than_the_bill_is_refused(self):
        """The operator typed the Rs 5,000 note, not the Rs 1,050 bill.

        It used to be stored as paid_amount=5000 against total_amount=1050, and
        cancelling then wrote InvoiceRefund(amount=5000).
        """
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Cash', 'amount': '5000.00'},
        ]), format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('change given', str(response.data).lower())
        self.assertFalse(Invoice.objects.exists())

    def test_paying_exactly_the_bill_is_fine(self):
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Cash', 'amount': '1050.00'},
        ]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.paid_amount, Decimal('1050.00'))
        self.assertEqual(invoice.status, 'paid')

    def test_a_part_payment_is_refused(self):
        """#37: split tender is fine, but the bill is paid in full."""
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Cash', 'amount': '500.00'},
        ]), format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn('full amount', str(response.data))
        self.assertFalse(Invoice.objects.exists())

    def test_a_refund_can_never_exceed_what_was_taken(self):
        response = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Cash', 'amount': '1050.00'},
        ]), format='json')
        self.api.post(f'/billing/invoices/{response.data["id"]}/cancel/',
                      {'reason': 'x'}, format='json')

        refunded = InvoiceRefund.objects.aggregate(t=Sum('amount'))['t']
        self.assertEqual(refunded, Decimal('1050.00'))


class TemplateDeletionTests(MoneyReversalTestBase):
    """BILL-05: deleting a template used to wipe customers' stored value."""

    def test_a_value_card_template_with_issued_cards_cannot_be_deleted(self):
        template = ValueCard.objects.create(
            title='Gold', post_tax_price=Decimal('8000.00'),
            value=Decimal('10000.00'), expiry_days=365)
        ClientValueCard.objects.create(
            client=self.customer, value_card=template,
            balance=Decimal('10000.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

        with self.assertRaises(ProtectedError):
            template.delete()

        # The customer still has their money.
        self.assertEqual(
            ClientValueCard.objects.get(client=self.customer).balance,
            Decimal('10000.00'))

    def test_deleting_via_the_api_does_not_wipe_customer_balances(self):
        template = ValueCard.objects.create(
            title='Silver', post_tax_price=Decimal('1000.00'),
            value=Decimal('1200.00'), expiry_days=365)
        ClientValueCard.objects.create(
            client=self.customer, value_card=template,
            balance=Decimal('1200.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

        response = self.api.delete(f'/marketing/api/cards/{template.id}/')
        # Whatever the endpoint chooses to answer, the money must survive.
        self.assertNotEqual(response.status_code, 204)
        self.assertTrue(ClientValueCard.objects.filter(client=self.customer).exists())

    def test_an_unissued_template_can_still_be_deleted(self):
        template = ValueCard.objects.create(
            title='Unused', post_tax_price=Decimal('500.00'),
            value=Decimal('600.00'), expiry_days=365)
        template.delete()
        self.assertFalse(ValueCard.objects.filter(pk=template.pk).exists())


class PromotionReversalTests(MoneyReversalTestBase):
    """BILL-06 and BILL-11."""

    def promo(self, **overrides):
        return Promotion.objects.create(
            name='NEWYEAR20', promo_type='Discount', level='Organisation',
            discount_type='Percentage', discount_value=Decimal('20.00'),
            is_active=True,
            start_date=datetime.date.today() - datetime.timedelta(days=1),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
            **overrides)

    def test_refunds_are_not_offered_so_a_used_coupon_stays_used(self):
        """Owner decisions #45/F19: no refunds, same-day cancel only. The old
        refund path was also how a one-per-customer coupon could be farmed."""
        promo = self.promo(max_usage_per_client=1)

        first = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '840.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertEqual(first.status_code, 201, first.data)

        refunded = self.api.post(f'/billing/invoices/{first.data["id"]}/refund/',
                                 {'reason': 'unhappy'}, format='json')
        self.assertEqual(refunded.status_code, 400, refunded.data)
        self.assertNotEqual(Invoice.objects.get(pk=first.data['id']).status, 'refunded')

        usage = PromotionUsage.objects.filter(promotion=promo)
        self.assertEqual(usage.count(), 1)
        self.assertFalse(usage.first().is_reversed)

        # ...and it still consumes the allowance.
        again = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '840.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertEqual(again.status_code, 400, again.data)
        self.assertIn('limit', str(again.data).lower())

    def test_a_cancellation_does_release_the_allowance(self):
        """Nothing was delivered, so the customer keeps their entitlement."""
        promo = self.promo(max_usage_per_client=1)

        first = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '840.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertEqual(first.status_code, 201, first.data)

        self.api.post(f'/billing/invoices/{first.data["id"]}/cancel/',
                      {'reason': 'walked out'}, format='json')

        usage = PromotionUsage.objects.get(promotion=promo)
        self.assertTrue(usage.is_reversed)
        self.assertTrue(usage.releases_allowance)

        again = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '840.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertEqual(again.status_code, 201, again.data)

    def test_the_usage_record_is_never_deleted(self):
        """It used to vanish, taking the marketing ROI report with it."""
        promo = self.promo()
        first = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '840.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.api.post(f'/billing/invoices/{first.data["id"]}/cancel/',
                      {'reason': 'x'}, format='json')
        self.assertEqual(PromotionUsage.objects.filter(promotion=promo).count(), 1)

    def test_cashback_clawback_never_drives_the_wallet_negative(self):
        """BILL-11: award, customer spends it, cancel the awarding bill.

        The clawback used to write the full award regardless, leaving the wallet
        at -500 - which silently eats the customer's next top-up and hides a
        loss the salon has actually taken.
        """
        promo = Promotion.objects.create(
            name='CB50', promo_type='Cashback', level='Organisation',
            start_date=datetime.date.today() - datetime.timedelta(days=1),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
            config={'cashback_discount': '50', 'cashback_min_bill': '0',
                    'cashback_max': '0'})

        awarding = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '1050.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertEqual(awarding.status_code, 201, awarding.data)
        awarded = self.cashback_balance()
        self.assertGreater(awarded, Decimal('0'))

        # The customer spends all of it on a second bill.
        spend = bill_and_pay(self.api, self.payload([
            {'payment_method': 'Cashback Wallet', 'amount': str(awarded)},
            {'payment_method': 'Cash', 'amount': str(Decimal('1050.00') - awarded)},
        ]), format='json')
        self.assertEqual(spend.status_code, 201, spend.data)
        self.assertEqual(self.cashback_balance(), Decimal('0.00'))

        # Now cancel the bill that awarded it.
        cancelled = self.api.post(f'/billing/invoices/{awarding.data["id"]}/cancel/',
                                  {'reason': 'clawback'}, format='json')
        self.assertEqual(cancelled.status_code, 200, cancelled.data)

        # Was -500.
        self.assertGreaterEqual(
            self.cashback_balance(), Decimal('0.00'),
            'the clawback drove the customer wallet negative')

    def test_an_unspent_cashback_award_is_fully_clawed_back(self):
        promo = Promotion.objects.create(
            name='CB50b', promo_type='Cashback', level='Organisation',
            start_date=datetime.date.today() - datetime.timedelta(days=1),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
            config={'cashback_discount': '50', 'cashback_min_bill': '0',
                    'cashback_max': '0'})

        awarding = bill_and_pay(self.api, {
            **self.payload([{'payment_method': 'Cash', 'amount': '1050.00'}]),
            'promo_id': promo.id,
        }, format='json')
        self.assertGreater(self.cashback_balance(), Decimal('0'))

        self.api.post(f'/billing/invoices/{awarding.data["id"]}/cancel/',
                      {'reason': 'x'}, format='json')
        self.assertEqual(self.cashback_balance(), Decimal('0.00'))
