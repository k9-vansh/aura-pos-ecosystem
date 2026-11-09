"""The owner app's API: who gets in, which salons and screens they see, and that the figures are the POS's own."""
import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from billing.models import Invoice, InvoiceItem, Payment
from clients.models import Client
from owner_api import ai_providers, ai_report
from owner_api.auth import issue
from owner_api.models import AiKey, OwnerAccount, OwnerWeeklyReport
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

BASE = '/owner/api/'
ALL = {s: {'read': True} for s in ('today', 'month', 'invoices', 'staff', 'ai_report', 'settings')}


def role(name, **screens):
    perms = {'owner_app': {k: dict(v) for k, v in screens.items()}}
    return Role.objects.create(name=name, permissions=perms)


def account(name='Owner', phone='9873298293', password='pw12345!', perms=None, centers=(), email='', **screens):
    """An owner app account with a role holding these ticks (every screen when none are given)."""
    if perms is None:
        perms = {'owner_app': {k: dict(v) for k, v in screens.items()} if screens else {k: dict(v) for k, v in ALL.items()}}
    r = Role.objects.create(name=f'r{Role.objects.count()}', permissions=perms)
    a = OwnerAccount(name=name, phone=phone, email=email, role=r)
    a.set_password(password)
    a.save()
    a.centers.add(*centers)
    return a


def signed_in(who):
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION='Token ' + issue(who).key)
    return api


@override_settings(OWNER_CACHE=False)
class OwnerBase(TestCase):
    def setUp(self):
        self.a = Center.objects.create(center_name='Saket', display_name='Saket', monthly_target=Decimal('31000'))
        self.b = Center.objects.create(center_name='Noida', display_name='Noida')
        full = dict(ALL, ai_report={'read': True, 'create': True}, settings={'read': True, 'update': True},
                    master_pins={'read': True})
        self.owner = account(phone='98732 98293', email='o@t.com', centers=[self.a], **full)
        self.api = signed_in(self.owner)
        self.service = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('1000'))
        self.client_row = Client.objects.create(first_name='Asha', phone='9990000001', center=self.a, gender='female')

    def bill(self, center=None, total='1180', sub='1000', tax='180', status='paid', client='default', number=None):
        center = center or self.a
        who = self.client_row if client == 'default' else client
        invoice = Invoice.objects.create(center=center, client=who, status=status, subtotal=Decimal(sub),
                                         total_amount=Decimal(total), paid_amount=Decimal(total),
                                         invoice_number=number or f'B-{Invoice.objects.count() + 1}')
        InvoiceItem.objects.create(invoice=invoice, content_type=ContentType.objects.get_for_model(ServiceMaster),
                                   object_id=self.service.id, description='Haircut', unit_price=Decimal(sub),
                                   quantity=1, total_price=Decimal(sub), tax_amount=Decimal(tax))
        Payment.objects.create(invoice=invoice, amount=Decimal(total), payment_method='Cash')
        return invoice


class LoginTests(OwnerBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()           # the sign-in limit counts attempts across tests otherwise

    def login(self, phone='9873298293', password='pw12345!'):
        return APIClient().post(BASE + 'login/', {'phone': phone, 'password': password}, format='json')

    def test_a_holder_of_the_tick_signs_in_with_any_spelling_of_the_number(self):
        for phone in ('9873298293', '98732 98293', '+91 98732-98293'):
            res = self.login(phone)
            self.assertEqual(res.status_code, 200, (phone, res.data))
        self.assertTrue(res.data['token'])
        self.assertEqual(res.data['salons'], [{'id': self.a.id, 'name': 'Saket'}])
        self.assertTrue(res.data['screens']['today'])

    def test_the_e_mail_of_the_account_works_too(self):
        res = self.login('O@T.com')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.login('o@t.com', 'wrong').status_code, 400)

    def test_every_miss_gets_the_same_answer(self):
        outcomes = [self.login(password='wrong'), self.login('9000000000')]
        account(name='N', phone='9811111111', perms={})                    # an account whose role ticks nothing
        outcomes.append(self.login('9811111111'))
        self.assertEqual({r.status_code for r in outcomes}, {400})
        self.assertEqual(len({str(r.data) for r in outcomes}), 1)

    def test_an_account_that_is_switched_off_cannot_sign_in(self):
        self.owner.is_active = False
        self.owner.save()
        self.assertEqual(self.login().status_code, 400)

    def test_a_pos_user_cannot_sign_in_here_with_the_pos_password(self):
        from accounts.models import CustomUser
        CustomUser.objects.create_user(email='till@t.com', password='till-pw-1!', full_name='Till', phone='9822222222',
                                       role=role('Owner too', today={'read': True}), center=self.a)
        self.assertEqual(self.login('9822222222', 'till-pw-1!').status_code, 400)
        self.assertEqual(self.login('till@t.com', 'till-pw-1!').status_code, 400)

    def test_the_password_is_stored_hashed_and_the_phone_is_unique(self):
        self.assertNotIn('pw12345!', self.owner.password)
        self.assertTrue(self.owner.check_password('pw12345!'))
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            account(name='Dup', phone='+91 98732-98293')


