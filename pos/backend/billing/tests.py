"""
Invariant tests for invoice money math.

Each test names the audit finding it pins. These are the four invariants that
would have caught most of the report:

    sum(item.total_price)  == invoice.subtotal
    invoice.paid_amount    == sum(non-tip payments)
    product.current_stock  == sum(StockTransaction.quantity_change)
    payroll.net_pay        == base + commissions - deductions
"""

import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import (
    AdvancePayment, CashbackTransaction, Invoice, InvoiceItem,
    InvoiceNumberSequence, InvoiceRefund, Payment,
)
from billing import payment_methods as pm
from billing import pricing
from billing.serializers import InvoiceSerializer
from clients.models import Client, ClientPackage, ClientValueCard
from inventory.models import Product, StockTransaction
from marketing.models import Membership, Package, Promotion, ValueCard
from pos_backend.money import D, money, rupees, split
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import CenterService, ServiceMaster
from staff.models import ServiceLog, StaffMember


class BillingTestBase(TestCase):
    """Shared fixtures: one center, one owner, one client, a service and a product."""

    def setUp(self):
        from pos_backend.testing import pin_gate_off
        pin_gate_off()                      # these tests spend wallets without caring about the PIN
        self.center = Center.objects.create(center_name='Test Center', display_name='Test Center')
        self.other_center = Center.objects.create(center_name='Other Center')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )

        self.client_obj = Client.objects.create(
            first_name='Asha', last_name='Rao', phone='9990000001', center=self.center,
        )

        self.service = ServiceMaster.objects.create(
            name='Haircut', category='Cuts',
            default_price=Decimal('1000.00'), tax_percentage=Decimal('5.00'),
            incentive=Decimal('10.00'),
        )
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('500.00'), gst_percent=Decimal('18.00'),
            current_stock=20, center=self.center, incentive=Decimal('5.00'),
        )
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'), commission_percentage=Decimal('10.00'),
        )

        self.svc_ct = 'services.servicemaster'
        self.prod_ct = 'inventory.product'

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    # ─── helpers ──────────────────────────────────────────────────────────

    def build_payload(self, items=None, payments=None, **overrides):
        payload = {
            'client': self.client_obj.id,
            'center': self.center.id,
            'status': 'paid',
            'discount': '0.00',
            'items': items if items is not None else [self.service_line()],
        }
        if payments is not None:
            payload['payments'] = payments
        payload.update(overrides)
        return payload

    def service_line(self, qty=1, discount='0.00', unit_price='1000.00', **extra):
        line = {
            'content_type': self.svc_ct,
            'object_id': self.service.id,
            'description': 'Haircut',
            'unit_price': unit_price,
            'discount': discount,
            'quantity': qty,
            'staff_members': [self.staff.id],
        }
        line.update(extra)
        return line

    def product_line(self, qty=1, discount='0.00', unit_price='500.00', **extra):
        line = {
            'content_type': self.prod_ct,
            'object_id': self.product.id,
            'description': 'Shampoo',
            'unit_price': unit_price,
            'discount': discount,
            'quantity': qty,
            'staff_members': [self.staff.id],
        }
        line.update(extra)
        return line

    def create_invoice(self, payload, expect_status=201):
        from pos_backend.testing import bill_and_pay
        response = (bill_and_pay(self.api, payload) if expect_status == 201
                    else self.api.post('/billing/invoices/', payload, format='json'))
        self.assertEqual(
            response.status_code, expect_status,
            msg=f'unexpected {response.status_code}: {getattr(response, "data", None)}',
        )
        return response

    def assert_items_sum_to_subtotal(self, invoice):
        """INVARIANT: sum(item.total_price) == invoice.subtotal (H2)."""
        line_sum = money(sum(D(i.total_price) for i in invoice.items.all()))
        self.assertEqual(
            line_sum, money(invoice.subtotal),
            f'sum(items)={line_sum} != subtotal={invoice.subtotal}',
        )

    def assert_tax_matches_items(self, invoice):
        """INVARIANT: cgst + sgst == sum(item.tax_amount)."""
        item_tax = money(sum(D(i.tax_amount) for i in invoice.items.all()))
        header_tax = money(D(invoice.cgst) + D(invoice.sgst))
        self.assertEqual(item_tax, header_tax,
                         f'sum(item tax)={item_tax} != cgst+sgst={header_tax}')

    def assert_paid_matches_payments(self, invoice):
        """INVARIANT: paid_amount == sum(non-tip payments) (H11)."""
        paid = money(sum(D(p.amount) for p in invoice.payments.filter(is_tip=False)))
        self.assertEqual(paid, money(invoice.paid_amount))

    def assert_stock_matches_ledger(self, product, opening):
        """INVARIANT: current_stock == opening + sum(ledger deltas) (H17)."""
        product.refresh_from_db()
        ledger = sum(t.quantity_change for t in StockTransaction.objects.filter(product=product))
        self.assertEqual(
            product.current_stock, opening + ledger,
            f'stock={product.current_stock} != opening({opening}) + ledger({ledger})',
        )


class MoneyHelperTests(TestCase):
    """pos_backend.money - the primitives everything else depends on."""

    def test_rupees_rounds_half_up_not_bankers(self):
        """H1: Python round() gave banker's rounding, so Rs 100.50 stored as 100."""
        cases = [('100.50', '101'), ('101.50', '102'), ('102.50', '103'),
                 ('103.50', '104'), ('2360.50', '2361'), ('100.49', '100')]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(rupees(raw), Decimal(expected))

    def test_rupees_matches_javascript_math_round(self):
        """The frontend uses Math.round; the stored total must agree with it."""
        for cents in range(0, 400):
            raw = Decimal(cents) / Decimal('4')  # hits .00 .25 .50 .75
            js = Decimal(str(int(raw + Decimal('0.5'))))  # Math.round for positives
            self.assertEqual(rupees(raw), js, f'divergence at {raw}')

    def test_D_never_raises_on_bad_input(self):
        """C4: Decimal(str(None)) raised InvalidOperation and 500'd checkout."""
        for bad in [None, '', 'null', 'abc', 'NaN', 'Infinity', '-Infinity', [], {}]:
            with self.subTest(bad=bad):
                self.assertEqual(D(bad), Decimal('0.00'))

    def test_D_rejects_non_finite_decimals(self):
        self.assertEqual(D(Decimal('NaN')), Decimal('0.00'))
        self.assertEqual(D(Decimal('Infinity')), Decimal('0.00'))

    def test_split_loses_no_paise(self):
        """M18: Rs 100 / 3 stored at 2dp gave 33.33 x 3 = 99.99."""
        for total in ['100.00', '0.01', '1000.00', '33.33', '7.77']:
            for parts in range(1, 8):
                with self.subTest(total=total, parts=parts):
                    shares = split(total, parts)
                    self.assertEqual(len(shares), parts)
                    self.assertEqual(money(sum(shares)), money(total))

    def test_split_handles_zero_and_negative_parts(self):
        self.assertEqual(split('100.00', 0), [])
        self.assertEqual(split('100.00', -1), [])


