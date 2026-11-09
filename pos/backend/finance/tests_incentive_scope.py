"""Incentive engine: who a rule may pay, and on which days.

Pins the 19 Oct 2026 review of the incentive and Manage modules:
  * add-on and target rules respect the person's role, their centre and the
    rule's dates (they used to pay everyone);
  * daily slabs and daily targets are judged day by day, so a month's payroll
    holds them;
  * a rule that changes in the middle of a month governs each side of the change;
  * the most specific add-on keyword wins, and a product is not a service done.
"""

import datetime
from decimal import Decimal

from finance.models import IncentiveRule
from finance.tests import FinanceTestBase
from services.models import ServiceMaster
from inventory.models import Product


class ScopeAndDaysBase(FinanceTestBase):
    def sell(self, service, price, staff, day, quantity=1, description=None):
        when = datetime.datetime.combine(day, datetime.time(11, 0))
        invoice = self.make_invoice(str(Decimal(price) * quantity), when=when)
        return self.add_item(invoice, service, price, quantity=quantity, staff=staff,
                             description=description)

    def row(self, results, staff):
        return next(r for r in results if r.staff_id == staff.id)

    def month_start(self):
        return datetime.date.today().replace(day=1)


class AddOnAndTargetScopeTests(ScopeAndDaysBase):
    def setUp(self):
        super().setUp()
        self.pedi = ServiceMaster.objects.create(name='Algae Pedicure', category='Nails',
                                                 default_price=Decimal('1800.00'))
        self.day = self.month_start()

    def addon(self, **kw):
        defaults = dict(name='pedi add-on', category='service_addon', rule_type='slab', frequency='daily',
                        applicable_role='pedicurist_k_ambassador',
                        tiers=[{'service_name': 'Algae', 'match_keyword': 'algae', 'incentive_amount': 150}])
        defaults.update(kw)
        return IncentiveRule.objects.create(**defaults)

    def test_an_add_for_pedicurists_does_not_pay_a_hair_stylist(self):
        self.sell(self.pedi, '1800.00', self.stylist, self.day)
        results = self.run_engine(rules=[self.addon()])
        self.assertEqual(self.row(results, self.stylist).addon_total, Decimal('0.00'))

    def test_it_does_pay_the_pedicurist_it_is_for(self):
        pedicurist = self.beautician
        pedicurist.designation = 'Pedicurist'
        pedicurist.save()
        self.sell(self.pedi, '1800.00', pedicurist, self.day)
        results = self.run_engine(rules=[self.addon()])
        self.assertEqual(self.row(results, pedicurist).addon_total, Decimal('150.00'))

    def test_another_centres_add_on_pays_nobody_here(self):
        self.sell(self.pedi, '1800.00', self.stylist, self.day)
        results = self.run_engine(rules=[self.addon(applicable_role='all', center=self.other_center)])
        self.assertEqual(self.row(results, self.stylist).addon_total, Decimal('0.00'))

    def test_an_add_on_that_ended_before_the_sale_pays_nothing(self):
        self.sell(self.pedi, '1800.00', self.stylist, self.day + datetime.timedelta(days=2))
        rule = self.addon(applicable_role='all', effective_to=self.day)
        results = self.run_engine(rules=[rule])
        self.assertEqual(self.row(results, self.stylist).addon_total, Decimal('0.00'))

    def test_the_longest_matching_keyword_wins(self):
        ritual = ServiceMaster.objects.create(name='Express Ritual Facial', category='Spa',
                                              default_price=Decimal('3600.00'))
        self.sell(ritual, '3600.00', self.stylist, self.day)
        rule = self.addon(applicable_role='all', tiers=[
            {'service_name': 'Ritual', 'match_keyword': 'ritual', 'incentive_amount': 50},
            {'service_name': 'Express Ritual', 'match_keyword': 'express ritual', 'incentive_amount': 150},
        ])
        results = self.run_engine(rules=[rule])
        self.assertEqual(self.row(results, self.stylist).addon_total, Decimal('150.00'))

    def test_a_target_for_pedicurists_does_not_pay_a_stylist(self):
        self.sell(self.pedi, '1800.00', self.stylist, self.day, quantity=10)
        rule = IncentiveRule.objects.create(
            name='pedi target', category='service_target', rule_type='target', frequency='daily',
            applicable_role='pedicurist_k_ambassador',
            tiers=[{'service_name': 'Algae', 'match_keyword': 'algae', 'target_count': 5, 'reward_amount': 500}])
        results = self.run_engine(rules=[rule])
        self.assertEqual(self.row(results, self.stylist).target_total, Decimal('0.00'))

    def test_a_product_with_the_keyword_is_not_a_service_done(self):
        oil = Product.objects.create(name='Ritual Oil', price=Decimal('500.00'), gst_percent=Decimal('18'),
                                     current_stock=50, center=self.center)
        invoice = self.make_invoice('5000.00',
                                    when=datetime.datetime.combine(self.day, datetime.time(11, 0)))
        self.add_item(invoice, oil, '500.00', quantity=10, staff=self.stylist)
        rule = IncentiveRule.objects.create(
            name='ritual target', category='service_target', rule_type='target', frequency='monthly',
            applicable_role='all',
            tiers=[{'service_name': 'Ritual', 'match_keyword': 'ritual', 'target_count': 5, 'reward_amount': 500}])
        results = self.run_engine(rules=[rule])
        self.assertEqual(self.row(results, self.stylist).target_total, Decimal('0.00'))