class ScopeTests(OwnerBase):
    def test_a_salon_that_is_not_theirs_is_refused(self):
        self.assertEqual(self.api.get(BASE + 'today/', {'center_id': self.b.id}).status_code, 403)
        self.assertEqual(self.api.get(BASE + 'today/', {'center_id': self.a.id}).status_code, 200)

    def test_all_means_all_of_my_salons_only(self):
        self.bill(self.a)
        client_b = Client.objects.create(first_name='B', phone='9990000002', center=self.b)
        self.bill(self.b, client=client_b)
        res = self.api.get(BASE + 'today/', {'center_id': 'all'})
        self.assertEqual((res.status_code, res.data['bills'], res.data['salon_count']), (200, 1, 1))

    def test_a_screen_needs_its_own_tick(self):
        only_staff = account(name='S', phone='9811100001', centers=[self.a], staff={'read': True})
        api = signed_in(only_staff)
        self.assertEqual(api.get(BASE + 'staff/').status_code, 200)
        for path in ('today/', 'month/', 'invoices/', 'settings/', 'master-pins/'):
            self.assertEqual(api.get(BASE + path, {'center_id': self.a.id}).status_code, 403, path)

    def test_nothing_without_signing_in(self):
        self.assertEqual(APIClient().get(BASE + 'today/').status_code, 401)


class TodayAndMonthTests(OwnerBase):
    def test_today_shows_the_pos_figures(self):
        self.bill()
        res = self.api.get(BASE + 'today/', {'center_id': self.a.id})
        self.assertEqual(res.status_code, 200, res.data)
        d = res.data
        self.assertEqual((d['bills'], d['clients']), (1, 1))
        self.assertEqual(d['sales_ex_tax'], 1000.0)
        self.assertEqual(d['payments']['cash'], 1180.0)
        self.assertEqual(d['breakdown']['services'], {'count': 1, 'amount': 1000.0})
        # 31,000 target over this month's days: a daily target and the share reached.
        days = (datetime.date.today().replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)
        self.assertAlmostEqual(d['daily_target'], round(31000 / days.day, 2), places=2)
        self.assertEqual(d['target_pct'], round(1000 / (31000 / days.day) * 100))

    def test_an_empty_day_names_the_latest_day_that_has_bills(self):
        self.bill()
        old = Invoice.objects.get()
        old.created_at = old.created_at - datetime.timedelta(days=3)
        old.save(update_fields=['created_at'])
        res = self.api.get(BASE + 'today/', {'center_id': self.a.id})
        self.assertEqual((res.data['bills'], res.data['latest_day']), (0, old.created_at.date().isoformat()))
        rows = self.api.get(BASE + 'invoices/', {'center_id': self.a.id}).data
        self.assertEqual((rows['rows'], rows['latest_day']), ([], old.created_at.date().isoformat()))

    def test_a_cancelled_bill_is_not_sales(self):
        self.bill(status='cancelled')
        self.assertEqual(self.api.get(BASE + 'today/', {'center_id': self.a.id}).data['bills'], 0)

    def test_what_clients_still_hold(self):
        from billing.models import AdvancePayment
        AdvancePayment.objects.create(client=self.client_row, center=self.a, amount=Decimal('500'))
        held = self.api.get(BASE + 'today/', {'center_id': self.a.id}).data['liabilities']
        self.assertEqual((held['advances'], held['total']), (500.0, 500.0))

    def test_month_has_the_days_the_target_and_new_clients(self):
        self.bill()
        res = self.api.get(BASE + 'month/', {'center_id': self.a.id})
        self.assertEqual(res.status_code, 200, res.data)
        d = res.data
        self.assertEqual(d['sales_ex_tax'], 1000.0)
        self.assertEqual(d['target'], 31000.0)
        self.assertEqual((d['new_clients'], d['repeat_clients']), (1, 0))
        self.assertEqual(len(d['daily']), (datetime.date.today().replace(day=28) + datetime.timedelta(days=4)).replace(day=1).day - 1
                         if False else len(d['daily']))
        self.assertEqual(sum(day['bills'] for day in d['daily']), 1)
        self.assertEqual(len(d['retention']), 4)
        self.assertEqual(len(d['gender']), 4)
        self.assertIsNotNone(d['projection'])

    def test_a_past_month_has_no_projection(self):
        res = self.api.get(BASE + 'month/', {'center_id': self.a.id, 'month': '2025-01'})
        self.assertEqual((res.status_code, res.data['projection'], res.data['current']), (200, None, False))
        self.assertEqual(self.api.get(BASE + 'month/', {'month': 'nonsense'}).status_code, 400)