class PaymentMethodClassificationTests(TestCase):
    """C7 / C8 - substring matching put wallet money in the wrong bucket."""

    def test_cashback_wallet_is_not_cash(self):
        """C7: 'cash' in 'cashback wallet' booked wallet redemptions as cash."""
        self.assertEqual(pm.report_bucket('Cashback Wallet'), 'cashback_wallet')
        self.assertEqual(pm.report_bucket('Cash'), 'cash')
        self.assertNotEqual(pm.report_bucket('Cashback Wallet'), 'cash')

    def test_cashback_wallet_does_not_route_to_advance(self):
        """C8: 'wallet' in 'cashback wallet' debited the advance ledger."""
        self.assertTrue(pm.is_cashback('Cashback Wallet'))
        self.assertFalse(pm.is_advance('Cashback Wallet'))
        self.assertTrue(pm.is_advance('Advance'))
        self.assertFalse(pm.is_cashback('Advance'))

    def test_every_method_buckets_exactly_once(self):
        seen = {}
        for method, _label in Payment.PAYMENT_METHOD_CHOICES:
            bucket = pm.report_bucket(method)
            self.assertIn(bucket, pm.REPORT_BUCKETS)
            seen[method] = bucket
        self.assertEqual(seen[Payment.CASH], 'cash')
        self.assertEqual(seen[Payment.CASHBACK_WALLET], 'cashback_wallet')
        self.assertEqual(seen[Payment.NET_BANKING], 'cheque_net_banking')
        self.assertEqual(seen[Payment.NEARBUY], 'nearbuy')

    def test_unknown_method_never_touches_a_wallet(self):
        self.assertEqual(pm.canonical('Bitcoin'), Payment.OTHER)
        self.assertFalse(pm.is_advance('Bitcoin'))
        self.assertFalse(pm.is_cashback('Bitcoin'))
        self.assertFalse(pm.is_value_card('Bitcoin'))

    def test_liability_methods_flagged(self):
        """H25: value card / cashback / advance are not new money."""
        for method in ['Advance', 'Value Card', 'Cashback Wallet']:
            self.assertTrue(pm.is_liability_drawdown(method), method)
        for method in ['Cash', 'UPI', 'Credit Card', 'Cheque']:
            self.assertFalse(pm.is_liability_drawdown(method), method)

    def test_legacy_value_card_id_still_parses(self):
        """H34b: a NaN parse client-side meant the card was never debited."""
        self.assertEqual(pm.extract_value_card_id('Value Card 42', None), 42)
        self.assertEqual(pm.extract_value_card_id('Value Card', 7), 7)
        self.assertIsNone(pm.extract_value_card_id('Value Card', None))
        self.assertEqual(pm.extract_value_card_id('Value Card 42', 'null'), 42)


class CatalogPricingTests(BillingTestBase):
    """C1 - the content-type typo that zeroed every service line."""

    def test_service_content_type_resolves(self):
        """C1: gated on 'service', but the ContentType model is 'servicemaster'."""
        ct = ContentType.objects.get_for_model(ServiceMaster)
        self.assertEqual(ct.model, 'servicemaster')
        self.assertEqual(pricing.content_type_label(ct), pricing.CT_SERVICE)

    def test_service_price_is_not_zero(self):
        resolved = pricing.resolve(self.svc_ct, self.service.id, center=self.center)
        self.assertTrue(resolved.resolved)
        self.assertEqual(resolved.list_price, Decimal('1000.00'))
        self.assertEqual(resolved.tax_percentage, Decimal('5.00'))

    def test_center_service_price_override_applies(self):
        """C1 also disabled the per-center price override."""
        CenterService.objects.create(
            center=self.center, service=self.service,
            price=Decimal('1500.00'), incentive=Decimal('20.00'), is_active=True,
        )
        resolved = pricing.resolve(self.svc_ct, self.service.id, center=self.center)
        self.assertEqual(resolved.list_price, Decimal('1500.00'))
        self.assertEqual(resolved.incentive_percent, Decimal('20.00'))

    def test_center_override_does_not_leak_to_other_centers(self):
        CenterService.objects.create(
            center=self.center, service=self.service, price=Decimal('1500.00'), is_active=True,
        )
        resolved = pricing.resolve(self.svc_ct, self.service.id, center=self.other_center)
        self.assertEqual(resolved.list_price, Decimal('1000.00'))

    def test_product_with_null_price_does_not_raise(self):
        """A null price used to raise InvalidOperation and skip enforcement."""
        p = Product.objects.create(name='Freebie', price=None, center=self.center)
        resolved = pricing.resolve(self.prod_ct, p.id, center=self.center)
        self.assertTrue(resolved.resolved)
        self.assertEqual(resolved.list_price, Decimal('0.00'))

    def test_membership_tax_derived_from_price_pair(self):
        """M29: memberships had no tax column and were billed at 0% GST."""
        m = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1180.00'), expiry_days=365,
        )
        resolved = pricing.resolve('marketing.membership', m.id)
        self.assertEqual(resolved.list_price, Decimal('1000.00'))
        self.assertEqual(resolved.tax_percentage, Decimal('18.00'))

    def test_unresolvable_item_falls_back_to_client_price(self):
        resolved = pricing.resolve(self.svc_ct, 999999, center=self.center)
        self.assertFalse(resolved.resolved)
        unit, disc, _note = pricing.reconcile_line_price(
            resolved, Decimal('750.00'), Decimal('0.00'), self.svc_ct
        )
        self.assertEqual(unit, Decimal('750.00'))


