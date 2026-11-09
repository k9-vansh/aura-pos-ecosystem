"""Phase 8: appointments (B5.5).

  * #20 branch hours and closed weekdays, per branch, from Settings;
  * D14 Checked-in and No-show; Completed only by billing;
  * each status move and a stylist change need their calendar tick;
  * service lines linked to the catalogue, with an end time;
  * the blacklist check finds the client whatever way the phone is written.
"""

import datetime

from accounts.models import CustomUser
from appointments.models import Appointment
from appointments.tests import URL, AppointmentTestBase
from clients.models import Client
from pos_backend import app_settings
from pos_backend.models import AppSetting
from rest_framework.test import APIClient
from salon_admin.models import Role


def next_weekday(day):
    """The next date (from tomorrow) falling on weekday `day` (Mon=0)."""
    d = datetime.date.today() + datetime.timedelta(days=1)
    while d.weekday() != day:
        d += datetime.timedelta(days=1)
    return d


class Phase8Base(AppointmentTestBase):
    def setUp(self):
        super().setUp()
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.n = 0

    def set(self, key, value, center=None):
        scope = app_settings.branch_scope(center) if center else 'global'
        AppSetting.objects.update_or_create(key=key, scope=scope, defaults={'value': value})
        app_settings.invalidate()

    def as_user(self, actions=()):
        self.n += 1
        perms = {'appointments': {'calendar': {
            'read': True, 'create': True, 'update': True,
            'actions': {a: True for a in actions}}}}
        role = Role.objects.create(name=f'Desk{self.n}', permissions=perms)
        user = CustomUser.objects.create_user(email=f'desk{self.n}@t.com', password='StrongPass!234',
                                              full_name='Desk', role=role, center=self.center)
        user.centers.add(self.center)
        api = APIClient()
        api.force_authenticate(user)
        return api