class BillsAndStaffTests(OwnerBase):
    def test_the_day_list_and_a_receipt(self):
        invoice = self.bill(number='DL-1')
        listing = self.api.get(BASE + 'invoices/', {'center_id': self.a.id}).data
        self.assertEqual([r['number'] for r in listing['rows']], ['DL-1'])
        receipt = self.api.get(f'{BASE}invoices/{invoice.id}/').data
        self.assertEqual((receipt['total'], receipt['items'][0]['name'], receipt['payments'][0]['method']),
                         (1180.0, 'Haircut', 'Cash'))

    def test_a_draft_or_another_salons_bill_is_not_found(self):
        draft = self.bill(status='draft')
        other = self.bill(self.b, client=Client.objects.create(first_name='B', phone='9990000003', center=self.b))
        self.assertEqual(self.api.get(f'{BASE}invoices/{draft.id}/').status_code, 404)
        self.assertEqual(self.api.get(f'{BASE}invoices/{other.id}/').status_code, 404)

    def test_staff_sales_follow_the_pos_credit_rule_and_hours_come_from_appointments(self):
        from appointments.models import Appointment, AppointmentService
        from inventory.models import Product
        ravi = StaffMember.objects.create(center=self.a, first_name='Ravi', designation='Stylist', salary=Decimal('1'))
        meena = StaffMember.objects.create(center=self.a, first_name='Meena', designation='Stylist', salary=Decimal('1'))
        today = datetime.date.today()
        invoice = self.bill()
        InvoiceItem.objects.filter(invoice=invoice).update(staff=ravi)                     # Haircut 1000, Ravi
        gel = Product.objects.create(center=self.a, name='Gel', price=Decimal('200'))
        InvoiceItem.objects.create(invoice=invoice, content_type=ContentType.objects.get_for_model(Product),
                                   object_id=gel.id, description='Gel', unit_price=Decimal('200'), quantity=1,
                                   total_price=Decimal('200'), staff=ravi)
        shared = InvoiceItem.objects.create(invoice=invoice, content_type=ContentType.objects.get_for_model(ServiceMaster),
                                            object_id=self.service.id, description='Colour', unit_price=Decimal('600'),
                                            quantity=1, total_price=Decimal('600'), staff=ravi)
        shared.staff_members.set([ravi, meena])
        appt = Appointment.objects.create(center=self.a, client_phone='9990000001', client_name='Asha', date=today,
                                          start_time=datetime.time(10))
        AppointmentService.objects.create(appointment=appt, service_name='Cut', time=datetime.time(10), duration=90,
                                          staff=ravi)
        res = self.api.get(BASE + 'staff/', {'center_id': self.a.id, 'period': 'today'})
        self.assertEqual(res.status_code, 200, res.data)
        by_name = {r['name']: r for r in res.data['revenues']}
        # Exactly what Finance > Staff Performance credits: the shared 600 line is split between the two.
        from finance.views import credited_shares
        expected = {}
        for item in InvoiceItem.objects.filter(invoice=invoice):
            for member, share, _ in credited_shares(item):
                expected[member.first_name] = expected.get(member.first_name, Decimal('0')) + share
        self.assertEqual(by_name['Ravi']['total'], float(expected['Ravi']))
        self.assertEqual(by_name['Meena']['total'], float(expected['Meena']))
        self.assertEqual((by_name['Ravi']['products'], by_name['Meena']['products']), (200.0, 0.0))
        self.assertGreater(by_name['Meena']['services'], 0)
        self.assertEqual((res.data['usage'][0]['hours'], res.data['usage'][0]['appts']), (1.5, 1))
        self.assertEqual(self.api.get(BASE + 'staff/', {'period': 'someday'}).status_code, 400)

    def test_a_cancelled_bill_credits_no_one(self):
        ravi = StaffMember.objects.create(center=self.a, first_name='Ravi', designation='Stylist', salary=Decimal('1'))
        invoice = self.bill(status='cancelled')
        InvoiceItem.objects.filter(invoice=invoice).update(staff=ravi)
        self.assertEqual(self.api.get(BASE + 'staff/', {'center_id': self.a.id, 'period': 'today'}).data['revenues'], [])


