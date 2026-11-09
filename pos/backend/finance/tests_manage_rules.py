"""Finance > Manage (incentive rules): saving, copying, scoping and validation."""

from decimal import Decimal

from accounts.models import CustomUser
from finance.models import IncentiveRule
from finance.tests import FinanceTestBase
from rest_framework.test import APIClient
from salon_admin.models import Role


def daily_payload(**extra):
    """What the Manage screen sends for a Daily Business Slab: BOTH the engine's
    `tiers` and its own `slabs` rows."""
    body = {
        'name': 'Daily slab', 'category': 'daily_business', 'frequency': 'daily', 'rule_type': 'slab',
        'applicable_role': 'all', 'flat_percent': 5, 'flat_amount': 200, 'effective_from': '2026-01-01',
        'tiers': [{'min_amount': 8000, 'bonus_type': 'flat', 'bonus_amount': 350, 'bonus_percent': 0},
                  {'min_amount': 20000, 'bonus_type': 'percentage', 'bonus_amount': 0, 'bonus_percent': 5}],
        'slabs': [{'min_amount': 8000, 'type': 'flat', 'incentive_amount': 350},
                  {'min_amount': 20000, 'type': 'percent', 'incentive_amount': 5}],
    }
    body.update(extra)
    return body


class ManageRuleSavingTests(FinanceTestBase):
    def test_a_daily_slab_keeps_the_engines_tiers_not_the_screens_rows(self):
        r = self.api.post('/finance/api/rules/', daily_payload(), format='json')
        self.assertEqual(r.status_code, 201, r.data)
        rule = IncentiveRule.objects.get(pk=r.data['id'])
        self.assertEqual(rule.tiers[0]['bonus_amount'], 350)
        self.assertEqual(rule.tiers[1]['bonus_type'], 'percentage')

    def test_an_add_on_and_a_target_keep_their_keywords(self):
        addon = self.api.post('/finance/api/rules/', {
            'name': 'Add-on', 'category': 'service_addon', 'frequency': 'daily', 'rule_type': 'slab',
            'applicable_role': 'all', 'effective_from': '2026-01-01',
            'tiers': [{'service_name': 'Fusio', 'match_keyword': 'fusio', 'worth_amount': 0, 'incentive_amount': 100}],
            'slabs': [{'name': 'Fusio', 'keyword': 'fusio', 'worth': 0, 'incentive_amount': 100}]}, format='json')
        self.assertEqual(addon.status_code, 201, addon.data)
        self.assertEqual(IncentiveRule.objects.get(pk=addon.data['id']).tiers[0]['match_keyword'], 'fusio')
        target = self.api.post('/finance/api/rules/', {
            'name': 'Target', 'category': 'service_target', 'frequency': 'daily', 'rule_type': 'target',
            'applicable_role': 'all', 'effective_from': '2026-01-01',
            'tiers': [{'service_name': 'Ritual', 'match_keyword': 'ritual', 'target_count': 5, 'reward_amount': 500}],
            'slabs': [{'name': 'Ritual', 'keyword': 'ritual', 'min_amount': 5, 'incentive_amount': 500}]}, format='json')
        self.assertEqual(IncentiveRule.objects.get(pk=target.data['id']).tiers[0]['target_count'], 5)

    def test_value_card_slabs_still_come_from_the_slabs(self):
        slabs = [{'name': 'Elite', 'min_amount': 11000, 'max_amount': 14000, 'incentive_amount': 200}]
        r = self.api.post('/finance/api/rules/', {
            'name': 'Cards', 'category': 'value_cards', 'frequency': 'monthly', 'rule_type': 'slabs',
            'applicable_role': 'all', 'effective_from': '2026-01-01', 'slabs': slabs}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(IncentiveRule.objects.get(pk=r.data['id']).tiers[0]['name'], 'Elite')

    def test_senseless_tiers_are_refused(self):
        bad = {
            'negative bonus': daily_payload(tiers=[{'min_amount': 1000, 'bonus_type': 'flat', 'bonus_amount': -50}], slabs=[]),
            'over 100%': daily_payload(tiers=[{'min_amount': 1000, 'bonus_type': 'percentage', 'bonus_percent': 150}], slabs=[]),
            'same level twice': daily_payload(tiers=[{'min_amount': 1000, 'bonus_amount': 10},
                                                     {'min_amount': 1000, 'bonus_amount': 20}], slabs=[]),
        }
        for label, body in bad.items():
            self.assertEqual(self.api.post('/finance/api/rules/', body, format='json').status_code, 400, label)
        no_keyword = {'name': 'T', 'category': 'service_target', 'frequency': 'daily', 'rule_type': 'target',
                      'applicable_role': 'all', 'effective_from': '2026-01-01',
                      'tiers': [{'service_name': '', 'match_keyword': '', 'target_count': 5, 'reward_amount': 100}]}
        self.assertEqual(self.api.post('/finance/api/rules/', no_keyword, format='json').status_code, 400)
        zero_target = dict(no_keyword, tiers=[{'match_keyword': 'x', 'target_count': 0, 'reward_amount': 100}])
        self.assertEqual(self.api.post('/finance/api/rules/', zero_target, format='json').status_code, 400)

    def test_the_default_multiple_tiers_are_accepted(self):
        r = self.api.post('/finance/api/rules/', {
            'name': 'Services', 'category': 'services', 'frequency': 'monthly', 'rule_type': 'multiple',
            'applicable_role': 'all', 'effective_from': '2026-01-01',
            'tiers': [{'min_multiple': 5.0, 'max_multiple': 5.99, 'incentive_percent': 3.0},
                      {'min_multiple': 6.0, 'max_multiple': 6.99, 'incentive_percent': 6.0},
                      {'min_multiple': 7.0, 'max_multiple': None, 'incentive_percent': 7.0}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)


class ManageRuleScopeTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        role = Role.objects.create(name='Branch finance', permissions={
            'all_centers': False,
            'finance': {'manage': {'read': True, 'create': True, 'update': True, 'delete': True}}})
        self.branch = CustomUser.objects.create_user(email='b@test.com', password='pw', full_name='Branch',
                                                     role=role, center=self.center)
        self.branch.centers.set([self.center])
        self.org_rule = IncentiveRule.objects.create(name='Org', category='services', rule_type='percentage',
                                                     flat_percent=Decimal('5'), applicable_role='all', center=None)
        self.own_rule = IncentiveRule.objects.create(name='Own', category='services', rule_type='percentage',
                                                     flat_percent=Decimal('5'), applicable_role='all', center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.branch)

    def test_a_branch_user_cannot_edit_or_delete_an_organisation_wide_rule(self):
        self.assertEqual(self.api.patch(f'/finance/api/rules/{self.org_rule.id}/', {'name': 'Hacked'},
                                        format='json').status_code, 403)
        self.assertEqual(self.api.delete(f'/finance/api/rules/{self.org_rule.id}/').status_code, 403)
        self.org_rule.refresh_from_db()
        self.assertEqual(self.org_rule.name, 'Org')

    def test_a_branch_user_cannot_create_an_organisation_wide_rule(self):
        body = daily_payload(center=None)
        self.assertEqual(self.api.post('/finance/api/rules/', body, format='json').status_code, 403)

    def test_a_branch_user_manages_their_own_centres_rules(self):
        self.assertEqual(self.api.patch(f'/finance/api/rules/{self.own_rule.id}/', {'name': 'Mine'},
                                        format='json').status_code, 200)
        body = daily_payload(center=self.center.id)
        self.assertEqual(self.api.post('/finance/api/rules/', body, format='json').status_code, 201)

    def test_a_rule_cannot_be_moved_to_another_centre_or_to_everyone(self):
        for target in (self.other_center.id, None):
            r = self.api.patch(f'/finance/api/rules/{self.own_rule.id}/', {'center': target}, format='json')
            self.assertEqual(r.status_code, 403, target)

    def test_the_owner_can_manage_an_organisation_wide_rule(self):
        self.api.force_authenticate(self.owner)
        self.assertEqual(self.api.patch(f'/finance/api/rules/{self.org_rule.id}/', {'name': 'Renamed'},
                                        format='json').status_code, 200)


class ManageRuleDuplicateTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        self.rule = IncentiveRule.objects.create(name='Base', category='services', rule_type='percentage',
                                                 flat_percent=Decimal('5'), applicable_role='all',
                                                 center=self.center)

    def test_the_copy_goes_to_the_chosen_centre(self):
        r = self.api.post(f'/finance/api/rules/{self.rule.id}/duplicate/',
                          {'target_center_id': self.other_center.id}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(IncentiveRule.objects.get(pk=r.data['id']).center_id, self.other_center.id)

    def test_an_empty_target_means_organisation_wide(self):
        r = self.api.post(f'/finance/api/rules/{self.rule.id}/duplicate/', {'target_center_id': None}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(IncentiveRule.objects.get(pk=r.data['id']).center_id)

    def test_no_target_keeps_the_original_centre(self):
        r = self.api.post(f'/finance/api/rules/{self.rule.id}/duplicate/', {}, format='json')
        self.assertEqual(IncentiveRule.objects.get(pk=r.data['id']).center_id, self.center.id)
