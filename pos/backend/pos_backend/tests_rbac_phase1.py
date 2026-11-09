"""Phase 1: fully role-based access (owner decision #6) and Admin > Settings.

Pins the blueprint findings fixed in this phase so they cannot come back:
R1 (no role-name powers), R2 (unprotected endpoints), R3/R4 (billing ticks),
R5 (shifts), R6 (stock requests), R7 (fulfil), R9/R10 (strict report reads),
R11 (owner-only actions now grantable), R12 (normalised login), R13 (chat),
R17 (transfer create), R18 (dashboard socket), R21 (closing amend), R22
(central service edit), R23 (tool scoping), #7 (deactivate, never delete),
D7 (no SAP login at sign-in), and the Settings API.
"""

import datetime
from decimal import Decimal

from asgiref.sync import async_to_sync
from channels.testing import WebsocketCommunicator
from django.test import TestCase, TransactionTestCase
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory.models import Product, StockRequest
from pos_backend import app_settings
from pos_backend.models import AppSetting
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


def grant(**modules):
    out = {}
    for module, subs in modules.items():
        out[module] = {}
        for sub, letters in subs.items():
            entry = {'read': 'r' in letters, 'create': 'c' in letters,
                     'update': 'u' in letters, 'delete': 'd' in letters}
            out[module][sub] = entry
    return out


def with_actions(perms, module, sub, *actions):
    perms.setdefault(module, {}).setdefault(sub, {})
    perms[module][sub]['actions'] = {a: True for a in actions}
    return perms


class Base(TestCase):
    def setUp(self):
        self.branch = Center.objects.create(center_name='Main', gst_enabled=True)
        self.other = Center.objects.create(center_name='Other', gst_enabled=True)
        self.n = 0
        # Settings are cached for minutes; the test's rollback removes the rows
        # but not the cache, which then leaked a saved value into later tests.
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)

    def user(self, perms, branch=None, superuser=False):
        self.n += 1
        if superuser:
            u = CustomUser.objects.create_superuser(
                email=f's{self.n}@t.com', password='StrongPass!234', full_name='Super')
        else:
            role = Role.objects.create(name=f'Role {self.n}', permissions=perms)
            u = CustomUser.objects.create_user(
                email=f'u{self.n}@t.com', password='StrongPass!234', full_name=f'U{self.n}',
                role=role, center=branch or self.branch)
            u.centers.add(branch or self.branch)
        return u

    def api(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c


class NoPowerFromARoleNameTests(Base):
    """R1: a role called "owner" is just a role."""

    def test_named_owner_with_no_grants_is_refused_everywhere(self):
        api = self.api(self.user({}, superuser=False))
        role = Role.objects.create(name='owner', permissions={})
        u = CustomUser.objects.create_user(email='o@t.com', password='StrongPass!234',
                                           full_name='O', role=role, center=self.branch)
        api = self.api(u)
        for url in ('/billing/invoices/', '/staff/api/payrolls/', '/finance/api/register_summary/',
                    '/salon_admin/api/dashboard/summary/', '/inventory/api/products/'):
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403)

    def test_superuser_passes(self):
        api = self.api(self.user(None, superuser=True))
        self.assertEqual(api.get('/salon_admin/api/dashboard/summary/').status_code, 200)