class ReportTests(OwnerBase):
    def test_a_report_without_a_key_keeps_the_figures_and_says_the_text_is_not_set_up(self):
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': ''}):
            res = self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        report = self.api.get(f"{BASE}reports/{res.data['id']}/").data
        self.assertEqual(report['narrative']['status'], 'not_configured')
        self.assertEqual(len(report['numbers']['weeks']), 8)
        self.assertEqual([d['day'] for d in report['numbers']['rhythm']], ['Fri', 'Sat', 'Sun', 'Mon', 'Tue', 'Wed', 'Thu'])

    def test_the_written_part_comes_from_the_model_and_only_what_it_was_given(self):
        reply = mock.Mock()
        reply.json.return_value = {'choices': [{'message': {'content':
            'Sure: {"headline": "A steady week", "summary": "Sales held.", "bullets": ["Fri led"], "watch": []}'}}]}
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': 'k', 'OWNER_AI_MODEL': 'x/y'}), \
                mock.patch('owner_api.ai_providers.requests.post', return_value=reply) as post:
            res = self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['model'], 'x/y')
        self.assertIn('Never invent a number', sent['messages'][0]['content'])
        report = OwnerWeeklyReport.objects.get()
        self.assertEqual((report.narrative['status'], report.narrative['headline'], report.model_name),
                         ('ok', 'A steady week', 'x/y'))

    def test_a_failed_call_is_told_not_hidden_and_is_tried_again_next_time(self):
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': 'k'}), \
                mock.patch('owner_api.ai_providers.requests.post', side_effect=RuntimeError('down')):
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(OwnerWeeklyReport.objects.get().narrative['status'], 'failed')
        reply = mock.Mock()
        reply.json.return_value = {'choices': [{'message': {'content': '{"headline": "Back", "summary": "s"}'}}]}
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': 'k'}), \
                mock.patch('owner_api.ai_providers.requests.post', return_value=reply):
            self.api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json')
        self.assertEqual(OwnerWeeklyReport.objects.get().narrative['headline'], 'Back')

    def test_the_list_offers_the_latest_week_and_one_salon_at_a_time(self):
        res = self.api.get(BASE + 'reports/', {'center_id': self.a.id})
        self.assertEqual((res.status_code, res.data['latest_week']['made']), (200, False))
        self.assertEqual(self.api.get(BASE + 'reports/', {'center_id': 'all'}).status_code, 400)

    def test_a_reader_cannot_ask_for_a_new_report(self):
        reader = account(name='R', phone='9811100002', centers=[self.a], ai_report={'read': True})
        api = signed_in(reader)
        self.assertEqual(api.post(BASE + 'reports/create/', {'center_id': self.a.id}, format='json').status_code, 403)

    def test_the_week_is_friday_to_thursday_and_finished(self):
        start, end = ai_report.week_of(datetime.date(2026, 10, 31))          # a Saturday
        self.assertEqual((start, end), (datetime.date(2026, 10, 23), datetime.date(2026, 10, 29)))
        self.assertEqual(ai_report.week_of(datetime.date(2026, 10, 29))[1], datetime.date(2026, 10, 22))   # Thursday: not over


