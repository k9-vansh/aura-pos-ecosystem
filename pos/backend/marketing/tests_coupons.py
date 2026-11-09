"""Promotional coupons: a brand's codes, paid with at the till as a Gift Card.

Covers making/loading the codes, every reason a code is refused, a real bill paid
with one (and cancelled), the till's Check, what the owner sees (list, search,
export, usage report), hostile input, and what SAP is told.
"""

import datetime
import re
from decimal import Decimal

from django.core.cache import cache
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing import payment_methods as pm
from billing.models import Invoice, Payment
from billing.tests import BillingTestBase
from marketing import coupons
from marketing.models import CouponCode, Promotion
from salon_admin.models import Role


def day(offset=0):
    return datetime.date.today() + datetime.timedelta(days=offset)


class CouponBase(BillingTestBase):
    """The base bill is Haircut Rs 1000 + 5% GST = Rs 1050."""
    BILL = Decimal('1050.00')

    def setUp(self):
        super().setUp()
        cache.clear()

    def coupon_promo(self, codes=('BLK-AAAA-1111',), name='Blinkit 500', **config):
        cfg = {'brand': 'Blinkit', 'coupon_value_type': 'Flat', 'coupon_value': 500}
        fields = dict(name=name, promo_type='Coupon', level='Organisation',
                      start_date=day(-1), end_date=day(30))
        for key in ('level', 'center', 'start_date', 'end_date', 'is_active'):
            if key in config:
                fields[key] = config.pop(key)
        cfg.update(config)
        promo = Promotion.objects.create(config=cfg, **fields)
        if codes:
            coupons.import_codes(promo, '\n'.join(codes))
        return promo

    def check(self, code, amount='1050', center=None, api=None):
        r = (api or self.api).post('/billing/invoices/check_coupon/', {
            'code': code, 'amount': amount, 'center': (center or self.center).id}, format='json')
        self.assertEqual(r.status_code, 200, getattr(r, 'data', None))
        return r.data

    def bill(self, code, gift='500', expect=201, extra_payments=None, **overrides):
        gift = Decimal(str(gift))
        payments = [{'payment_method': 'Gift Card', 'amount': str(gift), 'reference': code}]
        rest = self.BILL - gift
        if extra_payments is not None:
            payments += extra_payments
        elif rest > 0:
            payments.append({'payment_method': 'Cash', 'amount': str(rest)})
        payload = self.build_payload(status='paid', payments=payments, **overrides)
        r = self.api.post('/billing/invoices/', payload, format='json')
        self.assertEqual(r.status_code, expect, getattr(r, 'data', None))
        return r

    def cancel(self, invoice_id):
        return self.api.post(f'/billing/invoices/{invoice_id}/cancel/', {'reason': 'customer changed mind'},
                             format='json')

    def code(self, text):
        return CouponCode.objects.get(code_key=coupons.normalise(text))


# ── Making and loading codes ──────────────────────────────────────────────

