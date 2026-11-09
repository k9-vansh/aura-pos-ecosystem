"""
What the log says about how a request ended, who it blames, and where.

A refused sign-in used to be recorded as "<email> logged in." because the log was
written for every write request whatever the answer. Everything that belongs to
nobody also piled up as a separate "System Activity" person per stray address.
"""
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from audit_logs import middleware
from audit_logs.models import SystemLog
from audit_logs.serializers import SystemLogSerializer
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class WhatHappenedTests(TestCase):
    def test_a_refused_sign_in_is_not_a_login(self):
        self.assertEqual(middleware._get_action('POST', '/accounts/api/login/', 200), 'LOGIN')
        self.assertEqual(middleware._get_action('POST', '/accounts/api/login/', 400), 'LOGIN_FAILED')
        self.assertEqual(middleware._get_action('POST', '/accounts/api/login/', 429), 'LOGIN_FAILED')

    def test_the_wording_matches_what_happened(self):
        d = middleware._human_description
        self.assertEqual(d('LOGIN_FAILED', 'Session', '', 'x@y.com', {'username': 'x@y.com'}, '', 400),
                         'Failed sign-in attempt for x@y.com.')
        self.assertIn('too many attempts', d('LOGIN_FAILED', 'Session', '', 'x', {'username': 'x'}, '', 429))
        done = d('CREATE', 'Client', '', 'Ravi', {'first_name': 'Asha'}, 'Saket', 201)
        self.assertEqual(done, 'Ravi created a new Client "Asha" at Saket.')
        refused = d('CREATE', 'Client', '', 'Ravi', {'first_name': 'Asha'}, 'Saket', 403)
        self.assertIn('did not go through (not allowed)', refused)

    def test_where_an_address_is(self):
        self.assertEqual(middleware._cached_geo('127.0.0.1')['city'], 'This computer')
        self.assertEqual(middleware._cached_geo('192.168.1.20')['city'], 'Local network')
        self.assertEqual(middleware._ip_scope('::1'), 'loopback')
        self.assertEqual(middleware._ip_scope('10.1.2.3'), 'private')
        self.assertEqual(middleware._ip_scope('49.36.1.9'), 'public')


class ApiTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Delhi Main Warehouse')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='Owner',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def make(self, **kw):
        defaults = dict(action='CREATE', module='CLIENTS', path='/clients/api/clients/',
                        human_description='x', ip_address='127.0.0.1', status_code=201)
        defaults.update(kw)
        return SystemLog.objects.create(**defaults)

    def person(self, **kw):
        return self.make(user_id=self.user.pk, user_id_snapshot=self.user.pk,
                         user_name='Owner', user_email='o@t.com', **kw)

    def test_outcome_is_reported_and_filterable(self):
        self.person()
        self.person(status_code=403)
        self.make(action='LOGIN_FAILED', status_code=429, user_email='nobody@x.com')
        rows = self.api.get('/audit_logs/logs/').data['results']
        self.assertEqual(sorted(r['outcome'] for r in rows), ['blocked', 'failed', 'success'])
        self.assertEqual(len(self.api.get('/audit_logs/logs/', {'outcome': 'failed'}).data['results']), 2)
        self.assertEqual(len(self.api.get('/audit_logs/logs/', {'outcome': 'success'}).data['results']), 1)

    def test_rows_written_before_status_was_recorded_count_as_success(self):
        row = self.make(status_code=None)
        self.assertEqual(SystemLogSerializer(row).data['outcome'], 'success')

    def test_stray_activity_is_two_clear_entries_not_one_per_address(self):
        self.make(action='LOGIN_FAILED', status_code=400, user_email='a@x.com', ip_address='192.168.1.5')
        self.make(action='LOGIN_FAILED', status_code=400, user_email='b@x.com', ip_address='192.168.1.6')
        self.make(action='LOGIN_FAILED', status_code=429, user_email='b@x.com', ip_address='10.0.0.7')
        self.make(action='CREATE', status_code=401, ip_address='192.168.1.9')
        self.person()
        rows = self.api.get('/audit_logs/logs/users/').data['results']
        kinds = sorted((r['kind'], r['name'], r['actions']) for r in rows)
        self.assertEqual(kinds, [('anonymous', 'Not signed in', 1),
                                 ('failed_login', 'Failed sign-ins', 3),
                                 ('user', 'Owner', 1)])

    def test_the_stray_entries_can_be_opened(self):
        self.make(action='LOGIN_FAILED', status_code=400, user_email='a@x.com')
        self.make(action='CREATE', status_code=401)
        failed = self.api.get('/audit_logs/logs/', {'user_id': 'FAILED'}).data['results']
        anon = self.api.get('/audit_logs/logs/', {'user_id': 'SYSTEM'}).data['results']
        self.assertEqual([r['action'] for r in failed], ['LOGIN_FAILED'])
        self.assertEqual([r['action'] for r in anon], ['CREATE'])

    def test_the_location_reads_as_branch_and_place(self):
        row = self.make(center_name='Delhi Main Warehouse', geo_city='This computer', geo_country='Local')
        self.assertEqual(SystemLogSerializer(row).data['location_label'], 'Delhi Main Warehouse - This computer')
        remote = self.make(center_name='Noida', geo_city='Noida', geo_region='Uttar Pradesh', geo_country='India')
        self.assertEqual(SystemLogSerializer(remote).data['location_label'],
                         'Noida - Noida, Uttar Pradesh, India')


class DevicePositionTests(TestCase):
    def test_a_sign_in_keeps_the_position_the_browser_reported(self):
        body = {'username': 'o@t.com', 'device_location': {'lat': 28.613939, 'lng': 77.209021, 'accuracy': 12.34}}
        self.assertEqual(middleware._take_device_position(body), (28.613939, 77.209021, 12.3))
        self.assertNotIn('device_location', body)

    def test_nonsense_positions_are_ignored(self):
        for bad in ({'lat': 120, 'lng': 10}, {'lat': 'x', 'lng': 1}, 'here', None, {'lat': 1}):
            self.assertIsNone(middleware._take_device_position({'device_location': bad}))

    def test_it_is_stored_and_served(self):
        middleware._write_log('', None, '/accounts/api/login/', 'POST',
                              b'{"username":"a@b.com","device_location":{"lat":28.6,"lng":77.2,"accuracy":20}}',
                              '127.0.0.1', 'UA', status=400)
        row = SystemLog.objects.get()
        self.assertEqual((row.geo_lat, row.geo_lng, row.geo_accuracy), (28.6, 77.2, 20.0))
        self.assertNotIn('device_location', row.description)
        self.assertEqual(SystemLogSerializer(row).data['geo_lat'], 28.6)
