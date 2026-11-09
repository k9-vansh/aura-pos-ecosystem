"""Each Dashboard tab is its own permission: a role can be given one tab and not the rest."""
from pos_backend.permission_tree import normalise_permissions, submodule_map
from pos_backend.tests_rbac_phase1 import Base, TestCase, grant

# tab -> the endpoints behind it (the tab's own data, and its report sections)
TABS = {
    'summary': ['/salon_admin/api/dashboard/', '/salon_admin/api/dashboard/summary/'],
    'revenues': ['/salon_admin/api/dashboard/revenues/', '/salon_admin/api/analytics/revenues/'],
    'clients': ['/salon_admin/api/dashboard/clients/', '/salon_admin/api/analytics/clients/'],
    'finance': ['/salon_admin/api/dashboard/finance/', '/salon_admin/api/analytics/memberships/',
                '/salon_admin/api/analytics/value-cards/'],
    'services_products': ['/salon_admin/api/dashboard/services_products/',
                          '/salon_admin/api/analytics/services/', '/salon_admin/api/analytics/products/'],
    'staff': ['/salon_admin/api/dashboard/staff/'],
    'usage_report': [f'/salon_admin/api/usage-report/{section}/' for section in (
        'overview', 'offers', 'coupons', 'packages', 'cashback', 'customers', 'whatsapp', 'forecast', 'verify')],
}
# The Usage Report tab shows the Finance tab's Memberships and Value cards reports too, so its tick opens them.
SHARED = {('usage_report', '/salon_admin/api/analytics/memberships/'),
          ('usage_report', '/salon_admin/api/analytics/value-cards/')}


class DashboardTabPermissions(Base):
    def test_the_tree_lists_every_tab(self):
        self.assertEqual(set(submodule_map()['dashboard']), set(TABS))

    def test_a_role_given_one_tab_opens_that_tab_and_no_other(self):
        for tab, urls in TABS.items():
            api = self.api(self.user(grant(dashboard={tab: 'r'})))
            for other, other_urls in TABS.items():
                for url in other_urls:
                    with self.subTest(granted=tab, url=url):
                        res = api.get(url)
                        if other == tab or (tab, url) in SHARED:
                            self.assertEqual(res.status_code, 200, (url, getattr(res, 'data', None)))
                        else:
                            self.assertEqual(res.status_code, 403, url)

    def test_a_role_saved_before_the_split_still_opens_every_tab_it_had(self):
        api = self.api(self.user(grant(dashboard={'analytics': 'r'})))
        for tab, urls in TABS.items():
            for url in urls:
                with self.subTest(url=url):
                    # The Usage Report came later and goes to the marketing team by its own tick: an old
                    # role does not get it (its two shared reports are Finance's, which the role has).
                    wanted = 403 if tab == 'usage_report' else 200
                    self.assertEqual(api.get(url).status_code, wanted, url)
        normal = normalise_permissions(grant(dashboard={'analytics': 'r'}))['dashboard']
        self.assertEqual({tab for tab, e in normal.items() if e['read']}, set(TABS) - {'usage_report'})

    def test_home_access_still_shows_the_overview_figures_only(self):
        api = self.api(self.user(grant(overview={'home': 'r'})))
        self.assertEqual(api.get('/salon_admin/api/dashboard/summary/').status_code, 200)
        self.assertEqual(api.get('/salon_admin/api/dashboard/revenues/').status_code, 403)

    def test_a_role_with_no_dashboard_tick_opens_nothing(self):
        api = self.api(self.user({}))
        for urls in TABS.values():
            for url in urls:
                with self.subTest(url=url):
                    self.assertEqual(api.get(url).status_code, 403)

    def test_live_updates_go_to_the_overview_and_revenue_tabs_only(self):
        from pos_backend.realtime import dashboard_groups_for
        self.assertTrue(dashboard_groups_for(self.user(grant(dashboard={'summary': 'r'}))))
        self.assertTrue(dashboard_groups_for(self.user(grant(dashboard={'revenues': 'r'}))))
        self.assertFalse(dashboard_groups_for(self.user(grant(dashboard={'clients': 'r'}))))
        self.assertTrue(dashboard_groups_for(self.user(grant(dashboard={'analytics': 'r'}))))


class DashboardTabRoleScreen(TestCase):
    def test_roles_api_accepts_a_single_tab(self):
        from rest_framework.test import APIClient
        from accounts.models import CustomUser
        su = CustomUser.objects.create_superuser(email='s@t.com', password='StrongPass!234', full_name='S')
        api = APIClient()
        api.force_authenticate(su)
        res = api.post('/salon_admin/api/roles/',
                       {'name': 'Clients chart only', 'permissions': grant(dashboard={'clients': 'r'})},
                       format='json')
        self.assertEqual(res.status_code, 201, res.data)
        stored = res.data['permissions']['dashboard']
        self.assertTrue(stored['clients']['read'])
        self.assertFalse(stored.get('revenues', {}).get('read', False))


class WalkInBillsOnClientsTab(Base):
    """A bill with no client attached is one unknown visitor, not a missing one."""

    def test_a_walk_in_bill_shows_in_the_unknown_trend_and_footfall(self):
        from decimal import Decimal
        from datetime import date
        from billing.models import Invoice
        Invoice.objects.create(client=None, center=self.branch, status='paid', subtotal=Decimal('2000'),
                               cgst=0, sgst=0, total_amount=Decimal('2000'), paid_amount=Decimal('2000'))
        api = self.api(self.user(grant(dashboard={'clients': 'r'})))
        today = date.today().isoformat()
        res = api.get(f'/salon_admin/api/dashboard/clients/?start_date={today}&end_date={today}')
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
        unknown = list(res.data['trends'].values())[-1]['unknown']
        self.assertEqual((unknown['count'], unknown['new'], unknown['repeat']), (1, 1, 0))
        self.assertEqual(unknown['revenue'], Decimal('2000'))
        self.assertEqual(res.data['footfall']['total']['unknown'], 1)
        self.assertEqual(res.data['daily_footfall'][-1]['count'], 1)