class InvoiceForgeryTests(BillingTestBase):
    """C2 / C3 - the header used to be whatever the client claimed."""

    # Owner decision #25 (UNDERSTANDING_LOG.md): the cashier may set any price
    # and that price is final - it is billed and sent to SAP. These tests used
    # to expect the catalog price to be forced back in. Today every override is
    # written to the bill's change log; from Phase 5 an override above the
    # role's free limit also needs an approval (tested there).

    def _price_log(self, invoice_id):
        from billing.models import BillChangeLog
        log = BillChangeLog.objects.filter(invoice_id=invoice_id, action='Price Adjusted').first()
        self.assertIsNotNone(log, 'a price override must be written to the change log')
        return log.notes

    def test_cashier_price_below_catalog_is_billed_and_audited(self):
        payload = self.build_payload(
            items=[self.service_line(unit_price='1.00')],
            subtotal='1.00', cgst='0.03', sgst='0.03', total_amount='1.00',
        )
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        self.assertEqual(money(invoice.items.first().unit_price), Decimal('1.00'))
        self.assertEqual(money(invoice.subtotal), Decimal('1.00'))
        self.assert_items_sum_to_subtotal(invoice)
        self.assertIn('overridden to Rs 1.00', self._price_log(invoice.pk))

    def test_zeroed_tax_is_recomputed_not_accepted(self):
        """C3: cgst/sgst came straight from the client, unvalidated."""
        payload = self.build_payload(cgst='0.00', sgst='0.00', total_amount='1000.00')
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        self.assertEqual(money(invoice.cgst), Decimal('25.00'))
        self.assertEqual(money(invoice.sgst), Decimal('25.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('1050.00'))

    def test_inflated_tax_is_recomputed(self):
        payload = self.build_payload(cgst='9999.00', sgst='9999.00')
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('50.00'))

    def test_cashier_price_above_catalog_is_billed_and_audited(self):
        payload = self.build_payload(items=[self.service_line(unit_price='1200.00')])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        item = invoice.items.first()
        self.assertEqual(money(item.unit_price), Decimal('1200.00'))
        self.assertEqual(money(item.total_price), Decimal('1200.00'))
        self.assertIn('overridden to Rs 1200.00', self._price_log(invoice.pk))

    def test_cashier_price_is_kept_exactly_and_tax_follows_it(self):
        payload = self.build_payload(items=[self.service_line(unit_price='800.00')])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        item = invoice.items.first()

        self.assertEqual(money(item.unit_price), Decimal('800.00'))
        self.assertEqual(money(item.discount), Decimal('0.00'))
        self.assertEqual(money(item.total_price), Decimal('800.00'))
        # 5% GST on the price actually charged, split exactly.
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('40.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('840.00'))
        self.assertIn('overridden to Rs 800.00', self._price_log(invoice.pk))

    def test_negotiated_price_works_through_the_discount_field(self):
        """A real reduction still works - the POS sends it as a discount."""
        payload = self.build_payload(items=[self.service_line(
            unit_price='1000.00', discount='200.00',
        )])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        item = invoice.items.first()

        self.assertEqual(money(item.unit_price), Decimal('1000.00'))
        self.assertEqual(money(item.discount), Decimal('200.00'))
        self.assertEqual(money(item.total_price), Decimal('800.00'))
        self.assert_items_sum_to_subtotal(invoice)

    def test_per_unit_discount_scales_with_quantity(self):
        """Two units at Rs 800 each must bill Rs 1600, not Rs 1800."""
        payload = self.build_payload(items=[self.service_line(
            qty=2, unit_price='1000.00', discount='400.00',
        )])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.subtotal), Decimal('1600.00'))

    def test_deep_discount_is_written_to_the_change_log(self):
        from billing.models import BillChangeLog

        payload = self.build_payload(items=[self.service_line(
            unit_price='1000.00', discount='900.00',
        )])
        response = self.create_invoice(payload)
        logs = BillChangeLog.objects.filter(
            invoice_id=response.data['id'], action='Price Adjusted',
        )
        self.assertTrue(logs.exists())
        self.assertIn('90.00% discount', logs.first().notes)

    def test_negative_quantity_is_rejected(self):
        payload = self.build_payload(items=[self.service_line(qty=0)])
        self.create_invoice(payload, expect_status=400)

    def test_discount_above_line_value_is_rejected(self):
        payload = self.build_payload(items=[self.service_line(discount='5000.00')])
        self.create_invoice(payload, expect_status=400)


class TaxConventionTests(BillingTestBase):
    """H2 / H31 / H13 - tax base and storage convention."""

    def test_line_total_is_tax_exclusive(self):
        """H2: total_price was tax-inclusive while subtotal was tax-exclusive."""
        response = self.create_invoice(self.build_payload())
        invoice = Invoice.objects.get(pk=response.data['id'])
        item = invoice.items.first()

        self.assertEqual(money(item.total_price), Decimal('1000.00'))
        self.assertEqual(money(item.tax_amount), Decimal('50.00'))
        self.assertEqual(item.gross_total, Decimal('1050.00'))
        self.assert_items_sum_to_subtotal(invoice)
        self.assert_tax_matches_items(invoice)

    def test_tax_is_charged_on_the_post_discount_value(self):
        """H31: a fully-discounted bill still carried GST."""
        payload = self.build_payload(discount='1000.00')
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        self.assertEqual(money(invoice.discount), Decimal('1000.00'))
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('0.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('0.00'))

    def test_partial_global_discount_reduces_tax_proportionally(self):
        payload = self.build_payload(
            items=[self.service_line(), self.product_line()], discount='300.00',
        )
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        # Product MRP 500 contains its 18% GST (decision F22): taxable 423.73.
        # subtotal 1000 + 423.73 = 1423.73; the bill discount is pre-tax and
        # apportioned by taxable value -> 210.71 / 89.29.
        # service: 789.29 @5% = 39.46; product: 334.44 @18% = 60.20.
        self.assertEqual(money(invoice.subtotal), Decimal('1423.73'))
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('99.66'))
        self.assert_tax_matches_items(invoice)
        self.assert_items_sum_to_subtotal(invoice)

    def test_cgst_and_sgst_split_without_losing_a_paisa(self):
        # 5% on 999.00 = 49.95 -> halves must be 24.98 / 24.97
        self.service.default_price = Decimal('999.00')
        self.service.save()
        payload = self.build_payload(items=[self.service_line(unit_price='999.00')])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('49.95'))
        self.assert_tax_matches_items(invoice)

    def test_rounding_column_reconciles(self):
        """total_amount - (subtotal - discount + tax) == rounding."""
        self.service.default_price = Decimal('990.00')
        self.service.save()
        payload = self.build_payload(items=[self.service_line(unit_price='990.00')])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])

        raw = money(D(invoice.subtotal) - D(invoice.discount)
                    + D(invoice.cgst) + D(invoice.sgst))
        self.assertEqual(money(D(raw) + D(invoice.rounding)), money(invoice.total_amount))
        # 990 + 49.50 = 1039.50 -> half-up -> 1040
        self.assertEqual(money(invoice.total_amount), Decimal('1040.00'))


