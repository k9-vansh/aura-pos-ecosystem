"""The client app's API: who gets in, that a client sees only their own things, and what the PIN rules do."""
import datetime
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from appointments.models import Appointment, AppointmentService
from billing.models import AdvancePayment, CashbackTransaction, Invoice
from clients import pins
from clients.models import Client, ClientValueCard, PinAttempt
from client_api.auth import issue
from client_api.models import ClientToken
from marketing.models import ValueCard
from salon_admin.models import Center

BASE = '/client/api/'


@override_settings(OWNER_CACHE=False)
class ClientBase(TestCase):
    def setUp(self):
        cache.clear()
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        self.asha = Client.objects.create(first_name='Asha', last_name='Rao', phone='9990000001', center=self.center, email='a@x.in')
        self.ravi = Client.objects.create(first_name='Ravi', phone='9990000002', center=self.center)
        self.pin = pins.set_pin(self.asha, '4821')
        pins.set_pin(self.ravi, '7305')

    def login(self, phone='9990000001', pin='4821'):
        return APIClient().post(BASE + 'login/', {'phone': phone, 'pin': pin}, format='json')

    def api(self, client=None):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Token ' + issue(client or self.asha).key)
        return api

    def bill(self, client, total='590'):
        return Invoice.objects.create(center=self.center, client=client, status='paid', subtotal=Decimal('500'),
                                      total_amount=Decimal(total), paid_amount=Decimal(total),
                                      invoice_number=f'B-{Invoice.objects.count() + 1}')


class LoginTests(ClientBase):
    def test_the_right_number_and_pin_get_in_with_any_spelling_of_the_number(self):
        for phone in ('9990000001', '99900 00001', '+91 99900-00001'):
            res = self.login(phone)
            self.assertEqual(res.status_code, 200, (phone, res.data))
        self.assertTrue(res.data['token'])
        self.assertEqual((res.data['first_name'], res.data['salon']), ('Asha', 'Saket'))
        self.assertNotIn('pin', res.data)

    def test_every_miss_gets_the_same_answer(self):
        nopin = Client.objects.create(first_name='N', phone='9990000003', center=self.center)
        Client.objects.filter(pk=nopin.pk).update(pin_sealed='')           # from before PINs existed
        gone = Client.objects.create(first_name='G', phone='9990000004', center=self.center, is_active=False)
        pins.set_pin(gone, '1357')
        outcomes = [self.login(pin='0000'), self.login('9000000000'), self.login(nopin.phone), self.login(gone.phone, '1357')]
        self.assertEqual({r.status_code for r in outcomes}, {400})
        self.assertEqual(len({r.data['detail'] for r in outcomes}), 1)

    def test_five_wrong_pins_lock_the_client_and_even_the_right_pin_waits(self):
        for _ in range(5):
            self.assertEqual(self.login(pin='0000').status_code, 400)
        cache.clear()                                       # the sign-in rate limit is a separate guard
        locked = self.login()
        self.assertEqual((locked.status_code, locked.data['locked']), (429, True))

    def test_a_wrong_pin_says_how_many_tries_are_left(self):
        self.assertEqual(self.login(pin='0000').data['attempts_left'], 4)

    def test_a_pin_must_be_four_digits(self):
        self.assertEqual(self.login(pin='12').status_code, 400)
        self.assertEqual(self.login(phone='').status_code, 400)

    def test_signing_in_is_written_down(self):
        self.login()
        self.assertTrue(PinAttempt.objects.filter(kind='login', ok=True, client=self.asha).exists())


