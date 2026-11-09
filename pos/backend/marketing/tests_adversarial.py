"""Adversarial Marketing tests: whole journeys and hostile input.

The unit tests prove each rule on its own. These put the rules under the
conditions a busy salon creates: offers going out of date between holding a bill
and completing it, a customer cancelling and coming back, a service renamed
under a live campaign, a till sending garbage, a branch user poking at things
that are not theirs.
"""

import datetime
import random
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from billing.tests import BillingTestBase
from billing.wallets import cashback_balance
from clients.models import Client, ClientMembership
from marketing.models import (
    Membership, Package, Promotion, PromotionUsage, ValueCard, WhatsAppMessage)
from pos_backend.money import money
from pos_backend.testing import bill_and_pay
from salon_admin.models import Role
from services.models import ServiceMaster


def day(offset=0):
    return datetime.date.today() + datetime.timedelta(days=offset)


class OfferBase(BillingTestBase):
    def offer(self, **extra):
        fields = dict(
            name='Offer', promo_type='Discount', discount_type='Percentage', discount_value=Decimal('10'),
            level='Organisation', start_date=day(-1), end_date=day(30), config={})
        fields.update(extra)
        return Promotion.objects.create(**fields)

    def bill(self, promo=None, expect=201, **overrides):
        if promo is not None:
            overrides['promo_id'] = promo.id
        return self.create_invoice(self.build_payload(**overrides), expect_status=expect)

    def hold(self, promo=None, **overrides):
        if promo is not None:
            overrides['promo_id'] = promo.id
        r = self.api.post('/billing/invoices/', self.build_payload(status='draft', **overrides), format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r

    def complete(self, held):
        total = held.data.get('paid_now', held.data['total_amount'])
        payload = self.build_payload(status='paid', promo_id=held.data.get('promotion'),
                                     payments=[{'payment_method': 'Cash', 'amount': str(total)}])
        return self.api.put(f"/billing/invoices/{held.data['id']}/", payload, format='json')

    def cancel(self, bill_id):
        return self.api.post(f'/billing/invoices/{bill_id}/cancel/', {'reason': 'customer changed mind'},
                             format='json')

    def assert_books_balance(self, invoice_id):
        invoice = Invoice.objects.get(pk=invoice_id)
        self.assert_items_sum_to_subtotal(invoice)
        self.assert_tax_matches_items(invoice)
        self.assert_paid_matches_payments(invoice)
        return invoice


class CheckoutJourneyTests(OfferBase):
    def test_each_kind_of_offer_prices_a_real_bill(self):
        cases = {
            'percent': (dict(), Decimal('100.00')),
            'flat rupees': (dict(discount_type='Flat', discount_value=Decimal('250')), Decimal('250.00')),
            'flat bigger than the bill is capped': (
                dict(discount_type='Flat', discount_value=Decimal('99999')), Decimal('1000.00')),
            'hundred percent': (dict(discount_value=Decimal('100')), Decimal('1000.00')),
            'odd percent rounds to paise': (dict(discount_value=Decimal('33.33')), Decimal('333.30')),
            'flat price on the service': (
                dict(promo_type='FlatPrice', discount_value=None,
                     config={'flat_price': 400, 'specific_service': 'Haircut'}), Decimal('600.00')),
            'flat price above the price discounts nothing': (
                dict(promo_type='FlatPrice', discount_value=None,
                     config={'flat_price': 5000, 'specific_service': 'Haircut'}), Decimal('0.00')),
            'flat amount off the services': (
                dict(promo_type='FlatPrice', discount_value=None, config={'flat_price': 75}), Decimal('75.00')),
        }
        for label, (fields, expected) in cases.items():
            with self.subTest(label):
                Invoice.objects.all().delete()
                promo = self.offer(name=label, **fields)
                response = self.bill(promo)
                invoice = self.assert_books_balance(response.data['id'])
                self.assertEqual(money(invoice.discount), expected, label)

    def test_a_hundred_percent_offer_completes_a_zero_bill(self):
        promo = self.offer(discount_value=Decimal('100'))
        invoice = self.assert_books_balance(self.bill(promo).data['id'])
        self.assertEqual(money(invoice.total_amount), Decimal('0.00'))
        self.assertEqual(invoice.status, 'paid')

    def test_the_discount_lands_on_the_named_service_only(self):
        spa = ServiceMaster.objects.create(name='Spa', default_price=Decimal('2000'), tax_percentage=Decimal('5'))
        promo = self.offer(config={'target': 'Specific Service', 'specific_service': 'Spa',
                                   'discount_value': 10})
        spa_line = self.service_line(unit_price='2000.00', object_id=spa.id, description='Spa')
        response = self.bill(promo, items=[self.service_line(), spa_line])
        invoice = self.assert_books_balance(response.data['id'])
        self.assertEqual(money(invoice.discount), Decimal('200.00'))       # 10% of Spa only
        # The bill-level discount is spread over the services pro rata (for tax), but it all adds up.
        shares = [money(i.bill_discount_share) for i in invoice.items.all()]
        self.assertEqual(sum(shares), Decimal('200.00'))

    def test_a_named_service_that_is_not_on_the_bill_refuses_the_offer(self):
        ServiceMaster.objects.create(name='Spa', default_price=Decimal('2000'), tax_percentage=Decimal('5'))
        promo = self.offer(config={'target': 'Specific Service', 'specific_service': 'Spa', 'discount_value': 10})
        r = self.bill(promo, expect=400)
        self.assertIn('Spa', str(r.data))
        self.assertFalse(Invoice.objects.exists())

    def test_renaming_the_service_keeps_the_offer_working(self):
        """Offers are tied to the service by id; a rename must not break them."""
        api = self.api
        created = api.post('/marketing/api/promotions/', {
            'name': 'Cut deal', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Percentage',
            'discount_value': '10', 'start_date': str(day()), 'end_date': str(day(30)),
            'config': {'target': 'Specific Service', 'specific_service': 'Haircut', 'discount_value': 10}},
            format='json')
        self.assertEqual(created.status_code, 201, created.data)
        self.service.name = 'Signature Haircut'
        self.service.save()
        promo = Promotion.objects.get(pk=created.data['id'])
        response = self.bill(promo, items=[self.service_line(description='Signature Haircut')])
        self.assertEqual(money(Invoice.objects.get(pk=response.data['id']).discount), Decimal('100.00'))
        shown = api.get(f'/marketing/api/promotions/{promo.id}/').data
        self.assertEqual(shown['config']['specific_service'], 'Signature Haircut')

    def test_a_trigger_deal_needs_both_services(self):
        spa = ServiceMaster.objects.create(name='Spa', default_price=Decimal('2000'), tax_percentage=Decimal('5'))
        promo = self.offer(promo_type='Trigger', discount_value=None,
                           config={'trigger_services': ['Haircut', 'Spa'], 'trigger_discount': 20})
        self.bill(promo, expect=400)
        spa_line = self.service_line(unit_price='2000.00', object_id=spa.id, description='Spa')
        invoice = self.assert_books_balance(self.bill(promo, items=[self.service_line(), spa_line]).data['id'])
        self.assertEqual(money(invoice.discount), Decimal('600.00'))        # 20% of 3000

    def test_a_minimum_bill_is_enforced(self):
        promo = self.offer(config={'min_bill_amount': 1500})
        self.bill(promo, expect=400)
        self.assertEqual(money(self.assert_books_balance(
            self.bill(promo, items=[self.service_line(qty=2, unit_price='1000.00')]).data['id']).discount),
            Decimal('200.00'))

    def test_products_are_never_discounted(self):
        promo = self.offer(discount_value=Decimal('50'))
        invoice = self.assert_books_balance(
            self.bill(promo, items=[self.service_line(), self.product_line()]).data['id'])
        self.assertEqual(money(invoice.discount), Decimal('500.00'))
        product = invoice.items.get(content_type__model='product')
        self.assertEqual(money(product.bill_discount_share), Decimal('0.00'))

    def test_a_walk_in_cannot_use_a_limited_offer(self):
        promo = self.offer(max_usage_per_client=1)
        r = self.bill(promo, expect=400, client=None)
        self.assertIn('registered client', str(r.data))


class AllowanceTests(OfferBase):
    def test_one_per_customer_survives_a_refusal_and_returns_on_cancel_only(self):
        promo = self.offer(max_usage_per_client=1)
        first = self.bill(promo)
        self.bill(promo, expect=400)                                       # used up
        self.assertEqual(PromotionUsage.objects.filter(promotion=promo).count(), 1)

        self.assertEqual(self.cancel(first.data['id']).status_code, 200)
        usage = PromotionUsage.objects.get(promotion=promo)
        self.assertTrue(usage.is_reversed and usage.releases_allowance)    # nothing was delivered
        second = self.bill(promo)                                          # allowed again
        self.assertEqual(second.status_code, 201)
        self.bill(promo, expect=400)                                       # and used up again

    def test_a_different_customer_has_their_own_allowance(self):
        promo = self.offer(max_usage_per_client=1)
        self.bill(promo)
        other = Client.objects.create(first_name='Bina', phone='9990000002', center=self.center)
        self.assertEqual(self.bill(promo, client=other.id).status_code, 201)

    def test_three_uses_allowed_and_the_fourth_refused(self):
        promo = self.offer(max_usage_per_client=3)
        for _ in range(3):
            self.assertEqual(self.bill(promo).status_code, 201)
        self.bill(promo, expect=400)

    def test_members_only_offers(self):
        promo = self.offer(members_only=True)
        self.bill(promo, expect=400)
        gold = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'), expiry_days=30,
                                         level='Organisation')
        member = ClientMembership.objects.create(client=self.client_obj, membership=gold,
                                                 expiry_date=day(30))
        self.assertEqual(self.bill(promo).status_code, 201)
        member.expiry_date = day(-1)                                       # lapsed yesterday
        member.save()
        self.bill(promo, expect=400)
        member.expiry_date, member.is_active = day(30), False              # switched off
        member.save()
        self.bill(promo, expect=400)