class ManagerDiscountTests(BillingTestBase):
    """C9 - the same value arrived as discount AND manager_discount."""

    def test_manager_discount_is_not_deducted_twice(self):
        """C9: base_total = gross - disc - mgr_disc took Rs 200 off twice.

        The manager discount labels part of `discount`; it is not a second
        reduction. (This used to send a pre-reduced unit price of 800 and rely
        on the server forcing the list price back - decision #25 removed that.)
        """
        payload = self.build_payload(items=[self.service_line(
            unit_price='1000.00', discount='200.00', manager_discount='200.00',
        )])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        item = invoice.items.first()

        # Rs 1000 list, Rs 200 discount -> Rs 800 taxable. Not Rs 600.
        self.assertEqual(money(item.total_price), Decimal('800.00'))
        self.assertEqual(money(item.discount), Decimal('200.00'))
        self.assertEqual(money(item.manager_discount), Decimal('200.00'))
        self.assert_items_sum_to_subtotal(invoice)

    def test_manager_discount_cannot_exceed_the_discount(self):
        payload = self.build_payload(items=[self.service_line(
            unit_price='900.00', discount='100.00', manager_discount='900.00',
        )])
        response = self.create_invoice(payload)
        item = Invoice.objects.get(pk=response.data['id']).items.first()
        self.assertLessEqual(money(item.manager_discount), money(item.discount))


class ZeroTotalInvoiceTests(BillingTestBase):
    """C5 - a zero-total bill with staff assigned raised TypeError."""

    def test_fully_discounted_invoice_finalises(self):
        """C5: _vc_incentive_ratio was the float 1.0, then Decimal * float."""
        payload = self.build_payload(discount='1000.00', payments=[])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.total_amount), Decimal('0.00'))

    def test_complimentary_invoice_creates_zero_value_service_logs(self):
        payload = self.build_payload(discount='1000.00', payments=[])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        invoice.status = 'paid'
        invoice.save(update_fields=['status'])
        # No exception is the assertion; commissions on a free bill are zero.
        logs = ServiceLog.all_objects.filter(invoice=invoice)
        for log in logs:
            self.assertGreaterEqual(money(log.price), Decimal('0.00'))