class ProtectedEndpointTests(Base):
    """R2: endpoints that answered any signed-in user."""

    URLS = [
        '/salon_admin/api/dashboard/', '/salon_admin/api/dashboard/summary/',
        '/salon_admin/api/dashboard/revenues/', '/salon_admin/api/dashboard/clients/',
        '/salon_admin/api/dashboard/finance/', '/salon_admin/api/dashboard/staff/',
        '/salon_admin/api/dashboard/services_products/',
        '/staff/api/reports/revenue/', '/staff/api/reports/usage/',
        '/staff/api/reports/consumption/', '/inventory/api/low_stock/',
        '/salon_admin/api/sap-branches/', '/salon_admin/api/centers/bulk-import-template/',
        '/accounts/api/chat/users/', '/accounts/api/chat/unread/',
    ]

    def test_a_role_with_no_permissions_is_refused(self):
        api = self.api(self.user({}))
        for url in self.URLS:
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403)

    def test_dashboard_needs_dashboard_or_home(self):
        self.assertEqual(self.api(self.user(grant(dashboard={'analytics': 'r'})))
                         .get('/salon_admin/api/dashboard/summary/').status_code, 200)
        self.assertEqual(self.api(self.user(grant(overview={'home': 'r'})))
                         .get('/salon_admin/api/dashboard/summary/').status_code, 200)

    def test_staff_reports_need_reports_not_just_the_directory(self):
        """R9: directory access used to open everyone's earnings."""
        api = self.api(self.user(grant(staff={'directory': 'r'})))
        self.assertEqual(api.get('/staff/api/reports/revenue/').status_code, 403)
        api = self.api(self.user(grant(staff={'reports': 'r'})))
        self.assertEqual(api.get('/staff/api/reports/revenue/').status_code, 200)

    def test_inventory_users_still_get_fast_movers(self):
        api = self.api(self.user(grant(inventory={'products': 'r'})))
        self.assertEqual(api.get('/staff/api/reports/usage/').status_code, 200)


class StrictFinanceReadTests(Base):
    """R9/R10: one finance tab no longer reads every finance report."""

    def test_petty_cash_clerk_cannot_read_revenue_reports(self):
        api = self.api(self.user(grant(finance={'pettycash': 'rcu'})))
        for url in ('/finance/api/monthly_sales/', '/finance/api/detailed_revenues/',
                    '/finance/api/reports/tax/', '/finance/api/reports/staff-performance/'):
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403)

    def test_petty_cash_clerk_still_reads_what_the_closing_screen_needs(self):
        api = self.api(self.user(grant(finance={'pettycash': 'rcu'})))
        self.assertEqual(api.get('/finance/api/register_summary/').status_code, 200)
        self.assertEqual(api.get('/finance/api/shifts/').status_code, 200)

    def test_tax_report_has_its_own_tick(self):
        api = self.api(self.user(grant(finance={'tax_report': 'r'})))
        self.assertEqual(api.get('/finance/api/reports/tax/').status_code, 200)
        self.assertEqual(api.get('/finance/api/monthly_sales/').status_code, 403)

    def test_shifts_need_shifts_not_incentive_rules(self):
        """R5"""
        payload = {'center': self.branch.id, 'starting_float': '100.00'}
        self.assertEqual(self.api(self.user(grant(finance={'manage': 'rcu'})))
                         .post('/finance/api/shifts/', payload, format='json').status_code, 403)
        self.assertEqual(self.api(self.user(grant(finance={'shifts': 'rcu'})))
                         .post('/finance/api/shifts/', payload, format='json').status_code, 201)

    def test_incentive_rules_follow_the_manage_tick(self):
        """The Manage tick showed the rules tab but every save was refused,
        because the rules were governed by `incentives`."""
        manager = self.api(self.user(grant(finance={'manage': 'rcud'})))
        viewer = self.api(self.user(grant(finance={'incentives': 'rcud'})))
        for url in ('/finance/api/rules/', '/finance/api/incentives/'):
            with self.subTest(url):
                self.assertEqual(manager.get(url).status_code, 200)
                self.assertEqual(viewer.get(url).status_code, 200)
                # Incentives holders read the rules; only Manage changes them.
                self.assertEqual(viewer.post(url, {}, format='json').status_code, 403)
                self.assertNotEqual(manager.post(url, {}, format='json').status_code, 403)


