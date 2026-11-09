"""Found by the 2026-10-21 permission audit: ticks that opened more than their name says."""
from pos_backend.tests_rbac_phase1 import Base, grant
from staff.models import StaffMember


class ListsStayWithTheirOwners(Base):
    LISTS = ['/clients/api/clients/', '/staff/api/members/', '/inventory/api/products/']

    def test_a_dashboard_tab_does_not_open_clients_staff_or_products(self):
        api = self.api(self.user(grant(dashboard={'revenues': 'r', 'clients': 'r'})))
        for url in self.LISTS:
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403)

    def test_it_still_opens_the_branch_list_its_selector_needs(self):
        api = self.api(self.user(grant(dashboard={'revenues': 'r'})))
        self.assertEqual(api.get('/salon_admin/api/centers/').status_code, 200)

    def test_marketing_and_finance_do_not_open_the_client_list(self):
        for perms in (grant(marketing={'promotions': 'r'}), grant(finance={'register_summary': 'r'})):
            with self.subTest(perms=list(perms)):
                self.assertEqual(self.api(self.user(perms)).get('/clients/api/clients/').status_code, 403)

    def test_the_till_and_calendar_still_find_clients_and_staff(self):
        for module, sub in (('billing', 'new_invoice'), ('appointments', 'calendar')):
            api = self.api(self.user(grant(**{module: {sub: 'r'}})))
            with self.subTest(module):
                self.assertEqual(api.get('/clients/api/clients/').status_code, 200)
                self.assertEqual(api.get('/staff/api/members/').status_code, 200)

    def test_finance_does_not_open_the_staff_directory(self):
        api = self.api(self.user(grant(finance={'register_summary': 'r'})))
        self.assertEqual(api.get('/staff/api/members/').status_code, 403)


class MarketingUsageIsItsOwnTick(Base):
    URL = '/marketing/api/promotions/usage_report/'

    def test_the_offers_list_does_not_open_the_usage_figures(self):
        for perms in (grant(marketing={'promotions': 'r'}), grant(billing={'new_invoice': 'r'})):
            with self.subTest(perms=list(perms)):
                self.assertEqual(self.api(self.user(perms)).get(self.URL).status_code, 403)

    def test_the_usage_tick_opens_it_without_the_offers_tick(self):
        self.assertEqual(self.api(self.user(grant(marketing={'usage': 'r'}))).get(self.URL).status_code, 200)


class TheLedgerIsReadOnly(Base):
    def test_nobody_writes_the_stock_ledger_through_the_api(self):
        # Not even a role file that carries write verbs the tree never offers.
        api = self.api(self.user(grant(inventory={'stock_history': 'rcud'})))
        self.assertEqual(api.get('/inventory/api/stock-transactions/').status_code, 200)
        self.assertEqual(api.post('/inventory/api/stock-transactions/', {}, format='json').status_code, 403)
        self.assertEqual(api.delete('/inventory/api/stock-transactions/1/').status_code, 403)
        su = self.api(self.user({}, superuser=True))
        self.assertEqual(su.post('/inventory/api/stock-transactions/', {}, format='json').status_code, 405)
        self.assertEqual(su.delete('/inventory/api/stock-transactions/1/').status_code, 405)

    def test_a_read_only_tick_never_writes_even_if_the_role_file_says_so(self):
        # finance.refunds offers read only; a hand-edited role carrying create must not gain it.
        from pos_backend.permissions import has_module_permission
        user = self.user(grant(finance={'refunds': 'rcud'}))
        self.assertTrue(has_module_permission(user, 'finance', 'read', 'refunds'))
        for verb in ('create', 'update', 'delete'):
            self.assertFalse(has_module_permission(user, 'finance', verb, 'refunds'), verb)


class ActionTicksNotOnAUrl(Base):
    """Action ticks the route walk cannot see (they are checked inside the view or serializer)."""

    def _with(self, perms, module, sub, *actions):
        from pos_backend.tests_rbac_phase1 import with_actions
        return with_actions(perms, module, sub, *actions)

    def test_centre_bulk_import_needs_its_tick(self):
        url = '/salon_admin/api/centers/bulk-import/'
        plain = self.api(self.user(grant(admin={'centers': 'rcud'})))
        self.assertEqual(plain.post(url, {}, format='multipart').status_code, 403)
        ticked = self.api(self.user(self._with(grant(admin={'centers': 'rcud'}), 'admin', 'centers', 'bulk_import')))
        self.assertEqual(ticked.post(url, {}, format='multipart').status_code, 400)     # past the gate: no file

    def test_integration_test_message_needs_its_tick(self):
        url = '/notifications/api/integrations/whatsapp/test/'
        plain = self.api(self.user(grant(admin={'integrations': 'rcud'})))
        self.assertEqual(plain.post(url, {}, format='json').status_code, 403)
        ticked = self.api(self.user(self._with(grant(admin={'integrations': 'rcud'}), 'admin', 'integrations', 'send_test')))
        self.assertEqual(ticked.post(url, {}, format='json').status_code, 400)          # past the gate: no address

    def test_a_staff_app_pin_needs_its_tick(self):
        person = StaffMember.objects.create(center=self.branch, first_name='Ravi', designation='Stylist')
        url = f'/staff/api/members/{person.id}/'
        plain = self.api(self.user(grant(staff={'directory': 'rcud'})))
        res = plain.patch(url, {'new_app_pin': '4821'}, format='json')
        self.assertEqual(res.status_code, 400, getattr(res, 'data', None))
        ticked = self.api(self.user(self._with(grant(staff={'directory': 'rcud'}), 'staff', 'directory', 'set_app_pin')))
        res = ticked.patch(url, {'new_app_pin': '4821'}, format='json')
        self.assertEqual(res.status_code, 200, getattr(res, 'data', None))
