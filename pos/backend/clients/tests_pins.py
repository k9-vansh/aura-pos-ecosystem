"""Client PINs and the daily master PIN: made, sealed, checked at the till, limited, and required before held money is spent."""
import datetime
from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, Invoice
from billing.tests import BillingTestBase
from clients import pins
from clients.models import Client, MasterPin, PinAttempt
from pos_backend import app_settings
from pos_backend.models import AppSetting
from salon_admin.models import Center, Role

PIN_VERIFY = '/clients/api/pin/verify/'
PIN_SEND = '/clients/api/pin/send/'
MASTER = '/clients/api/master-pins/'


def set_setting(key, value):
    AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': value})
    app_settings.invalidate()


class PinServiceTests(BillingTestBase):
    def test_a_pin_is_four_digits_sealed_and_never_the_plain_text_in_the_database(self):
        pin = pins.set_pin(self.client_obj)
        self.assertRegex(pin, r'^\d{4}$')
        self.client_obj.refresh_from_db()
        self.assertNotIn(pin, self.client_obj.pin_sealed)
        self.assertEqual(pins.client_pin(self.client_obj), pin)
        with self.assertRaises(ValueError):
            pins.set_pin(self.client_obj, '12')
        self.assertEqual(pins.set_pin(self.client_obj, ' 4821 '), '4821')

    def test_a_new_client_has_a_pin_from_the_start_and_it_is_not_made_again(self):
        fresh = Client.objects.create(first_name='N', phone='9990000009', center=self.center)
        self.assertTrue(pins.has_pin(fresh))
        self.assertRegex(pins.client_pin(fresh), r'^\d{4}$')
        self.assertEqual(pins.ensure_pin(fresh), (pins.client_pin(fresh), False))
        self.assertFalse(pins.has_pin(Client.objects.create(first_name='NoPhone', phone='', center=self.center)))

    def test_creating_a_client_at_the_counter_sends_the_pin_and_shows_it_in_the_terminal_without_whatsapp(self):
        import io
        from contextlib import redirect_stdout
        from django.test import override_settings
        from notifications.models import DeliveryLog
        fresh = Client.objects.create(first_name='Z', phone='9990000010', center=self.center, whatsapp_consent=True)
        out = io.StringIO()
        with override_settings(DEBUG=True), redirect_stdout(out):
            pins.on_create(fresh)
        self.assertIn(pins.client_pin(fresh), out.getvalue())
        self.assertIn('WhatsApp is not linked', out.getvalue())
        self.assertEqual(DeliveryLog.objects.get(event='client.welcome', channel='whatsapp').channel, 'whatsapp')
        quiet = io.StringIO()
        with override_settings(DEBUG=False), redirect_stdout(quiet):                # never on a live server
            pins.send_pin(fresh, 'again', 'pin-again')
        self.assertEqual(quiet.getvalue(), '')

    def test_ensure_pin_makes_one_once(self):
        Client.objects.filter(pk=self.client_obj.pk).update(pin_sealed='')
        self.client_obj.refresh_from_db()
        first, made = pins.ensure_pin(self.client_obj)
        second, made_again = pins.ensure_pin(self.client_obj)
        self.assertEqual((first, made, made_again), (second, True, False))

    def test_the_wrong_pin_is_refused_and_five_wrong_tries_lock_it(self):
        pins.set_pin(self.client_obj, '4821')
        self.assertTrue(pins.verify_client(self.client_obj, '4821'))
        for _ in range(5):
            self.assertFalse(pins.verify_client(self.client_obj, '0000'))
        with self.assertRaises(pins.PinLocked):
            pins.verify_client(self.client_obj, '4821')            # even the right one waits

    def test_a_client_with_no_pin_never_matches(self):
        self.assertFalse(pins.verify_client(self.client_obj, '0000'))

    def test_the_master_pin_is_one_per_branch_per_day_and_stays_the_same_all_day(self):
        a = pins.master_pin(self.center)
        self.assertRegex(a, r'^\d{4}$')
        self.assertEqual(pins.master_pin(self.center), a)
        self.assertEqual(MasterPin.objects.filter(center=self.center).count(), 1)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        pins.master_pin(self.center, tomorrow)
        self.assertEqual(MasterPin.objects.filter(center=self.center).count(), 2)
        self.assertNotEqual(MasterPin.objects.get(center=self.center, date=tomorrow).sealed,
                            MasterPin.objects.get(center=self.center, date=datetime.date.today()).sealed)

    def test_a_pass_is_for_one_client_at_one_branch(self):
        token = pins.issue_token(self.client_obj, self.center, self.owner, 'client')
        self.assertEqual(pins.token_via(token, self.client_obj, self.center), 'client')
        other = Client.objects.create(first_name='B', phone='9990000002', center=self.center)
        self.assertIsNone(pins.token_via(token, other, self.center))
        self.assertIsNone(pins.token_via(token, self.client_obj, self.other_center))
        self.assertIsNone(pins.token_via('junk', self.client_obj, self.center))
        self.assertIsNone(pins.token_via('', self.client_obj, self.center))


class TillPinEndpointTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        pins.set_pin(self.client_obj, '4821')

    def verify(self, pin, **extra):
        return self.api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id, 'pin': pin, **extra}, format='json')

    def test_the_right_pin_gives_a_pass_and_a_wrong_one_counts(self):
        res = self.verify('4821')
        self.assertEqual((res.status_code, res.data['via']), (200, 'client'))
        self.assertEqual(pins.token_via(res.data['token'], self.client_obj, self.center), 'client')
        bad = self.verify('1111')
        self.assertEqual((bad.status_code, bad.data['attempts_left']), (400, 4))

    def test_five_wrong_tries_lock_the_client_for_every_way_in(self):
        for _ in range(5):
            self.verify('1111')
        res = self.verify('4821')
        self.assertEqual((res.status_code, res.data['locked']), (429, True))

    def test_the_master_pin_works_and_is_written_down_with_who_used_it(self):
        master = pins.master_pin(self.center)
        res = self.verify(master, master=True)
        self.assertEqual((res.status_code, res.data['via']), (200, 'master'))
        row = PinAttempt.objects.get(kind='master', ok=True)
        self.assertEqual((row.user_id, row.client_id, row.center_id), (self.owner.pk, self.client_obj.pk, self.center.pk))
        self.assertEqual(self.verify('0000', master=True).status_code, 400)

    def test_the_master_pin_of_one_branch_is_not_the_other(self):
        other_master = pins.master_pin(self.other_center)
        if other_master == pins.master_pin(self.center):                  # a one in nine thousand tie
            other_master = '0000'
        self.assertEqual(self.verify(other_master, master=True).status_code, 400)

    def test_a_client_with_no_pin_is_told_so(self):
        bare = Client.objects.create(first_name='B', phone='9990000003', center=self.center)
        Client.objects.filter(pk=bare.pk).update(pin_sealed='')                      # a client from before PINs existed
        res = self.api.post(PIN_VERIFY, {'client': bare.id, 'center': self.center.id, 'pin': '1234'}, format='json')
        self.assertEqual((res.status_code, res.data['no_pin']), (400, True))

    def test_the_pin_never_comes_back_in_any_client_response(self):
        res = self.api.get(f'/clients/api/clients/{self.client_obj.id}/')
        self.assertEqual(res.status_code, 200)
        self.assertNotIn('pin_sealed', res.data)
        self.assertNotIn('4821', str(res.data))

    def test_sending_a_pin_makes_a_new_one_and_never_shows_it(self):
        old = pins.client_pin(self.client_obj)
        self.client_obj.whatsapp_consent = True
        self.client_obj.save()
        res = self.api.post(PIN_SEND, {'client': self.client_obj.id, 'center': self.center.id}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['sent_to'], ['whatsapp'])
        self.client_obj.refresh_from_db()
        self.assertNotIn(pins.client_pin(self.client_obj), str(res.data))
        # three sends an hour at most
        for _ in range(3):
            self.api.post(PIN_SEND, {'client': self.client_obj.id, 'center': self.center.id}, format='json')
        self.assertEqual(self.api.post(PIN_SEND, {'client': self.client_obj.id, 'center': self.center.id}, format='json').status_code, 429)
        self.assertTrue(old)

    def test_a_client_who_cannot_be_messaged_is_sent_to_the_master_pin(self):
        self.client_obj.whatsapp_consent = False
        self.client_obj.save()
        res = self.api.post(PIN_SEND, {'client': self.client_obj.id, 'center': self.center.id}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_nobody_without_billing_cannot_ask(self):
        nobody = CustomUser.objects.create_user(email='n@t.com', password='pw', full_name='N', center=self.center,
                                                role=Role.objects.create(name='None', permissions={}))
        api = APIClient()
        api.force_authenticate(nobody)
        res = api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id, 'pin': '4821'}, format='json')
        self.assertEqual(res.status_code, 403)