class DailyRulesAcrossAMonthTests(ScopeAndDaysBase):
    def setUp(self):
        super().setUp()
        self.cut = ServiceMaster.objects.create(name='Keratin Treatment', category='Hair',
                                                default_price=Decimal('1000.00'))
        self.d1 = self.month_start()
        self.d2 = self.d1 + datetime.timedelta(days=1)
        self.d3 = self.d1 + datetime.timedelta(days=2)

    def slab_rule(self):
        return IncentiveRule.objects.create(
            name='daily slab', category='daily_business', rule_type='slab', frequency='daily',
            applicable_role='all',
            tiers=[{'min_amount': 5000, 'bonus_type': 'flat', 'bonus_amount': 200},
                   {'min_amount': 10000, 'bonus_type': 'flat', 'bonus_amount': 500}])

    def test_each_day_is_read_against_the_slabs_on_its_own(self):
        self.sell(self.cut, '6000.00', self.stylist, self.d1)      # day 1: 6,000 -> 200
        self.sell(self.cut, '11000.00', self.stylist, self.d2)     # day 2: 11,000 -> 500
        self.sell(self.cut, '2000.00', self.stylist, self.d3)      # day 3: 2,000 -> nothing
        results = self.run_engine(rules=[self.slab_rule()], frequency='monthly',
                                  start=self.d1, end=self.d3)
        row = self.row(results, self.stylist)
        self.assertEqual(row.daily_bonus, Decimal('700.00'))
        self.assertEqual([d['bonus'] for d in row.daily_bonus_days], [Decimal('200.00'), Decimal('500.00')])
        self.assertEqual(row.as_dict()['daily_business_incentive'], Decimal('700.00'))

    def test_small_days_are_not_added_together_into_a_big_slab(self):
        for day in (self.d1, self.d2, self.d3):
            self.sell(self.cut, '4000.00', self.stylist, day)       # 12,000 over three days, 4,000 each
        results = self.run_engine(rules=[self.slab_rule()], frequency='monthly',
                                  start=self.d1, end=self.d3)
        self.assertEqual(self.row(results, self.stylist).daily_bonus, Decimal('0.00'))

    def test_a_daily_target_pays_for_every_day_it_is_met(self):
        for day in (self.d1, self.d2):
            self.sell(self.cut, '1000.00', self.stylist, day, quantity=5)
        self.sell(self.cut, '1000.00', self.stylist, self.d3, quantity=2)      # not met
        rule = IncentiveRule.objects.create(
            name='keratin daily', category='service_target', rule_type='target', frequency='daily',
            applicable_role='all',
            tiers=[{'service_name': 'Keratin', 'match_keyword': 'keratin', 'target_count': 5, 'reward_amount': 300}])
        results = self.run_engine(rules=[rule], frequency='monthly', start=self.d1, end=self.d3)
        row = self.row(results, self.stylist)
        self.assertEqual(row.target_total, Decimal('600.00'))
        self.assertEqual(len(row.target_achievements), 2)

    def test_a_monthly_target_is_read_on_the_whole_periods_count(self):
        for day in (self.d1, self.d2):
            self.sell(self.cut, '1000.00', self.stylist, day, quantity=3)
        rule = IncentiveRule.objects.create(
            name='keratin monthly', category='service_target', rule_type='target', frequency='monthly',
            applicable_role='all',
            tiers=[{'service_name': 'Keratin', 'match_keyword': 'keratin', 'target_count': 5, 'reward_amount': 300}])
        results = self.run_engine(rules=[rule], frequency='monthly', start=self.d1, end=self.d3)
        self.assertEqual(self.row(results, self.stylist).target_total, Decimal('300.00'))


class RuleChangedMidMonthTests(ScopeAndDaysBase):
    def test_each_side_of_the_change_uses_its_own_rule(self):
        cut = ServiceMaster.objects.create(name='Haircut', category='Cuts', default_price=Decimal('1000.00'))
        first = self.month_start()
        change = first + datetime.timedelta(days=10)
        self.sell(cut, '10000.00', self.stylist, first)                          # before the change
        self.sell(cut, '10000.00', self.stylist, change + datetime.timedelta(days=1))   # after it
        old = IncentiveRule.objects.create(
            name='old 5%', category='services', rule_type='percentage', flat_percent=Decimal('5.00'),
            applicable_role='all', effective_to=change - datetime.timedelta(days=1))
        new = IncentiveRule.objects.create(
            name='new 10%', category='services', rule_type='percentage', flat_percent=Decimal('10.00'),
            applicable_role='all', effective_from=change)
        results = self.run_engine(rules=[old, new], start=first, end=first + datetime.timedelta(days=20))
        row = self.row(results, self.stylist)
        # 10,000 at 5% + 10,000 at 10%, not 20,000 at 10%.
        self.assertEqual(row.incentive['services'], Decimal('1500.00'))
        self.assertIn('old 5%', row.rules_applied['services'])
        self.assertIn('new 10%', row.rules_applied['services'])
        self.assertEqual(sum(Decimal(str(line['calculated_incentive'])) for line in row.lines), Decimal('1500.00'))