class AdminTabsAreSeparateTests(Base):
    """Found in the live role check: holding Admin > Bills listed every login
    and every role, because Admin reads fell back to "any Admin tab"."""

    def test_one_admin_tab_does_not_read_users_roles_or_the_bill_audit(self):
        api = self.api(self.user(grant(admin={'bills': 'r'})))
        for url in ('/accounts/api/users/', '/salon_admin/api/roles/',
                    '/billing/change-logs/', '/app-settings/api/'):
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403)

    def test_each_screen_still_reads_what_it_shows(self):
        users_admin = self.api(self.user(grant(admin={'users': 'r'})))
        self.assertEqual(users_admin.get('/accounts/api/users/').status_code, 200)
        # The Users screen offers a role picker.
        self.assertEqual(users_admin.get('/salon_admin/api/roles/').status_code, 200)

        logs_reader = self.api(self.user(grant(logs={'activity': 'r'})))
        # The activity log filters by user.
        self.assertEqual(logs_reader.get('/accounts/api/users/').status_code, 200)
        self.assertEqual(logs_reader.get('/salon_admin/api/roles/').status_code, 403)

        roles_admin = self.api(self.user(grant(admin={'roles': 'r'})))
        self.assertEqual(roles_admin.get('/salon_admin/api/roles/').status_code, 200)
        self.assertEqual(roles_admin.get('/accounts/api/users/').status_code, 403)


class BillingScreenReadsTests(Base):
    """Found in the live role check: the Billing screen's Staff Activity card
    was refused for a cashier once staff reports became strict."""

    def test_the_till_reads_todays_staff_activity_but_not_other_staff_reports(self):
        api = self.api(self.user(grant(billing={'new_invoice': 'rc', 'invoices': 'r'})))
        self.assertEqual(api.get('/staff/api/reports/revenue/').status_code, 200)
        self.assertEqual(api.get('/staff/api/reports/consumption/').status_code, 403)


