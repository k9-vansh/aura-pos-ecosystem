"""
The activity log: access and filtering.

The access bug is the important one. `get_queryset` raised
`PermissionDenied("You do not have permission to view system logs.")` for anyone
who was not an owner, so once Logs became a grantable module, granting it still
refused - which is one flavour of "I gave them permission and it says no
permission".

The filtering was too thin to answer the questions people ask of an audit log:
what did this person do, who touched this record, what happened between these
times. There was no user_id filter and no way to list who appears in the log, so
the only way to filter was to already know what to type. The date filters also
interpolated the raw parameter, so `?start_date=yesterday` was a 500.
"""

import datetime

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from audit_logs.models import SystemLog
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

URL = '/audit_logs/logs/'


def grant_logs(read=True):
    return {'logs': {'activity': {'read': read, 'create': False,
                                  'update': False, 'delete': False}}}


class LogTestBase(TestCase):
    def setUp(self):
        self.branch_a = Center.objects.create(center_name='Branch A', display_name='A')
        self.branch_b = Center.objects.create(center_name='Branch B', display_name='B')

        self.owner_role = Role.objects.create(name='Owner',
                                             permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.branch_a)

        self.asha = CustomUser.objects.create_user(
            email='asha@test.com', password='StrongPass!234', full_name='Asha Rao',
            role=self.owner_role, center=self.branch_a)
        self.bela = CustomUser.objects.create_user(
            email='bela@test.com', password='StrongPass!234', full_name='Bela Nair',
            role=self.owner_role, center=self.branch_b)

        now = timezone.now()
        self.today = now.date()
        self.yesterday = self.today - datetime.timedelta(days=1)

        def log(user, action, module, center, when, entity_type='Invoice',
                entity_id='1'):
            row = SystemLog.objects.create(
                user=user, user_name=user.full_name, user_email=user.email,
                user_role='Owner', user_id_snapshot=user.pk,
                center_id=center.id if center else None,
                center_name=center.center_name if center else '',
                action=action, module=module, entity_type=entity_type,
                entity_id=entity_id, human_description=f'{action} {module}',
                path=f'/{module}/api/', ip_address='10.0.0.1',
                device_type='Desktop',
            )
            SystemLog.objects.filter(pk=row.pk).update(timestamp=when)
            return row

        # Asha: 3 today at branch A
        log(self.asha, 'Create', 'billing', self.branch_a, now)
        log(self.asha, 'Update', 'billing', self.branch_a, now)
        log(self.asha, 'Delete', 'inventory', self.branch_a, now)
        # Bela: 2 yesterday at branch B
        log(self.bela, 'Create', 'staff', self.branch_b,
            now - datetime.timedelta(days=1))
        log(self.bela, 'Create', 'staff', self.branch_b,
            now - datetime.timedelta(days=1))
        # One row with no branch, as a login has.
        log(self.asha, 'Login', 'accounts', None, now)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def rows(self, **params):
        response = self.api.get(URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data.get('results', response.data)


class AccessTests(LogTestBase):
    def test_a_granted_non_owner_can_read_the_log(self):
        """This was refused outright, whatever the role said."""
        role = Role.objects.create(name='Auditor', permissions=grant_logs())
        auditor = CustomUser.objects.create_user(
            email='auditor@test.com', password='StrongPass!234',
            full_name='Auditor', role=role, center=self.branch_a)
        client = APIClient()
        client.force_authenticate(auditor)

        response = client.get(URL)
        self.assertEqual(
            response.status_code, 200,
            'a user granted Logs was still refused the activity log')

    def test_a_non_owner_without_the_grant_is_refused(self):
        role = Role.objects.create(name='Stylist', permissions={
            'billing': {'invoices': {'read': True}}})
        stylist = CustomUser.objects.create_user(
            email='stylist@test.com', password='StrongPass!234',
            full_name='Stylist', role=role, center=self.branch_a)
        client = APIClient()
        client.force_authenticate(stylist)
        self.assertEqual(client.get(URL).status_code, 403)

    def test_a_granted_non_owner_sees_only_their_own_branch(self):
        """Plus rows with no branch, which is where logins live."""
        role = Role.objects.create(name='Auditor A', permissions=grant_logs())
        auditor = CustomUser.objects.create_user(
            email='aa@test.com', password='StrongPass!234', full_name='AA',
            role=role, center=self.branch_a)
        auditor.centers.set([self.branch_a])
        client = APIClient()
        client.force_authenticate(auditor)

        rows = client.get(URL).data['results']
        branches = {r.get('center_name') for r in rows}
        self.assertNotIn('Branch B', branches)
        self.assertIn('Branch A', branches)

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))