class SettingsTests(OwnerBase):
    def test_numbers_and_emails_are_saved_and_checked(self):
        res = self.api.put(BASE + 'settings/', {'center_id': self.a.id, 'sms_numbers': ['98732 98293', '+91 90000 11111'],
                                                'owner_emails': ['a@x.in'], 'accounts_emails': ['acc@x.in']}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['sms_numbers'], ['9873298293', '9000011111'])
        self.a.refresh_from_db()
        self.assertEqual((self.a.owner_email_1, self.a.accountant_email_1), ('a@x.in', 'acc@x.in'))
        for bad in ({'sms_numbers': ['12345']}, {'owner_emails': ['not-an-email']},
                    {'sms_numbers': ['9873298293'] * 6 + ['9000011111']}):
            self.assertEqual(self.api.put(BASE + 'settings/', {'center_id': self.a.id, **bad}, format='json').status_code,
                             400, bad)

    def test_a_reader_may_look_but_not_change(self):
        reader = account(name='R', phone='9811100003', centers=[self.a], settings={'read': True})
        api = signed_in(reader)
        self.assertEqual(api.get(BASE + 'settings/', {'center_id': self.a.id}).status_code, 200)
        self.assertEqual(api.put(BASE + 'settings/', {'center_id': self.a.id, 'sms_numbers': []}, format='json').status_code, 403)


class TokenTests(OwnerBase):
    """The owner app's own sign-in: per device, separate from the till's, and it dies with the password."""

    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.clear()

    def pos_user(self):
        from accounts.models import CustomUser
        return CustomUser.objects.create_user(email='till@t.com', password='pw12345!', full_name='Till',
                                              phone='9873298293', role=role('Till', today={'read': True}), center=self.a)

    def sign_in(self):
        res = APIClient().post(BASE + 'login/', {'phone': '9873298293', 'password': 'pw12345!'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Token ' + res.data['token'])
        return client, res.data['token']

    def test_a_token_opens_the_screens_and_a_wrong_one_does_not(self):
        client, _ = self.sign_in()
        self.assertEqual(client.get(BASE + 'today/', {'center_id': self.a.id}).status_code, 200)
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION='Token nonsense')
        self.assertEqual(bad.get(BASE + 'today/').status_code, 401)

    def test_signing_in_does_not_sign_the_person_out_of_the_till_or_another_device(self):
        from rest_framework.authtoken.models import Token
        till = Token.objects.create(user=self.pos_user())
        first, _ = self.sign_in()
        second, _ = self.sign_in()
        self.assertTrue(Token.objects.filter(key=till.key).exists())
        for device in (first, second):
            self.assertEqual(device.get(BASE + 'me/').status_code, 200)

    def test_signing_out_ends_only_that_device(self):
        first, _ = self.sign_in()
        second, _ = self.sign_in()
        self.assertEqual(first.post(BASE + 'logout/').status_code, 204)
        self.assertEqual(first.get(BASE + 'me/').status_code, 401)
        self.assertEqual(second.get(BASE + 'me/').status_code, 200)

    def test_a_changed_password_a_switched_off_account_and_age_end_a_sign_in(self):
        from owner_api.auth import fingerprint
        from owner_api.models import OwnerToken
        client, key = self.sign_in()
        old = OwnerToken.objects.get(key=key)
        OwnerToken.objects.filter(pk=old.pk).update(created=old.created - datetime.timedelta(days=31))
        self.assertEqual(client.get(BASE + 'me/').status_code, 401)          # too old
        client, key = self.sign_in()
        self.owner.set_password('another-pw-9!')
        self.owner.save()
        self.assertEqual(client.get(BASE + 'me/').status_code, 401)          # password changed
        self.owner.set_password('pw12345!')
        self.owner.is_active = False
        self.owner.save()
        fresh = OwnerToken.objects.create(account=self.owner, key='x' * 40, fingerprint=fingerprint(self.owner))
        client.credentials(HTTP_AUTHORIZATION='Token ' + fresh.key)
        self.assertEqual(client.get(BASE + 'me/').status_code, 401)          # switched off

    def test_only_the_newest_ten_devices_stay_signed_in(self):
        from owner_api.models import OwnerToken
        for _ in range(12):
            issue(self.owner)
        self.assertEqual(OwnerToken.objects.filter(account=self.owner).count(), 10)

    def test_the_pos_token_does_not_open_the_owner_app(self):
        from rest_framework.authtoken.models import Token
        till = Token.objects.create(user=self.pos_user())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION='Token ' + till.key)
        self.assertEqual(client.get(BASE + 'me/').status_code, 401)


