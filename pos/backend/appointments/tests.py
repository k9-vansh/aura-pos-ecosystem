"""
Appointments.

This module had NO tests at all - `tests.py` was still Django's
"# Create your tests here." stub. It is also the only module that never got the
query-parameter hardening the others did, and a smoke check found the reason it
mattered: `?date=not-a-date` reached the ORM and raised ValidationError, an
unhandled 500 on the appointments list from one bad parameter.

Double-booking is the thing worth protecting here. A stylist booked twice at
10:00 means one customer waits or leaves, and it is the kind of error the
software should refuse rather than record.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from appointments.models import Appointment
from clients.models import Client
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

URL = '/appointments/api/appointments/'


class AppointmentTestBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.other_center = Center.objects.create(center_name='Other')

        self.owner_role = Role.objects.create(name='Owner',
                                             permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center)

        self.ravi = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))
        self.asha = StaffMember.objects.create(
            center=self.center, first_name='Asha', designation='Stylist',
            salary=Decimal('20000.00'))

        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'), duration_mins=30)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.today = datetime.date.today()
        self.tomorrow = self.today + datetime.timedelta(days=1)

    @staticmethod
    def unwrap(response):
        """The list endpoint returns a bare array unless ?page is sent."""
        data = response.data
        return data.get('results', data) if hasattr(data, 'get') else data

    def rows(self, **params):
        response = self.api.get(URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return self.unwrap(response)

    def book(self, date=None, start_time='10:00', staff=None, services=None,
             phone='9990000001', name='Walk In', **extra):
        payload = {
            'center': self.center.id,
            'client_name': name,
            'client_phone': phone,
            'date': (date or self.today).isoformat(),
            'start_time': start_time,
            'status': 'Scheduled',
        }
        if services is not None:
            payload['services'] = services
        elif staff is not None:
            payload['services'] = [{
                'service_name': 'Haircut', 'time': start_time,
                'duration': 30, 'staff': staff.id,
            }]
        payload.update(extra)
        return self.api.post(URL, payload, format='json')


class BookingTests(AppointmentTestBase):
    def test_a_basic_booking_is_accepted(self):
        response = self.book()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Appointment.objects.count(), 1)

    def test_a_booking_with_a_service_and_stylist_is_accepted(self):
        response = self.book(staff=self.ravi)
        self.assertEqual(response.status_code, 201, response.data)

    def test_completed_is_set_by_billing_not_by_hand(self):
        created = self.book().data['id']
        response = self.api.patch(f'{URL}{created}/', {'status': 'Completed'},
                                  format='json')
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(Appointment.objects.get(pk=created).status, 'Scheduled')

    def test_an_appointment_can_be_cancelled(self):
        created = self.book().data['id']
        response = self.api.patch(f'{URL}{created}/', {'status': 'Cancelled'},
                                  format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_an_appointment_can_be_deleted(self):
        created = self.book().data['id']
        self.assertEqual(self.api.delete(f'{URL}{created}/').status_code, 204)
        self.assertEqual(Appointment.objects.count(), 0)


class DoubleBookingTests(AppointmentTestBase):
    """One stylist cannot be in two places at once."""

    def test_the_same_stylist_at_the_same_time_is_refused(self):
        first = self.book(staff=self.ravi, start_time='10:00')
        self.assertEqual(first.status_code, 201, first.data)

        second = self.book(staff=self.ravi, start_time='10:00',
                           phone='9990000002', name='Someone Else')
        self.assertEqual(
            second.status_code, 400,
            'the same stylist was booked twice at 10:00')

    def test_a_different_stylist_at_the_same_time_is_fine(self):
        self.book(staff=self.ravi, start_time='10:00')
        second = self.book(staff=self.asha, start_time='10:00',
                           phone='9990000002')
        self.assertEqual(second.status_code, 201, second.data)

    def test_the_same_stylist_at_a_different_time_is_fine(self):
        self.book(staff=self.ravi, start_time='10:00')
        second = self.book(staff=self.ravi, start_time='14:00',
                           phone='9990000002')
        self.assertEqual(second.status_code, 201, second.data)

    def test_the_same_stylist_on_a_different_day_is_fine(self):
        self.book(staff=self.ravi, start_time='10:00', date=self.today)
        second = self.book(staff=self.ravi, start_time='10:00',
                           date=self.tomorrow, phone='9990000002')
        self.assertEqual(second.status_code, 201, second.data)

    def test_a_cancelled_appointment_frees_the_slot(self):
        """Otherwise a cancellation blocks that slot for ever."""
        first = self.book(staff=self.ravi, start_time='10:00').data['id']
        self.api.patch(f'{URL}{first}/', {'status': 'Cancelled'}, format='json')

        second = self.book(staff=self.ravi, start_time='10:00',
                           phone='9990000002')
        self.assertEqual(
            second.status_code, 201,
            'a cancelled appointment still blocked its slot')

    def test_restoring_onto_a_taken_slot_is_refused(self):
        """#22: restore re-checks. It used to double-book the stylist."""
        first = self.book(staff=self.ravi, start_time='10:00').data['id']
        self.api.patch(f'{URL}{first}/', {'status': 'Cancelled'}, format='json')
        self.assertEqual(self.book(staff=self.ravi, start_time='10:00', phone='9990000002').status_code, 201)
        restored = self.api.patch(f'{URL}{first}/', {'status': 'Scheduled'}, format='json')
        self.assertEqual(restored.status_code, 400, 'restore double-booked the stylist')

    def test_moving_to_a_day_where_the_stylist_is_busy_is_refused(self):
        self.book(staff=self.ravi, start_time='10:00', date=self.tomorrow)
        today = self.book(staff=self.ravi, start_time='10:00', date=self.today,
                          phone='9990000002').data['id']
        moved = self.api.patch(f'{URL}{today}/', {'date': self.tomorrow.isoformat()}, format='json')
        self.assertEqual(moved.status_code, 400, 'a date move double-booked the stylist')