class MakingCodesTests(CouponBase):
    def create(self, **extra):
        body = {'name': 'Zomato 200', 'promo_type': 'Coupon', 'level': 'Organisation',
                'start_date': str(day()), 'end_date': str(day(30)),
                'config': {'brand': 'Zomato', 'coupon_value_type': 'Flat', 'coupon_value': 200}}
        body.update(extra)
        return self.api.post('/marketing/api/promotions/', body, format='json')

    def test_a_brand_list_is_loaded_with_the_promotion(self):
        r = self.create(code_source='list', codes_text='ZOM-0001\nZOM-0002\nzom 0003')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data['codes_summary'], {'total': 3, 'used': 0, 'void': 0, 'unused': 3})
        self.assertEqual(r.data['codes_load']['added'], 3)
        self.assertEqual(CouponCode.objects.filter(promotion_id=r.data['id']).count(), 3)

    def test_ten_thousand_random_codes_are_made_unique_and_readable(self):
        r = self.create(code_source='generate', quantity=10000, code_prefix='zom')
        self.assertEqual(r.status_code, 201, r.data)
        codes = list(CouponCode.objects.filter(promotion_id=r.data['id']).values_list('code', 'code_key', 'source'))
        self.assertEqual(len(codes), 10000)
        self.assertEqual(len({k for _, k, _ in codes}), 10000)
        for code, key, source in codes[:200]:
            self.assertRegex(code, r'^ZOM-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$')
            self.assertEqual(key, code.replace('-', ''))
            self.assertEqual(source, 'generated')

    def test_codes_have_no_look_alike_characters(self):
        promo = self.coupon_promo(codes=())
        coupons.generate_codes(promo, 2000)
        text = ''.join(CouponCode.objects.filter(promotion=promo).values_list('code_key', flat=True))
        for bad in 'IO01':
            self.assertNotIn(bad, text)

    def test_a_promotion_with_no_way_to_get_codes_is_refused_and_not_left_behind(self):
        r = self.create()
        self.assertEqual(r.status_code, 400)
        self.assertIn('code_source', r.data)
        self.assertFalse(Promotion.objects.filter(name='Zomato 200').exists())

    def test_a_failed_code_load_rolls_the_whole_promotion_back(self):
        r = self.create(code_source='list', codes_text='ab\n\n  \nx')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertFalse(Promotion.objects.filter(name='Zomato 200').exists())
        r = self.create(code_source='generate', quantity=0)
        self.assertEqual(r.status_code, 400)
        r = self.create(code_source='generate', quantity=50001)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(Promotion.objects.filter(name='Zomato 200').exists())

    def test_the_terms_are_checked(self):
        bad = [
            ({'coupon_value_type': 'Flat', 'coupon_value': 200}, 'brand'),
            ({'brand': 'Z', 'coupon_value_type': 'Flat'}, 'coupon_value'),
            ({'brand': 'Z', 'coupon_value_type': 'Flat', 'coupon_value': 0}, 'coupon_value'),
            ({'brand': 'Z', 'coupon_value_type': 'Flat', 'coupon_value': -5}, 'coupon_value'),
            ({'brand': 'Z', 'coupon_value_type': 'Percentage', 'coupon_value': 150}, 'coupon_value'),
            ({'brand': 'Z', 'coupon_value_type': 'Magic', 'coupon_value': 5}, 'coupon_value_type'),
            ({'brand': 'Z', 'coupon_value_type': 'Flat', 'coupon_value': 'abc'}, 'coupon_value'),
            ({'brand': 'Z', 'coupon_value_type': 'Flat', 'coupon_value': 5, 'coupon_min_bill': -1}, 'coupon_min_bill'),
            ({'brand': 'B' * 61, 'coupon_value_type': 'Flat', 'coupon_value': 5}, 'brand'),
        ]
        for config, field in bad:
            with self.subTest(config):
                r = self.create(config=config, code_source='generate', quantity=1)
                self.assertEqual(r.status_code, 400, r.data)
                self.assertIn(field, r.data['config'])
        self.assertFalse(Promotion.objects.filter(name='Zomato 200').exists())

    def test_more_codes_can_be_added_later_by_list_or_by_making_them(self):
        promo = self.coupon_promo(codes=('KEEP-0001',))
        r = self.api.post(f'/marketing/api/promotions/{promo.id}/add_codes/', {'codes_text': 'KEEP-0002\nKEEP-0003'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['codes_summary']['total'], 3)
        r = self.api.post(f'/marketing/api/promotions/{promo.id}/generate_codes/', {'quantity': 7, 'code_prefix': 'KEEP'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['codes_summary']['total'], 10)

    def test_the_load_report_names_every_problem(self):
        other = self.coupon_promo(codes=('OLD-CODE-1',), name='Other')
        promo = self.coupon_promo(codes=())
        text = 'code,amount\nNEW-0001\nNEW-0002,150\nnew 0001\nOLD-CODE-1\nab\nNEW-0003,abc\nNEW-0004,-5\n' + 'X' * 41
        report = coupons.import_codes(promo, text)
        self.assertEqual(report['added'], 2)
        self.assertEqual(report['repeated_in_list'], 1)
        self.assertEqual(report['already_held'], 1)
        self.assertEqual(report['already_held_examples'][0], {'code': 'OLD-CODE-1', 'promotion': 'Other'})
        self.assertEqual(report['invalid'], 4)
        self.assertEqual(self.code('NEW-0002').value, Decimal('150.00'))
        self.assertIsNone(self.code('NEW-0001').value)
        self.assertEqual(CouponCode.objects.get(code_key='OLDCODE1').promotion_id, other.id)

    def test_a_code_can_only_belong_to_one_promotion(self):
        first = self.coupon_promo(codes=('SAME-1234',), name='A')
        second = self.coupon_promo(codes=(), name='B')
        report = coupons.import_codes(second, 'same 1234')
        self.assertEqual(report['added'], 0)
        self.assertEqual(report['already_held'], 1)
        self.assertEqual(CouponCode.objects.filter(code_key='SAME1234').count(), 1)
        self.assertEqual(self.code('SAME1234').promotion_id, first.id)

    def test_delimiters_quotes_blank_lines_and_a_bom_are_tolerated(self):
        promo = self.coupon_promo(codes=())
        text = '﻿Code;Amount\r\n"AAA-1111";100\r\n\r\n\tBBB-2222\t200\r\n  CCC-3333  \r\n'
        report = coupons.import_codes(promo, text)
        self.assertEqual(report['added'], 3, report)
        self.assertEqual(self.code('AAA1111').value, Decimal('100.00'))
        self.assertEqual(self.code('BBB2222').value, Decimal('200.00'))

    def test_a_list_too_long_is_refused(self):
        promo = self.coupon_promo(codes=())
        with self.assertRaises(coupons.CouponError):
            coupons.import_codes(promo, '\n'.join(f'CODE{i:07d}' for i in range(coupons.MAX_PER_REQUEST + 1)))

    def test_a_prefix_that_is_too_long_is_refused(self):
        promo = self.coupon_promo(codes=())
        with self.assertRaises(coupons.CouponError):
            coupons.generate_codes(promo, 5, 'ABCDEFGHIJ')

    def test_a_promotion_holding_codes_cannot_become_another_kind(self):
        promo = self.coupon_promo()
        r = self.api.patch(f'/marketing/api/promotions/{promo.id}/', {'promo_type': 'Discount', 'discount_value': 10}, format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('promo_type', r.data)


# ── Why a code is refused ─────────────────────────────────────────────────

class CheckingCodesTests(CouponBase):
    def test_a_good_code_says_what_it_pays(self):
        self.coupon_promo()
        r = self.check('BLK-AAAA-1111')
        self.assertTrue(r['valid'], r)
        self.assertEqual((r['worth'], r['applies'], r['brand']), ('500.00', '500.00', 'Blinkit'))
        self.assertEqual(r['code'], 'BLK-AAAA-1111')

    def test_the_way_it_is_typed_does_not_matter(self):
        self.coupon_promo()
        for typed in ('blk-aaaa-1111', ' BLK AAAA 1111 ', 'blkaaaa1111', 'Blk_Aaaa_1111'):
            with self.subTest(typed):
                self.assertTrue(self.check(typed)['valid'])

    def test_every_refusal_has_a_plain_reason(self):
        promo = self.coupon_promo(codes=('ZZZ-0001', 'ZZZ-0002'))
        cases = {
            'NOPE-0000': 'not valid',
            '': 'Enter the coupon code',
            'ab': 'not valid',
        }
        for code, words in cases.items():
            with self.subTest(code):
                r = self.check(code)
                self.assertFalse(r['valid'])
                self.assertIn(words, r['message'])
        CouponCode.objects.filter(code_key='ZZZ0002').update(is_void=True)
        self.assertIn('withdrawn', self.check('ZZZ-0002')['message'])
        promo.is_active = False
        promo.save()
        self.assertIn('switched off', self.check('ZZZ-0001')['message'])
        promo.is_active = True
        promo.end_date = day(-1)
        promo.save()
        self.assertIn('expired', self.check('ZZZ-0001')['message'])
        promo.end_date, promo.start_date = day(30), day(2)
        promo.save()
        self.assertIn('can be used from', self.check('ZZZ-0001')['message'])

    def test_a_coupon_below_its_minimum_bill_is_refused(self):
        self.coupon_promo(coupon_min_bill=2000)
        r = self.check('BLK-AAAA-1111', amount='1050')
        self.assertFalse(r['valid'])
        self.assertIn('at least Rs 2000.00', r['message'])
        self.assertTrue(self.check('BLK-AAAA-1111', amount='2000')['valid'])

    def test_a_coupon_bigger_than_the_bill_pays_only_the_bill(self):
        self.coupon_promo(coupon_value=5000)
        r = self.check('BLK-AAAA-1111', amount='1050')
        self.assertEqual((r['worth'], r['applies']), ('5000.00', '1050.00'))

    def test_a_percentage_coupon_is_worked_on_the_bill_and_capped(self):
        self.coupon_promo(coupon_value_type='Percentage', coupon_value=20, coupon_max_discount=150)
        r = self.check('BLK-AAAA-1111', amount='1000')
        self.assertEqual(r['applies'], '150.00')
        r = self.check('BLK-AAAA-1111', amount='500')
        self.assertEqual(r['applies'], '100.00')

    def test_a_code_with_its_own_value_uses_it(self):
        promo = self.coupon_promo(codes=())
        coupons.import_codes(promo, 'OWN-1111,120\nOWN-2222')
        self.assertEqual(self.check('OWN-1111')['applies'], '120.00')
        self.assertEqual(self.check('OWN-2222')['applies'], '500.00')

    def test_a_branch_coupon_works_only_at_its_branch(self):
        self.coupon_promo(level='Center', center=self.other_center)
        r = self.check('BLK-AAAA-1111')
        self.assertFalse(r['valid'])
        self.assertIn('not valid at this branch', r['message'])
        self.assertTrue(self.check('BLK-AAAA-1111', center=self.other_center)['valid'])

    def test_no_amount_yet_is_explained(self):
        self.coupon_promo()
        r = self.check('BLK-AAAA-1111', amount='0')
        self.assertFalse(r['valid'])

    def test_garbage_in_the_check_never_crashes(self):
        self.coupon_promo()
        for body in ({}, {'code': None}, {'code': ['a']}, {'code': 'x' * 5000, 'amount': '1050'},
                     {'code': 'BLK-AAAA-1111', 'amount': 'abc'}, {'code': 'BLK-AAAA-1111', 'amount': '-5'},
                     {'code': "'; DROP TABLE x;--", 'amount': '10'}, {'code': '<script>', 'amount': '10'},
                     {'code': 'BLK-AAAA-1111', 'amount': '1050', 'center': 'nine'},
                     {'code': 'BLK-AAAA-1111', 'amount': '1050', 'center': 10 ** 20}):
            with self.subTest(body):
                r = self.api.post('/billing/invoices/check_coupon/', body, format='json')
                self.assertIn(r.status_code, (200, 400), getattr(r, 'data', None))

    def test_wrong_guesses_lock_the_box_for_a_while(self):
        self.coupon_promo()
        for i in range(coupons.MAX_WRONG_CODES):
            self.assertFalse(self.check(f'GUESS-{i:04d}')['valid'])
        r = self.check('BLK-AAAA-1111')
        self.assertFalse(r['valid'])
        self.assertIn('Too many wrong codes', r['message'])
        cache.clear()
        self.assertTrue(self.check('BLK-AAAA-1111')['valid'])

    def test_a_used_code_is_not_a_guess(self):
        self.coupon_promo()
        self.bill('BLK-AAAA-1111')
        for _ in range(coupons.MAX_WRONG_CODES + 2):
            self.assertIn('already used', self.check('BLK-AAAA-1111')['message'])

    def test_a_good_code_clears_the_count(self):
        self.coupon_promo()
        for i in range(coupons.MAX_WRONG_CODES - 1):
            self.check(f'GUESS-{i:04d}')
        self.assertTrue(self.check('BLK-AAAA-1111')['valid'])
        for i in range(coupons.MAX_WRONG_CODES - 1):
            self.check(f'OTHER-{i:04d}')
        self.assertTrue(self.check('BLK-AAAA-1111')['valid'])


# ── A bill paid with a coupon ─────────────────────────────────────────────

class PayingWithACouponTests(CouponBase):
    def test_a_bill_part_paid_with_a_coupon_completes_and_spends_the_code(self):
        self.coupon_promo()
        r = self.bill('blk aaaa 1111')
        invoice = Invoice.objects.get(pk=r.data['id'])
        self.assertEqual((invoice.status, invoice.total_amount, invoice.paid_amount), ('paid', self.BILL, self.BILL))
        gift = invoice.payments.get(payment_method='Gift Card')
        self.assertEqual(gift.amount, Decimal('500.00'))
        self.assertEqual(gift.reference, 'BLK-AAAA-1111')          # the code as we hold it
        code = self.code('BLK-AAAA-1111')
        self.assertTrue(code.is_used)
        self.assertEqual((code.invoice_id, code.payment_id, code.client_id, code.center_id),
                         (invoice.id, gift.id, self.client_obj.id, self.center.id))
        self.assertEqual(code.amount_applied, Decimal('500.00'))
        self.assertIsNotNone(code.used_at)
        self.assertEqual(code.status, 'Used')

    def test_the_same_code_cannot_pay_twice(self):
        self.coupon_promo()
        first = self.bill('BLK-AAAA-1111')
        again = self.bill('BLK-AAAA-1111', expect=400)
        self.assertIn('already used', str(again.data))
        self.assertEqual(Invoice.objects.count(), 1)
        self.assertEqual(self.code('BLK-AAAA-1111').invoice_id, first.data['id'])

    def test_the_cashier_cannot_ask_for_more_than_the_coupon_pays(self):
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111', gift='600', expect=400)
        self.assertIn('at most Rs 500.00', str(r.data))
        self.assertFalse(self.code('BLK-AAAA-1111').is_used)
        self.assertEqual(Invoice.objects.count(), 0)

    def test_a_coupon_may_pay_less_than_it_is_worth(self):
        self.coupon_promo()
        self.bill('BLK-AAAA-1111', gift='300')
        code = self.code('BLK-AAAA-1111')
        self.assertTrue(code.is_used)                               # spent: the rest is lost
        self.assertEqual(code.amount_applied, Decimal('300.00'))

    def test_a_coupon_worth_more_than_the_bill_pays_the_whole_bill(self):
        self.coupon_promo(coupon_value=5000)
        r = self.bill('BLK-AAAA-1111', gift='1050', extra_payments=[])
        invoice = Invoice.objects.get(pk=r.data['id'])
        self.assertEqual((invoice.status, invoice.paid_amount), ('paid', self.BILL))
        self.assertEqual(self.code('BLK-AAAA-1111').amount_applied, self.BILL)
        self.bill('BLK-AAAA-1111', gift='1050', extra_payments=[], expect=400)

    def test_a_percentage_coupon_pays_its_share_of_this_bill(self):
        self.coupon_promo(coupon_value_type='Percentage', coupon_value=10)
        self.bill('BLK-AAAA-1111', gift='105')
        self.assertEqual(self.code('BLK-AAAA-1111').amount_applied, Decimal('105.00'))

    def test_the_cash_part_is_still_needed(self):
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111', gift='500', extra_payments=[], expect=400)
        self.assertIn('full amount', str(r.data).lower())
        self.assertFalse(self.code('BLK-AAAA-1111').is_used)

    def test_a_walk_in_can_use_a_coupon(self):
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111', client=None)
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(self.code('BLK-AAAA-1111').client_id)

    def test_only_one_coupon_per_bill(self):
        self.coupon_promo(codes=('ONE-0001', 'ONE-0002'))
        payments = [{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'ONE-0001'},
                    {'payment_method': 'Gift Card', 'amount': '500', 'reference': 'ONE-0002'},
                    {'payment_method': 'Cash', 'amount': '50'}]
        r = self.api.post('/billing/invoices/', self.build_payload(status='paid', payments=payments), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('Only one gift card', str(r.data))
        self.assertEqual(CouponCode.objects.filter(is_used=True).count(), 0)

    def test_a_gift_card_without_a_code_is_refused(self):
        self.coupon_promo()
        for ref in ('', '   ', None):
            with self.subTest(ref):
                payments = [{'payment_method': 'Gift Card', 'amount': '500', 'reference': ref},
                            {'payment_method': 'Cash', 'amount': '550'}]
                r = self.api.post('/billing/invoices/', self.build_payload(status='paid', payments=payments), format='json')
                self.assertEqual(r.status_code, 400, r.data)

    def test_a_wrong_code_refuses_the_bill_and_writes_nothing(self):
        self.coupon_promo()
        self.bill('BLK-AAAA-9999', expect=400)
        self.assertEqual(Invoice.objects.count(), 0)
        self.assertEqual(Payment.objects.count(), 0)

    def test_a_branch_coupon_is_refused_at_another_branch(self):
        self.coupon_promo(level='Center', center=self.other_center)
        self.bill('BLK-AAAA-1111', expect=400)
        self.assertFalse(self.code('BLK-AAAA-1111').is_used)

    def test_a_held_bill_takes_no_coupon(self):
        self.coupon_promo()
        payments = [{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'BLK-AAAA-1111'}]
        r = self.api.post('/billing/invoices/', self.build_payload(status='draft', payments=payments), format='json')
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.code('BLK-AAAA-1111').is_used)

    def test_a_held_bill_completed_later_spends_the_code(self):
        self.coupon_promo()
        held = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        payments = [{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'blk-aaaa-1111'},
                    {'payment_method': 'Cash', 'amount': '550'}]
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            self.build_payload(status='paid', payments=payments), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertTrue(self.code('BLK-AAAA-1111').is_used)

    def test_a_coupon_that_stops_being_valid_between_check_and_pay_is_refused(self):
        promo = self.coupon_promo()
        self.assertTrue(self.check('BLK-AAAA-1111')['valid'])
        promo.is_active = False
        promo.save()
        self.bill('BLK-AAAA-1111', expect=400)
        self.assertFalse(self.code('BLK-AAAA-1111').is_used)

    def test_the_pay_endpoint_takes_a_coupon_too(self):
        self.coupon_promo(coupon_value=1050)
        held = self.api.post('/billing/invoices/', self.build_payload(status='draft'), format='json')
        r = self.api.post(f"/billing/invoices/{held.data['id']}/pay/",
                          {'payment_method': 'Gift Card', 'amount': '1050', 'reference': 'blk aaaa 1111'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['status'], 'paid')
        self.assertTrue(self.code('BLK-AAAA-1111').is_used)
        no_code = self.api.post(f"/billing/invoices/{held.data['id']}/pay/",
                                {'payment_method': 'Gift Card', 'amount': '10'}, format='json')
        self.assertEqual(no_code.status_code, 400)

    def test_a_coupon_promotion_cannot_be_chosen_as_a_bill_offer(self):
        promo = self.coupon_promo()
        r = self.api.post('/billing/invoices/', self.build_payload(status='draft', promo_id=promo.id), format='json')
        self.assertEqual(r.status_code, 400, r.data)
        self.assertIn('Gift Card', str(r.data))

    def test_the_tills_offer_list_leaves_coupons_out(self):
        coupon = self.coupon_promo()
        normal = Promotion.objects.create(name='Ten', promo_type='Discount', discount_type='Percentage',
                                          discount_value=Decimal('10'), level='Organisation',
                                          start_date=day(-1), end_date=day(30), config={})
        rows = self.api.get(f'/marketing/api/promotions/?usable_at={self.center.id}').data
        ids = {p['id'] for p in (rows['results'] if isinstance(rows, dict) else rows)}
        self.assertIn(normal.id, ids)
        self.assertNotIn(coupon.id, ids)

    def test_a_coupon_does_not_change_the_bill_only_how_it_is_paid(self):
        self.coupon_promo()
        with_coupon = self.bill('BLK-AAAA-1111').data
        plain = self.create_invoice(self.build_payload()).data
        for field in ('subtotal', 'discount', 'cgst', 'sgst', 'total_amount'):
            self.assertEqual(with_coupon[field], plain[field], field)


class CancellingTests(CouponBase):
    def test_cancelling_the_bill_puts_the_coupon_back(self):
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111')
        self.assertEqual(self.cancel(r.data['id']).status_code, 200)
        code = self.code('BLK-AAAA-1111')
        self.assertEqual((code.is_used, code.invoice_id, code.payment_id, code.amount_applied, code.times_released),
                         (False, None, None, Decimal('0.00'), 1))
        self.assertEqual(code.status, 'Unused')
        self.assertTrue(self.check('BLK-AAAA-1111')['valid'])

    def test_a_released_coupon_can_be_used_again_and_again(self):
        self.coupon_promo()
        for round_ in range(3):
            r = self.bill('BLK-AAAA-1111')
            self.assertTrue(self.code('BLK-AAAA-1111').is_used)
            self.cancel(r.data['id'])
        self.assertEqual(self.code('BLK-AAAA-1111').times_released, 3)
        self.assertEqual(Invoice.objects.filter(status='cancelled').count(), 3)

    def test_cancelling_twice_does_not_release_a_coupon_used_on_another_bill(self):
        self.coupon_promo()
        first = self.bill('BLK-AAAA-1111')
        self.cancel(first.data['id'])
        second = self.bill('BLK-AAAA-1111')
        self.assertEqual(self.cancel(first.data['id']).status_code, 400)
        self.assertTrue(self.code('BLK-AAAA-1111').is_used)
        self.assertEqual(self.code('BLK-AAAA-1111').invoice_id, second.data['id'])

    def test_a_coupon_payment_cannot_be_swapped_after_billing(self):
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111')
        gift = Payment.objects.get(invoice_id=r.data['id'], payment_method='Gift Card')
        cash = Payment.objects.get(invoice_id=r.data['id'], payment_method='Cash')
        swap = self.api.post(f"/billing/invoices/{r.data['id']}/change_payment/",
                             {'payment_id': gift.id, 'payment_method': 'Cash'}, format='json')
        self.assertEqual(swap.status_code, 400, swap.data)
        into = self.api.post(f"/billing/invoices/{r.data['id']}/change_payment/",
                             {'payment_id': cash.id, 'payment_method': 'Gift Card'}, format='json')
        self.assertEqual(into.status_code, 400, into.data)
        self.assertTrue(self.code('BLK-AAAA-1111').is_used)
        # the cash part can still change to UPI
        ok = self.api.post(f"/billing/invoices/{r.data['id']}/change_payment/",
                           {'payment_id': cash.id, 'payment_method': 'UPI', 'reference': 'T1'}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        self.assertTrue(self.code('BLK-AAAA-1111').is_used)


# ── Money classification, reports ─────────────────────────────────────────

class MoneyTests(CouponBase):
    def test_gift_card_is_its_own_kind_of_payment(self):
        self.assertEqual(pm.canonical('Gift Card'), 'Gift Card')
        for alias in ('gift card', 'giftcard', 'coupon', ' GIFT  CARD '):
            self.assertEqual(pm.canonical(alias), 'Gift Card')
        self.assertTrue(pm.is_gift_card('Gift Card'))
        self.assertFalse(pm.is_gift_card('Cashback Wallet'))
        self.assertFalse(pm.is_wallet('Gift Card'))
        self.assertEqual(pm.report_bucket('Gift Card'), 'gift_card')
        self.assertIn('gift_card', pm.REPORT_BUCKETS)

    def test_gift_card_is_real_money_not_a_balance_drawdown(self):
        self.assertFalse(pm.is_liability_drawdown('Gift Card'))
        self.assertNotIn('gift_card', pm.LIABILITY_BUCKETS)

    def test_every_branch_takes_gift_cards(self):
        self.assertIn('Gift Card', pm.allowed_at(self.center))

    def test_staff_commission_is_on_the_whole_bill(self):
        from billing.services import _real_money_ratio
        self.coupon_promo()
        r = self.bill('BLK-AAAA-1111')
        self.assertEqual(_real_money_ratio(Invoice.objects.get(pk=r.data['id'])), Decimal('1'))

    def test_the_register_summary_shows_the_coupon_money_on_its_own_line(self):
        from finance.views import _compute_payment_breakdown
        self.coupon_promo()
        self.bill('BLK-AAAA-1111')
        breakdown = _compute_payment_breakdown(Invoice.objects.all())
        self.assertEqual(breakdown['gift_card']['amount'], Decimal('500.00'))
        self.assertEqual(breakdown['cash']['amount'], Decimal('550.00'))

    def test_the_day_closing_carries_the_coupon_money(self):
        from finance import closing
        self.coupon_promo()
        self.bill('BLK-AAAA-1111')
        figures = closing.compute_day(self.center.id, datetime.date.today())
        self.assertEqual(figures['gift_card'], Decimal('500.00'))
        self.assertEqual(figures['system_cash'], Decimal('550.00'))

    def test_the_usage_report_counts_coupon_uses_and_what_they_paid(self):
        promo = self.coupon_promo(codes=('USE-0001', 'USE-0002', 'USE-0003'))
        self.bill('USE-0001')
        second = self.bill('USE-0002')
        self.cancel(second.data['id'])
        rows = self.api.get('/marketing/api/promotions/usage_report/').data
        row = next(r for r in rows if r['id'] == f'promo_{promo.id}')
        self.assertEqual((row['used'], float(row['revenue'])), (1, 500.0))

    def test_sap_posts_a_coupon_to_the_gift_card_account_once_mapped(self):
        from pos_backend import app_settings
        from pos_backend.models import AppSetting
        from pos_backend.sap_utils import payment_account
        key = app_settings.payment_account_key('Gift Card')
        self.assertEqual(key, 'sap.account.method.gift_card')
        self.assertIn(key, app_settings.BY_KEY)
        self.assertEqual(payment_account('Gift Card'), payment_account('Other'))   # until it is mapped
        AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': 'BRANDREC01'})
        app_settings.invalidate()
        self.assertEqual(payment_account('Gift Card'), 'BRANDREC01')
        self.assertEqual(payment_account('gift card'), 'BRANDREC01')
        self.assertNotEqual(payment_account('Cash'), 'BRANDREC01')


# ── Looking at the codes ──────────────────────────────────────────────────

class ListingCodesTests(CouponBase):
    def setUp(self):
        super().setUp()
        self.promo = self.coupon_promo(codes=[f'LST-{i:04d}' for i in range(1, 121)])
        self.bill('LST-0003')
        self.url = f'/marketing/api/promotions/{self.promo.id}/codes/'

    def test_the_list_is_paged_and_counted(self):
        r = self.api.get(self.url, {'page_size': 50})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['summary'], {'total': 120, 'used': 1, 'void': 0, 'unused': 119})
        self.assertEqual((r.data['count'], len(r.data['results']), r.data['page']), (120, 50, 1))
        last = self.api.get(self.url, {'page_size': 50, 'page': 3}).data
        self.assertEqual(len(last['results']), 20)

    def test_a_used_code_shows_where_and_by_whom(self):
        row = self.api.get(self.url, {'status': 'used'}).data['results'][0]
        self.assertEqual((row['code'], row['status']), ('LST-0003', 'Used'))
        self.assertEqual(row['client_phone'], '9990000001')
        self.assertEqual(row['branch'], 'Test Center')
        self.assertTrue(row['bill'])
        self.assertEqual(row['amount_applied'], '500.00')

    def test_filters_and_search(self):
        unused = self.api.get(self.url, {'status': 'unused'}).data
        self.assertEqual(unused['count'], 119)
        self.assertTrue(all(r['status'] == 'Unused' for r in unused['results']))
        self.assertEqual(self.api.get(self.url, {'q': 'lst 0042'}).data['count'], 1)
        self.assertEqual(self.api.get(self.url, {'q': '9990000001'}).data['count'], 1)
        self.assertEqual(self.api.get(self.url, {'q': 'zzzz'}).data['count'], 0)

    def test_odd_paging_input_never_crashes(self):
        for params in ({'page': 'x'}, {'page_size': 'x'}, {'page': -4}, {'page_size': 0}, {'page_size': 10 ** 9},
                       {'status': 'nonsense'}, {'q': "'; --"}):
            with self.subTest(params):
                self.assertEqual(self.api.get(self.url, params).status_code, 200)

    def test_a_code_can_be_withdrawn_and_brought_back_but_not_once_used(self):
        row = self.api.get(self.url, {'q': 'LST-0010'}).data['results'][0]
        void = f'/marketing/api/promotions/{self.promo.id}/void_code/'
        r = self.api.post(void, {'code_id': row['id'], 'void': True}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['codes_summary']['void'], 1)
        self.assertIn('withdrawn', self.check('LST-0010')['message'])
        self.assertEqual(self.api.get(self.url, {'status': 'void'}).data['count'], 1)
        self.api.post(void, {'code_id': row['id'], 'void': False}, format='json')
        self.assertTrue(self.check('LST-0010')['valid'])
        used = self.code('LST-0003')
        self.assertEqual(self.api.post(void, {'code_id': used.id}, format='json').status_code, 400)
        self.assertEqual(self.api.post(void, {'code_id': 10 ** 9}, format='json').status_code, 400)

    def test_rubbish_in_the_code_actions_is_refused_never_a_crash(self):
        base = f'/marketing/api/promotions/{self.promo.id}'
        for body in ({}, {'code_id': 'abc'}, {'code_id': None}, {'code_id': [1]}, {'code_id': {'a': 1}},
                     {'code_id': 10 ** 30}, {'code_id': -1}, {'code_id': '1; DROP TABLE x'}):
            with self.subTest(('void', body)):
                self.assertEqual(self.api.post(f'{base}/void_code/', body, format='json').status_code, 400)
        for body in ({}, {'quantity': 'abc'}, {'quantity': None}, {'quantity': [5]}, {'quantity': {'a': 1}},
                     {'quantity': 2.5, 'code_prefix': ['x']}, {'quantity': -3}, {'quantity': 0},
                     {'quantity': 5, 'code_prefix': 'WAYTOOLONGPREFIX'}):
            with self.subTest(('make', body)):
                self.assertIn(self.api.post(f'{base}/generate_codes/', body, format='json').status_code, (200, 400))
        for body in ({}, {'codes_text': None}, {'codes_text': ['A-1']}, {'codes_text': {'a': 1}},
                     {'codes_text': 12345}, {'codes_text': '\x00\x00'}, {'codes_text': 'é' * 300}):
            with self.subTest(('add', body)):
                self.assertIn(self.api.post(f'{base}/add_codes/', body, format='json').status_code, (200, 400))

    def test_the_export_is_a_csv_that_is_safe_to_open(self):
        coupons.import_codes(self.promo, '=CMD-1234')
        r = self.api.get(f'/marketing/api/promotions/{self.promo.id}/export_codes/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r['Content-Type'])
        text = r.content.decode('utf-8-sig')
        lines = text.strip().splitlines()
        self.assertTrue(lines[0].startswith('Code,Status'))
        self.assertEqual(len(lines), 1 + 121)
        self.assertTrue(any(l.startswith('LST-0003,Used') for l in lines))
        self.assertFalse(any(l.startswith('=') for l in lines))
        self.assertTrue(any(l.startswith("'=CMD-1234") for l in lines))

    def test_only_a_coupon_promotion_has_codes(self):
        plain = Promotion.objects.create(name='Ten', promo_type='Discount', discount_value=Decimal('10'),
                                         level='Organisation', start_date=day(-1), end_date=day(30), config={})
        self.assertEqual(self.api.get(f'/marketing/api/promotions/{plain.id}/codes/').status_code, 400)

    def test_a_promotion_with_used_coupons_cannot_be_deleted(self):
        r = self.api.delete(f'/marketing/api/promotions/{self.promo.id}/')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(CouponCode.objects.filter(promotion=self.promo).exists())

    def test_an_untouched_coupon_promotion_can_be_deleted_with_its_codes(self):
        fresh = self.coupon_promo(codes=('DEL-0001', 'DEL-0002'), name='Fresh')
        self.assertEqual(self.api.delete(f'/marketing/api/promotions/{fresh.id}/').status_code, 204)
        self.assertFalse(CouponCode.objects.filter(code_key__startswith='DEL').exists())

    def test_the_promotion_list_shows_the_counts(self):
        rows = self.api.get('/marketing/api/promotions/').data
        rows = rows['results'] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r['id'] == self.promo.id)
        self.assertEqual(row['codes_summary'], {'total': 120, 'used': 1, 'void': 0, 'unused': 119})


# ── Who may do what ───────────────────────────────────────────────────────

class PermissionTests(CouponBase):
    def user_with(self, name, permissions, center=None):
        role = Role.objects.create(name=name, permissions=permissions)
        user = CustomUser.objects.create_user(email=f'{name}@t.com', password='pw', full_name=name,
                                              role=role, center=center or self.center)
        api = APIClient()
        api.force_authenticate(user)
        return api

    def test_a_cashier_can_check_and_pay_but_not_see_or_load_codes(self):
        promo = self.coupon_promo()
        till = self.user_with('till', {'billing': {'new_invoice': {'read': True, 'create': True}}})
        self.assertTrue(self.check('BLK-AAAA-1111', api=till)['valid'])
        codes = f'/marketing/api/promotions/{promo.id}/codes/'
        self.assertEqual(till.get(codes).status_code, 403)
        self.assertEqual(till.get(f'/marketing/api/promotions/{promo.id}/export_codes/').status_code, 403)
        self.assertEqual(till.post(f'/marketing/api/promotions/{promo.id}/add_codes/',
                                   {'codes_text': 'HACK-0001'}, format='json').status_code, 403)
        self.assertEqual(till.post(f'/marketing/api/promotions/{promo.id}/void_code/',
                                   {'code_id': 1}, format='json').status_code, 403)
        self.assertFalse(CouponCode.objects.filter(code_key='HACK0001').exists())

    def test_someone_with_no_billing_tick_cannot_check_a_code(self):
        self.coupon_promo()
        nobody = self.user_with('nobody', {'marketing': {'promotions': {'read': True}}})
        r = nobody.post('/billing/invoices/check_coupon/', {'code': 'BLK-AAAA-1111', 'amount': '1050'}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_reading_the_codes_needs_the_promotions_tick_and_loading_needs_edit(self):
        promo = self.coupon_promo()
        viewer = self.user_with('viewer', {'marketing': {'promotions': {'read': True}}})
        self.assertEqual(viewer.get(f'/marketing/api/promotions/{promo.id}/codes/').status_code, 200)
        self.assertEqual(viewer.post(f'/marketing/api/promotions/{promo.id}/add_codes/',
                                     {'codes_text': 'NEW-0001'}, format='json').status_code, 403)
        editor = self.user_with('editor', {'marketing': {'promotions': {'read': True, 'update': True}}})
        self.assertEqual(editor.post(f'/marketing/api/promotions/{promo.id}/add_codes/',
                                     {'codes_text': 'NEW-0001'}, format='json').status_code, 200)

    def test_a_branch_user_does_not_see_who_used_a_code_at_another_branch(self):
        promo = self.coupon_promo(codes=('BR-0001', 'BR-0002'))
        self.bill('BR-0001')
        other_user = self.user_with('other', {'marketing': {'promotions': {'read': True}}},
                                    center=self.other_center)
        data = other_user.get(f'/marketing/api/promotions/{promo.id}/codes/').data
        used = [r for r in data['results'] if r['status'] == 'Used']
        self.assertEqual(used, [])
        self.assertEqual(data['count'], 1)

    def test_a_branch_user_cannot_check_a_code_for_another_branch(self):
        self.coupon_promo()
        till = self.user_with('till2', {'billing': {'new_invoice': {'read': True, 'create': True}}},
                              center=self.other_center)
        r = till.post('/billing/invoices/check_coupon/', {'code': 'BLK-AAAA-1111', 'amount': '1050',
                                                           'center': self.center.id}, format='json')
        self.assertEqual(r.status_code, 403)


class PureFunctionTests(CouponBase):
    def test_normalise(self):
        self.assertEqual(coupons.normalise(' blk-aaaa 1111 '), 'BLKAAAA1111')
        self.assertEqual(coupons.normalise(None), '')
        self.assertEqual(coupons.normalise(12345), '12345')
        self.assertEqual(coupons.normalise('é-ü ✓'), '')

    def test_random_codes_have_the_promised_shape(self):
        for _ in range(500):
            code, key = coupons._random_code('AB')
            self.assertRegex(code, r'^AB-[A-Z2-9]{4}-[A-Z2-9]{4}$')
            self.assertEqual(key, re.sub('-', '', code))