class WalletRoutingTests(BillingTestBase):
    """C8 / H34b / M35b - which ledger a payment actually moves."""

    def setUp(self):
        super().setUp()
        AdvancePayment.objects.create(
            client=self.client_obj, amount=Decimal('2000.00'), notes='top up',
        )
        CashbackTransaction.objects.create(
            client=self.client_obj, amount=Decimal('300.00'), notes='earned',
        )

    def test_cashback_payment_debits_cashback_not_advance(self):
        """C8: paying by Cashback Wallet used to debit the advance balance."""
        payload = self.build_payload(payments=[
            {'payment_method': 'Cashback Wallet', 'amount': '300.00'},
            {'payment_method': 'Cash', 'amount': '750.00'},
        ])
        self.create_invoice(payload)

        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.cashback_balance, Decimal('0.00'))
        self.assertEqual(self.client_obj.advance_balance, Decimal('2000.00'))

    def test_advance_payment_debits_advance(self):
        payload = self.build_payload(payments=[
            {'payment_method': 'Advance', 'amount': '1050.00'},
        ])
        self.create_invoice(payload)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('950.00'))
        self.assertEqual(self.client_obj.cashback_balance, Decimal('300.00'))

    def test_overdrawing_advance_is_rejected_not_capped(self):
        """The old code capped the deduction and recorded the full payment."""
        payload = self.build_payload(payments=[
            {'payment_method': 'Advance', 'amount': '5000.00'},
        ])
        self.create_invoice(payload, expect_status=400)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('2000.00'))

    def test_two_advance_payments_are_checked_against_one_balance(self):
        """Aggregation matters: 2 x Rs 1500 must not both pass on Rs 2000."""
        self.service.default_price = Decimal('3000.00')
        self.service.save()
        payload = self.build_payload(
            items=[self.service_line(unit_price='3000.00')],
            payments=[
                {'payment_method': 'Advance', 'amount': '1500.00'},
                {'payment_method': 'Advance', 'amount': '1500.00'},
            ],
        )
        self.create_invoice(payload, expect_status=400)

    def test_walkin_cannot_pay_by_advance(self):
        """M35b: invoice.client was None -> AttributeError -> 500."""
        payload = self.build_payload(
            client=None,
            payments=[{'payment_method': 'Advance', 'amount': '1050.00'}],
        )
        self.create_invoice(payload, expect_status=400)

    def test_value_card_cannot_be_typed_in_as_a_payment(self):
        """H34b: a missing id silently skipped the debit - free money. A card
        is now chosen on the bill, never entered as a payment amount."""
        payload = self.build_payload(payments=[
            {'payment_method': 'Value Card', 'amount': '1050.00'},
        ])
        self.create_invoice(payload, expect_status=400)

    def test_value_card_debits_correctly(self):
        """F5: the card pays for the service at its face value; the bill
        carries the card's benefit and the card's GST on the rest."""
        card_def = ValueCard.objects.create(
            title='Elite', pre_tax_price=Decimal('5000.00'),
            value=Decimal('6000.00'), expiry_days=365, gst_percent=Decimal('5.00'),
        )
        card = ClientValueCard.objects.create(
            client=self.client_obj, value_card=card_def,
            balance=Decimal('6000.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        )
        response = self.create_invoice(self.build_payload(value_card_id=card.id))
        card.refresh_from_db()
        self.assertEqual(money(card.balance), Decimal('5000.00'))
        invoice = Invoice.objects.get(pk=response.data['id'])
        # Rs 1000 of face value buys 1000 x 5000/6000 = 833.33 taxable + 5%.
        pay = invoice.payments.get()
        self.assertEqual((pay.payment_method, pay.card_face, pay.amount),
                         ('Value Card', Decimal('1000.00'), Decimal('875.00')))
        self.assertEqual(invoice.status, 'paid')

    def test_value_card_belonging_to_another_client_is_rejected(self):
        other = Client.objects.create(first_name='Other', phone='9990000002', center=self.center)
        card_def = ValueCard.objects.create(
            title='Luxe', pre_tax_price=Decimal('1000.00'),
            value=Decimal('1200.00'), expiry_days=365,
        )
        card = ClientValueCard.objects.create(
            client=other, value_card=card_def, balance=Decimal('1200.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        )
        self.create_invoice(self.build_payload(value_card_id=card.id), expect_status=400)


class TipTests(BillingTestBase):
    """Owner decision #39: no tips. Tips taken before are kept and readable."""

    def test_a_tip_payment_is_refused(self):
        payload = self.build_payload(payments=[
            {'payment_method': 'Cash', 'amount': '1050.00'},
            {'payment_method': 'Cash', 'amount': '100.00', 'is_tip': True},
        ])
        response = self.create_invoice(payload, expect_status=400)
        self.assertIn('Tips are not taken', str(response.data))

    def test_a_declared_tip_is_ignored_and_paying_more_than_the_bill_is_refused(self):
        payload = self.build_payload(
            tip_amount='100.00',
            payments=[{'payment_method': 'Cash', 'amount': '1150.00'}],
        )
        self.create_invoice(payload, expect_status=400)
        response = self.create_invoice(self.build_payload(
            tip_amount='100.00', payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.tip_amount), Decimal('0.00'))
        self.assertEqual(invoice.status, 'paid')

    def test_the_pay_action_refuses_a_tip(self):
        response = self.create_invoice(self.build_payload(status='draft'))
        result = self.api.post(f"/billing/invoices/{response.data['id']}/pay/",
                               {'amount': '50.00', 'payment_method': 'Cash', 'is_tip': True},
                               format='json')
        self.assertEqual(result.status_code, 400)

    def test_an_old_bill_s_tip_still_counts_in_what_was_collected(self):
        response = self.create_invoice(self.build_payload())
        Invoice.objects.filter(pk=response.data['id']).update(tip_amount=Decimal('100.00'))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.collectable_total, money(invoice.total_amount) + Decimal('100.00'))


class StockTests(BillingTestBase):
    """H17 - current_stock and the ledger must not drift apart."""

    def test_sale_deducts_stock_and_logs_the_applied_delta(self):
        opening = self.product.current_stock
        payload = self.build_payload(items=[self.product_line(qty=3)])
        self.create_invoice(payload)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, opening - 3)
        self.assert_stock_matches_ledger(self.product, opening)

    def test_selling_beyond_stock_is_refused_and_moves_nothing(self):
        """Owner decision #52: nothing is sold without stock (this used to
        assert that an oversell went through)."""
        self.product.current_stock = 2
        self.product.save()
        opening = 2
        payload = self.build_payload(items=[self.product_line(qty=5)])
        self.create_invoice(payload, expect_status=400)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, opening)
        self.assert_stock_matches_ledger(self.product, opening)

    def test_cancel_restores_stock_and_balances_the_ledger(self):
        opening = self.product.current_stock
        response = self.create_invoice(self.build_payload(items=[self.product_line(qty=4)]))
        invoice_id = response.data['id']

        cancel = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                               {'reason': 'customer changed mind'}, format='json')
        self.assertEqual(cancel.status_code, 200, cancel.data)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, opening)
        self.assert_stock_matches_ledger(self.product, opening)


class IdempotencyTests(BillingTestBase):
    """finalize_invoice used to say "call once" with nothing enforcing it."""

    def test_finalize_twice_does_not_double_deduct_stock(self):
        from billing.services import finalize_invoice
        from django.db import transaction

        opening = self.product.current_stock
        response = self.create_invoice(self.build_payload(items=[self.product_line(qty=2)]))
        invoice = Invoice.objects.get(pk=response.data['id'])

        with transaction.atomic():
            finalize_invoice(invoice, None, {}, skip_payment_deductions=True)

        self.product.refresh_from_db()
        self.assertEqual(self.product.current_stock, opening - 2)

    def test_finalize_twice_does_not_double_issue_a_membership(self):
        from billing.services import finalize_invoice
        from clients.models import ClientMembership
        from django.db import transaction

        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1000.00'), expiry_days=365,
        )
        payload = self.build_payload(items=[{
            'content_type': 'marketing.membership',
            'object_id': membership.id,
            'description': 'Gold',
            'unit_price': '1000.00',
            'quantity': 1,
            'staff_members': [self.staff.id],
        }])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(ClientMembership.objects.filter(client=self.client_obj).count(), 1)

        with transaction.atomic():
            finalize_invoice(invoice, None, {}, skip_payment_deductions=True)
        self.assertEqual(ClientMembership.objects.filter(client=self.client_obj).count(), 1)


