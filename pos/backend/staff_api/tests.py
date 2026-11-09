"""The staff app's API: who gets in, that a person sees only their own work, and that the figures are the POS's own."""
import datetime
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.contrib.contenttypes.models import ContentType
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from appointments.models import Appointment, AppointmentService
from billing.models import Invoice, InvoiceItem
from clients.models import Client
from salon_admin.models import Center
from services.models import ServiceMaster
from staff.models import StaffMember
from staff_api.auth import issue
from staff_api.models import StaffToken

BASE = '/team/api/'


class StaffBase(TestCase):
    def setUp(self):
        cache.clear()
        self.center = Center.objects.create(center_name='Saket', display_name='Saket', phone='011-4000-1000')
        self.ravi = self.person('Ravi', '9811100001', '4821')
        self.meena = self.person('Meena', '9811100002', '7305')
        self.client_row = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.service = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('1000'))

    def person(self, name, phone, pin, **extra):
        return StaffMember.objects.create(center=self.center, first_name=name, designation='Stylist', phone=phone,
                                          app_password=make_password(pin) if pin else None, salary=Decimal('1'), **extra)

    def login(self, phone='9811100001', pin='4821'):
        return APIClient().post(BASE + 'login/', {'phone': phone, 'pin': pin}, format='json')

    def api(self, staff=None):
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Token ' + issue(staff or self.ravi).key)
        return api

    def bill(self, status='paid', client=None):
        invoice = Invoice.objects.create(center=self.center, client=client or self.client_row, status=status, subtotal=Decimal('1000'),
                                         total_amount=Decimal('1180'), paid_amount=Decimal('1180'),
                                         invoice_number=f'B-{Invoice.objects.count() + 1}')
        return invoice

    def line(self, invoice, price='1000', staff=None, shared=(), model=ServiceMaster, redeemed=False, name='Haircut'):
        item = InvoiceItem.objects.create(invoice=invoice, content_type=ContentType.objects.get_for_model(model), object_id=self.service.id,
                                          description=name, unit_price=Decimal(price), quantity=1, total_price=Decimal(price),
                                          tax_amount=Decimal('0'), staff=staff, is_redemption=redeemed)
        if shared:
            item.staff_members.set(shared)
        return item


class LoginTests(StaffBase):
    def test_the_right_number_and_pin_get_in_with_any_spelling(self):
        for phone in ('9811100001', '98111 00001', '+91 98111-00001'):
            res = self.login(phone)
            self.assertEqual(res.status_code, 200, (phone, res.data))
        self.assertEqual((res.data['first_name'], res.data['salon']), ('Ravi', 'Saket'))
        self.assertTrue(res.data['token'])

    def test_every_miss_gets_the_same_answer(self):
        self.person('NoPin', '9811100003', None)
        gone = self.person('Gone', '9811100004', '1357', is_active=False)
        outcomes = [self.login(pin='0000'), self.login('9000000000'), self.login('9811100003', '1111'), self.login(gone.phone, '1357')]
        self.assertEqual({r.status_code for r in outcomes}, {400})
        self.assertEqual(len({r.data['detail'] for r in outcomes}), 1)

    def test_five_wrong_pins_lock_the_number_and_even_the_right_one_waits(self):
        for _ in range(5):
            cache.delete('throttle_login_127.0.0.1')
            self.login(pin='0000')
        cache.clear()
        # the lock is its own cache entry: put it back as five misses would have left it
        for _ in range(5):
            cache.set('staff-fails:9811100001', 5, 900)
        res = self.login()
        self.assertEqual((res.status_code, res.data['locked']), (429, True))

    def test_signing_in_clears_the_count_of_misses(self):
        self.login(pin='0000')
        self.assertEqual(cache.get('staff-fails:9811100001'), 1)
        self.assertEqual(self.login().status_code, 200)
        self.assertIsNone(cache.get('staff-fails:9811100001'))

    def test_a_pin_has_to_be_given(self):
        self.assertEqual(self.login(pin='').status_code, 400)
        self.assertEqual(self.login(phone='123').status_code, 400)