class ClosingAmendTests(Base):
    """R21: a recorded closing changes only with "amend closing", and is logged."""

    def setUp(self):
        super().setUp()
        from finance.models import DailyClosing
        self.closing = DailyClosing.objects.create(
            center=self.branch, date=datetime.date.today(), cash_in_hand=Decimal('100'))

    def test_update_without_the_tick_is_refused(self):
        api = self.api(self.user(grant(finance={'pettycash': 'rcud'})))
        r = api.patch(f'/finance/api/daily-closing/{self.closing.id}/',
                      {'cash_in_hand': '5'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.assertIn('amend closing', str(r.data).lower())

    def test_update_with_the_tick_is_logged(self):
        from audit_logs.models import SystemLog
        perms = with_actions(grant(finance={'pettycash': 'rcu'}), 'finance', 'pettycash', 'amend_closing')
        api = self.api(self.user(perms))
        r = api.patch(f'/finance/api/daily-closing/{self.closing.id}/',
                      {'cash_in_hand': '5'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(SystemLog.objects.filter(entity_type='Daily Closing',
                                                 entity_id=str(self.closing.id)).exists())


class InventoryScopeTests(Base):
    def setUp(self):
        super().setUp()
        self.product = Product.objects.create(name='Shampoo', price=Decimal('300'),
                                              current_stock=10, center=self.branch)

    def test_stock_requests_list_works_for_the_branch_user(self):
        """R6: this crashed for everyone but the owner."""
        StockRequest.objects.create(requesting_center=self.branch, notes='mine')
        StockRequest.objects.create(requesting_center=self.other, notes='theirs')
        api = self.api(self.user(grant(inventory={'stock_requests': 'rc'})))
        r = api.get('/inventory/api/stock_requests/')
        self.assertEqual(r.status_code, 200)
        rows = r.data if isinstance(r.data, list) else r.data['results']
        self.assertEqual([x['notes'] for x in rows], ['mine'])

    def test_approving_needs_update_on_approve(self):
        """R7: read or create on the approve tick is not enough, and a status can no longer be typed in."""
        req = StockRequest.objects.create(requesting_center=self.branch)
        api = self.api(self.user(grant(inventory={'stock_requests': 'ru',
                                                  'approve_stock_requests': 'rc'})))
        r = api.post(f'/inventory/api/stock_requests/{req.id}/approve/', {}, format='json')
        self.assertEqual(r.status_code, 403)
        api.patch(f'/inventory/api/stock_requests/{req.id}/', {'status': 'Fulfilled'}, format='json')
        self.assertEqual(StockRequest.objects.get(pk=req.id).status, 'Pending')
        ok = self.api(self.user(grant(inventory={'approve_stock_requests': 'ru'})))
        self.assertEqual(ok.post(f'/inventory/api/stock_requests/{req.id}/approve/', {}, format='json').status_code, 200)

    def test_a_branch_user_can_create_a_transfer_from_their_branch(self):
        """R17: this crashed for every non-owner."""
        api = self.api(self.user(grant(inventory={'stock_transfers': 'rc'})))
        r = api.post('/inventory/api/stock_transfers/', {
            'from_center': self.branch.id, 'to_center': self.other.id,
            'items': [{'product': self.product.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def test_nobody_sends_stock_from_a_branch_they_are_not_in(self):
        api = self.api(self.user(grant(inventory={'stock_transfers': 'rc'}), branch=self.other))
        r = api.post('/inventory/api/stock_transfers/', {
            'from_center': self.branch.id, 'to_center': self.other.id,
            'items': [{'product': self.product.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(r.status_code, 403)


class ServiceCentralEditTests(Base):
    """R22: a branch user can no longer change the central price by accident."""

    def setUp(self):
        super().setUp()
        self.service = ServiceMaster.objects.create(name='Cut', default_price=Decimal('500'),
                                                    tax_percentage=Decimal('5'))

    def test_branch_user_without_a_branch_is_refused(self):
        api = self.api(self.user(grant(admin={'services': 'ru'})))
        r = api.patch(f'/services/api/master/{self.service.id}/', {'default_price': '1'}, format='json')
        self.assertEqual(r.status_code, 403)
        self.service.refresh_from_db()
        self.assertEqual(self.service.default_price, Decimal('500'))

    def test_branch_user_with_their_branch_sets_an_override(self):
        api = self.api(self.user(grant(admin={'services': 'ru'})))
        r = api.patch(f'/services/api/master/{self.service.id}/?center_id={self.branch.id}',
                      {'default_price': '450'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.service.refresh_from_db()
        self.assertEqual(self.service.default_price, Decimal('500'))


class UserAccountTests(Base):
    def test_delete_deactivates_and_keeps_the_user(self):
        """#7"""
        admin = self.user(grant(admin={'users': 'rcud'}))
        target = self.user({})
        r = self.api(admin).delete(f'/accounts/api/users/{target.id}/')
        self.assertEqual(r.status_code, 200)
        target.refresh_from_db()
        self.assertFalse(target.is_active)

    def test_reset_password_needs_the_tick(self):
        target = self.user({})
        no_tick = self.api(self.user(grant(admin={'users': 'rcud'})))
        self.assertEqual(no_tick.post(f'/accounts/api/users/{target.id}/reset-password/',
                                      {'new_password': 'Another!Pass987'}, format='json').status_code, 403)
        perms = with_actions(grant(admin={'users': 'ru'}), 'admin', 'users', 'reset_password')
        r = self.api(self.user(perms)).post(f'/accounts/api/users/{target.id}/reset-password/',
                                            {'new_password': 'Another!Pass987'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_login_sends_normalised_permissions_and_no_sap_session(self):
        """R12 and D7."""
        role = Role.objects.create(name='Legacy', permissions={
            'marketing': {'campaigns': {'read': True}}})
        CustomUser.objects.create_user(email='legacy@t.com', password='StrongPass!234',
                                       full_name='L', role=role, center=self.branch)
        r = APIClient().post('/accounts/api/login/',
                             {'username': 'legacy@t.com', 'password': 'StrongPass!234'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        perms = r.data['permissions']
        self.assertTrue(perms['marketing']['promotions']['read'])
        self.assertFalse(perms['billing']['invoices']['read'])
        self.assertIn('actions', perms['billing']['invoices'])
        self.assertFalse(any(k.startswith('sap') for k in r.data))


class SettingsApiTests(Base):
    def test_needs_the_settings_permission(self):
        self.assertEqual(self.api(self.user({})).get('/app-settings/api/').status_code, 403)

    def test_all_branch_role_changes_a_global_value_and_it_takes_effect(self):
        perms = full_access_permissions()
        api = self.api(self.user(perms))
        r = api.patch('/app-settings/api/', {'changes': [
            {'key': 'wallets.scope', 'center': None, 'value': 'center'}]}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        from billing.wallets import wallet_scope
        self.assertEqual(wallet_scope(), 'center')
        # Back to the server default.
        api.patch('/app-settings/api/', {'changes': [
            {'key': 'wallets.scope', 'center': None, 'value': None}]}, format='json')
        self.assertEqual(wallet_scope(), 'global')

    def test_branch_user_may_only_change_their_branch(self):
        api = self.api(self.user(grant(admin={'settings': 'ru'})))
        r = api.patch('/app-settings/api/', {'changes': [
            {'key': 'billing.discount_audit_percent', 'center': None, 'value': 30}]}, format='json')
        self.assertEqual(r.status_code, 403)
        r = api.patch('/app-settings/api/', {'changes': [
            {'key': 'billing.discount_audit_percent', 'center': self.branch.id, 'value': 30}]},
            format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(app_settings.get('billing.discount_audit_percent', center=self.branch), 30)
        self.assertEqual(app_settings.get('billing.discount_audit_percent'), 50)

    def test_invalid_values_are_rejected_and_nothing_is_saved(self):
        api = self.api(self.user(full_access_permissions()))
        r = api.patch('/app-settings/api/', {'changes': [
            {'key': 'billing.discount_audit_percent', 'center': None, 'value': 500},
            {'key': 'wallets.scope', 'center': None, 'value': 'center'}]}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(AppSetting.objects.exists())

    def test_every_change_is_audited(self):
        from audit_logs.models import SystemLog
        api = self.api(self.user(full_access_permissions()))
        api.patch('/app-settings/api/', {'changes': [
            {'key': 'staff.package_redemption_earns_commission', 'center': None, 'value': True}]},
            format='json')
        self.assertTrue(SystemLog.objects.filter(module='SETTINGS').exists())


class DashboardSocketTests(TransactionTestCase):
    """R18: the live dashboard socket needs a token and the Dashboard permission."""

    def _connect(self, token):
        from pos_backend.consumers import DashboardConsumer

        async def go():
            comm = WebsocketCommunicator(DashboardConsumer.as_asgi(), f'/ws/dashboard/?token={token}')
            connected, _ = await comm.connect(3)
            if connected:
                await comm.disconnect()
            return connected
        return async_to_sync(go)()

    def test_no_token_is_refused(self):
        self.assertFalse(self._connect(''))

    def test_a_role_without_dashboard_is_refused(self):
        center = Center.objects.create(center_name='M')
        role = Role.objects.create(name='None', permissions={})
        u = CustomUser.objects.create_user(email='n@t.com', password='StrongPass!234',
                                           full_name='N', role=role, center=center)
        self.assertFalse(self._connect(Token.objects.create(user=u).key))

    def test_a_dashboard_user_connects(self):
        center = Center.objects.create(center_name='M')
        role = Role.objects.create(name='Dash', permissions=grant(dashboard={'analytics': 'r'}))
        u = CustomUser.objects.create_user(email='d@t.com', password='StrongPass!234',
                                           full_name='D', role=role, center=center)
        u.centers.add(center)
        self.assertTrue(self._connect(Token.objects.create(user=u).key))