class PackageRedemptionTests(BillingTestBase):
    """H19 / H20 / H22 - redemption no longer depends on an emoji."""

    def setUp(self):
        super().setUp()
        self.pkg_def = Package.objects.create(
            name='5 Haircuts', price=Decimal('4000.00'), validity_days=180,
            services_json=[{'id': self.service.id, 'pkgQty': 5}],
        )
        self.client_pkg = ClientPackage.objects.create(
            client=self.client_obj, package=self.pkg_def,
            services_remaining={str(self.service.id): 5},
            expiry_date=datetime.date.today() + datetime.timedelta(days=180),
        )

    def redemption_line(self, qty=1, use_marker=False):
        line = self.service_line(qty=qty, unit_price='0.00')
        if use_marker:
            line['description'] = '\U0001F381 [Redeem] Haircut'
        else:
            line['is_redemption'] = True
            line['description'] = 'Haircut (redeemed)'
        return line

    def test_redemption_deducts_from_the_package(self):
        payload = self.build_payload(items=[self.redemption_line(qty=2)], payments=[])
        self.create_invoice(payload)
        self.client_pkg.refresh_from_db()
        self.assertEqual(int(self.client_pkg.services_remaining[str(self.service.id)]), 3)

    def test_legacy_emoji_marker_still_deducts(self):
        """H19: older clients still send the marker rather than the flag."""
        payload = self.build_payload(items=[self.redemption_line(qty=1, use_marker=True)],
                                     payments=[])
        self.create_invoice(payload)
        self.client_pkg.refresh_from_db()
        self.assertEqual(int(self.client_pkg.services_remaining[str(self.service.id)]), 4)

    def test_redemption_contributes_nothing_to_the_total(self):
        payload = self.build_payload(items=[self.redemption_line(qty=1)], payments=[])
        response = self.create_invoice(payload)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.total_amount), Decimal('0.00'))
        self.assertEqual(money(invoice.subtotal), Decimal('0.00'))

    def test_over_redemption_is_rejected_without_payments(self):
        """H20: the check sat inside `if payments and client:` and was skipped."""
        payload = self.build_payload(items=[self.redemption_line(qty=99)], payments=[])
        self.create_invoice(payload, expect_status=400)
        self.client_pkg.refresh_from_db()
        self.assertEqual(int(self.client_pkg.services_remaining[str(self.service.id)]), 5)

    def test_redemption_records_its_source_package(self):
        payload = self.build_payload(items=[self.redemption_line(qty=1)], payments=[])
        response = self.create_invoice(payload)
        item = Invoice.objects.get(pk=response.data['id']).items.first()
        self.assertEqual(item.redeemed_from_package_id, self.client_pkg.id)

    def test_cancel_restores_to_the_same_package(self):
        """H22: the reversal credited the first matching package, not the debited one."""
        response = self.create_invoice(
            self.build_payload(items=[self.redemption_line(qty=2)], payments=[])
        )
        invoice_id = response.data['id']
        self.client_pkg.refresh_from_db()
        self.assertEqual(int(self.client_pkg.services_remaining[str(self.service.id)]), 3)

        cancel = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                               {'reason': 'billing error'}, format='json')
        self.assertEqual(cancel.status_code, 200, cancel.data)

        self.client_pkg.refresh_from_db()
        self.assertEqual(int(self.client_pkg.services_remaining[str(self.service.id)]), 5)

    def test_cancel_does_not_resurrect_an_expired_package(self):
        response = self.create_invoice(
            self.build_payload(items=[self.redemption_line(qty=1)], payments=[])
        )
        invoice_id = response.data['id']
        self.client_pkg.refresh_from_db()
        self.client_pkg.expiry_date = datetime.date.today() - datetime.timedelta(days=1)
        self.client_pkg.is_active = False
        self.client_pkg.save()

        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'error'}, format='json')
        self.client_pkg.refresh_from_db()
        self.assertFalse(self.client_pkg.is_active)


class MembershipRenewalTests(BillingTestBase):
    """H21 - an in-place expiry extension could not be reversed on refund."""

    def setUp(self):
        super().setUp()
        self.membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1000.00'), expiry_days=30,
        )

    def membership_line(self, qty=1):
        return {
            'content_type': 'marketing.membership',
            'object_id': self.membership.id,
            'description': 'Gold',
            'unit_price': '1000.00',
            'quantity': qty,
            'staff_members': [self.staff.id],
        }

    def test_renewal_stacks_as_a_separate_row(self):
        from clients.models import ClientMembership

        self.create_invoice(self.build_payload(items=[self.membership_line()]))
        self.create_invoice(self.build_payload(items=[self.membership_line()]))

        rows = ClientMembership.objects.filter(client=self.client_obj).order_by('start_date')
        self.assertEqual(rows.count(), 2)
        # The second period begins where the first ends.
        self.assertEqual(rows[1].start_date, rows[0].expiry_date)

    def test_refunding_a_renewal_removes_the_extra_days(self):
        """H21: the extension survived the refund, giving free validity."""
        from clients.models import ClientMembership

        self.create_invoice(self.build_payload(items=[self.membership_line()]))
        second = self.create_invoice(self.build_payload(items=[self.membership_line()]))
        self.assertEqual(ClientMembership.objects.filter(client=self.client_obj).count(), 2)

        self.api.post(f'/billing/invoices/{second.data["id"]}/cancel/',
                      {'reason': 'duplicate charge'}, format='json')

        remaining = ClientMembership.objects.filter(client=self.client_obj)
        self.assertEqual(remaining.count(), 1)