class TokenTests(ClientBase):
    def test_a_token_opens_the_app_and_a_wrong_one_does_not(self):
        self.assertEqual(self.api().get(BASE + 'me/').status_code, 200)
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION='Token nonsense')
        self.assertEqual(bad.get(BASE + 'me/').status_code, 401)
        self.assertEqual(APIClient().get(BASE + 'home/').status_code, 401)

    def test_signing_out_ends_only_that_phone(self):
        first, second = self.api(), self.api()
        self.assertEqual(first.post(BASE + 'logout/').status_code, 204)
        self.assertEqual(first.get(BASE + 'me/').status_code, 401)
        self.assertEqual(second.get(BASE + 'me/').status_code, 200)

    def test_age_a_switched_off_client_and_a_new_pin_end_a_sign_in(self):
        api = self.api()
        ClientToken.objects.update(created=ClientToken.objects.get().created - datetime.timedelta(days=61))
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)
        api = self.api()
        pins.set_pin(self.asha, '9999')                       # e.g. the till sent a new PIN
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)
        api = self.api()
        Client.objects.filter(pk=self.asha.pk).update(is_active=False)
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)

    def test_only_the_newest_five_phones_stay_signed_in(self):
        for _ in range(8):
            issue(self.asha)
        self.assertEqual(ClientToken.objects.filter(client=self.asha).count(), 5)

    def test_a_pos_token_does_not_open_the_client_app(self):
        from accounts.models import CustomUser
        from rest_framework.authtoken.models import Token
        from salon_admin.models import Role
        user = CustomUser.objects.create_user(email='u@t.com', password='pw', full_name='U', center=self.center,
                                              role=Role.objects.create(name='R', permissions={}))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Token ' + Token.objects.create(user=user).key)
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)


