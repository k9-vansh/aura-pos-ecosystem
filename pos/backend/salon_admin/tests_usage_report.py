"""Dashboard > Usage Report, worked by hand.

One small month: a 10% offer used once (and once more on a bill that was cancelled), a 10%
cashback offer used once, a Rs 500 brand coupon used once, and a plain bill. Every number
below was worked out on paper first.

  bill 1  Haircut Rs 1000, 10% offer      -> discount 100, net sale 900,  total 945
  bill 2  Haircut Rs 1000, 10% cashback   -> cashback 100 credited,       total 1050
  bill 3  Haircut Rs 1000, coupon Rs 500 + cash 550                       total 1050
  bill 4  Haircut Rs 1000, nothing                                        total 1050
  bill 5  like bill 1, then cancelled
"""
import datetime
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from billing.tests import BillingTestBase
from clients.models import Client
from marketing import coupons
from marketing.models import CouponCode, Promotion, WhatsAppCampaign, WhatsAppMessage
from salon_admin.models import Role
from salon_admin.usage_report import SECTIONS

BASE = '/salon_admin/api/usage-report'


def day(offset=0):
    return datetime.date.today() + datetime.timedelta(days=offset)


class ReportBase(BillingTestBase):
    def setUp(self):
        super().setUp()
        self.offer = Promotion.objects.create(
            name='Ten off', promo_type='Discount', discount_type='Percentage', discount_value=Decimal('10'),
            level='Organisation', start_date=day(-5), end_date=day(30), config={})
        self.cashback = Promotion.objects.create(
            name='Ten back', promo_type='Cashback', level='Organisation', start_date=day(-5), end_date=day(30),
            config={'cashback_discount': 10, 'cashback_validity_days': 30})
        self.coupon = Promotion.objects.create(
            name='Blinkit 500', promo_type='Coupon', level='Organisation', start_date=day(-5), end_date=day(30),
            config={'brand': 'Blinkit', 'coupon_value_type': 'Flat', 'coupon_value': 500})
        coupons.import_codes(self.coupon, 'BLK-0001\nBLK-0002\nBLK-0003')

    def month(self):
        self.bills = {}
        self.bills[1] = self.create_invoice(self.build_payload(promo_id=self.offer.id)).data
        self.bills[2] = self.create_invoice(self.build_payload(promo_id=self.cashback.id)).data
        pay = [{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'BLK-0001'},
               {'payment_method': 'Cash', 'amount': '550'}]
        self.bills[3] = self.create_invoice(self.build_payload(status='paid', payments=pay)).data
        self.bills[4] = self.create_invoice(self.build_payload()).data
        self.bills[5] = self.create_invoice(self.build_payload(promo_id=self.offer.id)).data
        r = self.api.post(f"/billing/invoices/{self.bills[5]['id']}/cancel/", {'reason': 'test'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def get(self, section, api=None, **params):
        r = (api or self.api).get(f'{BASE}/{section}/', params)
        self.assertEqual(r.status_code, 200, getattr(r, 'data', None))
        return r.data

    def user_with(self, name, permissions, center=None):
        role = Role.objects.create(name=name, permissions=permissions)
        user = CustomUser.objects.create_user(email=f'{name}@t.com', password='pw', full_name=name,
                                              role=role, center=center or self.center)
        api = APIClient()
        api.force_authenticate(user)
        return api


class NumbersTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.month()

    def test_the_bills_are_what_the_comment_says(self):
        self.assertEqual(Decimal(self.bills[1]['total_amount']), Decimal('945.00'))
        self.assertEqual(Decimal(self.bills[2]['total_amount']), Decimal('1050.00'))
        self.assertEqual(Decimal(self.bills[3]['total_amount']), Decimal('1050.00'))
        self.assertEqual(Invoice.objects.filter(status='paid').count(), 4)

    def test_offers(self):
        d = self.get('offers')
        t = d['totals']
        self.assertEqual((t['bills'], t['discount'], t['cashback'], t['cost']), (2, 100.0, 100.0, 200.0))
        self.assertEqual(t['net_sales'], 1900.0)
        self.assertEqual(t['cancelled'], 1)
        rows = {r['name']: r for r in d['rows']}
        self.assertEqual((rows['Ten off']['bills'], rows['Ten off']['discount'], rows['Ten off']['net_sales']), (1, 100.0, 900.0))
        self.assertEqual((rows['Ten back']['bills'], rows['Ten back']['cashback'], rows['Ten back']['net_sales']), (1, 100.0, 1000.0))
        self.assertEqual(rows['Ten off']['cost_pct'], 10.0)
        self.assertEqual(rows['Ten off']['cancelled'], 1)
        self.assertEqual(rows['Ten off']['verdict'], 'Low use')           # one bill
        self.assertEqual([r['name'] for r in d['rows']], ['Ten back', 'Ten off'])   # by sales, high first
        self.assertEqual(d['by_centre'][0]['bills'], 2)
        self.assertEqual(sum(m['bills'] for m in d['trend']), 2)

    def test_an_offer_nobody_used_is_listed(self):
        Promotion.objects.create(name='Dust', promo_type='Discount', discount_type='Flat', discount_value=Decimal('5'),
                                 level='Organisation', start_date=day(-5), end_date=day(30), config={})
        d = self.get('offers')
        self.assertEqual([u['name'] for u in d['unused']], ['Dust'])
        self.assertEqual(d['totals']['offers_unused'], 1)

    def test_coupons(self):
        d = self.get('coupons')
        t = d['totals']
        self.assertEqual((t['codes'], t['used'], t['unused'], t['used_in_period']), (3, 1, 2, 1))
        self.assertEqual((t['applied'], t['brand_owes'], t['wasted']), (500.0, 500.0, 0.0))
        row = d['rows'][0]
        self.assertEqual((row['brand'], row['used_pct'], row['avg_bill']), ('Blinkit', 33.3, 1050.0))
        self.assertEqual(d['brands'][0]['brand_owes'], 500.0)
        self.assertEqual(d['by_centre'][0]['applied'], 500.0)
        self.assertEqual(d['top_customers'][0]['coupons'], 1)
        self.assertTrue(d['top_customers'][0]['phone'].endswith('0001'))
        self.assertTrue(d['top_customers'][0]['phone'].startswith('X'))     # masked

    def test_a_coupon_bigger_than_the_bill_shows_what_was_lost(self):
        coupons.import_codes(self.coupon, 'BIG-0001,5000')
        pay = [{'payment_method': 'Gift Card', 'amount': '1050', 'reference': 'BIG-0001'}]
        self.create_invoice(self.build_payload(status='paid', payments=pay))
        t = self.get('coupons')['totals']
        self.assertEqual(t['applied'], 1550.0)
        self.assertEqual(t['wasted'], 3950.0)               # Rs 5000 coupon, Rs 1050 taken

    def test_cashback(self):
        d = self.get('cashback')
        t = d['totals']
        self.assertEqual((t['earned'], t['spent'], t['expired'], t['owed_now'], t['holders']), (100.0, 0.0, 0.0, 100.0, 1))
        self.assertEqual(d['campaigns'][0]['campaign'], 'Ten back')
        self.assertEqual(d['campaigns'][0]['cashback'], 100.0)
        self.assertEqual(d['top_holders'][0]['balance'], 100.0)
        self.assertEqual(d['ageing'][2]['label'], '8 to 30 days')
        self.assertEqual(d['ageing'][2]['amount'], 100.0)
        self.assertEqual(t['expiring_30'], 100.0)

    def test_cashback_spent_on_a_later_bill_is_counted_once(self):
        pay = [{'payment_method': 'Cashback Wallet', 'amount': '100'}, {'payment_method': 'Cash', 'amount': '950'}]
        self.create_invoice(self.build_payload(status='paid', payments=pay))
        t = self.get('cashback')['totals']
        self.assertEqual((t['earned'], t['spent'], t['owed_now']), (100.0, 100.0, 0.0))
        self.assertEqual((t['wallet_bills'], t['wallet_paid']), (1, 100.0))
        self.assertEqual(t['used_pct'], 100.0)

    def test_customers(self):
        d = self.get('customers')
        t = d['totals']
        self.assertEqual((t['bills'], t['linked'], t['linked_pct']), (4, 3, 75.0))
        self.assertEqual(t['linked_sales'], 2900.0)                     # 900 + 1000 + 1000
        self.assertEqual((t['avg_linked'], t['avg_other']), (966.67, 1000.0))
        kinds = {k['key']: k['bills'] for k in d['kinds']}
        self.assertEqual((kinds['promo'], kinds['coupon'], kinds['member']), (2, 1, 0))
        self.assertEqual(t['customers'], 1)
        self.assertEqual(t['new_customers'], 1)
        self.assertEqual(t['new_with_perk'], 1)
        self.assertEqual(d['by_centre'][0]['linked'], 3)

    def test_overview_ties_to_the_other_sections(self):
        d = self.get('overview')
        h = d['head']
        self.assertEqual((h['bills'], h['linked_bills'], h['linked_pct']), (4, 3, 75.0))
        self.assertEqual(h['cost'], 200.0)                                # 100 discount + 100 cashback
        self.assertEqual(h['cost_split'], {'offers': 100.0, 'cashback': 100.0, 'membership': 0.0, 'card': 0.0})
        self.assertEqual((h['coupons_applied'], h['brand_owes'], h['cashback_owed']), (500.0, 500.0, 100.0))
        self.assertEqual(h['linked_sales'], 2900.0)
        self.assertEqual(h['sales_per_rupee'], 14.5)
        self.assertEqual([o['name'] for o in d['top_offers']], ['Ten back', 'Ten off'])
        self.assertEqual({m['key']: m['bills'] for m in d['mix']}['promo'], 2)
        self.assertEqual(sum(m['bills'] for m in d['mix']), 3)           # 2 on an offer + 1 on a coupon
        self.assertTrue(any(a['section'] == 'coupons' or a['section'] == 'cashback' for a in d['alerts']))
        self.assertEqual(h['linked_sales'], self.get('customers')['totals']['linked_sales'])
        self.assertEqual(h['cost_split']['offers'], self.get('offers')['totals']['discount'])

    def test_verify_finds_nothing_wrong_in_a_clean_month(self):
        d = self.get('verify')
        bad = [c for c in d['checks'] if c['status'] == 'fail']
        self.assertEqual(bad, [], [(c['key'], c['samples'], c['expected'], c['actual']) for c in bad])
        keys = {c['key'] for c in d['checks']}
        self.assertTrue({'coupon_money', 'coupon_links', 'promo_discount', 'cashback_spend', 'cashback_earned',
                         'cashback_balance', 'card_balance', 'package_sessions', 'membership_dates'} <= keys)
        self.assertEqual(d['summary']['failed'], 0)
        money = {c['key']: c for c in d['checks']}
        self.assertEqual((money['coupon_money']['expected'], money['coupon_money']['actual']), (500.0, 500.0))
        self.assertEqual((money['cashback_earned']['expected'], money['cashback_earned']['actual']), (100.0, 100.0))

    def test_verify_catches_a_coupon_left_on_a_cancelled_bill(self):
        code = CouponCode.objects.get(code_key='BLK0001')
        invoice = Invoice.objects.get(pk=self.bills[3]['id'])
        Invoice.objects.filter(pk=invoice.pk).update(status='cancelled')          # cancelled behind the code's back
        d = self.get('verify')
        links = next(c for c in d['checks'] if c['key'] == 'coupon_links')
        self.assertEqual(links['status'], 'fail')
        self.assertTrue(any('BLK-0001' in s for s in links['samples']), links['samples'])
        self.assertGreaterEqual(d['summary']['failed'], 1)
        self.assertTrue(code.is_used)

    def test_verify_catches_a_bill_changed_after_its_offer(self):
        Invoice.objects.filter(pk=self.bills[1]['id']).update(discount=Decimal('250'))
        d = self.get('verify')
        check = next(c for c in d['checks'] if c['key'] == 'promo_discount')
        self.assertEqual(check['status'], 'fail')
        self.assertTrue(any('250.00' in s and '100.00' in s for s in check['samples']), check['samples'])

    def test_verify_catches_a_cashback_balance_that_does_not_add_up(self):
        from billing.models import CashbackTransaction
        CashbackTransaction.objects.filter(kind='earned').update(remaining=Decimal('40'))
        check = next(c for c in self.get('verify')['checks'] if c['key'] == 'cashback_balance')
        self.assertEqual((check['status'], check['bad']), ('fail', 1))

    def test_verify_catches_a_card_balance_that_does_not_add_up(self):
        from clients.models import ClientValueCard
        from marketing.models import ValueCard
        template = ValueCard.objects.create(title='Silver', pre_tax_price=Decimal('1000'), post_tax_price=Decimal('1180'),
                                            gst_percent=Decimal('18'), value=Decimal('1200'), expiry_days=365)
        card = ClientValueCard.objects.create(
            client=self.client_obj, value_card=template, balance=Decimal('900'), face_value=Decimal('1200'),
            expiry_date=day(300), source_invoice=Invoice.objects.get(pk=self.bills[4]['id']), center=self.center)
        check = next(c for c in self.get('verify')['checks'] if c['key'] == 'card_balance')
        self.assertEqual((check['status'], check['bad']), ('fail', 1))      # 1200 loaded, nothing spent, 900 left
        card.balance = Decimal('1200')
        card.save()
        check = next(c for c in self.get('verify')['checks'] if c['key'] == 'card_balance')
        self.assertEqual(check['status'], 'ok')


class FilterTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.month()

    def test_the_kind_filter_narrows_every_section(self):
        d = self.get('offers', perk='Cashback')
        self.assertEqual([r['name'] for r in d['rows']], ['Ten back'])
        self.assertEqual(self.get('offers', perk='Coupon')['enabled'], False)
        self.assertEqual(self.get('coupons', perk='Discount')['enabled'], False)
        self.assertEqual(self.get('coupons', perk='Coupon')['totals']['used_in_period'], 1)
        self.assertEqual(self.get('cashback', perk='Discount')['enabled'], False)
        self.assertEqual(self.get('packages', perk='Discount')['enabled'], False)

    def test_the_name_filter(self):
        self.assertEqual([r['name'] for r in self.get('offers', q='back')['rows']], ['Ten back'])
        self.assertEqual(self.get('offers', q='zzz')['rows'], [])
        self.assertEqual(len(self.get('coupons', q='blinkit')['rows']), 1)       # the brand counts too
        self.assertEqual(self.get('coupons', q='zomato')['rows'], [])

    def test_the_status_filter(self):
        Promotion.objects.filter(pk=self.offer.pk).update(end_date=day(-1))
        self.assertEqual([r['status'] for r in self.get('offers', status='ended')['rows']], ['ended'])
        self.assertEqual([r['name'] for r in self.get('offers', status='running')['rows']], ['Ten back'])
        self.assertEqual(self.get('offers', status='off')['rows'], [])

    def test_the_customer_filter(self):
        # the one customer made their first bill this month: new, not repeat
        self.assertEqual(self.get('offers', customer='new')['totals']['bills'], 2)
        self.assertEqual(self.get('offers', customer='repeat')['totals']['bills'], 0)
        self.assertEqual(self.get('customers', customer='new')['totals']['bills'], 4)
        self.assertEqual(self.get('customers', customer='repeat')['totals']['bills'], 0)
        self.assertEqual(self.get('coupons', customer='repeat')['totals']['used_in_period'], 0)
        self.assertEqual(self.get('cashback', customer='repeat')['totals']['earned'], 0.0)

    def test_a_date_range_with_nothing_in_it(self):
        old = {'start_date': str(day(-400)), 'end_date': str(day(-380))}
        self.assertEqual(self.get('offers', **old)['totals']['bills'], 0)
        self.assertEqual(self.get('coupons', **old)['totals']['used_in_period'], 0)
        self.assertEqual(self.get('overview', **old)['head']['linked_bills'], 0)
        # but what is held today does not depend on the dates
        self.assertEqual(self.get('cashback', **old)['totals']['owed_now'], 100.0)
        self.assertEqual(self.get('coupons', **old)['totals']['unused'], 2)

    def test_the_previous_period_is_compared(self):
        d = self.get('offers')
        self.assertEqual(d['totals']['bills_prev'], 0)
        self.assertIsNone(d['totals']['bills_change'])               # nothing before: no percentage
        d = self.get('overview')
        self.assertIsNone(d['head']['linked_sales_change'])

    def test_centres(self):
        other = self.get('offers', center_id=self.other_center.id)
        self.assertEqual(other['totals']['bills'], 0)
        mine = self.get('offers', center_id=self.center.id)
        self.assertEqual(mine['totals']['bills'], 2)
        self.assertEqual(self.get('coupons', center_id=self.other_center.id)['totals']['used_in_period'], 0)
        self.assertEqual(self.get('customers', center_id=self.other_center.id)['totals']['bills'], 0)
        self.assertEqual(self.get('cashback', center_id=self.other_center.id)['totals']['earned'], 0.0)

    def test_hostile_input_never_crashes(self):
        for params in ({'start_date': 'nope'}, {'end_date': '2026-13-45'}, {'center_id': 'abc'}, {'center_id': 10 ** 20},
                       {'perk': 'x,y,,Discount'}, {'perk': 'Coupon' * 500}, {'q': "'; DROP TABLE x;--"},
                       {'q': 'é' * 500}, {'customer': 'nobody'}, {'status': 'weird'},
                       {'start_date': '2026-12-31', 'end_date': '2026-01-01'}):
            for section in ('overview', 'offers', 'coupons', 'packages', 'cashback', 'customers', 'whatsapp', 'forecast', 'verify'):
                with self.subTest((section, params)):
                    r = self.api.get(f'{BASE}/{section}/', params)
                    self.assertEqual(r.status_code, 200, getattr(r, 'data', None))

    def test_an_unknown_section_is_a_404(self):
        self.assertEqual(self.api.get(f'{BASE}/nonsense/').status_code, 404)


class OtherSectionsTests(ReportBase):
    def test_packages_sold_used_and_held(self):
        from billing.models import InvoiceItem
        from clients.models import ClientPackage
        from marketing.models import Package
        pack = self.api.post('/marketing/api/packages/', {
            'name': '3 cuts', 'level': 'Organisation', 'price': '2000', 'gst_percent': '18', 'validity_days': 90,
            'services_json': [{'id': self.service.id, 'pkgQty': 3}]}, format='json')
        self.assertEqual(pack.status_code, 201, pack.data)
        line = {'content_type': 'marketing.package', 'object_id': pack.data['id'], 'description': '3 cuts',
                'unit_price': '2000', 'discount': '0', 'quantity': 1, 'tax_percentage': '18', 'staff_members': [self.staff.id]}
        self.create_invoice(self.build_payload(items=[line]))
        cp = ClientPackage.objects.get(client=self.client_obj)
        self.assertEqual(cp.services_remaining, {str(self.service.id): 3})
        redeem = dict(self.service_line(), is_redemption=True, unit_price='0')
        self.create_invoice(self.build_payload(items=[redeem]))
        d = self.get('packages')
        t = d['totals']
        self.assertEqual((t['sold'], t['revenue'], t['sessions_used'], t['sessions_left']), (1, 2000.0, 1, 2))
        row = d['rows'][0]
        self.assertEqual((row['name'], row['sessions_sold'], row['holders'], row['value_left']), ('3 cuts', 3, 1, round(2000 * 2 / 3, 2)))
        self.assertEqual((row['cohort_sessions'], row['cohort_used'], row['utilisation']), (3, 1, 33.3))
        self.assertEqual(d['services'][0]['sessions'], 1)
        self.assertEqual(self.get('verify')['summary']['failed'], 0)
        self.assertEqual(self.get('overview')['sold'][1]['value'], 2000.0)

    def test_whatsapp(self):
        camp = WhatsAppCampaign.objects.create(center=self.center, message='Festive offer', recipients=3, status='Done')
        now = datetime.datetime.now()
        for name, phone, status in (('A', '9990000001', 'Delivered'), ('B', '9990000002', 'Sent'),
                                    ('C', '9990000003', 'Not sent - WhatsApp not set up')):
            WhatsAppMessage.objects.create(campaign=camp, center=self.center, date=now.date(), time=now.time(),
                                           client_name=name, client_phone=phone, message='Festive offer', status=status)
        self.create_invoice(self.build_payload())              # the customer (9990000001) comes in after it
        d = self.get('whatsapp')
        t = d['totals']
        self.assertEqual((t['campaigns'], t['messages'], t['delivered'], t['sent'], t['not_sent']), (1, 3, 1, 1, 1))
        row = d['rows'][0]
        self.assertEqual((row['responded'], row['sales_7d']), (1, 1000.0))
        self.assertEqual(row['response_pct'], 50.0)                     # of the two actually sent

    def test_a_campaign_that_never_went_out_has_no_response_figure(self):
        camp = WhatsAppCampaign.objects.create(center=self.center, message='Never sent', recipients=1)
        now = datetime.datetime.now()
        WhatsAppMessage.objects.create(campaign=camp, center=self.center, date=now.date(), time=now.time(), client_name='A',
                                       client_phone='9990000001', message='x', status='Not sent - WhatsApp not set up')
        row = self.get('whatsapp')['rows'][0]
        self.assertIsNone(row['responded'])
        self.assertIsNone(row['response_pct'])

    def test_forecast_what_falls_due(self):
        from billing.wallets import credit_cashback
        from clients.models import ClientMembership, ClientValueCard
        from marketing.models import Membership, ValueCard
        credit_cashback(self.client_obj, Decimal('200'), center=self.center, expires_on=day(20))
        credit_cashback(self.client_obj, Decimal('50'), center=self.center, expires_on=day(70))
        tpl = ValueCard.objects.create(title='Gold card', pre_tax_price=Decimal('1000'), post_tax_price=Decimal('1180'),
                                       gst_percent=Decimal('18'), value=Decimal('1200'), expiry_days=365)
        ClientValueCard.objects.create(client=self.client_obj, value_card=tpl, balance=Decimal('700'), face_value=Decimal('1200'),
                                       expiry_date=day(25), center=self.center)
        mem = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'), post_tax_price=Decimal('1180'),
                                        gst_percent=Decimal('18'), discount_percent=Decimal('10'), expiry_days=365)
        ClientMembership.objects.create(client=self.client_obj, membership=mem, expiry_date=day(45), center=self.center)
        due = {row['kind']: {h['days']: h for h in row['horizons']} for row in self.get('forecast')['due']}
        self.assertEqual([due['Cashback expiring'][d]['amount'] for d in (30, 60, 90)], [200.0, 200.0, 250.0])
        self.assertEqual([due['Value cards expiring'][d]['amount'] for d in (30, 60, 90)], [700.0, 700.0, 700.0])
        self.assertEqual([due['Memberships ending'][d]['count'] for d in (30, 60, 90)], [0, 1, 1])
        self.assertEqual(due['Memberships ending'][60]['amount'], 1000.0)

    def test_forecast_coupons_and_offers_at_todays_pace(self):
        self.month()
        d = self.get('forecast')
        run = next(r for r in d['coupons'] if r['name'] == 'Blinkit 500')
        self.assertEqual(run['unused'], 2)
        self.assertAlmostEqual(run['per_day'], round(1 / 14, 1), places=1)
        self.assertGreaterEqual(run['expected_owed'], 0)
        offer = next(r for r in d['offers'] if r['name'] == 'Ten off')
        self.assertEqual((offer['bills_so_far'], offer['cost_so_far']), (1, 100.0))
        self.assertAlmostEqual(offer['cost_per_day'], round(100 / 14, 2), places=2)
        self.assertAlmostEqual(offer['cost_at_end'], round(100 + 100 / 14 * offer['days_left'], 2), places=1)

    def test_the_sales_outlook_needs_three_full_months(self):
        s = self.get('forecast')['sales']
        self.assertIsNone(s['projection'])
        self.assertLess(s['months_of_history'], 3)

    def test_the_sales_outlook_draws_a_straight_line(self):
        from salon_admin.usage_report.forecast import project
        values, slope = project([100.0, 200.0, 300.0])
        self.assertEqual([round(v) for v in values], [400, 500, 600])
        self.assertEqual(round(slope), 100)
        values, _ = project([300.0, 200.0, 100.0])
        self.assertEqual([round(v) for v in values], [0, 0, 0])             # never below zero


class CustomerCountTests(ReportBase):
    """Who comes back, worked by hand: three customers last period, two of them again this period.

      last period  Asha  used the 10% offer      this period  Asha   plain bill
                   Bela  plain bill                           Chitra plain bill
                   Chitra plain bill
    """

    def setUp(self):
        super().setUp()
        self.bela = Client.objects.create(first_name='Bela', last_name='K', phone='9990000002', center=self.center)
        self.chitra = Client.objects.create(first_name='Chitra', last_name='K', phone='9990000003', center=self.center)
        self.window = {'start_date': str(day(-9)), 'end_date': str(day(0))}
        last = [self.create_invoice(self.build_payload(promo_id=self.offer.id)).data['id'],
                self.create_invoice(self.build_payload(client=self.bela.id)).data['id'],
                self.create_invoice(self.build_payload(client=self.chitra.id)).data['id']]
        prev_start = datetime.date.fromisoformat(self.get('customers', **self.window)['frame']['prev_start'])
        Invoice.objects.filter(pk__in=last).update(created_at=datetime.datetime.combine(prev_start, datetime.time(10, 0)))
        self.create_invoice(self.build_payload())
        self.create_invoice(self.build_payload(client=self.chitra.id))

    def test_who_came_back_after_using_an_offer_and_after_not(self):
        d = self.get('customers', **self.window)
        used, plain = d['cohorts']
        self.assertEqual((used['customers'], used['came_back'], used['came_back_pct']), (1, 1, 100.0))
        self.assertEqual((plain['customers'], plain['came_back'], plain['came_back_pct']), (2, 1, 50.0))
        t = d['totals']
        self.assertEqual((t['bills'], t['customers'], t['customers_with_perk']), (2, 2, 0))
        self.assertEqual((d['by_centre'][0]['centre'], d['by_centre'][0]['bills']), ('Test Center', 2))

    def test_the_customer_filter_applies_to_both_periods(self):
        # Nobody is new this period (every first bill was last period), so the "new" filter leaves nothing
        # in either period; everyone is a returning customer, so that filter changes nothing here.
        d = self.get('customers', customer='repeat', **self.window)
        self.assertEqual(d['totals']['customers'], 2)
        self.assertEqual([(c['customers'], c['came_back']) for c in d['cohorts']], [(1, 1), (2, 1)])
        d = self.get('customers', customer='new', **self.window)
        self.assertEqual(d['totals']['customers'], 0)
        self.assertEqual([(c['customers'], c['came_back']) for c in d['cohorts']], [(0, 0), (0, 0)])

    def test_a_very_long_list_of_new_customers_gives_the_same_answers(self):
        asked = [(s, {'customer': 'new', **self.window}) for s in ('customers', 'offers')] + [
            ('customers', {'customer': 'new'}), ('offers', {'customer': 'new'})]
        short = [self.get(s, **p) for s, p in asked]
        with patch('salon_admin.usage_report.base.NEW_LIST_LIMIT', 0):       # past the limit the filter is a subquery
            long = [self.get(s, **p) for s, p in asked]
        self.assertEqual(short, long)


class AccessTests(ReportBase):
    def setUp(self):
        super().setUp()
        self.month()
        self.team = self.user_with('marketing_team', {'dashboard': {'usage_report': {'read': True}}})

    def test_the_marketing_tick_opens_the_whole_report(self):
        for section in ('overview', 'offers', 'coupons', 'packages', 'cashback', 'customers', 'whatsapp', 'forecast', 'verify'):
            self.assertEqual(self.team.get(f'{BASE}/{section}/').status_code, 200, section)

    def test_and_the_memberships_and_cards_reports_inside_it(self):
        self.assertEqual(self.team.get('/salon_admin/api/analytics/memberships/').status_code, 200)
        self.assertEqual(self.team.get('/salon_admin/api/analytics/value-cards/').status_code, 200)

    def test_but_nothing_else_on_the_dashboard(self):
        for url in ('/salon_admin/api/analytics/revenues/', '/salon_admin/api/analytics/clients/',
                    '/salon_admin/api/analytics/services/', '/salon_admin/api/analytics/products/',
                    '/salon_admin/api/dashboard/summary/', '/salon_admin/api/dashboard/revenues/',
                    '/salon_admin/api/dashboard/finance/', '/salon_admin/api/dashboard/staff/'):
            self.assertEqual(self.team.get(url).status_code, 403, url)

    def test_the_finance_tick_alone_does_not_open_the_usage_report(self):
        finance = self.user_with('finance_only', {'dashboard': {'finance': {'read': True}}})
        self.assertEqual(finance.get('/salon_admin/api/analytics/memberships/').status_code, 200)
        for section in ('overview', 'offers', 'coupons', 'verify'):
            self.assertEqual(finance.get(f'{BASE}/{section}/').status_code, 403, section)

    def test_a_cashier_and_a_stranger_get_nothing(self):
        till = self.user_with('till', {'billing': {'new_invoice': {'read': True, 'create': True}}})
        self.assertEqual(till.get(f'{BASE}/overview/').status_code, 403)
        self.assertEqual(APIClient().get(f'{BASE}/overview/').status_code in (401, 403), True)

    def test_a_branch_marketing_user_sees_only_their_branch(self):
        elsewhere = self.user_with('branch_b', {'dashboard': {'usage_report': {'read': True}}}, center=self.other_center)
        self.assertEqual(elsewhere.get(f'{BASE}/offers/').data['totals']['bills'], 0)
        self.assertEqual(elsewhere.get(f'{BASE}/customers/').data['totals']['bills'], 0)
        self.assertEqual(elsewhere.get(f'{BASE}/coupons/').data['totals']['used_in_period'], 0)
        self.assertEqual(elsewhere.get(f'{BASE}/cashback/').data['totals']['earned'], 0.0)
        # asking for another branch's figures by id does not get around it
        pried = elsewhere.get(f'{BASE}/offers/', {'center_id': self.center.id})
        self.assertEqual(pried.data['totals']['bills'], 0)

    def test_the_tick_is_in_the_permission_tree_and_both_copies_match(self):
        import json
        from pathlib import Path
        from pos_backend.permission_tree import load_tree
        keys = {s['key'] for m in load_tree()['modules'] if m['key'] == 'dashboard' for s in m['submodules']}
        self.assertIn('usage_report', keys)
        backend = Path(__file__).resolve().parent.parent / 'pos_backend' / 'permission_tree.json'
        frontend = Path(__file__).resolve().parent.parent.parent / 'frontend' / 'src' / 'app' / 'services' / 'permission-tree.json'
        if frontend.exists():
            self.assertEqual(json.loads(backend.read_text(encoding='utf-8')), json.loads(frontend.read_text(encoding='utf-8')))


@override_settings(USAGE_REPORT_CACHE_SECONDS=60)
class KeptAnswerTests(ReportBase):
    """A slow section is kept for a short while so the Overview and its own tab do not both work it out."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.month()
        self.team = self.user_with('marketing_team', {'dashboard': {'usage_report': {'read': True}}})
        slow = patch('salon_admin.usage_report.base.SLOW_SECONDS', 0)       # treat every answer as a slow one
        slow.start()
        self.addCleanup(slow.stop)
        self.addCleanup(cache.clear)

    def bills_now(self, api=None, **params):
        return self.get('customers', api=api, **params)['totals']['bills']

    def test_a_slow_answer_is_kept_until_asked_afresh(self):
        before = self.bills_now()
        self.create_invoice(self.build_payload())
        self.assertEqual(self.bills_now(), before)                          # kept: the new bill is not in it yet
        self.assertEqual(self.bills_now(fresh=1), before + 1)               # Refresh asks for today's figures
        self.assertEqual(self.bills_now(), before + 1)                      # and what it found is kept in turn

    def test_the_overview_leaves_the_customers_answer_for_its_own_tab(self):
        self.get('overview')
        self.create_invoice(self.build_payload())
        kept = self.get('customers')['totals']['bills']                     # the Overview already worked it out
        self.assertEqual(kept, self.get('customers', fresh=1)['totals']['bills'] - 1)

    def test_what_one_filter_or_one_person_saw_is_not_handed_to_another(self):
        before = self.bills_now()
        self.create_invoice(self.build_payload())
        self.assertEqual(self.bills_now(), before)                          # the first user's own answer, kept
        self.assertEqual(self.bills_now(status='running'), before + 1)      # another filter: worked out on its own
        self.assertEqual(self.bills_now(api=self.team), before + 1)         # another user: worked out on their own

    def test_verify_is_never_kept_but_the_others_are(self):
        calls = []

        def build(ctx):
            calls.append(1)
            return {'n': len(calls)}

        with patch.dict(SECTIONS, {'verify': build, 'packages': build}):
            self.assertEqual(self.get('verify')['n'], 1)
            self.assertEqual(self.get('verify')['n'], 2)
            self.assertEqual(self.get('packages')['n'], 3)
            self.assertEqual(self.get('packages')['n'], 3)
            self.assertEqual(self.get('packages', fresh=1)['n'], 4)

    def test_nothing_is_kept_when_it_is_switched_off(self):
        with override_settings(USAGE_REPORT_CACHE_SECONDS=0):
            before = self.bills_now()
            self.create_invoice(self.build_payload())
            self.assertEqual(self.bills_now(), before + 1)
