"""
Does performing a redeemed service earn the stylist anything?

A redemption line is worth Rs 0 - the customer paid when they bought the
package - so the ServiceLog was written at Rs 0 and the incentive engine skipped
the line outright. The stylist who did the work earned nothing, and saw a Rs 0
row against real work.

That is defensible: the package sale already paid commission. But it is a policy,
not a rule, and the person who sold the package is often not the person holding
the scissors. `PACKAGE_REDEMPTION_EARNS_COMMISSION` makes the choice explicit and
defaults to the existing behaviour.

These tests pin both directions, because getting this wrong changes what staff
are paid.
"""

import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings

from billing.models import Invoice, InvoiceItem, Payment
from billing.services import commission_base, redemption_earns_commission
from pos_backend.money import money
from clients.models import Client, ClientPackage
from marketing.models import Package
from salon_admin.models import Center
from services.models import ServiceMaster
from staff.models import ServiceLog, StaffMember


class RedemptionCommissionPolicyTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.stylist = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'),
            commission_percentage=Decimal('10.00'))

        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))
        self.package = Package.objects.create(
            name='Haircut x5', price=Decimal('2000.00'), validity_days=365,
            services_json=[{'service_id': self.service.id, 'quantity': 5}])
        self.held = ClientPackage.objects.create(
            client=self.customer, package=self.package,
            services_remaining={str(self.service.id): 5},
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

    def redemption_item(self, quantity=1):
        invoice = Invoice.objects.create(
            client=self.customer, center=self.center,
            status=Invoice.STATUS_PAID,
            subtotal=Decimal('0'), total_amount=Decimal('0'))
        item = InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(self.service),
            object_id=self.service.id,
            description='\U0001F381 [Redeem] Haircut',
            # The catalog price is still recorded on the line; only the line
            # TOTAL is zero. That is what makes paying for a redemption possible.
            unit_price=Decimal('500.00'),
            quantity=quantity,
            total_price=Decimal('0'),
            is_redemption=True,
            redeemed_from_package=self.held,
            staff=self.stylist,
        )
        return invoice, item

    def sold_item(self):
        invoice = Invoice.objects.create(
            client=self.customer, center=self.center,
            status=Invoice.STATUS_PAID,
            subtotal=Decimal('500.00'), total_amount=Decimal('590.00'),
            paid_amount=Decimal('590.00'))
        Payment.objects.create(
            invoice=invoice, payment_method=Payment.CASH,
            amount=Decimal('590.00'))
        item = InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(self.service),
            object_id=self.service.id,
            description='Haircut',
            unit_price=Decimal('500.00'), quantity=1,
            total_price=Decimal('500.00'),
            staff=self.stylist,
        )
        return invoice, item

    # ── the default: unchanged ────────────────────────────────────────────

    def test_the_default_is_that_a_redemption_pays_nothing(self):
        self.assertFalse(redemption_earns_commission())

        _, item = self.redemption_item()
        self.assertEqual(commission_base(item), Decimal('0'))

    def test_a_sold_service_always_pays_on_its_line_total(self):
        _, item = self.sold_item()
        self.assertEqual(commission_base(item), Decimal('500.00'))

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=False)
    def test_the_default_is_explicit_too(self):
        _, item = self.redemption_item()
        self.assertEqual(commission_base(item), Decimal('0'))

    # ── the opt-in ────────────────────────────────────────────────────────

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_when_enabled_the_catalog_value_is_the_base(self):
        self.assertTrue(redemption_earns_commission())

        _, item = self.redemption_item()
        # What the line would have been worth had it been sold.
        self.assertEqual(commission_base(item), Decimal('500.00'))

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_when_enabled_quantity_is_respected(self):
        _, item = self.redemption_item(quantity=3)
        self.assertEqual(commission_base(item), Decimal('1500.00'))

    # ── the incentive engine agrees with the ServiceLog ───────────────────

    def incentive_for(self, items):
        from finance.incentives import calculate

        return calculate(
            StaffMember.objects.filter(pk=self.stylist.pk),
            items, rules=[],
            start_date=datetime.date.today(), end_date=datetime.date.today())

    def test_by_default_the_engine_skips_a_redemption(self):
        """`calculate` returns only staff with sales or incentive, so a skipped
        redemption means this stylist does not appear at all."""
        _, item = self.redemption_item()
        results = self.incentive_for(
            InvoiceItem.objects.filter(pk=item.pk).select_related('invoice'))
        self.assertEqual(list(results), [], 'a redemption attributed revenue')

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_when_enabled_the_engine_attributes_the_catalog_value(self):
        _, item = self.redemption_item()
        results = list(self.incentive_for(
            InvoiceItem.objects.filter(pk=item.pk).select_related('invoice')))
        self.assertEqual(len(results), 1, 'the stylist earned nothing')

        acc = results[0]
        self.assertEqual(acc.staff_id, self.stylist.id)
        # The service's catalog value is now attributed as sales.
        self.assertEqual(money(acc.total_sales), Decimal('500.00'))

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_a_sold_service_and_a_redemption_both_attribute(self):
        _, sold = self.sold_item()
        _, redeemed = self.redemption_item()
        results = list(self.incentive_for(
            InvoiceItem.objects.filter(pk__in=[sold.pk, redeemed.pk])
            .select_related('invoice')))
        self.assertEqual(len(results), 1)
        self.assertEqual(money(results[0].total_sales), Decimal('1000.00'))

    def test_the_service_log_is_still_written_so_the_work_is_visible(self):
        """Even unpaid, the stylist's activity must show the service."""
        from billing.services import _create_service_logs

        invoice, item = self.redemption_item()
        _create_service_logs(invoice, item, Decimal('1'))

        log = ServiceLog.all_objects.filter(invoice=invoice, staff=self.stylist).first()
        self.assertIsNotNone(log, 'the work vanished from the stylist\'s record')
        self.assertEqual(log.price, Decimal('0.00'))
        # And it is labelled, so a Rs 0 row is explicable rather than looking
        # like the zero-price bug this system already had.
        self.assertIn('[Redeem]', log.service_name)

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_when_enabled_the_service_log_carries_the_value(self):
        from billing.services import _create_service_logs

        invoice, item = self.redemption_item()
        _create_service_logs(invoice, item, Decimal('1'))

        log = ServiceLog.all_objects.filter(invoice=invoice, staff=self.stylist).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.price, Decimal('500.00'))

    @override_settings(PACKAGE_REDEMPTION_EARNS_COMMISSION=True)
    def test_the_money_ratio_still_applies(self):
        """A bill settled from a wallet pays proportionally less; a redemption
        should not be a way around that."""
        from billing.services import _create_service_logs

        invoice, item = self.redemption_item()
        _create_service_logs(invoice, item, Decimal('0.5'))

        log = ServiceLog.all_objects.filter(invoice=invoice, staff=self.stylist).first()
        self.assertEqual(log.price, Decimal('250.00'))
