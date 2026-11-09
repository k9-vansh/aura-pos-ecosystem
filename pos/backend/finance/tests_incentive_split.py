"""Incentive report: the tier is read at the exact multiple, and monthly and daily
incentive are reported apart so every rupee can be traced to where it was earned."""

import datetime
from decimal import Decimal

from finance.models import IncentiveRule
from finance.tests_incentive_scope import ScopeAndDaysBase
from services.models import ServiceMaster


class ExactMultipleTests(ScopeAndDaysBase):
    """3x pays the 3x percentage, 3.99x still pays it, 4x pays the 4x one."""

    def setUp(self):
        super().setUp()
        self.svc = ServiceMaster.objects.create(name='Colour', category='Hair', default_price=Decimal('1000.00'))
        self.day = self.month_start()
        self.rule = IncentiveRule.objects.create(
            name='multiples', category='services', rule_type='multiple', frequency='monthly',
            applicable_role='all',
            tiers=[{'min_multiple': 3, 'incentive_percent': 3}, {'min_multiple': 4, 'incentive_percent': 6}])

    def paid(self, sales):
        # Ravi's salary is 20,000, so 60,000 of services is exactly 3x.
        self.sell(self.svc, sales, self.stylist, self.day)
        row = self.row(self.run_engine(rules=[self.rule]), self.stylist)
        return row.incentive['services']

    def test_exactly_three_times_pays_the_three_times_percentage(self):
        self.assertEqual(self.paid('60000.00'), Decimal('1800.00'))          # 3% of 60,000

    def test_just_under_four_times_still_pays_the_three_times_percentage(self):
        # 79,999.60 / 20,000 = 3.999998: rounded to two decimals that was 4.00 and paid 6%.
        self.assertEqual(self.paid('79999.60'), Decimal('2399.99'))          # 3% of 79,999.60

    def test_exactly_four_times_pays_the_four_times_percentage(self):
        self.assertEqual(self.paid('80000.00'), Decimal('4800.00'))          # 6% of 80,000

    def test_the_multiple_shown_is_cut_not_rounded(self):
        self.sell(self.svc, '79999.60', self.stylist, self.day)
        row = self.row(self.run_engine(rules=[self.rule]), self.stylist)
        self.assertEqual(row.as_dict()['salary_multiple'], Decimal('3.99'))

    def test_below_the_first_tier_the_staff_members_own_percentage_applies(self):
        self.stylist.commission_percentage = Decimal('10')
        self.stylist.save()
        self.sell(self.svc, '10000.00', self.stylist, self.day)     # 0.5x
        row = self.row(self.run_engine(rules=[self.rule]), self.stylist)
        self.assertEqual(row.incentive['services'], Decimal('1000.00'))


class MonthlyAndDailySplitTests(ScopeAndDaysBase):
    def setUp(self):
        super().setUp()
        self.fusio = ServiceMaster.objects.create(name='Fusio Dose', category='Hair', default_price=Decimal('600.00'))
        self.cut = ServiceMaster.objects.create(name='Haircut', category='Hair', default_price=Decimal('1000.00'))
        self.d1 = self.month_start()
        self.d2 = self.d1 + datetime.timedelta(days=1)
        self.stylist.commission_percentage = Decimal('10')
        self.stylist.save()
        self.rules = [
            IncentiveRule.objects.create(
                name='slab', category='daily_business', rule_type='slab', frequency='daily', applicable_role='all',
                tiers=[{'min_amount': 5000, 'bonus_type': 'flat', 'bonus_amount': 200}]),
            IncentiveRule.objects.create(
                name='addon', category='service_addon', rule_type='slab', frequency='daily', applicable_role='all',
                tiers=[{'service_name': 'Fusio', 'match_keyword': 'fusio', 'incentive_amount': 100}]),
            IncentiveRule.objects.create(
                name='target', category='service_target', rule_type='target', frequency='daily',
                applicable_role='all',
                tiers=[{'service_name': 'Haircut', 'match_keyword': 'haircut', 'target_count': 3, 'reward_amount': 150}]),
        ]
        self.sell(self.fusio, '600.00', self.stylist, self.d1)                 # add-on 100 + 10% of 600
        self.sell(self.cut, '1000.00', self.stylist, self.d1, quantity=5)      # day 1 sales 5,600 -> slab 200; target 150
        self.sell(self.cut, '1000.00', self.stylist, self.d2, quantity=1)      # day 2: nothing daily
        self.row_ = self.row(self.run_engine(rules=self.rules, start=self.d1, end=self.d2), self.stylist).as_dict()

    def test_monthly_and_daily_add_up_to_the_total(self):
        row = self.row_
        self.assertEqual(row['monthly_incentive'] + row['daily_incentive'], row['total_incentive'])

    def test_the_monthly_part_is_the_percentage_and_nothing_else(self):
        # 6,600 of services at the staff's 10%.
        self.assertEqual(self.row_['monthly_incentive'], Decimal('660.00'))
        self.assertEqual(self.row_['service_percent_incentive'], self.row_['monthly_incentive'])

    def test_the_daily_part_is_slab_add_on_and_target_each_by_name(self):
        row = self.row_
        self.assertEqual(row['daily_business_incentive'], Decimal('200.00'))
        self.assertEqual(row['service_addon_incentive'], Decimal('100.00'))
        self.assertEqual(row['service_target_incentive'], Decimal('150.00'))
        self.assertEqual(row['daily_incentive'], Decimal('450.00'))

    def test_a_bill_line_keeps_its_add_on_apart_from_its_percentage(self):
        line = next(l for l in self.row_['details'] if l['item_name'] == 'Fusio Dose')
        self.assertEqual(line['addon_amount'], Decimal('100.00'))
        self.assertEqual(line['monthly_amount'], Decimal('60.00'))
        self.assertEqual(line['incentive_amount'], Decimal('160.00'))
        self.assertIn('Fusio', line['addon_label'])
        self.assertNotIn('add-on', line['calculation_rule'])

    def test_every_rupee_has_a_place(self):
        row = self.row_
        lines = sum((l['monthly_amount'] for l in row['details']), Decimal('0'))
        self.assertEqual(lines, row['monthly_incentive'])
        days = sum((d['bonus'] for d in row['daily_bonus_days']), Decimal('0'))
        addons = sum((l['addon_amount'] for l in row['details']), Decimal('0'))
        targets = sum((t['reward'] for t in row['target_achievements']), Decimal('0'))
        self.assertEqual(days + addons + targets, row['daily_incentive'])