class CancelAndRefundTests(BillingTestBase):
    """H10 / H32 - refund amounts, dates, and record preservation."""

    def test_refund_records_what_was_collected_not_the_bill_total(self):
        """H10: a partially paid bill produced a full-value refund record.

        Bills are paid in full now (#37); part-paid bills exist from before,
        so one is made the way they were left."""
        response = self.create_invoice(self.build_payload())
        invoice_id = response.data['id']
        Payment.objects.filter(invoice_id=invoice_id).update(amount=Decimal('400.00'))
        Invoice.objects.filter(pk=invoice_id).update(paid_amount=Decimal('400.00'), status='partial')

        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'service not delivered'}, format='json')

        refund = InvoiceRefund.objects.get(invoice_id=invoice_id)
        self.assertEqual(money(refund.amount), Decimal('400.00'))
        self.assertEqual(refund.refunded_on, datetime.date.today())

    def test_cancel_requires_a_reason(self):
        response = self.create_invoice(self.build_payload())
        result = self.api.post(f'/billing/invoices/{response.data["id"]}/cancel/', {}, format='json')
        self.assertEqual(result.status_code, 400)

    def test_cancel_is_not_repeatable(self):
        response = self.create_invoice(self.build_payload())
        invoice_id = response.data['id']
        first = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                              {'reason': 'error'}, format='json')
        self.assertEqual(first.status_code, 200)
        second = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                               {'reason': 'error'}, format='json')
        self.assertEqual(second.status_code, 400)

    def test_wallet_reversal_writes_a_row_instead_of_deleting_one(self):
        """H32: deleting the debit row destroyed the audit trail."""
        AdvancePayment.objects.create(client=self.client_obj, amount=Decimal('2000.00'))
        payload = self.build_payload(payments=[{'payment_method': 'Advance', 'amount': '1050.00'}])
        response = self.create_invoice(payload)
        invoice_id = response.data['id']

        self.assertEqual(
            AdvancePayment.objects.filter(invoice_id=invoice_id, amount__lt=0).count(), 1
        )
        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'error'}, format='json')

        # The debit still exists, plus a compensating credit.
        rows = AdvancePayment.objects.filter(invoice_id=invoice_id)
        self.assertEqual(rows.count(), 2)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('2000.00'))

    def test_invoices_cannot_be_deleted(self):
        """H32: DELETE cascaded away items, payments, refunds and change logs."""
        response = self.create_invoice(self.build_payload())
        result = self.api.delete(f'/billing/invoices/{response.data["id"]}/')
        self.assertEqual(result.status_code, 405)
        self.assertTrue(Invoice.objects.filter(pk=response.data['id']).exists())

    def test_advance_payments_cannot_be_deleted(self):
        row = AdvancePayment.objects.create(client=self.client_obj, amount=Decimal('500.00'))
        result = self.api.delete(f'/billing/advances/{row.id}/')
        self.assertIn(result.status_code, (403, 405))
        self.assertTrue(AdvancePayment.objects.filter(pk=row.id).exists())

    def test_negative_advance_top_up_is_rejected(self):
        result = self.api.post('/billing/advances/', {
            'client': self.client_obj.id, 'amount': '-5000.00',
        }, format='json')
        self.assertEqual(result.status_code, 400)


class InvoiceNumberTests(BillingTestBase):
    """M28 - GST requires a consecutive series per place of business."""

    def test_finalised_invoice_gets_a_number(self):
        """F20 / D12: branch code - financial year - sequence, no slashes."""
        response = self.create_invoice(self.build_payload())
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.center.refresh_from_db()
        fy = InvoiceNumberSequence.series_for(datetime.date.today())
        self.assertEqual(invoice.invoice_number, f'{self.center.branch_code}-{fy}-00001')
        self.assertNotIn('/', invoice.invoice_number)
        self.assertLessEqual(len(invoice.invoice_number), 16)   # GST serial limit

    def test_numbers_are_consecutive_per_center(self):
        numbers = []
        for _ in range(3):
            response = self.create_invoice(self.build_payload())
            numbers.append(Invoice.objects.get(pk=response.data['id']).invoice_number)
        tails = [int(n.split('-')[-1]) for n in numbers]
        self.assertEqual(tails, [1, 2, 3])

    def test_branch_codes_come_from_the_gstin_state_and_are_unique(self):
        delhi = Center.objects.create(center_name='Saket', gst_number='07AAAAA0000A1Z5')
        delhi2 = Center.objects.create(center_name='Vasant Kunj', gst_number='07BBBBB0000B1Z5')
        noida = Center.objects.create(center_name='Noida', gst_number='09CCCCC0000C1Z5')
        self.assertEqual((delhi.branch_code, delhi2.branch_code, noida.branch_code),
                         ('DL01', 'DL02', 'UP01'))

    def test_the_year_part_follows_the_indian_financial_year(self):
        self.assertEqual(InvoiceNumberSequence.series_for(datetime.date(2027, 3, 31)), '2627')
        self.assertEqual(InvoiceNumberSequence.series_for(datetime.date(2027, 4, 1)), '2728')

    def test_fiscal_year_boundary(self):
        self.assertEqual(InvoiceNumberSequence.fiscal_year_for(datetime.date(2026, 3, 31)), '2025-26')
        self.assertEqual(InvoiceNumberSequence.fiscal_year_for(datetime.date(2026, 4, 1)), '2026-27')

    def test_number_is_stable_across_refinalisation(self):
        response = self.create_invoice(self.build_payload())
        invoice = Invoice.objects.get(pk=response.data['id'])
        original = invoice.invoice_number
        invoice.assign_invoice_number()
        invoice.refresh_from_db()
        self.assertEqual(invoice.invoice_number, original)


