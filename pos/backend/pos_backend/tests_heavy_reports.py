"""Big reports are limited so they cannot starve the tills, and a report the database stopped answers readably."""

from django.db.utils import OperationalError
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend import heavy_reports
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class WhichPathsAreHeavy(SimpleTestCase):
    def test_the_big_reports_are_gated(self):
        for path in ('/salon_admin/api/dashboard/summary/', '/salon_admin/api/analytics/revenues/',
                     '/salon_admin/api/usage-report/overview/', '/finance/api/reports/tax/',
                     '/finance/api/monthly_sales/', '/finance/api/register_summary/', '/billing/invoices/summary/',
                     '/staff/api/members/incentive_report/', '/marketing/api/promotions/usage_report/'):
            with self.subTest(path):
                self.assertTrue(heavy_reports.is_heavy(path))

    def test_the_till_and_ordinary_lists_are_never_gated(self):
        for path in ('/billing/invoices/', '/billing/invoices/12/', '/clients/api/clients/', '/accounts/api/login/',
                     '/inventory/api/products/', '/appointments/api/appointments/', '/finance/api/petty-cash/'):
            with self.subTest(path):
                self.assertFalse(heavy_reports.is_heavy(path))


class TheDatabaseStoppedAQuery(SimpleTestCase):
    def test_a_stopped_query_answers_a_readable_503(self):
        for code in (3024, 1317):
            with self.subTest(code):
                response = heavy_reports.api_exception_handler(OperationalError(code, 'interrupted'), {})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data['code'], 'report_too_big')
                self.assertIn('shorter period or fewer centres', response.data['detail'])

    def test_other_database_errors_are_not_hidden(self):
        self.assertIsNone(heavy_reports.api_exception_handler(OperationalError(1205, 'lock wait timeout'), {}))
        self.assertIsNone(heavy_reports.api_exception_handler(OperationalError('disk I/O error'), {}))


class TheReplicaRouter(SimpleTestCase):
    """DB_REPLICA_HOST makes the big reports read from a replica; nothing else ever does."""

    def setUp(self):
        from pos_backend import db_router
        self.db_router = db_router
        self.router = db_router.ReportsReadFromReplica()
        self.addCleanup(db_router.use_replica, False)

    def with_replica(self):
        from unittest import mock
        from django.db import connections
        return mock.patch.dict(connections.databases, {'replica': {}})

    def test_outside_a_report_every_read_stays_on_the_primary(self):
        from billing.models import Invoice
        with self.with_replica():
            self.assertIsNone(self.router.db_for_read(Invoice))

    def test_inside_a_report_the_reads_go_to_the_replica(self):
        from billing.models import Invoice, Payment
        from clients.models import Client
        self.db_router.use_replica(True)
        with self.with_replica():
            for model in (Invoice, Payment, Client):
                self.assertEqual(self.router.db_for_read(model), 'replica')
        self.db_router.use_replica(False)
        with self.with_replica():
            self.assertIsNone(self.router.db_for_read(Invoice))

    def test_who_is_signed_in_is_always_read_from_the_primary(self):
        from accounts.models import CustomUser
        from rest_framework.authtoken.models import Token
        self.db_router.use_replica(True)
        with self.with_replica():
            self.assertIsNone(self.router.db_for_read(CustomUser))
            self.assertIsNone(self.router.db_for_read(Token))

    def test_writes_and_migrations_never_leave_the_primary(self):
        from billing.models import Invoice
        self.db_router.use_replica(True)
        with self.with_replica():
            self.assertIsNone(self.router.db_for_write(Invoice))
            self.assertTrue(self.router.allow_migrate('default', 'billing'))
            self.assertFalse(self.router.allow_migrate('replica', 'billing'))

    def test_with_no_replica_configured_a_report_reads_the_primary(self):
        from billing.models import Invoice
        self.db_router.use_replica(True)
        self.assertIsNone(self.router.db_for_read(Invoice))


class TheGate(TestCase):
    def setUp(self):
        heavy_reports.reset_gate()
        self.addCleanup(heavy_reports.reset_gate)
        center = Center.objects.create(center_name='Main', display_name='Main')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        owner = CustomUser.objects.create_user(email='o@test.com', password='StrongPass!234', full_name='O',
                                               role=role, center=center)
        self.api = APIClient()
        self.api.force_authenticate(owner)

    @override_settings(HEAVY_REPORT_SLOTS=1, HEAVY_REPORT_QUEUE=0, HEAVY_REPORT_WAIT_SECONDS=0.05)
    def test_when_every_turn_is_taken_a_report_is_told_to_retry_and_a_bill_is_not(self):
        slots = heavy_reports._gate()
        self.assertTrue(slots.acquire(blocking=False))                 # another report is being built
        try:
            busy = self.api.get('/billing/invoices/summary/')
            self.assertEqual(busy.status_code, 503)
            self.assertEqual(busy.json()['code'], 'reports_busy')
            self.assertEqual(busy['Retry-After'], '15')
            self.assertEqual(self.api.get('/billing/invoices/').status_code, 200)    # not a report: never held up
            self.assertEqual(self.api.get('/clients/api/clients/').status_code, 200)
        finally:
            slots.release()
        self.assertEqual(self.api.get('/billing/invoices/summary/').status_code, 200)    # the turn is free again

    @override_settings(HEAVY_REPORT_SLOTS=1, HEAVY_REPORT_QUEUE=1, HEAVY_REPORT_WAIT_SECONDS=0.05)
    def test_a_short_wait_for_a_turn_gives_up_with_the_same_answer(self):
        slots = heavy_reports._gate()
        self.assertTrue(slots.acquire(blocking=False))
        try:
            self.assertEqual(self.api.get('/billing/invoices/summary/').status_code, 503)
        finally:
            slots.release()

    @override_settings(HEAVY_REPORT_SLOTS=0)
    def test_slots_of_zero_switches_the_gate_off(self):
        self.assertIsNone(heavy_reports._gate())
        self.assertEqual(self.api.get('/billing/invoices/summary/').status_code, 200)

    @override_settings(HEAVY_REPORT_SLOTS=1)
    def test_a_report_gives_its_turn_back_even_when_it_fails(self):
        slots = heavy_reports._gate()
        for _ in range(3):                                              # a bad request must not use a turn up for ever
            self.api.get('/billing/invoices/summary/', {'start_date': 'not-a-date'})
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_the_time_limit_is_only_a_mysql_setting(self):
        self.assertFalse(heavy_reports._set_query_limit(30))            # SQLite: nothing to set, nothing breaks