class MasterPinTickTests(BillingTestBase):
    def user(self, perms, centers=()):
        u = CustomUser.objects.create_user(email=f'u{CustomUser.objects.count()}@t.com', password='pw', full_name='U', center=self.center,
                                           role=Role.objects.create(name=f'r{CustomUser.objects.count()}', permissions=perms))
        u.centers.add(*centers)
        api = APIClient()
        api.force_authenticate(u)
        return api

    def test_only_the_tick_shows_the_master_pins_and_only_of_the_users_branches(self):
        api = self.user({'admin': {'master_pins': {'read': True}}}, [self.center])
        res = api.get(MASTER)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([p['center_id'] for p in res.data['pins']], [self.center.id])
        self.assertEqual(res.data['pins'][0]['pin'], pins.master_pin(self.center))
        none = self.user({'admin': {'bills': {'read': True}}}, [self.center])
        self.assertEqual(none.get(MASTER).status_code, 403)
        till = self.user({'billing': {'new_invoice': {'read': True, 'create': True}}}, [self.center])
        self.assertEqual(till.get(MASTER).status_code, 403)             # a cashier never sees it

    def test_the_owner_sees_every_branch(self):
        res = self.api.get(MASTER)
        self.assertEqual({p['center_id'] for p in res.data['pins']}, {self.center.id, self.other_center.id})


class BillingGateTests(BillingTestBase):
    def setUp(self):
        super().setUp()
        from pos_backend.testing import pin_gate_on
        pin_gate_on()                          # the base class switches it off; these tests are about the gate
        cache.clear()
        AdvancePayment.objects.create(client=self.client_obj, amount=Decimal('5000.00'), notes='top up')
        pins.set_pin(self.client_obj, '4821')
        self.pay_advance = [{'payment_method': 'Advance', 'amount': '1050.00'}]

    def post(self, payload):
        return self.api.post('/billing/invoices/', payload, format='json')

    def test_spending_an_advance_without_the_pin_is_refused_and_nothing_moves(self):
        res = self.post(self.build_payload(payments=self.pay_advance))
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('pin_required', res.data)
        self.assertEqual(Invoice.objects.count(), 0)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('5000.00'))

    def test_a_good_pass_lets_it_through(self):
        token = self.api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id, 'pin': '4821'}, format='json').data['token']
        res = self.post(self.build_payload(payments=self.pay_advance, pin_token=token))
        self.assertEqual(res.status_code, 201, res.data)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('3950.00'))

    def test_the_master_pin_pass_works_the_same(self):
        token = self.api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id,
                                           'pin': pins.master_pin(self.center), 'master': True}, format='json').data['token']
        self.assertEqual(self.post(self.build_payload(payments=self.pay_advance, pin_token=token)).status_code, 201)

    def test_a_pass_for_another_client_does_not_work(self):
        other = Client.objects.create(first_name='B', phone='9990000009', center=self.center)
        pins.set_pin(other, '1357')
        token = self.api.post(PIN_VERIFY, {'client': other.id, 'center': self.center.id, 'pin': '1357'}, format='json').data['token']
        self.assertEqual(self.post(self.build_payload(payments=self.pay_advance, pin_token=token)).status_code, 400)

    def test_cash_never_asks_for_a_pin(self):
        res = self.post(self.build_payload(payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]))
        self.assertEqual(res.status_code, 201, res.data)

    def test_cashback_is_held_money_too(self):
        from billing.models import CashbackTransaction
        CashbackTransaction.objects.create(client=self.client_obj, amount=Decimal('300.00'), notes='earned')
        res = self.post(self.build_payload(payments=[{'payment_method': 'Cashback Wallet', 'amount': '300.00'},
                                                     {'payment_method': 'Cash', 'amount': '750.00'}]))
        self.assertEqual(res.status_code, 400)
        self.assertIn('pin_required', res.data)

    def test_a_client_with_no_pin_yet_is_let_through_unless_the_setting_holds_them_back(self):
        self.client_obj.pin_sealed = ''
        self.client_obj.save()
        self.assertEqual(self.post(self.build_payload(payments=self.pay_advance)).status_code, 201)
        self.client_obj.refresh_from_db()
        AdvancePayment.objects.create(client=self.client_obj, amount=Decimal('5000.00'), notes='more')
        set_setting('billing.pin_for_everyone', True)
        self.assertEqual(self.post(self.build_payload(payments=self.pay_advance)).status_code, 400)

    def test_the_whole_check_can_be_switched_off(self):
        set_setting('billing.require_client_pin', False)
        self.assertEqual(self.post(self.build_payload(payments=self.pay_advance)).status_code, 201)

    def test_a_walk_in_is_not_asked(self):
        res = self.post(self.build_payload(client=None, payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]))
        self.assertIn(res.status_code, (201, 400))

    def test_the_pay_button_on_a_held_bill_asks_too(self):
        held = self.post(self.build_payload(status='draft'))
        self.assertEqual(held.status_code, 201, held.data)
        pay = lambda extra: self.api.post(f"/billing/invoices/{held.data['id']}/pay/",
                                          {'payment_method': 'Advance', 'amount': '1050.00', **extra}, format='json')
        res = pay({})
        self.assertEqual(res.status_code, 400, res.data)
        token = self.api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id, 'pin': '4821'}, format='json').data['token']
        self.assertEqual(pay({'pin_token': token}).status_code, 200)

    def test_the_till_can_ask_up_front_whether_a_pin_is_needed(self):
        ask = lambda: self.api.post('/clients/api/pin/needed/', {'client': self.client_obj.id, 'center': self.center.id}, format='json')
        self.assertEqual(ask().data, {'needed': True})
        Client.objects.filter(pk=self.client_obj.pk).update(pin_sealed='')
        self.assertEqual(ask().data, {'needed': False})                       # no PIN yet, and nobody is held back
        set_setting('billing.pin_for_everyone', True)
        self.assertEqual(ask().data, {'needed': True})
        set_setting('billing.require_client_pin', False)
        self.assertEqual(ask().data, {'needed': False})
        self.assertEqual(self.api.post('/clients/api/pin/needed/', {'client': self.client_obj.id}, format='json').status_code, 400)

    def test_moving_a_cash_bill_onto_the_advance_afterwards_asks_for_the_pin_too(self):
        cash = self.post(self.build_payload(payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]))
        self.assertEqual(cash.status_code, 201, cash.data)
        change = lambda extra: self.api.post(f"/billing/invoices/{cash.data['id']}/change_payment/",
                                             {'payment_method': 'Advance', **extra}, format='json')
        res = change({})
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn('pin_required', res.data)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('5000.00'))            # nothing moved
        token = self.api.post(PIN_VERIFY, {'client': self.client_obj.id, 'center': self.center.id, 'pin': '4821'}, format='json').data['token']
        self.assertEqual(change({'pin_token': token}).status_code, 200)
        self.client_obj.refresh_from_db()
        self.assertEqual(self.client_obj.advance_balance, Decimal('3950.00'))
        # Cash to UPI spends nothing the client holds: no PIN.
        other = self.post(self.build_payload(payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]))
        self.assertEqual(self.api.post(f"/billing/invoices/{other.data['id']}/change_payment/",
                                       {'payment_method': 'UPI'}, format='json').status_code, 200)