class PromotionTests(BillingTestBase):
    """C4 / H12 - the live 500, plus stacking and scoping. An offer is added
    to a held bill (X10); at checkout the till sends promo_id instead."""

    def hold(self):
        return self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')

    def make_promo(self, **kw):
        defaults = dict(
            name='Monsoon 10', promo_type='Discount', discount_type='Percentage',
            discount_value=Decimal('10.00'), is_active=True,
            start_date=datetime.date.today() - datetime.timedelta(days=1),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        defaults.update(kw)
        return Promotion.objects.create(**defaults)

    def test_null_discount_value_does_not_500(self):
        """C4: this crashed PUT /billing/invoices/3/ four times in production."""
        promo = self.make_promo(discount_value=None)
        response = self.hold()
        invoice_id = response.data['id']

        result = self.api.post(f'/billing/invoices/{invoice_id}/apply_promo/',
                               {'promo_id': promo.id}, format='json')
        self.assertEqual(result.status_code, 400)
        self.assertIn('discount value', str(result.data).lower())

    def test_promotion_applies_once_only(self):
        """H12: repeated calls stacked the discount toward zero."""
        promo = self.make_promo()
        response = self.hold()
        invoice_id = response.data['id']

        first = self.api.post(f'/billing/invoices/{invoice_id}/apply_promo/',
                              {'promo_id': promo.id}, format='json')
        self.assertEqual(first.status_code, 200)
        after_first = Invoice.objects.get(pk=invoice_id).total_amount

        for _ in range(5):
            self.api.post(f'/billing/invoices/{invoice_id}/apply_promo/',
                          {'promo_id': promo.id}, format='json')

        self.assertEqual(Invoice.objects.get(pk=invoice_id).total_amount, after_first)

    def test_promotion_recomputes_tax(self):
        """H13: discount changed but cgst/sgst kept their old values."""
        promo = self.make_promo()
        response = self.hold()
        invoice_id = response.data['id']
        self.api.post(f'/billing/invoices/{invoice_id}/apply_promo/',
                      {'promo_id': promo.id}, format='json')

        invoice = Invoice.objects.get(pk=invoice_id)
        # 1000 subtotal, 100 discount -> 900 taxable @5% = 45
        self.assertEqual(money(invoice.discount), Decimal('100.00'))
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('45.00'))
        self.assert_tax_matches_items(invoice)

    def test_center_specific_promotion_does_not_apply_elsewhere(self):
        """H12: Promotion.center was never checked."""
        promo = self.make_promo(center=self.other_center, level='Center')
        response = self.hold()
        result = self.api.post(f'/billing/invoices/{response.data["id"]}/apply_promo/',
                               {'promo_id': promo.id}, format='json')
        self.assertEqual(result.status_code, 400)
        self.assertIn('not available at this center', str(result.data))

    def test_members_only_promotion_is_enforced(self):
        """H12: members_only was declared on the model and read by nothing."""
        promo = self.make_promo(members_only=True)
        response = self.hold()
        result = self.api.post(f'/billing/invoices/{response.data["id"]}/apply_promo/',
                               {'promo_id': promo.id}, format='json')
        self.assertEqual(result.status_code, 400)
        self.assertIn('members only', str(result.data).lower())

    def test_expired_promotion_is_rejected(self):
        promo = self.make_promo(
            start_date=datetime.date.today() - datetime.timedelta(days=60),
            end_date=datetime.date.today() - datetime.timedelta(days=30),
        )
        response = self.hold()
        result = self.api.post(f'/billing/invoices/{response.data["id"]}/apply_promo/',
                               {'promo_id': promo.id}, format='json')
        self.assertEqual(result.status_code, 400)


class CommissionTests(BillingTestBase):
    """C1 / H2 / M18 - what staff actually get credited."""

    def test_service_commission_is_not_zero(self):
        """C1: ServiceLog.price was 0 because the line price was 0."""
        self.create_invoice(self.build_payload())
        logs = ServiceLog.all_objects.filter(staff=self.staff)
        self.assertEqual(logs.count(), 1)
        self.assertEqual(money(logs.first().price), Decimal('1000.00'))

    def test_commission_uses_the_tax_exclusive_value(self):
        """H2: a tax-inclusive base inflated every commission by the GST rate."""
        self.create_invoice(self.build_payload(items=[self.product_line()]))
        log = ServiceLog.all_objects.filter(staff=self.staff).first()
        # Rs 500 MRP at 18% GST contains Rs 76.27 of GST (F22): the commission
        # base is the taxable 423.73, never the GST.
        self.assertEqual(money(log.price), Decimal('423.73'))

    def test_split_across_staff_sums_to_the_line_value(self):
        """M18: Rs 100 / 3 stored 33.33 three times, losing a paisa."""
        second = StaffMember.objects.create(
            center=self.center, first_name='Sita', designation='Stylist',
        )
        third = StaffMember.objects.create(
            center=self.center, first_name='Meena', designation='Stylist',
        )
        line = self.service_line()
        line['staff_members'] = [self.staff.id, second.id, third.id]
        response = self.create_invoice(self.build_payload(items=[line]))
        invoice = Invoice.objects.get(pk=response.data['id'])

        logs = ServiceLog.all_objects.filter(invoice=invoice)
        self.assertEqual(logs.count(), 3)
        total = money(sum(D(l.price) for l in logs))
        self.assertEqual(total, Decimal('1000.00'))

    def test_value_card_payment_reduces_the_commission_base(self):
        card_def = ValueCard.objects.create(
            title='Elite', pre_tax_price=Decimal('1000.00'),
            value=Decimal('2000.00'), expiry_days=365, gst_percent=Decimal('0'),
        )
        card = ClientValueCard.objects.create(
            client=self.client_obj, value_card=card_def, balance=Decimal('2000.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365),
        )
        # The card covers the Rs 1000 haircut (face 1000 -> taxable 500); the
        # Rs 500 shampoo is paid in cash. Bill: 500 + 500 (MRP) = Rs 1000.
        response = self.create_invoice(self.build_payload(
            items=[self.service_line(), self.product_line()], value_card_id=card.id))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(money(invoice.total_amount), Decimal('1000.00'))
        logs = {l.service_type: l for l in ServiceLog.all_objects.filter(invoice=invoice)}
        # Half the bill was settled from the card, so half the commission base.
        self.assertEqual(money(logs['Service'].price), Decimal('250.00'))

    def test_cancelled_invoice_logs_are_excluded_from_commission(self):
        response = self.create_invoice(self.build_payload())
        invoice_id = response.data['id']
        self.assertEqual(ServiceLog.objects.filter(invoice_id=invoice_id).count(), 1)

        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'error'}, format='json')
        # The record survives; the default manager hides it from payroll.
        self.assertEqual(ServiceLog.objects.filter(invoice_id=invoice_id).count(), 0)
        self.assertEqual(ServiceLog.all_objects.filter(invoice_id=invoice_id).count(), 1)


class FinalisedInvoiceEditTests(BillingTestBase):
    """M32 - a finalised bill's lines were editable with no reversal."""

    def test_cannot_edit_items_after_finalisation(self):
        response = self.create_invoice(self.build_payload(items=[self.product_line(qty=2)]))
        invoice_id = response.data['id']
        result = self.api.put(f'/billing/invoices/{invoice_id}/',
                              self.build_payload(items=[self.product_line(qty=9)]),
                              format='json')
        self.assertEqual(result.status_code, 400)
        self.assertIn('already been completed', str(result.data))

    def test_cannot_pay_a_cancelled_invoice(self):
        response = self.create_invoice(self.build_payload(payments=[]))
        invoice_id = response.data['id']
        self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                      {'reason': 'error'}, format='json')
        result = self.api.post(f'/billing/invoices/{invoice_id}/pay/',
                               {'amount': '100.00', 'payment_method': 'Cash'}, format='json')
        self.assertEqual(result.status_code, 400)