class QueryParameterTests(AppointmentTestBase):
    """One bad parameter must not be a server error."""

    def test_a_junk_date_is_not_a_500(self):
        """`?date=not-a-date` raised ValidationError straight out of the ORM."""
        for value in ('not-a-date', '2026-13-45', 'yesterday', ''):
            with self.subTest(value):
                response = self.api.get(URL, {'date': value})
                self.assertEqual(response.status_code, 200, value)

    def test_a_junk_center_id_is_not_a_500(self):
        for value in ('abc', 'null', 'undefined', ''):
            with self.subTest(value):
                self.assertEqual(self.api.get(URL, {'center_id': value}).status_code, 200)

    def test_a_valid_date_filters_correctly(self):
        self.book(date=self.today)
        self.book(date=self.tomorrow, phone='9990000002')

        rows = self.rows(date=self.today.isoformat())
        self.assertEqual(len(rows), 1)

    def test_a_date_range_filters_correctly(self):
        self.book(date=self.today)
        self.book(date=self.tomorrow, phone='9990000002')
        far = self.today + datetime.timedelta(days=30)
        self.book(date=far, phone='9990000003')

        rows = self.rows(start_date=self.today.isoformat(),
                         end_date=self.tomorrow.isoformat())
        self.assertEqual(len(rows), 2)

    def test_filtering_by_phone_finds_a_returning_customer(self):
        self.book(phone='9990000001')
        self.book(phone='9990000002', date=self.tomorrow)

        rows = self.rows(client_phone='9990000001')
        self.assertEqual(len(rows), 1)

    def test_filtering_by_status(self):
        first = self.book().data['id']
        self.book(phone='9990000002', date=self.tomorrow)
        Appointment.objects.filter(pk=first).update(status='Completed')   # as billing does

        rows = self.rows(status='Completed')
        self.assertEqual(len(rows), 1)


class BranchScopingTests(AppointmentTestBase):
    def test_a_manager_sees_only_their_own_branch(self):
        Appointment.objects.create(
            center=self.other_center, client_name='Elsewhere',
            client_phone='9990000009', date=self.today,
            start_time=datetime.time(10, 0), status='Scheduled')
        self.book(name='Here')

        role = Role.objects.create(name='Manager', permissions={
            'appointments': {'calendar': {'read': True, 'create': True}}})
        manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=role, center=self.center)
        manager.centers.set([self.center])

        client = APIClient()
        client.force_authenticate(manager)
        response = client.get(URL)
        self.assertEqual(response.status_code, 200, response.data)

        names = {r.get('client_name') for r in self.unwrap(response)}
        self.assertNotIn('Elsewhere', names)

    def test_an_owner_sees_every_branch(self):
        Appointment.objects.create(
            center=self.other_center, client_name='Elsewhere',
            client_phone='9990000009', date=self.today,
            start_time=datetime.time(10, 0), status='Scheduled')
        self.book(name='Here')

        names = {r.get('client_name') for r in self.rows()}
        self.assertIn('Elsewhere', names)
        self.assertIn('Here', names)

    def test_an_anonymous_caller_is_refused(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))