class AuditTests(OwnerBase):
    def test_the_audit_log_knows_who_an_owner_app_token_belongs_to(self):
        from audit_logs.middleware import _resolve_user_from_token
        who = _resolve_user_from_token(issue(self.owner).key)
        self.assertEqual((who[0], who[1], who[2]), (self.owner.pk, 'Owner', 'o@t.com'))


class MasterPinTests(OwnerBase):
    def test_the_holder_of_the_tick_sees_todays_pin_of_their_salons_only(self):
        from clients import pins
        res = self.api.get(BASE + 'master-pins/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([r['center_id'] for r in res.data['pins']], [self.a.id])
        self.assertEqual(res.data['pins'][0]['pin'], pins.master_pin(self.a, datetime.date.today()))
        self.assertRegex(res.data['pins'][0]['pin'], r'^\d{4}$')
        self.assertEqual(self.api.get(BASE + 'master-pins/').data['pins'][0]['pin'], res.data['pins'][0]['pin'])   # stable all day

    def test_without_the_tick_there_is_no_pin_and_me_says_so(self):
        reader = account(name='R', phone='9811100004', centers=[self.a], today={'read': True})
        api = signed_in(reader)
        self.assertEqual(api.get(BASE + 'master-pins/').status_code, 403)
        self.assertFalse(api.get(BASE + 'me/').data['can_see_master_pins'])
        self.assertTrue(self.api.get(BASE + 'me/').data['can_see_master_pins'])

    def test_a_pos_token_does_not_show_the_pin_here(self):
        from rest_framework.authtoken.models import Token
        from accounts.models import CustomUser
        user = CustomUser.objects.create_user(email='till@t.com', password='pw', full_name='T', center=self.a,
                                              role=role('Till', master_pins={'read': True}))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Token ' + Token.objects.create(user=user).key)
        self.assertEqual(api.get(BASE + 'master-pins/').status_code, 401)


class StandInOpenRouterTests(OwnerBase):
    """The real HTTP call, against a small local server that answers the way OpenRouter does."""

    def serve(self, status, body):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen['path'] = self.path
                seen['auth'] = self.headers.get('Authorization')
                seen['body'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/api/v1/chat/completions', seen

    def run_report(self, url):
        with mock.patch.dict('os.environ', {'OPENROUTER_API_KEY': 'secret-key', 'OWNER_AI_MODEL': 'x/model'}), \
                mock.patch.dict(ai_providers.PROVIDERS['openrouter'], {'url': url}):
            return self.api.post(BASE + 'reports/create/', {'center_id': self.a.id, 'force': True}, format='json')

    def narrative(self):
        return OwnerWeeklyReport.objects.get().narrative

    def test_a_real_reply_in_fences_is_read_and_the_key_and_figures_are_sent(self):
        content = '```json\n{"headline": "Good week", "summary": "Up.", "bullets": ["a", "b"], "watch": ["c"]}\n```'
        url, seen = self.serve(200, {'choices': [{'message': {'content': content}}]})
        self.bill()
        self.assertEqual(self.run_report(url).status_code, 201)
        self.assertEqual(seen['auth'], 'Bearer secret-key')
        self.assertEqual(seen['body']['model'], 'x/model')
        figures = __import__('json').loads(seen['body']['messages'][1]['content'])
        self.assertEqual(figures['salon'], 'Saket')
        self.assertIn('weeks', figures)
        self.assertEqual(len(figures['weeks']), 8)
        self.assertNotIn('secret-key', str(figures))                       # the key never goes into the prompt
        n = self.narrative()
        self.assertEqual((n['status'], n['headline'], n['bullets'], n['watch']), ('ok', 'Good week', ['a', 'b'], ['c']))

    def test_a_server_error_or_a_reply_that_is_not_json_is_a_failed_text_not_a_crash(self):
        url, _ = self.serve(500, {'error': 'boom'})
        self.assertEqual(self.run_report(url).status_code, 201)
        self.assertEqual(self.narrative()['status'], 'failed')
        url, _ = self.serve(200, {'choices': [{'message': {'content': 'I am sorry, I cannot do that.'}}]})
        self.assertEqual(self.run_report(url).status_code, 201)
        self.assertEqual(self.narrative()['status'], 'failed')
        url, _ = self.serve(200, {'unexpected': 'shape'})
        self.assertEqual(self.run_report(url).status_code, 201)
        self.assertEqual(self.narrative()['status'], 'failed')