class TokenTests(StaffBase):
    def test_a_token_opens_the_app_and_a_wrong_one_does_not(self):
        self.assertEqual(self.api().get(BASE + 'me/').status_code, 200)
        bad = APIClient()
        bad.credentials(HTTP_AUTHORIZATION='Token nonsense')
        self.assertEqual(bad.get(BASE + 'me/').status_code, 401)
        self.assertEqual(APIClient().get(BASE + 'jobcard/').status_code, 401)

    def test_signing_out_ends_only_that_phone(self):
        first, second = self.api(), self.api()
        self.assertEqual(first.post(BASE + 'logout/').status_code, 204)
        self.assertEqual(first.get(BASE + 'me/').status_code, 401)
        self.assertEqual(second.get(BASE + 'me/').status_code, 200)

    def test_age_a_new_pin_and_switching_the_person_off_end_a_sign_in(self):
        api = self.api()
        StaffToken.objects.update(created=StaffToken.objects.get().created - datetime.timedelta(days=31))
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)
        api = self.api()
        StaffMember.objects.filter(pk=self.ravi.pk).update(app_password=make_password('9999'))
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)
        self.ravi.refresh_from_db()
        api = self.api()
        StaffMember.objects.filter(pk=self.ravi.pk).update(is_active=False)
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)

    def test_only_the_newest_three_phones_stay_signed_in(self):
        for _ in range(6):
            issue(self.ravi)
        self.assertEqual(StaffToken.objects.filter(staff=self.ravi).count(), 3)

    def test_a_pos_token_does_not_open_the_staff_app(self):
        from accounts.models import CustomUser
        from rest_framework.authtoken.models import Token
        from salon_admin.models import Role
        user = CustomUser.objects.create_user(email='u@t.com', password='pw', full_name='U', center=self.center,
                                              role=Role.objects.create(name='R', permissions={}))
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION='Token ' + Token.objects.create(user=user).key)
        self.assertEqual(api.get(BASE + 'me/').status_code, 401)