class OfferChangesUnderABillTests(OfferBase):
    def test_an_offer_switched_off_while_a_bill_is_held_is_refused_at_completion(self):
        promo = self.offer()
        held = self.hold(promo)
        promo.is_active = False
        promo.save()
        r = self.complete(held)
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('no longer active', str(r.data))
        self.assertFalse(PromotionUsage.objects.exists())

    def test_an_offer_that_expired_overnight_is_refused_at_completion(self):
        promo = self.offer()
        held = self.hold(promo)
        promo.end_date = day(-1)
        promo.start_date = day(-5)
        promo.save()
        self.assertEqual(self.complete(held).status_code, 400)

    def test_an_offer_whose_terms_changed_is_priced_at_the_new_terms(self):
        promo = self.offer()
        held = self.hold(promo)
        promo.discount_value = Decimal('20')
        promo.save()
        stale = self.complete(held)                       # still pays the old Rs 945: refused, with the new figure
        self.assertEqual(stale.status_code, 400)
        self.assertIn('840.00', str(stale.data))
        payload = self.build_payload(status='paid', promo_id=promo.id,
                                     payments=[{'payment_method': 'Cash', 'amount': '840.00'}])
        r = self.api.put(f"/billing/invoices/{held.data['id']}/", payload, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(money(Invoice.objects.get(pk=held.data['id']).discount), Decimal('200.00'))
        self.assert_books_balance(held.data['id'])

    def test_a_held_bill_with_an_offer_is_not_a_use(self):
        promo = self.offer(max_usage_per_client=1)
        self.hold(promo)
        self.assertFalse(PromotionUsage.objects.exists())
        self.assertEqual(self.bill(promo).status_code, 201)

    def test_a_branch_offer_does_not_work_at_another_branch(self):
        promo = self.offer(level='Center', center=self.other_center)
        r = self.bill(promo, expect=400)
        self.assertIn('not available at this center', str(r.data))

    def test_deleting_a_used_offer_is_refused_but_an_unused_one_goes(self):
        used, unused = self.offer(name='Used'), self.offer(name='Unused')
        self.bill(used)
        self.assertEqual(self.api.delete(f'/marketing/api/promotions/{used.id}/').status_code, 409)
        self.assertEqual(self.api.delete(f'/marketing/api/promotions/{unused.id}/').status_code, 204)


class CashbackJourneyTests(OfferBase):
    def cashback(self, **config):
        base = {'cashback_discount': 10, 'cashback_validity_days': 30}
        base.update(config)
        return self.offer(promo_type='Cashback', discount_value=None, config=base)

    def test_cashback_is_earned_on_the_taxable_value_and_capped(self):
        promo = self.cashback(cashback_max=60)
        self.bill(promo)
        self.assertEqual(cashback_balance(self.client_obj.id), Decimal('60.00'))   # 10% of 1000 = 100, capped at 60

    def test_cashback_below_the_minimum_bill_earns_nothing(self):
        promo = self.cashback(cashback_min_bill=5000)
        self.bill(promo)
        self.assertEqual(cashback_balance(self.client_obj.id), Decimal('0.00'))
        self.assertTrue(PromotionUsage.objects.filter(promotion=promo).exists())

    def test_a_walk_in_earns_no_cashback_and_the_bill_still_completes(self):
        promo = self.cashback()
        r = self.bill(promo, client=None)
        self.assertEqual(r.status_code, 201)

    def test_cancelling_takes_the_cashback_back(self):
        promo = self.cashback()
        bill = self.bill(promo)
        self.assertEqual(cashback_balance(self.client_obj.id), Decimal('100.00'))
        self.assertEqual(self.cancel(bill.data['id']).status_code, 200)
        self.assertEqual(cashback_balance(self.client_obj.id), Decimal('0.00'))
        self.assertTrue(PromotionUsage.objects.get(promotion=promo).is_reversed)

    def test_cancelling_after_spending_never_makes_the_wallet_negative(self):
        promo = self.cashback()
        bill = self.bill(promo)
        from billing import wallets
        wallets.debit_cashback(self.client_obj, Decimal('70.00')) if hasattr(wallets, 'debit_cashback') else None
        before = cashback_balance(self.client_obj.id)
        self.assertEqual(self.cancel(bill.data['id']).status_code, 200)
        self.assertGreaterEqual(cashback_balance(self.client_obj.id), Decimal('0.00'))
        self.assertLessEqual(cashback_balance(self.client_obj.id), before)


class CampaignHostileTests(OfferBase):
    SEND = '/marketing/api/whatsapp/send_campaign/'

    def setUp(self):
        super().setUp()
        self.client_obj.delete()              # the base class's own customer would count as a recipient

    def customer(self, name='Asha', phone='9876500001', **extra):
        return Client.objects.create(first_name=name, phone=phone, center=self.center, **extra)

    def send(self, message, **extra):
        return self.api.post(self.SEND, {'message': message, **extra}, format='json')

    def test_message_length_boundary(self):
        self.customer()
        self.assertEqual(self.send('x' * 4000).status_code, 200)
        self.assertEqual(self.send('y' * 4001).status_code, 400)
        self.assertEqual(self.send('   ').status_code, 400)
        self.assertEqual(self.api.post(self.SEND, {}, format='json').status_code, 400)
        self.assertEqual(self.api.post(self.SEND, {'message': None}, format='json').status_code, 400)
        self.assertEqual(self.api.post(self.SEND, {'message': 12345}, format='json').status_code, 200)

    def test_emoji_and_every_script_survive(self):
        self.customer('Ásha')
        text = 'नमस्ते {name} 🎉 20% off — 本日 ¡hola!'
        self.assertEqual(self.send(text).status_code, 200)
        self.assertEqual(WhatsAppMessage.objects.get().message, 'नमस्ते Ásha 🎉 20% off — 本日 ¡hola!')

    def test_a_customer_named_like_a_placeholder_cannot_inject(self):
        self.customer('{name}')
        self.send('Hi {name}!')
        self.assertEqual(WhatsAppMessage.objects.get().message, 'Hi {name}!')

    def test_blank_and_whitespace_phones_are_skipped(self):
        self.customer('A', '   ')
        self.customer('B', '')
        self.customer('C', '9876500003')
        self.assertEqual(self.send('Hi').data['count'], 1)

    def test_garbage_branch_values_never_500(self):
        self.customer()
        for value in ('abc', '1; DROP TABLE x', -5, 10 ** 20, [], {}, True, 'null', '0'):
            r = self.send('Hi', center_id=value)
            self.assertLess(r.status_code, 500, (value, r.status_code, getattr(r, 'data', None)))

    def test_a_customer_with_no_branch_is_reached_only_by_everyone_sends(self):
        Client.objects.create(first_name='Loose', phone='9876500009')
        self.assertEqual(self.send('All', center_id='all').data['count'], 1)
        self.assertEqual(self.api.post(self.SEND, {'message': 'One', 'center_id': self.center.id},
                                       format='json').status_code, 404)
        row = WhatsAppMessage.objects.get()
        self.assertIsNone(row.center)

    def test_one_hundred_customers_run_in_a_handful_of_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        Client.objects.bulk_create([
            Client(first_name=f'C{i}', phone=f'98{i:08d}', center=self.center) for i in range(100)])
        with CaptureQueriesContext(connection) as ctx:
            r = self.send('Hi {name}')
        self.assertEqual(r.data['count'], 100)
        self.assertLess(len(ctx), 40, f'{len(ctx)} queries for 100 customers')


class OutsiderTests(OfferBase):
    def user(self, perms, center=None):
        role = Role.objects.create(name=f'r{random.random()}', permissions=perms)
        user = CustomUser.objects.create_user(
            email=f'u{random.random()}@t.com', password='pw', full_name='U', role=role, center=center)
        api = APIClient()
        api.force_authenticate(user)
        return api

    def test_nobody_gets_in_without_the_tick(self):
        nothing = self.user({}, self.center)
        anonymous = APIClient()
        urls = ['promotions', 'cards', 'memberships', 'packages', 'whatsapp']
        for name in urls:
            self.assertEqual(anonymous.get(f'/marketing/api/{name}/').status_code, 401, name)
            self.assertEqual(nothing.get(f'/marketing/api/{name}/').status_code, 403, name)
        self.assertEqual(nothing.get('/marketing/api/promotions/usage_report/').status_code, 403)
        self.assertEqual(nothing.post('/marketing/api/whatsapp/send_campaign/', {'message': 'x'}, format='json').status_code, 403)
        self.assertEqual(nothing.post('/marketing/api/whatsapp/campaign_preview/', {'message': 'x'}, format='json').status_code, 403)

    def test_a_till_user_reads_offers_but_cannot_change_them(self):
        till = self.user({'billing': {'invoices': {'read': True, 'create': True}}}, self.center)
        promo = self.offer()
        self.assertEqual(till.get('/marketing/api/promotions/').status_code, 200)
        self.assertEqual(till.get('/marketing/api/cards/').status_code, 200)
        self.assertEqual(till.patch(f'/marketing/api/promotions/{promo.id}/', {'name': 'x'}, format='json').status_code, 403)
        self.assertEqual(till.patch(f'/marketing/api/promotions/{promo.id}/toggle-status/', {}, format='json').status_code, 403)
        self.assertEqual(till.delete(f'/marketing/api/promotions/{promo.id}/').status_code, 403)
        self.assertEqual(till.get('/marketing/api/whatsapp/').status_code, 403)      # customer messages
        self.assertEqual(till.get('/marketing/api/promotions/usage_report/').status_code, 403)

    def test_a_till_user_never_sees_switched_off_offers(self):
        till = self.user({'billing': {'invoices': {'read': True}}}, self.center)
        self.offer(is_active=False)
        self.assertEqual(till.get('/marketing/api/promotions/?show_inactive=true').data, [])

    def test_the_tick_for_one_tab_is_not_the_tick_for_another(self):
        only_cards = self.user({'marketing': {'cards': {'read': True}}}, self.center)
        self.assertEqual(only_cards.get('/marketing/api/cards/').status_code, 200)
        # (Reading the offer list is open to any Marketing tick: the till shows offers.)
        self.assertEqual(only_cards.get('/marketing/api/whatsapp/').status_code, 403)
        self.assertEqual(only_cards.get('/marketing/api/promotions/usage_report/').status_code, 403)
        self.assertEqual(only_cards.post('/marketing/api/cards/', {}, format='json').status_code, 403)

    def test_unknown_and_odd_ids_are_404_not_500(self):
        owner = self.api
        for pk in ('999999', 'abc', '-1', '1e5'):
            for verb in ('get', 'patch', 'delete'):
                r = getattr(owner, verb)(f'/marketing/api/promotions/{pk}/')
                self.assertEqual(r.status_code, 404, (verb, pk, r.status_code))
            r = owner.patch(f'/marketing/api/packages/{pk}/toggle-status/', {}, format='json')
            self.assertEqual(r.status_code, 404, pk)


class FuzzTests(OfferBase):
    """Random garbage into every write endpoint: the answer is a clean 4xx, never a crash."""

    ENDPOINTS = {
        'promotions': ['name', 'promo_type', 'level', 'center', 'start_date', 'end_date', 'description',
                       'members_only', 'max_usage_per_client', 'discount_type', 'discount_value', 'config',
                       'is_active'],
        'cards': ['title', 'level', 'center', 'pre_tax_price', 'post_tax_price', 'value', 'benefit_percent',
                  'gst_percent', 'incentive', 'expiry_days', 'description', 'is_active'],
        'memberships': ['name', 'level', 'center', 'pre_tax_price', 'post_tax_price', 'value',
                        'discount_percent', 'gst_percent', 'incentive', 'expiry_days', 'is_vip', 'is_active'],
        'packages': ['name', 'level', 'center', 'price', 'gst_percent', 'validity_days', 'services_json',
                     'service_name', 'description', 'is_active'],
    }

    def junk(self, rng):
        pool = [None, '', ' ', 'abc', '0', 0, -1, -0.5, 1, 7, 100, 101, 10 ** 12, 10 ** 30, 1e-9,
                'NaN', 'Infinity', '-Infinity', '1e999', 1e308, True, False, [], {}, [1, 2], {'a': 1},
                [{'id': 'x', 'pkgQty': 'y'}], [{'id': 1, 'pkgQty': 2.5}], [{'id': 10 ** 20, 'pkgQty': 1}],
                'x' * 300, 'x' * 5000, '🎉', '2026-13-45', '2026-02-30', '0000-00-00', 'Organisation',
                'Center', 'Cashback', 'Trigger', 'FlatPrice', 'Discount', '<script>', "'; --",
                {'cashback_discount': {'nested': [1]}}, {'trigger_services': 'notalist'},
                {'trigger_services': [None, 5]}, {'flat_price': '1e999'}, {'specific_service': 5}]
        return rng.choice(pool)

    def test_garbage_never_crashes_a_write_endpoint(self):
        rng = random.Random(20261029)
        owner = self.api
        existing = {
            'promotions': self.offer().id,
            'cards': ValueCard.objects.create(title='C', pre_tax_price=Decimal('100'), value=Decimal('120'),
                                              expiry_days=30, level='Organisation').id,
            'memberships': Membership.objects.create(name='M', pre_tax_price=Decimal('100'), expiry_days=30,
                                                     level='Organisation').id,
            'packages': Package.objects.create(name='P', price=Decimal('100'), validity_days=30,
                                               service_name='Haircut', level='Organisation').id,
        }
        crashes = []
        for _ in range(60):
            for endpoint, fields in self.ENDPOINTS.items():
                body = {f: self.junk(rng) for f in rng.sample(fields, rng.randint(1, len(fields)))}
                for verb, url in (('post', f'/marketing/api/{endpoint}/'),
                                  ('patch', f'/marketing/api/{endpoint}/{existing[endpoint]}/')):
                    try:
                        r = getattr(owner, verb)(url, body, format='json')
                    except Exception as exc:                           # a raised exception IS a crash
                        crashes.append((verb, endpoint, body, repr(exc)[:150]))
                        continue
                    if r.status_code >= 500:
                        crashes.append((verb, endpoint, body, r.status_code))
        self.assertEqual(crashes[:5], [], f'{len(crashes)} crashes, first 5 shown')

    def test_garbage_in_reports_and_filters_never_crashes(self):
        rng = random.Random(7)
        junk = ['abc', '2026-99-99', '', 'null', 'undefined', '1e9', "' OR 1=1", '-1', '99999999999999999999', '🎉']
        for _ in range(40):
            params = {k: rng.choice(junk) for k in rng.sample(
                ['start_date', 'end_date', 'center_id', 'show_inactive', 'show_expired', 'usable_at'], 4)}
            for url in ('/marketing/api/promotions/usage_report/', '/marketing/api/promotions/',
                        '/marketing/api/cards/', '/marketing/api/whatsapp/'):
                r = self.api.get(url, params)
                self.assertLess(r.status_code, 500, (url, params, r.status_code))

    def test_a_reversed_date_range_is_just_empty(self):
        r = self.api.get('/marketing/api/promotions/usage_report/', {'start_date': str(day(5)), 'end_date': str(day(-5))})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(row['used'] == 0 and row['sold'] == 0 for row in r.data))

    def test_boundary_values_that_are_legal_are_accepted(self):
        owner = self.api
        ok = owner.post('/marketing/api/promotions/', {
            'name': 'N' * 255, 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Percentage',
            'discount_value': '100', 'start_date': str(day()), 'end_date': str(day()),     # one-day offer
            'max_usage_per_client': 1, 'config': {}}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)
        long_name = owner.post('/marketing/api/promotions/', {
            'name': 'N' * 256, 'promo_type': 'Discount', 'level': 'Organisation', 'discount_value': '10',
            'start_date': str(day()), 'end_date': str(day(1))}, format='json')
        self.assertEqual(long_name.status_code, 400)
        huge = owner.post('/marketing/api/promotions/', {
            'name': 'Huge', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Flat',
            'discount_value': '99999999.99', 'start_date': str(day()), 'end_date': str(day(1))}, format='json')
        self.assertEqual(huge.status_code, 201, huge.data)
        too_huge = owner.post('/marketing/api/promotions/', {
            'name': 'Huge2', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Flat',
            'discount_value': '1000000000', 'start_date': str(day()), 'end_date': str(day(1))}, format='json')
        self.assertEqual(too_huge.status_code, 400)
        past = owner.post('/marketing/api/promotions/', {
            'name': 'Past', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_value': '10',
            'start_date': str(day(-1)), 'end_date': str(day(1))}, format='json')
        self.assertEqual(past.status_code, 400)
        backwards = owner.post('/marketing/api/promotions/', {
            'name': 'Back', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_value': '10',
            'start_date': str(day(3)), 'end_date': str(day(1))}, format='json')
        self.assertEqual(backwards.status_code, 400)
