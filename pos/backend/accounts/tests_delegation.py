"""QA report C2, H4 and the admin-page access mapping.

Admin > Users and Admin > Roles are trusted grants (owner decision), a new account
gets a real password, and an admin page works for the role granted it.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment
from clients.models import Client
from salon_admin.models import Center, Role
from pos_backend.testing import full_access_permissions

STRONG = 'Zq7!vLm29xTr'


def grant(**modules):
    """A role dict: grant(billing={'invoices': ['read']}, admin={'users': ['read', 'create']})."""
    perms = {'all_centers': False}
    for module, subs in modules.items():
        perms[module] = {}
        for sub, verbs in subs.items():
            entry = {v: True for v in verbs if v != 'cancel'}
            if 'cancel' in verbs:
                entry['actions'] = {'cancel': True}
            perms[module][sub] = entry
    return perms


class DelegationTests(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='A', display_name='A')
        self.other = Center.objects.create(center_name='B', display_name='B')
        self.hq = Role.objects.create(name='HQ', permissions=full_access_permissions())
        self.useradmin_role = Role.objects.create(
            name='User admin', permissions=grant(admin={'users': ['read', 'create', 'update'], 'roles': ['read']},
                          appointments={'calendar': ['read']}))   # the rights it may hand on
        self.low = Role.objects.create(name='Receptionist', permissions=grant(appointments={'calendar': ['read']}))
        self.admin = CustomUser.objects.create_user(
            email='ua@x.test', password=STRONG, full_name='UA', role=self.useradmin_role, center=self.centre)
        self.admin.centers.set([self.centre])
        self.api = APIClient()
        self.api.force_authenticate(self.admin)

    def new_user(self, **extra):
        body = {'email': 'n@x.test', 'full_name': 'N', 'password': STRONG,
                'role': self.low.id, 'center': self.centre.id, 'centers': [self.centre.id]}
        body.update(extra)
        return self.api.post('/accounts/api/users/', body, format='json')

    def test_can_create_a_user_with_a_smaller_role(self):
        self.assertEqual(self.new_user().status_code, 201)

    def test_admin_users_is_a_trusted_grant(self):
        """Owner decision: whoever holds Admin > Users may assign any role and centre."""
        self.assertEqual(self.new_user(role=self.hq.id).status_code, 201)
        r = self.new_user(email='m@x.test', center=self.other.id, centers=[self.other.id])
        self.assertEqual(r.status_code, 201, r.data)

    def test_a_user_needs_a_role_and_a_centre(self):
        no_role = self.new_user(email='nr@x.test', role=None)
        self.assertEqual(no_role.status_code, 400, no_role.data)
        self.assertIn('role', no_role.data)
        no_centre = self.new_user(email='nc@x.test', center=None, centers=[])
        self.assertEqual(no_centre.status_code, 400, no_centre.data)
        self.assertFalse(CustomUser.objects.filter(email__in=['nr@x.test', 'nc@x.test']).exists())

    def test_the_first_centre_becomes_the_home_centre(self):
        r = self.new_user(email='hc@x.test', center=None, centers=[self.other.id, self.centre.id])
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(CustomUser.objects.get(email='hc@x.test').center_id, self.other.id)

    def test_only_the_superuser_can_change_the_superuser(self):
        owner = CustomUser.objects.create_superuser(email='o2@x.test', password=STRONG, full_name='O')
        r = self.api.patch(f'/accounts/api/users/{owner.id}/', {'full_name': 'Renamed'}, format='json')
        self.assertIn(r.status_code, (403, 404), r.data)

    def test_the_superuser_can_assign_anything(self):
        owner = CustomUser.objects.create_superuser(email='o@x.test', password=STRONG, full_name='O')
        self.api.force_authenticate(owner)
        self.assertEqual(self.new_user(role=self.hq.id).status_code, 201)

    def test_a_weak_password_is_refused_when_creating_a_user(self):
        for weak in ('1', 'password', 'qwerty123'):
            r = self.new_user(password=weak, email=f'{weak}@x.test')
            self.assertEqual(r.status_code, 400, (weak, r.data))


class RoleDelegationTests(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='A', display_name='A')
        self.role_admin = Role.objects.create(
            name='Role admin', permissions=grant(admin={'roles': ['read', 'create', 'update', 'delete']},
                                                billing={'invoices': ['read']}))
        self.user = CustomUser.objects.create_user(
            email='ra@x.test', password=STRONG, full_name='RA', role=self.role_admin, center=self.centre)
        self.user.centers.set([self.centre])
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_admin_roles_is_a_trusted_grant(self):
        """Owner decision: whoever holds Admin > Roles may create and edit any role."""
        r = self.api.post('/salon_admin/api/roles/', {
            'name': 'Big', 'permissions': grant(finance={'register_summary': ['read']})}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        r = self.api.patch(f'/salon_admin/api/roles/{self.role_admin.id}/', {
            'permissions': grant(admin={'roles': ['read', 'create', 'update', 'delete']},
                                 billing={'invoices': ['read', 'cancel']})}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_permissions_must_be_real_booleans(self):
        r = self.api.post('/salon_admin/api/roles/', {
            'name': 'Text', 'permissions': {'billing': {'invoices': {'read': True, 'create': 'false'}}}},
            format='json')
        self.assertEqual(r.status_code, 400, r.data)


class AdvanceAndAdminBillsTests(TestCase):
    def setUp(self):
        self.centre = Center.objects.create(center_name='A', display_name='A')
        self.client_obj = Client.objects.create(first_name='Asha', last_name='Rao', phone='9990000001',
                                                center=self.centre)
        self.cashier_role = Role.objects.create(
            name='Cashier', permissions=grant(billing={'new_invoice': ['read', 'create'], 'invoices': ['read']}))
        self.cashier = CustomUser.objects.create_user(
            email='c@x.test', password=STRONG, full_name='C', role=self.cashier_role, center=self.centre)
        self.cashier.centers.set([self.centre])

    def test_a_cashier_cannot_credit_a_wallet_directly(self):
        api = APIClient(); api.force_authenticate(self.cashier)
        r = api.post('/billing/advances/', {'client': self.client_obj.id, 'amount': '50000.00'}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertFalse(AdvancePayment.objects.filter(client=self.client_obj).exists())

    def test_the_owner_can_post_a_correcting_entry(self):
        owner = CustomUser.objects.create_superuser(email='o@x.test', password=STRONG, full_name='O')
        api = APIClient(); api.force_authenticate(owner)
        r = api.post('/billing/advances/', {'client': self.client_obj.id, 'amount': '100.00'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def test_admin_bills_lists_bills_without_the_billing_module(self):
        role = Role.objects.create(name='Bills admin', permissions=grant(admin={'bills': ['read']}))
        user = CustomUser.objects.create_user(email='ab@x.test', password=STRONG, full_name='AB',
                                              role=role, center=self.centre)
        user.centers.set([self.centre])
        api = APIClient(); api.force_authenticate(user)
        self.assertEqual(api.get('/billing/invoices/').status_code, 200)
        self.assertEqual(api.get(f'/billing/advances/?client_id={self.client_obj.id}').status_code, 200)

    def test_admin_clients_reads_a_clients_bill_history(self):
        role = Role.objects.create(name='Clients admin', permissions=grant(admin={'clients': ['read']}))
        user = CustomUser.objects.create_user(email='ac@x.test', password=STRONG, full_name='AC',
                                              role=role, center=self.centre)
        user.centers.set([self.centre])
        api = APIClient(); api.force_authenticate(user)
        self.assertEqual(api.get(f'/billing/invoices/?client_id={self.client_obj.id}').status_code, 200)

    def test_a_role_without_either_is_still_refused(self):
        role = Role.objects.create(name='Nothing', permissions=grant(appointments={'calendar': ['read']}))
        user = CustomUser.objects.create_user(email='no@x.test', password=STRONG, full_name='NO',
                                              role=role, center=self.centre)
        user.centers.set([self.centre])
        api = APIClient(); api.force_authenticate(user)
        self.assertEqual(api.get('/billing/invoices/').status_code, 403)
