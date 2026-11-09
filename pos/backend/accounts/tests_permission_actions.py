"""
"I gave the user permission for that module and it still says no permission."

Reported from real testing, and reproduced here. Two causes, both structural.

1. Every custom @action is a POST, and POST maps to the verb 'create'. So any
   action that is really an UPDATE demanded Add permission:
     payrolls/lock, payrolls/unlock, payrolls/mark_paid, shifts/close_shift,
     clients/carry_over, center-services/override, staff/sync_transfers.
   A manager granted Edit on Payrolls was told they had no permission to lock a
   payroll.

2. An action can hang off a viewset belonging to a DIFFERENT submodule than the
   action does. `products/checkout` and `products/audit` live on ProductViewSet,
   so they demanded permission on Product Master - not on Checkout and Stock
   Audit, the two submodules that actually govern them. Granting someone
   Checkout did not let them check anything out.

And one more, which caused false denials on ordinary screen loads: reading was
governed per-submodule, but a list endpoint is shared across a module's tabs.
The product list is read by Product Master, by Checkout and by Stock Audit, so a
user granted only Checkout could not load the products they were checking out.
Reads are now governed at MODULE level; writes stay strictly per-submodule.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client
from inventory.models import Product
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from staff.models import PayrollRecord, StaffMember


def grant(**modules):
    """Build a role permissions dict. `grant(inventory={'checkout': 'rcud'})`."""
    out = {}
    for module, submodules in modules.items():
        out[module] = {}
        for sub, letters in submodules.items():
            out[module][sub] = {
                'read': 'r' in letters,
                'create': 'c' in letters,
                'update': 'u' in letters,
                'delete': 'd' in letters,
            }
    return out


class ActionVerbTestBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('300.00'), gst_percent=Decimal('18.00'),
            current_stock=50, center=self.center)

    def user_with(self, permissions, email=None):
        role = Role.objects.create(name=f'R{id(permissions)}', permissions=permissions)
        user = CustomUser.objects.create_user(
            email=email or f'u{id(permissions)}@test.com',
            password='StrongPass!234', full_name='U', role=role, center=self.center)
        client = APIClient()
        client.force_authenticate(user)
        return client


class PayrollActionTests(ActionVerbTestBase):
    """Payroll finalisation is a set of grantable action ticks (decision #6).

    `generate`, `lock`, `unlock` and `mark_paid` used to be hard-wired to a role
    NAMED "owner". They are now ticks on Staff > Payrolls: CRUD on payrolls
    alone does not grant them, the tick does, and the refusal names the tick.
    """

    def setUp(self):
        super().setUp()
        today = datetime.date.today()
        self.payroll = PayrollRecord.objects.create(
            staff=self.staff, center=self.center,
            month=today.month, year=today.year,
            base_salary=Decimal('20000.00'),
        )

    def test_crud_on_payrolls_does_not_grant_finalisation_and_says_why(self):
        api = self.user_with(grant(staff={'payrolls': 'rcud'}))
        for action in ('lock', 'unlock', 'mark_paid', 'generate'):
            with self.subTest(action):
                url = (f'/staff/api/payrolls/{action}/' if action == 'generate'
                       else f'/staff/api/payrolls/{self.payroll.id}/{action}/')
                response = api.post(url, {}, format='json')
                self.assertEqual(response.status_code, 403)
                # The message names the missing tick, so the operator knows
                # exactly what to add to the role.
                self.assertIn(action.replace('_', ' '), str(response.data).lower())

    def test_the_lock_tick_grants_lock_and_nothing_else(self):
        perms = grant(staff={'payrolls': 'r'})
        perms['staff']['payrolls']['actions'] = {'lock': True}
        api = self.user_with(perms)

        self.assertNotEqual(
            api.post(f'/staff/api/payrolls/{self.payroll.id}/lock/', {}, format='json').status_code,
            403)
        self.assertEqual(
            api.post(f'/staff/api/payrolls/{self.payroll.id}/mark_paid/', {}, format='json').status_code,
            403)

    def test_a_role_named_owner_without_ticks_cannot_lock(self):
        role = Role.objects.create(name='Owner', permissions=grant(staff={'payrolls': 'rcud'}))
        user = CustomUser.objects.create_user(
            email='named@test.com', password='StrongPass!234', full_name='Named',
            role=role, center=self.center)
        api = APIClient()
        api.force_authenticate(user)
        response = api.post(f'/staff/api/payrolls/{self.payroll.id}/lock/', {}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_full_access_role_can_lock(self):
        role = Role.objects.create(name='Manager', permissions=full_access_permissions())
        user = CustomUser.objects.create_user(
            email='manager@test.com', password='StrongPass!234', full_name='Manager',
            role=role, center=self.center)
        api = APIClient()
        api.force_authenticate(user)
        response = api.post(f'/staff/api/payrolls/{self.payroll.id}/lock/', {}, format='json')
        self.assertNotEqual(response.status_code, 403)

    def test_reading_payrolls_only_needs_read(self):
        api = self.user_with(grant(staff={'payrolls': 'r'}))
        self.assertEqual(api.get('/staff/api/payrolls/').status_code, 200)

    def test_a_user_with_no_staff_permission_cannot_read_payrolls(self):
        api = self.user_with(grant(billing={'invoices': 'rcud'}))
        self.assertEqual(api.get('/staff/api/payrolls/').status_code, 403)


class UpdateVerbTests(ActionVerbTestBase):
    """The verb fix, tested where no owner-only guard hides it.

    Every custom @action is a POST and POST mapped to 'create', so an action that
    is really an update demanded Add permission.
    """

    def test_closing_a_shift_needs_edit_not_add(self):
        from finance.models import Shift

        shift = Shift.objects.create(
            center=self.center, starting_float=Decimal('1000.00'))

        # Edit alone must be enough. Shifts have their own submodule now
        # (finding R5) - they used to sit under Finance > Manage.
        api = self.user_with(grant(finance={'shifts': 'ru'}))
        response = api.post(f'/finance/api/shifts/{shift.id}/close_shift/',
                            {'closing_balance': '1500.00'}, format='json')
        self.assertNotEqual(
            response.status_code, 403,
            'Edit on Finance > Register Shifts was refused permission to close a shift')

    def test_closing_a_shift_is_refused_with_read_only(self):
        from finance.models import Shift

        shift = Shift.objects.create(
            center=self.center, starting_float=Decimal('1000.00'))
        api = self.user_with(grant(finance={'shifts': 'r'}))
        response = api.post(f'/finance/api/shifts/{shift.id}/close_shift/',
                            {'closing_balance': '1500.00'}, format='json')
        self.assertEqual(response.status_code, 403)

    def test_a_center_service_override_needs_edit_not_add(self):
        from services.models import ServiceMaster

        service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))
        api = self.user_with(grant(admin={'services': 'ru'}))
        response = api.post('/services/api/center/override/', {
            'center': self.center.id, 'service': service.id, 'price': '600.00',
        }, format='json')
        self.assertNotEqual(
            response.status_code, 403,
            'Edit on Admin > Services was refused permission to set a branch price')


class SubmoduleRoutingTests(ActionVerbTestBase):
    """checkout and audit are their own submodules, not Product Master."""

    def test_checkout_permission_can_check_stock_out(self):
        api = self.user_with(grant(inventory={'checkout': 'rc'}))
        response = api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 1}],
            'notes': 'salon use',
        }, format='json')
        self.assertNotEqual(
            response.status_code, 403,
            'a user granted Checkout was refused permission to check out')

    def test_product_master_permission_alone_cannot_check_out(self):
        """Checkout is its own grant. Managing the catalogue is not the same as
        taking stock out of it."""
        api = self.user_with(grant(inventory={'products': 'rcud'}))
        response = api.post('/inventory/api/products/checkout/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'quantity': 1}],
        }, format='json')
        self.assertEqual(response.status_code, 403)

    def test_audit_permission_can_run_a_stock_audit(self):
        api = self.user_with(grant(inventory={'audit': 'ru'}))
        response = api.post('/inventory/api/products/audit/', {
            'center_id': self.center.id,
            'items': [{'product_id': self.product.id, 'counted_quantity': 48}],
        }, format='json')
        self.assertNotEqual(response.status_code, 403)

    def test_a_user_with_only_checkout_can_still_read_the_product_list(self):
        """They have to see what they are checking out.

        This was the false denial on ordinary screen loads: reads were governed
        per-submodule, so Checkout alone could not load the products.
        """
        api = self.user_with(grant(inventory={'checkout': 'rc'}))
        self.assertEqual(api.get('/inventory/api/products/').status_code, 200)

    def test_reading_one_submodule_does_not_grant_writing_another(self):
        """Module-level READ must not become module-level WRITE."""
        api = self.user_with(grant(inventory={'checkout': 'rc'}))
        response = api.post('/inventory/api/products/', {
            'name': 'Sneaky', 'price': '10.00', 'center': self.center.id,
        }, format='json')
        self.assertEqual(response.status_code, 403)

        response = api.patch(f'/inventory/api/products/{self.product.id}/',
                             {'price': '1.00'}, format='json')
        self.assertEqual(response.status_code, 403)


class ClientActionTests(ActionVerbTestBase):
    def test_carry_over_is_an_update(self):
        customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        api = self.user_with(grant(admin={'clients': 'ru'}))
        response = api.post(f'/clients/api/clients/{customer.id}/carry_over/',
                            {'amount': '100.00'}, format='json')
        self.assertNotEqual(
            response.status_code, 403,
            'Edit on Clients was refused permission to carry a balance over')

    def test_the_till_can_read_and_create_a_customer_but_not_edit_the_master(self):
        """A receptionist must register a walk-in without owning the customer
        master."""
        api = self.user_with(grant(billing={'new_invoice': 'rc', 'invoices': 'r'}))

        self.assertEqual(api.get('/clients/api/clients/').status_code, 200)

        created = api.post('/clients/api/clients/', {
            'first_name': 'Walkin', 'phone': '9998887776',
        }, format='json')
        self.assertNotEqual(created.status_code, 403)

        existing = Client.objects.create(
            first_name='Old', phone='9990000009', center=self.center)
        edited = api.patch(f'/clients/api/clients/{existing.id}/',
                           {'first_name': 'Renamed'}, format='json')
        self.assertEqual(
            edited.status_code, 403,
            'billing permission should not allow editing the customer master')


class ReadDelegationBoundaryTests(ActionVerbTestBase):
    def test_an_empty_role_reads_nothing(self):
        """None of the delegation may become a way in for a role that grants
        nothing at all."""
        api = self.user_with(grant())
        for url in ('/inventory/api/products/', '/clients/api/clients/',
                    '/staff/api/members/', '/services/api/master/',
                    '/marketing/api/memberships/'):
            with self.subTest(url):
                self.assertEqual(api.get(url).status_code, 403, url)

    def test_billing_read_does_not_grant_finance_reports(self):
        """Delegation is only for shared reference data, not for reports."""
        api = self.user_with(grant(billing={'invoices': 'rcud'}))
        response = api.get('/finance/api/rules/')
        self.assertEqual(response.status_code, 403)

    def test_billing_read_does_not_grant_the_activity_log(self):
        api = self.user_with(grant(billing={'invoices': 'rcud'}))
        response = api.get('/audit_logs/logs/')
        self.assertEqual(response.status_code, 403)
