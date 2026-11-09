"""
Finance and incentive-engine tests.

Pins the incentive findings (C6, H3-H6, M25-M27), the report aggregation
findings (C7, H24-H26), and the daily-closing findings (H14, H31b).
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem, Payment
from finance import incentives
from finance.models import DailyClosing, IncentiveRule
from finance.views import _compute_payment_breakdown, _compute_revenue_breakdown
from inventory.models import Product
from marketing.models import Membership, Package, ValueCard
from pos_backend.money import D, money, ZERO
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import ServiceLog, StaffMember


class FinanceTestBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.other_center = Center.objects.create(center_name='Other')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )
        # Day closings live under Finance > Closing & Petty Cash, which is now
        # enforced server-side, so the manager needs it to reach the closing
        # endpoints at all. The centre-scoping tests below are about WHICH
        # branch they may close, which is a separate check.
        self.manager_role = Role.objects.create(name='Manager', permissions={
            'finance': {
                'register_summary': {'read': True},
                'incentives': {'read': True},
                'pettycash': {'read': True, 'create': True, 'update': True,
                              'delete': False},
            },
        })
        self.manager = CustomUser.objects.create_user(
            email='manager@test.com', password='pw', full_name='Manager',
            role=self.manager_role, center=self.center,
        )

        self.stylist = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Senior Stylist',
            salary=Decimal('20000.00'),
        )
        self.beautician = StaffMember.objects.create(
            center=self.center, first_name='Meena', designation='Beautician',
            salary=Decimal('15000.00'),
        )

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    # ─── fixtures ─────────────────────────────────────────────────────────

    def make_invoice(self, total='1000.00', status='paid', paid=None, when=None):
        invoice = Invoice.objects.create(
            center=self.center, status=status,
            subtotal=Decimal(total), total_amount=Decimal(total),
            paid_amount=Decimal(paid if paid is not None else total),
        )
        if when:
            Invoice.objects.filter(pk=invoice.pk).update(created_at=when)
            invoice.refresh_from_db()
        return invoice

    def add_item(self, invoice, obj, price='1000.00', tax='0.00', quantity=1, staff=None,
                 description=None):
        from django.contrib.contenttypes.models import ContentType

        item = InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(type(obj)) if obj else None,
            object_id=getattr(obj, 'id', None),
            description=description or getattr(obj, 'name', None) or getattr(obj, 'title', 'Item'),
            unit_price=Decimal(price),
            quantity=quantity,
            tax_percentage=Decimal(tax),
            tax_amount=money(Decimal(price) * quantity * Decimal(tax) / Decimal('100')),
            total_price=money(Decimal(price) * quantity),
        )
        if staff:
            item.staff_members.set([staff])
        return item

    def run_engine(self, rules=None, frequency='monthly', start=None, end=None):
        return incentives.calculate(
            staff_qs=StaffMember.objects.filter(is_active=True),
            invoice_items=InvoiceItem.objects.select_related(
                'invoice', 'content_type', 'staff'
            ).prefetch_related('staff_members', 'invoice__payments'),
            rules=rules or [],
            start_date=start or datetime.date.today().replace(day=1),
            end_date=end or datetime.date.today(),
            frequency=frequency,
        )


class TierSelectionTests(TestCase):
    """H5 - highest qualifying tier wins, never the sum of all of them."""

    def test_highest_tier_wins(self):
        tiers = [
            {'min_multiple': 5, 'incentive_percent': 3},
            {'min_multiple': 6, 'incentive_percent': 6},
            {'min_multiple': 7, 'incentive_percent': 7},
        ]
        chosen = incentives.highest_qualifying_tier(tiers, D('6.5'), ('min_multiple',))
        self.assertEqual(chosen['incentive_percent'], 6)

    def test_unordered_tiers_still_pick_the_highest(self):
        tiers = [
            {'min_multiple': 7, 'incentive_percent': 7},
            {'min_multiple': 5, 'incentive_percent': 3},
            {'min_multiple': 6, 'incentive_percent': 6},
        ]
        chosen = incentives.highest_qualifying_tier(tiers, D('9'), ('min_multiple',))
        self.assertEqual(chosen['incentive_percent'], 7)

    def test_no_tier_reached_returns_none(self):
        tiers = [{'min_multiple': 5, 'incentive_percent': 3}]
        self.assertIsNone(
            incentives.highest_qualifying_tier(tiers, D('2'), ('min_multiple',))
        )

    def test_range_tier_matching(self):
        tiers = [
            {'name': 'Elite', 'min_amount': 0, 'max_amount': 5000, 'incentive_amount': 200},
            {'name': 'Luxe', 'min_amount': 5001, 'max_amount': 10000, 'incentive_amount': 400},
        ]
        self.assertEqual(
            incentives.matching_range_tier(tiers, D('7500'))['name'], 'Luxe'
        )


class RuleSelectionTests(FinanceTestBase):
    """M25 - center beats org, role-specific beats 'all', newest breaks ties."""

    def make_rule(self, **kw):
        defaults = dict(
            name='rule', category='services', rule_type='percentage',
            flat_percent=Decimal('5.00'), applicable_role='all', is_active=True,
        )
        defaults.update(kw)
        return IncentiveRule.objects.create(**defaults)

    def test_center_rule_beats_org_rule(self):
        org = self.make_rule(name='org', center=None, flat_percent=Decimal('5.00'))
        centre = self.make_rule(name='centre', center=self.center, flat_percent=Decimal('9.00'))
        chosen = incentives.select_rule(
            [org, centre], 'services', self.center.id, 'Senior Stylist'
        )
        self.assertEqual(chosen.name, 'centre')

    def test_role_specific_beats_all(self):
        """M25: the old code returned the newest match, so 'all' could win."""
        generic = self.make_rule(name='generic', applicable_role='all')
        specific = self.make_rule(name='specific', applicable_role='lhds_uhds')
        chosen = incentives.select_rule(
            [specific, generic], 'services', self.center.id, 'Senior Stylist'
        )
        self.assertEqual(chosen.name, 'specific')

    def test_another_centers_rule_never_applies(self):
        foreign = self.make_rule(name='foreign', center=self.other_center)
        chosen = incentives.select_rule(
            [foreign], 'services', self.center.id, 'Senior Stylist'
        )
        self.assertIsNone(chosen)

    def test_role_matching(self):
        self.assertTrue(
            incentives.rule_applies_to_role(
                self.make_rule(applicable_role='lhds_uhds'), 'Senior Stylist')
        )
        self.assertTrue(
            incentives.rule_applies_to_role(
                self.make_rule(applicable_role='mhds_beauty'), 'Beautician')
        )
        self.assertFalse(
            incentives.rule_applies_to_role(
                self.make_rule(applicable_role='manager'), 'Senior Stylist')
        )
        self.assertTrue(
            incentives.rule_applies_to_role(
                self.make_rule(applicable_role='manager'), 'Salon Manager')
        )

    def test_partial_period_rule_is_flagged(self):
        """M25: a rule in force for part of the period applied to all of it."""
        today = datetime.date.today()
        rule = self.make_rule(
            name='mid-month', effective_from=today, center=self.center,
        )
        self.assertTrue(incentives.rule_covers_period(rule, today.replace(day=1), today))
        self.assertFalse(
            incentives.rule_covers_whole_period(rule, today.replace(day=1), today)
        )


class ItemCategorisationTests(FinanceTestBase):
    """H4 - category comes from content_type, not from words in the name."""

    def test_service_named_elite_is_not_a_value_card(self):
        """H4: 'elite' in the description routed a service to the card branch."""
        service = ServiceMaster.objects.create(
            name='Elite Hair Spa', category='Spa', default_price=Decimal('2000.00'),
        )
        invoice = self.make_invoice('2000.00')
        item = self.add_item(invoice, service, '2000.00', staff=self.stylist)
        item.refresh_from_db()
        self.assertEqual(incentives.categorise_item(item), incentives.CAT_SERVICE)

    def test_service_named_bridal_package_is_not_skipped(self):
        """H4/H3: 'package' in the name made the line vanish entirely."""
        service = ServiceMaster.objects.create(
            name='Bridal Package Makeover', category='Bridal',
            default_price=Decimal('9000.00'),
        )
        invoice = self.make_invoice('9000.00')
        item = self.add_item(invoice, service, '9000.00', staff=self.stylist)
        item.refresh_from_db()
        self.assertEqual(incentives.categorise_item(item), incentives.CAT_SERVICE)

        results = self.run_engine()
        row = next(r for r in results if r.staff_id == self.stylist.id)
        self.assertEqual(row.revenue[incentives.CAT_SERVICE], Decimal('9000.00'))

    def test_value_card_is_categorised_by_content_type(self):
        card = ValueCard.objects.create(
            title='Prestige', pre_tax_price=Decimal('5000.00'),
            value=Decimal('6000.00'), expiry_days=365,
        )
        invoice = self.make_invoice('5000.00')
        item = self.add_item(invoice, card, '5000.00', staff=self.stylist)
        item.refresh_from_db()
        self.assertEqual(incentives.categorise_item(item), incentives.CAT_VALUE_CARD)


class MembershipPackageIncentiveTests(FinanceTestBase):
    """H3 - these branches were unreachable dead code."""

    def test_membership_sale_earns_an_incentive(self):
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('50000.00'),
            post_tax_price=Decimal('50000.00'), expiry_days=365,
        )
        invoice = self.make_invoice('50000.00')
        self.add_item(invoice, membership, '50000.00', staff=self.stylist)

        rule = IncentiveRule.objects.create(
            name='membership 4%', category='memberships', rule_type='percentage',
            flat_percent=Decimal('4.00'), applicable_role='all', center=self.center,
        )
        results = self.run_engine(rules=[rule])
        row = next(r for r in results if r.staff_id == self.stylist.id)

        self.assertEqual(row.revenue[incentives.CAT_MEMBERSHIP], Decimal('50000.00'))
        self.assertEqual(row.incentive[incentives.CAT_MEMBERSHIP], Decimal('2000.00'))

    def test_package_sale_earns_an_incentive(self):
        package = Package.objects.create(
            name='5 Facials', price=Decimal('10000.00'), validity_days=180,
        )
        invoice = self.make_invoice('10000.00')
        self.add_item(invoice, package, '10000.00', staff=self.stylist)

        rule = IncentiveRule.objects.create(
            name='package 5%', category='packages', rule_type='percentage',
            flat_percent=Decimal('5.00'), applicable_role='all', center=self.center,
        )
        results = self.run_engine(rules=[rule])
        row = next(r for r in results if r.staff_id == self.stylist.id)
        self.assertEqual(row.incentive[incentives.CAT_PACKAGE], Decimal('500.00'))


class DailySlabTests(FinanceTestBase):
    """C6 - `Decimal / 100.0` raised TypeError on every percentage slab."""

    def test_percentage_daily_slab_does_not_crash(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('15000.00'),
        )
        invoice = self.make_invoice('15000.00')
        self.add_item(invoice, service, '15000.00', staff=self.stylist)

        rule = IncentiveRule.objects.create(
            name='daily 5%', category='daily_business', rule_type='slab',
            frequency='daily', applicable_role='all', center=self.center,
            tiers=[{'min_amount': 10000, 'bonus_type': 'percentage', 'bonus_percent': 5}],
        )
        results = self.run_engine(rules=[rule], frequency='daily')
        row = next(r for r in results if r.staff_id == self.stylist.id)
        self.assertEqual(row.daily_bonus, Decimal('750.00'))

    def test_flat_daily_slab(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('12000.00'),
        )
        invoice = self.make_invoice('12000.00')
        self.add_item(invoice, service, '12000.00', staff=self.stylist)

        rule = IncentiveRule.objects.create(
            name='daily flat', category='daily_business', rule_type='slab',
            frequency='daily', applicable_role='all', center=self.center,
            tiers=[
                {'min_amount': 5000, 'bonus_type': 'flat', 'bonus_amount': 200},
                {'min_amount': 10000, 'bonus_type': 'flat', 'bonus_amount': 500},
            ],
        )
        results = self.run_engine(rules=[rule], frequency='daily')
        row = next(r for r in results if r.staff_id == self.stylist.id)
        # Highest qualifying slab only, not 200 + 500.
        self.assertEqual(row.daily_bonus, Decimal('500.00'))


class ServiceTargetTests(FinanceTestBase):
    """H5 - targets paid cumulatively instead of highest-tier."""

    def _sell(self, count):
        service = ServiceMaster.objects.create(
            name='Keratin Treatment', category='Hair', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice(str(1000 * count))
        self.add_item(invoice, service, '1000.00', quantity=count, staff=self.stylist)
        return service

    def test_only_the_highest_target_tier_pays(self):
        self._sell(25)
        rule = IncentiveRule.objects.create(
            name='keratin target', category='service_target', rule_type='target_count',
            applicable_role='all', center=self.center,
            tiers=[
                {'service_name': 'Keratin', 'match_keyword': 'keratin',
                 'target_count': 10, 'reward_amount': 500},
                {'service_name': 'Keratin', 'match_keyword': 'keratin',
                 'target_count': 20, 'reward_amount': 1200},
            ],
        )
        results = self.run_engine(rules=[rule])
        row = next(r for r in results if r.staff_id == self.stylist.id)
        # 25 services: Rs 1200, not Rs 1700.
        self.assertEqual(row.target_total, Decimal('1200.00'))

    def test_targets_are_evaluated_monthly_too(self):
        """H5: service_target was only computed in daily mode."""
        self._sell(12)
        rule = IncentiveRule.objects.create(
            name='keratin target', category='service_target', rule_type='target_count',
            applicable_role='all', center=self.center,
            tiers=[{'match_keyword': 'keratin', 'target_count': 10, 'reward_amount': 500}],
        )
        results = self.run_engine(rules=[rule], frequency='monthly')
        row = next(r for r in results if r.staff_id == self.stylist.id)
        self.assertEqual(row.target_total, Decimal('500.00'))

    def test_target_counts_quantity_not_rows(self):
        self._sell(10)  # one line, quantity 10
        rule = IncentiveRule.objects.create(
            name='keratin target', category='service_target', rule_type='target_count',
            applicable_role='all', center=self.center,
            tiers=[{'match_keyword': 'keratin', 'target_count': 10, 'reward_amount': 500}],
        )
        results = self.run_engine(rules=[rule])
        row = next(r for r in results if r.staff_id == self.stylist.id)
        self.assertEqual(row.target_total, Decimal('500.00'))


class AddOnTests(FinanceTestBase):
    """H6 - an add-on used to REPLACE the base commission and break the total."""

    def test_addon_is_additive_to_the_base_percentage(self):
        service = ServiceMaster.objects.create(
            name='Head Massage', category='Spa', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice('1000.00')
        self.add_item(invoice, service, '1000.00', staff=self.stylist)

        base = IncentiveRule.objects.create(
            name='services 10%', category='services', rule_type='percentage',
            flat_percent=Decimal('10.00'), applicable_role='all', center=self.center,
        )
        addon = IncentiveRule.objects.create(
            name='massage bonus', category='service_addon', rule_type='service_bonus',
            applicable_role='all', center=self.center,
            tiers=[{'service_name': 'Head Massage', 'match_keyword': 'massage',
                    'incentive_amount': 50}],
        )
        results = self.run_engine(rules=[base, addon])
        row = next(r for r in results if r.staff_id == self.stylist.id)

        self.assertEqual(row.incentive[incentives.CAT_SERVICE], Decimal('100.00'))
        self.assertEqual(row.addon_total, Decimal('50.00'))
        self.assertEqual(row.total_incentive, Decimal('150.00'))

    def test_header_total_equals_the_sum_of_detail_rows(self):
        """H6: the header and the drill-down could not be reconciled."""
        service = ServiceMaster.objects.create(
            name='Head Massage', category='Spa', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice('2000.00')
        self.add_item(invoice, service, '1000.00', staff=self.stylist)
        self.add_item(invoice, service, '1000.00', staff=self.stylist)

        base = IncentiveRule.objects.create(
            name='services 10%', category='services', rule_type='percentage',
            flat_percent=Decimal('10.00'), applicable_role='all', center=self.center,
        )
        addon = IncentiveRule.objects.create(
            name='massage bonus', category='service_addon', rule_type='service_bonus',
            applicable_role='all', center=self.center,
            tiers=[{'match_keyword': 'massage', 'incentive_amount': 25}],
        )
        results = self.run_engine(rules=[base, addon])
        row = next(r for r in results if r.staff_id == self.stylist.id)

        detail_sum = money(sum(D(line['calculated_incentive']) for line in row.lines))
        header = money(row.incentive[incentives.CAT_SERVICE] + row.addon_total)
        self.assertEqual(detail_sum, header)


class ValueCardSlabTests(FinanceTestBase):
    """M27 - slabs matched the split amount and ignored quantity."""

    def test_slab_matches_the_full_card_value_not_the_split(self):
        card = ValueCard.objects.create(
            title='Infinity', pre_tax_price=Decimal('10000.00'),
            value=Decimal('12000.00'), expiry_days=365,
        )
        invoice = self.make_invoice('10000.00')
        item = self.add_item(invoice, card, '10000.00')
        item.staff_members.set([self.stylist, self.beautician])

        rule = IncentiveRule.objects.create(
            name='card slabs', category='value_cards', rule_type='slab',
            applicable_role='all', center=self.center,
            tiers=[
                {'name': 'Elite', 'min_amount': 0, 'max_amount': 6000, 'incentive_amount': 200},
                {'name': 'Infinity', 'min_amount': 6001, 'max_amount': 20000,
                 'incentive_amount': 800},
            ],
        )
        results = self.run_engine(rules=[rule])
        total = money(sum(r.incentive[incentives.CAT_VALUE_CARD] for r in results))
        # Rs 10,000 card hits the Infinity slab (Rs 800), split between two
        # staff. Matching the Rs 5,000 split would have hit Elite (Rs 200).
        self.assertEqual(total, Decimal('800.00'))

    def test_quantity_multiplies_the_slab_reward(self):
        card = ValueCard.objects.create(
            title='Elite', pre_tax_price=Decimal('2000.00'),
            value=Decimal('2500.00'), expiry_days=365,
        )
        invoice = self.make_invoice('6000.00')
        self.add_item(invoice, card, '2000.00', quantity=3, staff=self.stylist)

        rule = IncentiveRule.objects.create(
            name='card slabs', category='value_cards', rule_type='slab',
            applicable_role='all', center=self.center,
            tiers=[{'name': 'Elite', 'min_amount': 0, 'max_amount': 5000,
                    'incentive_amount': 200}],
        )
        results = self.run_engine(rules=[rule])
        row = next(r for r in results if r.staff_id == self.stylist.id)
        # Three cards on one line: three slab rewards, not one.
        self.assertEqual(row.incentive[incentives.CAT_VALUE_CARD], Decimal('600.00'))


class PaymentBreakdownTests(FinanceTestBase):
    """C7 - cashback wallet was reported as cash."""

    def test_cashback_is_not_reported_as_cash(self):
        invoice = self.make_invoice('1000.00')
        Payment.objects.create(invoice=invoice, amount=Decimal('600.00'), payment_method='Cash')
        Payment.objects.create(invoice=invoice, amount=Decimal('400.00'),
                               payment_method='Cashback Wallet')

        buckets = _compute_payment_breakdown(Invoice.objects.filter(pk=invoice.pk))
        self.assertEqual(money(buckets['cash']['amount']), Decimal('600.00'))
        self.assertEqual(money(buckets['cashback_wallet']['amount']), Decimal('400.00'))

    def test_tips_are_excluded_from_the_revenue_buckets(self):
        invoice = self.make_invoice('1000.00')
        Payment.objects.create(invoice=invoice, amount=Decimal('1000.00'), payment_method='Cash')
        Payment.objects.create(invoice=invoice, amount=Decimal('100.00'),
                               payment_method='Cash', is_tip=True)

        buckets = _compute_payment_breakdown(Invoice.objects.filter(pk=invoice.pk))
        self.assertEqual(money(buckets['cash']['amount']), Decimal('1000.00'))

    def test_every_bucket_key_is_always_present(self):
        buckets = _compute_payment_breakdown(Invoice.objects.none())
        from billing import payment_methods as pmods
        for key in pmods.REPORT_BUCKETS:
            self.assertIn(key, buckets)


class RevenueBreakdownTests(FinanceTestBase):
    """C1 / H2 - service revenue and the tax convention."""

    def test_service_revenue_is_not_zero(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
            tax_percentage=Decimal('5.00'),
        )
        invoice = self.make_invoice('1050.00')
        self.add_item(invoice, service, '1000.00', tax='5.00', staff=self.stylist)

        revenue = _compute_revenue_breakdown(Invoice.objects.filter(pk=invoice.pk))
        self.assertEqual(money(revenue['services']), Decimal('1000.00'))
        self.assertEqual(money(revenue['tax']), Decimal('50.00'))

    def test_revenue_is_tax_exclusive_so_tax_is_reported_once(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
            tax_percentage=Decimal('5.00'),
        )
        invoice = self.make_invoice('1050.00')
        self.add_item(invoice, service, '1000.00', tax='5.00', staff=self.stylist)

        revenue = _compute_revenue_breakdown(Invoice.objects.filter(pk=invoice.pk))
        # taxable + tax == the bill. Under the old tax-inclusive convention
        # this sum double-counted the GST.
        self.assertEqual(
            money(revenue['services'] + revenue['tax']), Decimal('1050.00')
        )

    def test_redemptions_contribute_no_revenue(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice('0.00')
        item = self.add_item(invoice, service, '0.00', staff=self.stylist)
        item.is_redemption = True
        item.save()

        revenue = _compute_revenue_breakdown(Invoice.objects.filter(pk=invoice.pk))
        self.assertEqual(money(revenue['services']), Decimal('0.00'))


class DailyClosingTests(FinanceTestBase):
    """H14 / H31b."""

    def test_opening_balance_carries_across_a_closed_day(self):
        """H14: it looked only at date-1, so any gap reset the float to zero."""
        DailyClosing.objects.create(
            center=self.center, date=datetime.date(2026, 8, 15),
            closing_balance=Decimal('4500.00'),
        )
        response = self.api.get(
            '/finance/api/daily-closing/opening_balance/',
            {'center_id': self.center.id, 'date': '2026-08-18'},
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data['opening_balance']), Decimal('4500.00'))
        self.assertEqual(response.data['from_date'], '2026-08-15')
        self.assertEqual(response.data['days_since_last_closing'], 3)

    def test_opening_balance_rejects_a_foreign_center(self):
        self.api.force_authenticate(self.manager)
        response = self.api.get(
            '/finance/api/daily-closing/opening_balance/',
            {'center_id': self.other_center.id, 'date': '2026-08-18'},
        )
        self.assertEqual(response.status_code, 403)

    def test_invalid_date_is_a_400_not_a_silent_zero(self):
        response = self.api.get(
            '/finance/api/daily-closing/opening_balance/',
            {'center_id': self.center.id, 'date': 'not-a-date'},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_owner_cannot_write_another_centers_closing(self):
        """H31b: create() was missing the single-center permission branch."""
        self.api.force_authenticate(self.manager)
        response = self.api.post('/finance/api/daily-closing/', {
            'center': self.other_center.id,
            'date': '2026-08-18',
            'closing_balance': '9999.00',
        }, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertFalse(DailyClosing.objects.filter(center=self.other_center).exists())

    def test_non_owner_cannot_silently_overwrite_an_existing_closing(self):
        """H31b: update_or_create rewrote the day with no audit trail."""
        DailyClosing.objects.create(
            center=self.center, date=datetime.date(2026, 8, 18),
            closing_balance=Decimal('1000.00'),
        )
        self.api.force_authenticate(self.manager)
        response = self.api.post('/finance/api/daily-closing/', {
            'center': self.center.id, 'date': '2026-08-18', 'closing_balance': '5000.00',
        }, format='json')
        self.assertEqual(response.status_code, 409)
        closing = DailyClosing.objects.get(center=self.center, date=datetime.date(2026, 8, 18))
        self.assertEqual(money(closing.closing_balance), Decimal('1000.00'))

    def test_owner_amendment_is_logged(self):
        from audit_logs.models import SystemLog

        DailyClosing.objects.create(
            center=self.center, date=datetime.date(2026, 8, 18),
            closing_balance=Decimal('1000.00'),
        )
        response = self.api.post('/finance/api/daily-closing/', {
            'center': self.center.id, 'date': '2026-08-18', 'closing_balance': '1200.00',
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(
            SystemLog.objects.filter(entity_type='Daily Closing', action='UPDATE').exists()
        )


class IncentiveExportTests(FinanceTestBase):
    """H28 - the Excel export returned 500 every time it was called."""

    def test_excel_export_succeeds(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice('1000.00')
        self.add_item(invoice, service, '1000.00', staff=self.stylist)

        response = self.api.get('/finance/api/reports/incentive-calculation/', {
            'export': 'true',
            'start_date': datetime.date.today().replace(day=1).isoformat(),
            'end_date': datetime.date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('spreadsheetml', response['Content-Type'])
        self.assertTrue(response.content.startswith(b'PK'))  # a real xlsx zip

    def test_json_report_reconciles(self):
        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
        )
        invoice = self.make_invoice('1000.00')
        self.add_item(invoice, service, '1000.00', staff=self.stylist)

        IncentiveRule.objects.create(
            name='services 10%', category='services', rule_type='percentage',
            flat_percent=Decimal('10.00'), applicable_role='all', center=self.center,
        )
        response = self.api.get('/finance/api/reports/incentive-calculation/', {
            'start_date': datetime.date.today().replace(day=1).isoformat(),
            'end_date': datetime.date.today().isoformat(),
        })
        self.assertEqual(response.status_code, 200, response.data)
        for row in response.data:
            self.assertTrue(row['details_reconcile'], row)


class ServiceMultipleBasisTests(FinanceTestBase):
    """The tier percentage is read at SERVICES / salary: products and value
    cards have their own incentive and must not lift it."""

    def make_tiered_rule(self):
        return IncentiveRule.objects.create(
            name='services tiers', category='services', rule_type='multiple',
            applicable_role='all', center=self.center,
            tiers=[{'min_multiple': '1', 'incentive_percent': '3'},
                   {'min_multiple': '3', 'incentive_percent': '6'}],
        )

    def test_a_big_card_sale_does_not_raise_the_service_percentage(self):
        from marketing.models import ValueCard
        service = ServiceMaster.objects.create(name='Cut', category='Cuts', default_price=Decimal('1000.00'))
        card = ValueCard.objects.create(title='Elite', pre_tax_price=Decimal('100000.00'), value=Decimal('120000.00'), expiry_days=365)
        # 25,000 of services (1.25x of 20,000) and 100,000 of cards (5x).
        self.add_item(self.make_invoice('25000.00'), service, '25000.00', staff=self.stylist)
        self.add_item(self.make_invoice('100000.00'), card, '100000.00', staff=self.stylist)

        row = next(r for r in self.run_engine(rules=[self.make_tiered_rule()])
                   if r.staff_id == self.stylist.id)
        self.assertEqual(row.salary_multiple, Decimal('1.25'))
        self.assertEqual(row.incentive[incentives.CAT_SERVICE], Decimal('750.00'))   # 3%, not 6%

    def test_services_alone_reaching_the_tier_pays_it(self):
        service = ServiceMaster.objects.create(name='Cut', category='Cuts', default_price=Decimal('1000.00'))
        self.add_item(self.make_invoice('70000.00'), service, '70000.00', staff=self.stylist)

        row = next(r for r in self.run_engine(rules=[self.make_tiered_rule()])
                   if r.staff_id == self.stylist.id)
        self.assertEqual(row.salary_multiple, Decimal('3.50'))
        self.assertEqual(row.incentive[incentives.CAT_SERVICE], Decimal('4200.00'))  # 6%


class IncentiveCentreSelectionTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        self.third = Center.objects.create(center_name='Third')
        self.other_staff = StaffMember.objects.create(
            center=self.other_center, first_name='Omar', designation='Stylist', salary=Decimal('20000.00'))
        self.third_staff = StaffMember.objects.create(
            center=self.third, first_name='Tara', designation='Stylist', salary=Decimal('20000.00'))
        service = ServiceMaster.objects.create(name='Cut', category='Cuts', default_price=Decimal('1000.00'))
        for staff in (self.stylist, self.other_staff, self.third_staff):
            self.add_item(self.make_invoice('1000.00'), service, '1000.00', staff=staff)

    def names(self, **params):
        params.setdefault('start_date', datetime.date.today().replace(day=1).isoformat())
        params.setdefault('end_date', datetime.date.today().isoformat())
        response = self.api.get('/finance/api/reports/incentive-calculation/', params)
        self.assertEqual(response.status_code, 200, response.data)
        return {row['staff_name'] for row in response.data}

    def test_several_chosen_centres_are_reported_together(self):
        got = self.names(center_ids=f'{self.center.id},{self.third.id}')
        self.assertEqual(got, {'Ravi', 'Tara'})

    def test_no_selection_still_means_everything_the_user_may_see(self):
        self.assertEqual(self.names(), {'Ravi', 'Omar', 'Tara'})

    def test_a_manager_cannot_widen_their_view_with_center_ids(self):
        self.api.force_authenticate(self.manager)          # assigned to `center` only
        got = self.names(center_ids=f'{self.center.id},{self.other_center.id}')
        self.assertEqual(got, {'Ravi'})


class ValueCardOwnIncentiveTests(FinanceTestBase):
    """A card's own incentive (flat Rs on the card form) pays when no rule does."""

    def make_card(self, incentive='500'):
        return ValueCard.objects.create(
            title='Black', pre_tax_price=Decimal('20000.00'), post_tax_price=Decimal('21000.00'),
            value=Decimal('20000.00'), expiry_days=365, incentive=Decimal(incentive))

    def row(self, rules=None):
        return next(r for r in self.run_engine(rules=rules or []) if r.staff_id == self.stylist.id)

    def test_the_cards_own_incentive_pays_when_no_rule_applies(self):
        card = self.make_card('500')
        self.add_item(self.make_invoice('20000.00'), card, '20000.00', staff=self.stylist)
        self.assertEqual(self.row().incentive[incentives.CAT_VALUE_CARD], Decimal('500.00'))

    def test_it_is_paid_per_card_sold(self):
        card = self.make_card('500')
        self.add_item(self.make_invoice('40000.00'), card, '20000.00', quantity=2, staff=self.stylist)
        self.assertEqual(self.row().incentive[incentives.CAT_VALUE_CARD], Decimal('1000.00'))

    def test_a_rule_that_pays_wins_over_the_cards_own_incentive(self):
        card = self.make_card('500')
        self.add_item(self.make_invoice('20000.00'), card, '20000.00', staff=self.stylist)
        rule = IncentiveRule.objects.create(
            name='cards 3%', category='value_cards', rule_type='percentage',
            flat_percent=Decimal('3.00'), applicable_role='all', center=self.center)
        self.assertEqual(self.row(rules=[rule]).incentive[incentives.CAT_VALUE_CARD], Decimal('600.00'))

    def test_a_card_with_no_incentive_set_pays_nothing(self):
        card = self.make_card('0')
        self.add_item(self.make_invoice('20000.00'), card, '20000.00', staff=self.stylist)
        self.assertEqual(self.row().incentive[incentives.CAT_VALUE_CARD], Decimal('0.00'))

    def test_two_staff_share_the_cards_incentive(self):
        card = self.make_card('500')
        invoice = self.make_invoice('20000.00')
        item = self.add_item(invoice, card, '20000.00')
        item.staff_members.set([self.stylist, self.beautician])
        results = {r.staff_id: r.incentive[incentives.CAT_VALUE_CARD] for r in self.run_engine(rules=[])}
        self.assertEqual(results[self.stylist.id] + results[self.beautician.id], Decimal('500.00'))