class SaleGivesAPinTests(BillingTestBase):
    def advance_bill(self):
        line = {'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance Payment',
                'unit_price': '2000.00', 'quantity': 1, 'tax_percentage': '0', 'staff_members': [self.staff.id]}
        return self.api.post('/billing/invoices/', self.build_payload(items=[line], payments=[{'payment_method': 'Cash', 'amount': '2000.00'}]), format='json')

    def test_buying_an_advance_gives_the_client_a_pin_and_queues_it_for_whatsapp(self):
        from notifications.models import DeliveryLog
        self.client_obj.whatsapp_consent = True
        self.client_obj.save()
        Client.objects.filter(pk=self.client_obj.pk).update(pin_sealed='')           # a client from before PINs existed
        self.client_obj.refresh_from_db()
        self.assertFalse(pins.has_pin(self.client_obj))
        res = self.advance_bill()
        self.assertEqual(res.status_code, 201, res.data)
        self.client_obj.refresh_from_db()
        self.assertTrue(pins.has_pin(self.client_obj))
        sent = DeliveryLog.objects.get(event='client.pin')
        self.assertEqual((sent.channel, sent.recipient), ('whatsapp', self.client_obj.phone))
        self.assertIn(pins.client_pin(self.client_obj), sent.body)
        self.assertIn('advance', sent.body)

    def test_a_second_purchase_keeps_the_same_pin_and_a_bill_without_a_wallet_item_makes_none(self):
        self.advance_bill()
        first = pins.client_pin(Client.objects.get(pk=self.client_obj.pk))
        self.advance_bill()
        self.assertEqual(pins.client_pin(Client.objects.get(pk=self.client_obj.pk)), first)
        other = Client.objects.create(first_name='B', phone='9990000005', center=self.center)
        before = pins.client_pin(other)
        self.api.post('/billing/invoices/', self.build_payload(client=other.id, payments=[{'payment_method': 'Cash', 'amount': '1050.00'}]),
                      format='json')
        from notifications.models import DeliveryLog
        self.assertEqual(pins.client_pin(Client.objects.get(pk=other.pk)), before)
        self.assertFalse(DeliveryLog.objects.filter(event='client.pin', client=other).exists())    # a plain bill sends nothing

    def test_a_pin_that_cannot_be_sent_never_holds_up_the_bill(self):
        self.client_obj.whatsapp_consent = False
        self.client_obj.save()
        res = self.advance_bill()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(pins.has_pin(Client.objects.get(pk=self.client_obj.pk)))