class JobCardTests(StaffBase):
    def test_a_person_is_credited_only_their_own_lines_by_the_pos_rule(self):
        from finance.views import credited_shares
        invoice = self.bill()
        self.line(invoice, '1000', staff=self.ravi)                                     # Ravi, whole
        shared = self.line(invoice, '600', staff=self.ravi, shared=[self.ravi, self.meena], name='Colour')   # split between two
        self.line(invoice, '400', staff=self.meena, name='Spa')                          # Meena only
        today = datetime.date.today().isoformat()
        res = self.api().get(BASE + 'jobcard/', {'date': today})
        self.assertEqual(res.status_code, 200, res.data)
        expected = 1000 + float(next(s for m, s, _ in credited_shares(shared) if m.pk == self.ravi.pk))
        self.assertEqual(res.data['total'], expected)
        self.assertEqual(sorted(l['item'] for l in res.data['lines']), ['Colour', 'Haircut'])
        mine = {k['key']: k for k in res.data['kinds']}
        self.assertEqual((mine['services']['count'], mine['services']['amount']), (2, expected))
        meena = self.api(self.meena).get(BASE + 'jobcard/', {'date': today}).data
        self.assertEqual(meena['total'], 400 + float(next(s for m, s, _ in credited_shares(shared) if m.pk == self.meena.pk)))

    def test_cancelled_and_draft_bills_credit_no_one_and_redemptions_stand_apart(self):
        self.line(self.bill(status='cancelled'), '500', staff=self.ravi)
        self.line(self.bill(status='draft'), '500', staff=self.ravi)
        self.line(self.bill(), '700', staff=self.ravi, redeemed=True, name='From card')
        res = self.api().get(BASE + 'jobcard/').data
        self.assertEqual((res['total'], res['redeemed']), (0.0, {'count': 1, 'amount': 700.0}))

    def test_the_kinds_are_told_apart(self):
        from inventory.models import Product
        invoice = self.bill()
        product = Product.objects.create(center=self.center, name='Gel', price=Decimal('200'))
        InvoiceItem.objects.create(invoice=invoice, content_type=ContentType.objects.get_for_model(Product), object_id=product.id,
                                   description='Gel', unit_price=Decimal('200'), quantity=2, total_price=Decimal('400'), staff=self.ravi)
        res = {k['key']: k for k in self.api().get(BASE + 'jobcard/').data['kinds']}
        self.assertEqual((res['products']['count'], res['products']['amount']), (2, 400.0))

    def test_another_day_is_another_card_and_a_bad_date_means_today(self):
        invoice = self.bill()
        self.line(invoice, '300', staff=self.ravi)
        Invoice.objects.filter(pk=invoice.pk).update(created_at=invoice.created_at - datetime.timedelta(days=2))
        self.assertEqual(self.api().get(BASE + 'jobcard/').data['total'], 0.0)
        older = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
        self.assertEqual(self.api().get(BASE + 'jobcard/', {'date': older}).data['total'], 300.0)
        self.assertEqual(self.api().get(BASE + 'jobcard/', {'date': 'nonsense'}).status_code, 200)

    def test_the_month_adds_up_the_days(self):
        self.line(self.bill(), '300', staff=self.ravi)
        res = self.api().get(BASE + 'month/').data
        self.assertEqual((res['total'], res['days_worked'], res['best_amount'], res['average_day']), (300.0, 1, 300.0, 300.0))
        self.assertEqual(sum(d['amount'] for d in res['days']), 300.0)
        self.assertEqual(self.api().get(BASE + 'month/', {'month': '2020-01'}).data['total'], 0.0)
        self.assertEqual(self.api().get(BASE + 'month/', {'month': 'nonsense'}).status_code, 200)


class AppointmentsAndProfileTests(StaffBase):
    def test_a_person_sees_only_their_own_bookings_with_counts_around_the_day(self):
        today = datetime.date.today()

        def book(staff, days, name='A'):
            a = Appointment.objects.create(center=self.center, client_phone='9990000001', client_name=name,
                                           date=today + datetime.timedelta(days=days), start_time=datetime.time(11))
            AppointmentService.objects.create(appointment=a, service_name='Cut', time=datetime.time(11), duration=45, staff=staff)
            return a
        mine, other = book(self.ravi, 0, 'Asha'), book(self.meena, 0, 'Bina')
        book(self.ravi, 1)
        res = self.api().get(BASE + 'appointments/', {'date': today.isoformat()}).data
        self.assertEqual([r['appointment'] for r in res['rows']], [mine.id])
        self.assertEqual((res['rows'][0]['client'], res['rows'][0]['end']), ('Asha', '11:45'))
        self.assertEqual(res['counts'][today.isoformat()], 1)
        self.assertEqual(res['counts'][(today + datetime.timedelta(days=1)).isoformat()], 1)
        self.assertNotIn(other.id, [r['appointment'] for r in res['rows']])

    def test_the_profile_masks_the_aadhaar_and_never_shows_the_pin(self):
        StaffMember.objects.filter(pk=self.ravi.pk).update(aadhar_number='1234 5678 9012', email='r@x.in', commission_percentage=Decimal('7.5'))
        res = self.api().get(BASE + 'profile/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data['aadhaar'], res.data['salon'], res.data['salon_phone'], res.data['commission']),
                         ('XXXX XXXX 9012', 'Saket', '011-4000-1000', 7.5))
        self.assertNotIn('1234 5678', str(res.data))
        self.assertNotIn('app_password', str(res.data))
        self.assertNotIn('pbkdf2', str(res.data))