class BranchHoursTests(Phase8Base):
    def line(self, time, duration=30, staff=None):
        return [{'service_name': 'Haircut', 'time': time, 'duration': duration,
                 'staff': staff.id if staff else None}]

    def test_a_service_before_opening_is_refused(self):
        response = self.book(start_time='08:30', services=self.line('08:30'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('outside the branch hours', str(response.data))

    def test_a_service_running_past_closing_is_refused(self):
        response = self.book(start_time='21:45', services=self.line('21:45', 30))
        self.assertEqual(response.status_code, 400)

    def test_a_service_ending_exactly_at_closing_is_fine(self):
        response = self.book(start_time='21:30', services=self.line('21:30', 30))
        self.assertEqual(response.status_code, 201, response.data)

    def test_hours_are_per_branch(self):
        self.set('appointments.open_time', '07:00', self.center)
        response = self.book(start_time='07:00', services=self.line('07:00'))
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_closed_weekday_is_refused(self):
        self.set('appointments.closed_days', 'Tue', self.center)
        response = self.book(date=next_weekday(1), services=self.line('10:00'))
        self.assertEqual(response.status_code, 400)
        self.assertIn('closed on Tuesdays', str(response.data))

    def test_moving_a_booking_onto_a_closed_day_is_refused(self):
        self.set('appointments.closed_days', 'Tue', self.center)
        created = self.book(date=next_weekday(2), services=self.line('10:00')).data['id']
        response = self.api.patch(f'{URL}{created}/', {'date': next_weekday(1).isoformat()}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_an_unrelated_edit_is_not_re_checked_against_new_hours(self):
        created = self.book(services=self.line('09:00')).data['id']
        self.set('appointments.open_time', '10:00', self.center)
        response = self.api.patch(f'{URL}{created}/', {'notes': 'call first'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_the_calendar_reads_the_branch_hours(self):
        self.set('appointments.close_time', '20:00', self.center)
        data = self.api.get(f'{URL}hours/', {'center_id': self.center.id}).data
        self.assertEqual((data['open_time'], data['close_time'], data['slot_minutes']),
                         ('09:00', '20:00', 15))

    def test_a_bad_time_setting_is_refused(self):
        spec = app_settings.BY_KEY['appointments.close_time']
        with self.assertRaises(app_settings.SettingError):
            app_settings.coerce(spec, '25:00')
        self.assertEqual(app_settings.coerce(spec, '24:00'), '24:00')


class StatusTests(Phase8Base):
    def test_check_in_and_no_show_need_their_ticks(self):
        created = self.book().data['id']
        plain = self.as_user()
        self.assertEqual(plain.patch(f'{URL}{created}/', {'status': 'Checked-in'},
                                     format='json').status_code, 403)
        desk = self.as_user(['check_in'])
        self.assertEqual(desk.patch(f'{URL}{created}/', {'status': 'Checked-in'},
                                    format='json').status_code, 200)
        self.assertEqual(desk.patch(f'{URL}{created}/', {'status': 'No-show'},
                                    format='json').status_code, 403)
        self.assertEqual(self.as_user(['no_show']).patch(
            f'{URL}{created}/', {'status': 'No-show'}, format='json').status_code, 200)

    def test_cancel_and_restore_need_their_ticks(self):
        created = self.book().data['id']
        self.assertEqual(self.as_user().patch(f'{URL}{created}/', {'status': 'Cancelled'},
                                              format='json').status_code, 403)
        self.assertEqual(self.as_user(['cancel']).patch(f'{URL}{created}/', {'status': 'Cancelled'},
                                                        format='json').status_code, 200)
        self.assertEqual(self.as_user(['cancel']).patch(f'{URL}{created}/', {'status': 'Scheduled'},
                                                        format='json').status_code, 403)
        self.assertEqual(self.as_user(['restore']).patch(f'{URL}{created}/', {'status': 'Scheduled'},
                                                         format='json').status_code, 200)

    def test_a_checked_in_client_still_holds_the_stylist(self):
        first = self.book(staff=self.ravi, start_time='10:00').data['id']
        self.api.patch(f'{URL}{first}/', {'status': 'Checked-in'}, format='json')
        clash = self.book(staff=self.ravi, start_time='10:00', phone='9990000002')
        self.assertEqual(clash.status_code, 400)

    def test_a_no_show_frees_the_stylist(self):
        first = self.book(staff=self.ravi, start_time='10:00').data['id']
        self.api.patch(f'{URL}{first}/', {'status': 'No-show'}, format='json')
        self.assertEqual(self.book(staff=self.ravi, start_time='10:00',
                                   phone='9990000002').status_code, 201)

    def test_a_billed_appointment_cannot_be_changed_back(self):
        created = self.book().data['id']
        Appointment.objects.filter(pk=created).update(status='Completed')
        response = self.api.patch(f'{URL}{created}/', {'status': 'Scheduled'}, format='json')
        self.assertEqual(response.status_code, 400)

    def test_a_new_booking_cannot_start_cancelled(self):
        self.assertEqual(self.book(status='Cancelled').status_code, 400)

    def test_a_walk_in_can_be_booked_checked_in_with_the_tick(self):
        self.assertEqual(self.book(status='Checked-in').status_code, 201)


class AssignStaffTests(Phase8Base):
    def test_changing_the_stylist_needs_the_tick(self):
        created = self.book(staff=self.ravi, start_time='10:00').data['id']
        swap = {'services': [{'service_name': 'Haircut', 'time': '10:00', 'duration': 30,
                              'staff': self.asha.id}]}
        self.assertEqual(self.as_user().patch(f'{URL}{created}/', swap, format='json').status_code, 403)
        self.assertEqual(self.as_user(['assign_staff']).patch(f'{URL}{created}/', swap,
                                                              format='json').status_code, 200)

    def test_resending_the_same_lines_or_adding_one_needs_no_tick(self):
        created = self.book(staff=self.ravi, start_time='10:00').data['id']
        same = {'services': [
            {'service_name': 'Haircut', 'time': '10:00', 'duration': 30, 'staff': self.ravi.id},
            {'service_name': 'Wash', 'time': '10:30', 'duration': 15, 'staff': self.asha.id},
        ]}
        response = self.as_user().patch(f'{URL}{created}/', same, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def test_unallocated_lines_are_allowed(self):
        response = self.book(services=[{'service_name': 'Haircut', 'time': '10:00', 'duration': 30}])
        self.assertEqual(response.status_code, 201, response.data)


class CatalogueLinkTests(Phase8Base):
    def test_a_line_takes_name_and_duration_from_the_catalogue(self):
        response = self.book(services=[{'service': self.service.id, 'time': '10:00'}])
        self.assertEqual(response.status_code, 201, response.data)
        line = response.data['services'][0]
        self.assertEqual((line['service'], line['service_name'], line['duration'], line['end_time']),
                         (self.service.id, 'Haircut', 30, '10:30'))

    def test_an_unknown_catalogue_service_is_refused(self):
        response = self.book(services=[{'service': 999999, 'time': '10:00'}])
        self.assertEqual(response.status_code, 400)

    def test_a_free_text_line_uses_the_branch_default_duration(self):
        self.set('appointments.default_duration', 60, self.center)
        response = self.book(services=[{'service_name': 'Consultation', 'time': '10:00'}])
        self.assertEqual(response.data['services'][0]['duration'], 60)


class BlacklistTests(Phase8Base):
    def test_a_blacklisted_client_is_found_however_the_phone_is_written(self):
        Client.objects.create(first_name='Banned', phone='9876543210', center=self.center,
                              is_blacklisted=True)
        response = self.book(phone='+91 98765 43210')
        self.assertEqual(response.status_code, 400)
        self.assertIn('blacklisted', str(response.data))