class WalletTests(ClientBase):
    def test_home_shows_only_this_clients_money(self):
        AdvancePayment.objects.create(client=self.asha, center=self.center, amount=Decimal('500'))
        AdvancePayment.objects.create(client=self.asha, center=self.center, amount=Decimal('-120'))
        AdvancePayment.objects.create(client=self.ravi, center=self.center, amount=Decimal('9000'))
        CashbackTransaction.objects.create(client=self.asha, center=self.center, amount=Decimal('75.50'))
        res = self.api().get(BASE + 'home/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual((res.data['advance'], res.data['cashback']), (380.0, 75.5))

    def test_value_cards_show_balance_and_hide_spent_and_expired_ones(self):
        template = ValueCard.objects.create(center=self.center, title='Gold 5000', value=Decimal('5000'), expiry_days=365)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        live = ClientValueCard.objects.create(client=self.asha, value_card=template, balance=Decimal('3200'), center=self.center,
                                              expiry_date=tomorrow)
        ClientValueCard.objects.create(client=self.asha, value_card=template, balance=Decimal('0'), center=self.center, expiry_date=tomorrow)
        ClientValueCard.objects.create(client=self.asha, value_card=template, balance=Decimal('900'), center=self.center, expiry_date=yesterday)
        ClientValueCard.objects.create(client=self.ravi, value_card=template, balance=Decimal('700'), center=self.center, expiry_date=tomorrow)
        res = self.api().get(BASE + 'home/')
        self.assertEqual([c['id'] for c in res.data['value_cards']], [live.id])
        self.assertEqual((res.data['value_cards'][0]['balance'], res.data['value_card_total']), (3200.0, 3200.0))


class BillsAndAppointmentsTests(ClientBase):
    def test_a_client_sees_their_bills_and_never_another_clients(self):
        mine, theirs = self.bill(self.asha), self.bill(self.ravi)
        Invoice.objects.create(center=self.center, client=self.asha, status='draft', invoice_number='D-1')
        api = self.api()
        rows = api.get(BASE + 'bills/').data['bills']
        self.assertEqual([r['id'] for r in rows], [mine.id])
        self.assertEqual(api.get(f'{BASE}bills/{mine.id}/').data['total'], 590.0)
        self.assertEqual(api.get(f'{BASE}bills/{theirs.id}/').status_code, 404)

    def test_appointments_split_into_coming_up_and_past(self):
        today = datetime.date.today()

        def appt(days, status='Scheduled', client=self.asha):
            a = Appointment.objects.create(center=self.center, client=client, client_phone=client.phone, client_name=client.first_name,
                                           date=today + datetime.timedelta(days=days), start_time=datetime.time(11), status=status)
            AppointmentService.objects.create(appointment=a, service_name='Haircut', time=datetime.time(11), duration=45)
            return a
        soon, later = appt(1), appt(5)
        done, cancelled = appt(-3, 'Completed'), appt(2, 'Cancelled')
        appt(1, client=self.ravi)
        res = self.api().get(BASE + 'appointments/').data
        self.assertEqual([a['id'] for a in res['upcoming']], [soon.id, later.id])
        self.assertEqual({a['id'] for a in res['past']}, {done.id, cancelled.id})
        self.assertEqual(res['upcoming'][0]['services'][0]['name'], 'Haircut')


class BookingTests(ClientBase):
    def setUp(self):
        super().setUp()
        from services.models import ServiceMaster
        self.cut = ServiceMaster.objects.create(name='Haircut', category='Hair', default_price=Decimal('500'), duration_mins=45)
        self.spa = ServiceMaster.objects.create(name='Hair Spa', category='Hair', default_price=Decimal('1200'), duration_mins=60)
        self.api_ = self.api()
        self.day = datetime.date.today() + datetime.timedelta(days=3)

    def book(self, **extra):
        body = {'date': self.day.isoformat(), 'time': '11:00', 'services': [self.cut.id], **extra}
        return self.api_.post(BASE + 'appointments/book/', body, format='json')

    def test_the_options_list_the_salons_services_with_price_and_time_and_the_days_slots(self):
        res = self.api_.get(BASE + 'book/options/', {'date': self.day.isoformat()})
        self.assertEqual(res.status_code, 200, res.data)
        by_name = {s['name']: s for s in res.data['services']}
        self.assertEqual((by_name['Haircut']['price'], by_name['Haircut']['minutes']), (500.0, 45))
        self.assertIn('11:00', res.data['slots'])
        self.assertNotIn('08:45', res.data['slots'])                  # before the salon opens (09:00 by default)

    def test_a_branch_price_and_a_switched_off_service_are_respected(self):
        from services.models import CenterService
        CenterService.objects.create(center=self.center, service=self.cut, price=Decimal('650'))
        CenterService.objects.create(center=self.center, service=self.spa, is_active=False)
        names = {s['name']: s['price'] for s in self.api_.get(BASE + 'book/options/').data['services']}
        self.assertEqual(names, {'Haircut': 650.0})

    def test_a_booking_lands_on_the_pos_calendar_as_a_scheduled_appointment(self):
        res = self.book(services=[self.cut.id, self.spa.id], note='Please use gentle shampoo')
        self.assertEqual(res.status_code, 201, res.data)
        appt = Appointment.objects.get(pk=res.data['id'])
        self.assertEqual((appt.status, appt.center_id, appt.client_id, appt.date), ('Scheduled', self.center.id, self.asha.id, self.day))
        self.assertIn('client app', appt.notes)
        self.assertIn('gentle shampoo', appt.notes)
        lines = list(appt.services.order_by('time').values_list('service_name', 'time', 'duration', 'price'))
        self.assertEqual(lines, [('Haircut', datetime.time(11, 0), 45, Decimal('500.00')), ('Hair Spa', datetime.time(11, 45), 60, Decimal('1200.00'))])
        # ...and the staff at the till see it in their own calendar API.
        from accounts.models import CustomUser
        from pos_backend.testing import full_access_permissions
        from salon_admin.models import Role
        staff = CustomUser.objects.create_user(email='fd@t.com', password='StrongPass!234', full_name='FD', center=self.center,
                                               role=Role.objects.create(name='Desk', permissions=full_access_permissions()))
        staff.centers.add(self.center)
        pos = APIClient()
        pos.force_authenticate(staff)
        rows = pos.get('/appointments/api/appointments/', {'date': self.day.isoformat()}).data
        rows = rows['results'] if isinstance(rows, dict) and 'results' in rows else rows
        self.assertIn(appt.id, [r['id'] for r in rows])

    def test_refusals_say_why(self):
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        self.assertEqual(self.book(date=yesterday).status_code, 400)                       # the past
        self.assertEqual(self.book(time='06:00').status_code, 400)                         # before opening
        self.assertEqual(self.book(time='21:45', services=[self.spa.id]).status_code, 400)  # would end after closing
        self.assertEqual(self.book(services=[]).status_code, 400)
        self.assertEqual(self.book(services=[999999]).status_code, 400)
        self.assertEqual(self.book(date='nonsense').status_code, 400)
        Client.objects.filter(pk=self.asha.pk).update(is_blacklisted=True)
        self.assertEqual(self.book().status_code, 400)

    def test_a_closed_weekday_takes_no_bookings(self):
        from pos_backend import app_settings
        name = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[self.day.weekday()]
        with mock.patch('client_api.booking.branch_hours', return_value={'open_time': '09:00', 'close_time': '22:00', 'closed_days': [name],
                                                                         'slot_minutes': 15, 'default_duration': 45}):
            self.assertEqual(self.api_.get(BASE + 'book/options/', {'date': self.day.isoformat()}).data['slots'], [])
            self.assertEqual(self.book().status_code, 400)
        self.assertTrue(app_settings)

    def test_one_booking_per_start_time_and_five_at_a_time(self):
        self.assertEqual(self.book().status_code, 201)
        self.assertEqual(self.book().status_code, 400)                                      # the same time again
        for i in range(4):
            self.assertEqual(self.book(time=f'1{2 + i}:00').status_code, 201, i)
        self.assertEqual(self.book(time='17:00').status_code, 400)                          # a sixth

    def test_a_client_cancels_only_their_own_coming_up_booking(self):
        mine = self.book().data['id']
        theirs = Appointment.objects.create(center=self.center, client=self.ravi, client_phone=self.ravi.phone, client_name='Ravi',
                                            date=self.day, start_time=datetime.time(12), status='Scheduled')
        self.assertEqual(self.api_.post(f'{BASE}appointments/{theirs.id}/cancel/').status_code, 400)
        self.assertEqual(self.api_.post(f'{BASE}appointments/{mine}/cancel/').status_code, 200)
        self.assertEqual(Appointment.objects.get(pk=mine).status, 'Cancelled')
        self.assertEqual(self.api_.post(f'{BASE}appointments/{mine}/cancel/').status_code, 400)   # already cancelled
        done = Appointment.objects.create(center=self.center, client=self.asha, client_phone=self.asha.phone, client_name='A',
                                          date=self.day, start_time=datetime.time(15), status='Checked-in')
        self.assertEqual(self.api_.post(f'{BASE}appointments/{done.id}/cancel/').status_code, 400)

    def test_the_list_says_which_visits_can_be_cancelled(self):
        self.book()
        up = self.api_.get(BASE + 'appointments/').data['upcoming']
        self.assertEqual([v['can_cancel'] for v in up], [True])

    def test_booking_needs_a_sign_in(self):
        self.assertEqual(APIClient().post(BASE + 'appointments/book/', {}, format='json').status_code, 401)


class PinTests(ClientBase):
    def test_the_client_can_see_their_own_pin(self):
        self.assertEqual(self.api().get(BASE + 'pin/').data['pin'], '4821')

    def test_changing_the_pin_needs_the_current_one_and_signs_in_again(self):
        api = self.api()
        self.assertEqual(api.post(BASE + 'pin/', {'current': '0000', 'new': '6391'}, format='json').status_code, 400)
        self.assertEqual(api.post(BASE + 'pin/', {'current': '4821', 'new': '1111'}, format='json').status_code, 400)   # too easy
        self.assertEqual(api.post(BASE + 'pin/', {'current': '4821', 'new': '63'}, format='json').status_code, 400)
        res = api.post(BASE + 'pin/', {'current': '4821', 'new': '6391'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.asha.refresh_from_db()
        self.assertEqual(pins.client_pin(self.asha), '6391')
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)             # the old token is gone
        fresh = APIClient()
        fresh.credentials(HTTP_AUTHORIZATION='Token ' + res.data['token'])
        self.assertEqual(fresh.get(BASE + 'me/').status_code, 200)
        self.assertEqual(self.login(pin='4821').status_code, 400)
        self.assertEqual(self.login(pin='6391').status_code, 200)

    def test_wrong_current_pins_are_counted_like_at_the_till(self):
        api = self.api()
        for _ in range(5):
            api.post(BASE + 'pin/', {'current': '0000', 'new': '6391'}, format='json')
        self.assertEqual(api.post(BASE + 'pin/', {'current': '4821', 'new': '6391'}, format='json').status_code, 429)

    def test_forgot_answers_the_same_for_every_number_and_sends_at_most_three_an_hour(self):
        with mock.patch('clients.pins.send_pin', return_value=['whatsapp']) as sent:
            known = APIClient().post(BASE + 'forgot/', {'phone': '99900 00001'}, format='json')
            unknown = APIClient().post(BASE + 'forgot/', {'phone': '9000000000'}, format='json')
            self.assertEqual((known.status_code, unknown.status_code), (200, 200))
            self.assertEqual(known.data, unknown.data)
            self.assertEqual(sent.call_count, 1)
            self.assertEqual(sent.call_args.args[0], self.asha)
            for _ in range(5):
                cache.clear()
                APIClient().post(BASE + 'forgot/', {'phone': '9990000001'}, format='json')
            self.assertEqual(sent.call_count, 3)
        self.assertEqual(pins.client_pin(Client.objects.get(pk=self.asha.pk)), '4821')       # the PIN itself is untouched