class FilterTests(LogTestBase):
    def test_filter_by_user_id(self):
        """This is what clicking a person in the user list does."""
        rows = self.rows(user_id=self.asha.pk)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r['user_email'] == 'asha@test.com' for r in rows))

    def test_filter_by_user_name_is_a_partial_match(self):
        rows = self.rows(user_name='asha')
        self.assertEqual(len(rows), 4)

    def test_filter_by_email_is_exact(self):
        self.assertEqual(len(self.rows(user_email='bela@test.com')), 2)
        self.assertEqual(len(self.rows(user_email='bela')), 0)

    def test_filter_by_action(self):
        self.assertEqual(len(self.rows(action='Delete')), 1)

    def test_several_actions_at_once(self):
        response = self.api.get(f'{URL}?action=Create&action=Delete')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data['results']), 4)

    def test_filter_by_module(self):
        self.assertEqual(len(self.rows(module='billing')), 2)

    def test_filter_by_entity(self):
        rows = self.rows(entity_type='Invoice', entity_id='1')
        self.assertEqual(len(rows), 6)

    def test_filter_by_branch(self):
        self.assertEqual(len(self.rows(center_id=self.branch_b.id)), 2)

    def test_filter_by_date_range(self):
        self.assertEqual(len(self.rows(start_date=self.today.isoformat())), 4)
        self.assertEqual(
            len(self.rows(start_date=self.yesterday.isoformat(),
                          end_date=self.yesterday.isoformat())), 2)

    def test_a_junk_date_is_ignored_not_a_500(self):
        """`?start_date=yesterday` used to reach the database."""
        for value in ('yesterday', 'not-a-date', '2026-13-45', ''):
            response = self.api.get(URL, {'start_date': value})
            self.assertEqual(response.status_code, 200, value)

    def test_a_junk_user_id_is_ignored_not_a_500(self):
        for value in ('abc', 'null', 'undefined'):
            response = self.api.get(URL, {'user_id': value})
            self.assertEqual(response.status_code, 200, value)

    def test_a_junk_center_id_is_ignored(self):
        for value in ('abc', 'null'):
            self.assertEqual(self.api.get(URL, {'center_id': value}).status_code, 200)

    def test_search_still_works(self):
        self.assertEqual(len(self.rows(search='Bela')), 2)

    def test_filters_combine(self):
        rows = self.rows(user_id=self.asha.pk, module='billing', action='Create')
        self.assertEqual(len(rows), 1)

    def test_ordering_is_newest_first_by_default(self):
        rows = self.rows()
        stamps = [r['timestamp'] for r in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))


class FacetTests(LogTestBase):
    def test_facets_list_the_available_filter_values(self):
        response = self.api.get(f'{URL}facets/')
        self.assertEqual(response.status_code, 200, response.data)

        actions = {row['value']: row['count'] for row in response.data['actions']}
        self.assertEqual(actions['Create'], 3)
        self.assertEqual(actions['Delete'], 1)

        modules = {row['value'] for row in response.data['modules']}
        self.assertEqual(modules, {'billing', 'inventory', 'staff', 'accounts'})
        self.assertEqual(response.data['total'], 6)

    def test_facets_respect_the_current_filters(self):
        response = self.api.get(f'{URL}facets/', {'user_id': self.bela.pk})
        modules = {row['value'] for row in response.data['modules']}
        self.assertEqual(modules, {'staff'})

    def test_facets_respect_branch_scoping(self):
        role = Role.objects.create(name='Auditor A2', permissions=grant_logs())
        auditor = CustomUser.objects.create_user(
            email='aa2@test.com', password='StrongPass!234', full_name='AA2',
            role=role, center=self.branch_a)
        auditor.centers.set([self.branch_a])
        client = APIClient()
        client.force_authenticate(auditor)

        modules = {row['value'] for row in client.get(f'{URL}facets/').data['modules']}
        self.assertNotIn('staff', modules)  # branch B only


class UserListTests(LogTestBase):
    def test_the_user_list_shows_everyone_with_counts(self):
        response = self.api.get(f'{URL}users/')
        self.assertEqual(response.status_code, 200, response.data)

        by_email = {r['email']: r for r in response.data['results']}
        self.assertEqual(by_email['asha@test.com']['actions'], 4)
        self.assertEqual(by_email['bela@test.com']['actions'], 2)
        self.assertEqual(by_email['asha@test.com']['name'], 'Asha Rao')
        self.assertIsNotNone(by_email['asha@test.com']['last_active'])

    def test_the_user_id_in_the_list_filters_the_log(self):
        """The list is only useful if clicking a row works."""
        listed = self.api.get(f'{URL}users/').data['results']
        asha = next(r for r in listed if r['email'] == 'asha@test.com')

        rows = self.rows(user_id=asha['user_id'])
        self.assertEqual(len(rows), asha['actions'])

    def test_the_list_is_ordered_by_most_recent_activity(self):
        listed = self.api.get(f'{URL}users/').data['results']
        self.assertEqual(listed[0]['email'], 'asha@test.com')

    def test_the_list_respects_branch_scoping(self):
        role = Role.objects.create(name='Auditor A3', permissions=grant_logs())
        auditor = CustomUser.objects.create_user(
            email='aa3@test.com', password='StrongPass!234', full_name='AA3',
            role=role, center=self.branch_a)
        auditor.centers.set([self.branch_a])
        client = APIClient()
        client.force_authenticate(auditor)

        emails = {r['email'] for r in client.get(f'{URL}users/').data['results']}
        self.assertNotIn('bela@test.com', emails)

    def test_a_deleted_user_keeps_their_history(self):
        """The log stores an identity snapshot for exactly this reason."""
        self.asha.delete()

        listed = self.api.get(f'{URL}users/').data['results']
        emails = {r['email'] for r in listed}
        self.assertIn('asha@test.com', emails)

        asha = next(r for r in listed if r['email'] == 'asha@test.com')
        self.assertEqual(asha['actions'], 4)


class SummaryTests(LogTestBase):
    def test_the_summary_counts_days_and_actions(self):
        response = self.api.get(f'{URL}summary/')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['total'], 6)
        self.assertEqual(response.data['distinct_users'], 2)

        by_day = {str(row['date']): row['count'] for row in response.data['by_day']}
        self.assertEqual(by_day[str(self.today)], 4)
        self.assertEqual(by_day[str(self.yesterday)], 2)

    def test_the_summary_respects_filters(self):
        response = self.api.get(f'{URL}summary/', {'user_id': self.bela.pk})
        self.assertEqual(response.data['total'], 2)
