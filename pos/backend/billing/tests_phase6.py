"""
Phase 6: the billing engine and the valid tax receipt.

  * one engine for the quote and the save (B5.6);
  * offers worked out by the server from their id - services only, pre-tax,
    one per bill (#28, #29, #30, #53, F16, D1), memberships only at the
    selling branch (#36);
  * the value card model F5 in Decimal (F5, F6, F7, D4, D4b, X11);
  * advance untaxed when paid in (#34, F8); cashback expiry, oldest first (#35);
  * full payment, payment references, cash change (#37, #38);
  * the payment-method change window and its SAP re-post (#41);
  * the bill's snapshots, biller and shift; the e-invoice queue (Q4b).
"""

import datetime
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing import wallets
from billing.models import CashbackTransaction, Invoice, Payment
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from finance.models import Shift
from inventory.models import Product
from marketing.models import Membership, Promotion, ValueCard
from pos_backend import app_settings
from pos_backend.models import AppSetting, BackgroundJob, SapPosting
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

TODAY = datetime.date.today()
NEXT_YEAR = TODAY + datetime.timedelta(days=365)


class Phase6Base(TestCase):
    def setUp(self):
        from pos_backend.testing import pin_gate_off
        pin_gate_off()                      # these tests spend wallets without caring about the PIN
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.center = Center.objects.create(
            center_name='Geetanjali Saket Pvt Ltd', display_name='Saket',
            gst_number='07AAAAA0000A1Z5', address='12 Main Road, Saket, New Delhi 110017',
            phone='011-4000000', sap_warehouse_code='WH-SAKET')
        self.other = Center.objects.create(center_name='Noida', gst_number='09CCCCC0000C1Z5',
                                           sap_warehouse_code='WH-NOIDA')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(
            email='biller@test.com', password='pw', full_name='Biller', role=role,
            center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client_obj = Client.objects.create(first_name='Asha', last_name='Rao',
                                                phone='9990000001', center=self.center)
        self.svc_a = ServiceMaster.objects.create(name='Bridal Makeup', default_price=Decimal('30000'),
                                                  tax_percentage=Decimal('5'), sac_code='999722')
        self.svc_b = ServiceMaster.objects.create(name='Hair Spa', default_price=Decimal('15000'),
                                                  tax_percentage=Decimal('18'))
        self.svc_c = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('6000'),
                                                  tax_percentage=Decimal('5'))
        self.product = Product.objects.create(name='Serum', price=Decimal('590'),
                                              gst_percent=Decimal('18'), current_stock=10,
                                              center=self.center, product_code='SER01')
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi',
                                                designation='Stylist')

    def line(self, svc, qty=1, **extra):
        out = {'content_type': 'services.servicemaster', 'object_id': svc.id,
               'description': svc.name, 'unit_price': str(svc.default_price), 'discount': '0.00',
               'quantity': qty, 'staff_members': [self.staff.id]}
        out.update(extra)
        return out

    def product_line(self, qty=1):
        return {'content_type': 'inventory.product', 'object_id': self.product.id,
                'description': 'Serum', 'unit_price': '590.00', 'discount': '0.00',
                'quantity': qty, 'staff_members': [self.staff.id]}

    def payload(self, items, **extra):
        out = {'client': self.client_obj.id, 'center': self.center.id, 'status': 'paid',
               'discount': '0.00', 'items': items}
        out.update(extra)
        return out

    def quote(self, items, **extra):
        return self.api.post('/billing/invoices/quote/', self.payload(items, **extra), format='json')

    def card(self, face='30000', pre='20000', rate='5', balance=None, center=None, **extra):
        tpl = ValueCard.objects.create(title='Black Card', pre_tax_price=Decimal(pre),
                                       post_tax_price=Decimal(pre) * (1 + Decimal(rate) / 100),
                                       value=Decimal(face), gst_percent=Decimal(rate),
                                       expiry_days=365)
        return ClientValueCard.objects.create(
            client=self.client_obj, value_card=tpl,
            balance=Decimal(balance or face), face_value=Decimal(face),
            pre_tax_price=Decimal(pre), gst_percent=Decimal(rate),
            expiry_date=extra.pop('expiry_date', NEXT_YEAR), center=center or self.center, **extra)

    def set(self, key, value, center=None):
        AppSetting.objects.update_or_create(
            key=key, scope=app_settings.branch_scope(center) if center else 'global',
            defaults={'value': value, 'center': center})
        app_settings.invalidate()


# ─── Value card (F5) ──────────────────────────────────────────────────────

class ValueCardTests(Phase6Base):

    def test_the_worked_example(self):
        """Blueprint B5.6 / plan 5.4: card face 30,000 bought for 20,000 + 5%.
        Service A 30,000 is covered; Service B 15,000 at 18% is paid now."""
        card = self.card()
        items = [self.line(self.svc_a), self.line(self.svc_b)]
        quote = self.quote(items, value_card_id=card.id)
        self.assertEqual(quote.status_code, 200, quote.data)
        self.assertEqual(quote.data['value_card']['settled'], '21000.00')
        self.assertEqual(quote.data['value_card']['balance_after'], '0.00')
        self.assertEqual(quote.data['total_amount'], '38700.00')
        self.assertEqual(quote.data['payable'], '17700.00')
        a = quote.data['lines'][0]
        self.assertEqual((a['card_benefit'], a['card_taxable'], a['card_tax']),
                         ('10000.00', '20000.00', '1000.00'))

        response = self.api.post('/billing/invoices/', self.payload(
            items, value_card_id=card.id,
            payments=[{'payment_method': 'UPI', 'amount': '17700.00', 'reference': 'UPI-88213'}]),
            format='json')
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual((invoice.status, invoice.total_amount, invoice.paid_amount),
                         ('paid', Decimal('38700.00'), Decimal('38700.00')))
        card.refresh_from_db()
        self.assertEqual(card.balance, Decimal('0.00'))
        rows = {p.payment_method: p for p in invoice.payments.all()}
        self.assertEqual((rows['Value Card'].amount, rows['Value Card'].card_face),
                         (Decimal('21000.00'), Decimal('30000.00')))
        self.assertEqual(rows['UPI'].reference, 'UPI-88213')
        # The receipt's blocks: covered vs paid now, tax per rate.
        self.assertEqual(response.data['value_card_cover']['settled'], '21000.00')
        self.assertEqual(response.data['paid_now'], '17700.00')
        self.assertEqual({r['rate']: r['tax'] for r in response.data['tax_breakdown']},
                         {'5.00': '1000.00', '18.00': '2700.00'})

    def test_partial_use_and_cancel_restores_the_face_value(self):
        """A 6,000 service: benefit 2,000, taxable 4,000, GST 200, 4,200
        adjusted; the balance becomes 24,000, and a same-day cancel gives the
        6,000 of face value back."""
        card = self.card()
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)], value_card_id=card.id))
        self.assertEqual(response.status_code, 201, response.data)
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.total_amount, Decimal('4200.00'))
        card.refresh_from_db()
        self.assertEqual(card.balance, Decimal('24000.00'))

        result = self.api.post(f'/billing/invoices/{invoice.id}/cancel/', {'reason': 'x'},
                               format='json')
        self.assertEqual(result.status_code, 200, result.data)
        card.refresh_from_db()
        self.assertEqual(card.balance, Decimal('30000.00'))

    def test_a_line_part_covered_at_another_rate(self):
        """Balance 6,000 on a 15,000 service at 18%: 6,000 face -> 4,000 at the
        card's 5% (200); the other 9,000 at 18% (1,620). SAP gets two lines."""
        card = self.card(balance='6000')
        quote = self.quote([self.line(self.svc_b)], value_card_id=card.id)
        self.assertEqual(quote.status_code, 200, quote.data)
        line = quote.data['lines'][0]
        self.assertEqual((line['card_face'], line['card_tax'], line['tax_amount'], line['total_price']),
                         ('6000.00', '200.00', '1820.00', '13000.00'))
        self.assertEqual(quote.data['payable'], '10620.00')

        from pos_backend.sap_utils import _sap_priced_lines
        bill = bill_and_pay(self.api, self.payload([self.line(self.svc_b)], value_card_id=card.id))
        item = Invoice.objects.get(pk=bill.data['id']).items.get()
        main, extra = _sap_priced_lines(item, 'S1', 'DL_Main')
        self.assertEqual((main['UnitPrice'], main['_TaxPercent'], extra['UnitPrice'], extra['_TaxPercent']),
                         ('4000.000000', Decimal('5.00'), '9000.00', Decimal('18.00')))

    def test_no_other_discount_with_a_card(self):
        """D4 / D4b: no coupon, no membership, no discount on a card bill."""
        card = self.card()
        promo = Promotion.objects.create(name='Ten', promo_type='Discount', discount_type='Percentage',
                                         discount_value=Decimal('10'), start_date=TODAY, end_date=NEXT_YEAR)
        self.assertEqual(self.quote([self.line(self.svc_c)], value_card_id=card.id,
                                    promo_id=promo.id).status_code, 400)
        self.assertEqual(self.quote([self.line(self.svc_c, discount='100.00')],
                                    value_card_id=card.id).status_code, 400)
        self.assertEqual(self.quote([self.line(self.svc_c)], value_card_id=card.id,
                                    discount='50.00').status_code, 400)

    def test_services_only_branch_only_and_expiry(self):
        card = self.card()
        only_product = self.quote([self.product_line()], value_card_id=card.id)
        self.assertIn('services only', str(only_product.data))
        elsewhere = self.card(center=self.other)
        self.assertIn('can be used only at', str(self.quote([self.line(self.svc_c)],
                                                            value_card_id=elsewhere.id).data))
        old = self.card(expiry_date=TODAY - datetime.timedelta(days=1))
        self.assertIn('expired', str(self.quote([self.line(self.svc_c)], value_card_id=old.id).data))

    def test_a_card_is_never_typed_in_as_a_payment(self):
        card = self.card()
        response = self.api.post('/billing/invoices/', self.payload(
            [self.line(self.svc_c)], payments=[{'payment_method': 'Value Card', 'amount': '6300',
                                                'value_card_id': card.id}]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_selling_a_card_fixes_its_terms_and_branch(self):
        tpl = ValueCard.objects.create(title='Gold', pre_tax_price=Decimal('20000'),
                                       value=Decimal('30000'), gst_percent=Decimal('5'),
                                       expiry_days=365)
        bill_and_pay(self.api, self.payload([{
            'content_type': 'marketing.valuecard', 'object_id': tpl.id, 'description': 'Gold',
            'unit_price': '20000.00', 'discount': '0.00', 'quantity': 1,
            'staff_members': [self.staff.id]}]))
        sold = ClientValueCard.objects.get(value_card=tpl)
        self.assertEqual((sold.face_value, sold.pre_tax_price, sold.gst_percent, sold.center_id),
                         (Decimal('30000.00'), Decimal('20000.00'), Decimal('5.00'), self.center.id))

    def test_expired_cards_are_forfeited(self):
        card = self.card(balance='1200', expiry_date=TODAY - datetime.timedelta(days=1))
        self.assertEqual(wallets.forfeit_expired_cards(), 1)
        card.refresh_from_db()
        self.assertEqual((card.balance, card.forfeited_amount, card.is_active),
                         (Decimal('0.00'), Decimal('1200.00'), False))
        self.assertEqual(wallets.forfeit_expired_cards(), 0)


# ─── Offers ───────────────────────────────────────────────────────────────

class OfferTests(Phase6Base):

    def promo(self, promo_type='Discount', **kw):
        defaults = dict(name='Offer', promo_type=promo_type, start_date=TODAY, end_date=NEXT_YEAR,
                        discount_type='Percentage', discount_value=None, config={})
        defaults.update(kw)
        return Promotion.objects.create(**defaults)

    def discount_of(self, promo, items):
        response = self.quote(items, promo_id=promo.id)
        self.assertEqual(response.status_code, 200, response.data)
        return response.data['discount']

    def test_discount_on_one_named_service_with_a_minimum_bill(self):
        promo = self.promo(config={'discount_type': 'Percentage', 'discount_value': 20,
                                   'target': 'Specific Service', 'specific_service': 'Haircut',
                                   'min_bill_amount': 10000})
        self.assertEqual(self.discount_of(promo, [self.line(self.svc_c), self.line(self.svc_b)]),
                         '1200.00')
        self.assertIn('at least Rs 10000', str(self.quote([self.line(self.svc_c)],
                                                          promo_id=promo.id).data))

    def test_flat_price_and_flat_amount(self):
        named = self.promo('FlatPrice', config={'specific_service': 'Haircut', 'flat_price': 4000})
        self.assertEqual(self.discount_of(named, [self.line(self.svc_c, qty=2)]), '4000.00')
        flat = self.promo('FlatPrice', config={'flat_price': 500})
        self.assertEqual(self.discount_of(flat, [self.line(self.svc_c)]), '500.00')

    def test_trigger_needs_both_services_and_discounts_the_services(self):
        """F16 + D1: both services on the bill -> the % off the services."""
        promo = self.promo('Trigger', config={'trigger_services': ['Haircut', 'Hair Spa'],
                                              'trigger_discount': 10})
        self.assertEqual(self.discount_of(promo, [self.line(self.svc_c), self.line(self.svc_b),
                                                  self.product_line()]), '2100.00')
        self.assertEqual(self.quote([self.line(self.svc_c)], promo_id=promo.id).status_code, 400)

    def test_a_promotion_from_another_branch_and_two_offers_are_refused(self):
        theirs = self.promo(level='Center', center=self.other, discount_value=Decimal('10'))
        self.assertIn('not available', str(self.quote([self.line(self.svc_c)], promo_id=theirs.id).data))
        gold = Membership.objects.create(name='Gold', discount_percent=Decimal('10'), expiry_days=30)
        cm = ClientMembership.objects.create(client=self.client_obj, membership=gold,
                                             expiry_date=NEXT_YEAR, center=self.center)
        mine = self.promo(discount_value=Decimal('10'))
        self.assertEqual(self.quote([self.line(self.svc_c)], promo_id=mine.id,
                                    membership_id=cm.id).status_code, 400)

    def test_membership_only_at_the_selling_branch(self):
        """#36 / Q3."""
        gold = Membership.objects.create(name='Gold', discount_percent=Decimal('10'), expiry_days=30)
        theirs = ClientMembership.objects.create(client=self.client_obj, membership=gold,
                                                 expiry_date=NEXT_YEAR, center=self.other)
        self.assertIn('can be used only at', str(self.quote([self.line(self.svc_c)],
                                                            membership_id=theirs.id).data))
        self.set('perks.selling_branch_only', 'false')
        self.assertEqual(self.quote([self.line(self.svc_c)], membership_id=theirs.id)
                         .data['discount'], '600.00')

    def test_the_till_lists_only_usable_promotions(self):
        here = self.promo(name='Here', discount_value=Decimal('5'), level='Center', center=self.center)
        org = self.promo(name='Everywhere', discount_value=Decimal('5'))
        self.promo(name='There', discount_value=Decimal('5'), level='Center', center=self.other)
        self.promo(name='Later', discount_value=Decimal('5'),
                   start_date=TODAY + datetime.timedelta(days=3))
        names = {p['name'] for p in self.api.get(
            f'/marketing/api/promotions/?usable_at={self.center.id}').data}
        self.assertEqual(names, {here.name, org.name})


# ─── Packages, advance, cashback ──────────────────────────────────────────

class PerkAndWalletTests(Phase6Base):

    def test_a_package_is_redeemed_only_where_it_was_sold(self):
        ClientPackage.objects.create(client=self.client_obj, services_remaining={str(self.svc_c.id): 2},
                                     expiry_date=NEXT_YEAR, center=self.other)
        response = self.api.post('/billing/invoices/', self.payload(
            [self.line(self.svc_c, is_redemption=True, unit_price='0.00')]), format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('only at the branch that sold it', str(response.data))

    def test_an_advance_is_not_taxed_when_paid_in(self):
        """#34 / F8 (the 'Charge GST when an advance is taken' setting off):
        whatever rate the till sends."""
        self.set('billing.gst_on_advance', 'false')
        quote = self.quote([{'content_type': 'billing.advancepayment', 'description': 'Advance',
                             'unit_price': '5000.00', 'discount': '0.00', 'quantity': 1,
                             'tax_percentage': '18.00'}])
        self.assertEqual((quote.data['tax_total'], quote.data['total_amount']), ('0.00', '5000.00'))

    def test_an_advance_carries_the_chosen_gst_when_the_sap_setting_is_on(self):
        """The SAP team's scenarios: GST at receipt, the whole amount paid is held."""
        quote = self.quote([{'content_type': 'billing.advancepayment', 'description': 'Advance',
                             'unit_price': '20000.00', 'discount': '0.00', 'quantity': 1,
                             'tax_percentage': '5.00'}])
        self.assertEqual((quote.data['tax_total'], quote.data['total_amount']), ('1000.00', '21000.00'))

    def test_cashback_is_spent_soonest_expiry_first_and_expires(self):
        late = wallets.credit_cashback(self.client_obj, Decimal('300'), expires_on=NEXT_YEAR)
        soon = wallets.credit_cashback(self.client_obj, Decimal('200'),
                                       expires_on=TODAY + datetime.timedelta(days=2))
        open_ended = wallets.credit_cashback(self.client_obj, Decimal('100'))
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)], payments=[
            {'payment_method': 'Cashback Wallet', 'amount': '250.00'},
            {'payment_method': 'Cash', 'amount': '6050.00'}]))
        self.assertEqual(response.status_code, 201, response.data)
        for row in (late, soon, open_ended):
            row.refresh_from_db()
        self.assertEqual((soon.remaining, late.remaining, open_ended.remaining),
                         (Decimal('0.00'), Decimal('250.00'), Decimal('100.00')))

        # Two days on, nothing of `soon` is left to expire; a year on, `late` is.
        self.assertEqual(wallets.expire_cashback(TODAY + datetime.timedelta(days=3)), 0)
        self.assertEqual(wallets.expire_cashback(NEXT_YEAR + datetime.timedelta(days=1)), 1)
        self.assertEqual(wallets.cashback_balance(self.client_obj.id), Decimal('100.00'))

    def test_cancelling_gives_cashback_back_to_its_credits(self):
        credit = wallets.credit_cashback(self.client_obj, Decimal('300'), expires_on=NEXT_YEAR)
        bill = bill_and_pay(self.api, self.payload([self.line(self.svc_c)], payments=[
            {'payment_method': 'Cashback Wallet', 'amount': '300.00'},
            {'payment_method': 'Cash', 'amount': '6000.00'}]))
        credit.refresh_from_db()
        self.assertEqual(credit.remaining, Decimal('0.00'))
        self.api.post(f"/billing/invoices/{bill.data['id']}/cancel/", {'reason': 'x'}, format='json')
        credit.refresh_from_db()
        self.assertEqual(credit.remaining, Decimal('300.00'))
        self.assertEqual(wallets.cashback_balance(self.client_obj.id), Decimal('300.00'))

    def test_campaign_cashback_carries_its_expiry(self):
        promo = Promotion.objects.create(
            name='CB', promo_type='Cashback', start_date=TODAY, end_date=NEXT_YEAR,
            config={'cashback_discount': 10, 'cashback_validity_days': 30})
        bill_and_pay(self.api, self.payload([self.line(self.svc_c)], promo_id=promo.id))
        credit = CashbackTransaction.objects.get(client=self.client_obj, amount__gt=0)
        self.assertEqual((credit.amount, credit.expires_on, credit.remaining),
                         (Decimal('600.00'), TODAY + datetime.timedelta(days=30), Decimal('600.00')))


# ─── Payments ─────────────────────────────────────────────────────────────

class PaymentTests(Phase6Base):

    def test_cash_received_and_change(self):
        """#38: the cash handed over is kept; only the bill amount is paid."""
        response = self.api.post('/billing/invoices/', self.payload([self.line(self.svc_c)], payments=[
            {'payment_method': 'Cash', 'amount': '6300.00', 'tendered': '7000.00'}]), format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data['change_given'], '700.00')
        self.assertEqual(Invoice.objects.get(pk=response.data['id']).paid_amount, Decimal('6300.00'))

    def test_online_payments_are_exact(self):
        response = self.api.post('/billing/invoices/', self.payload([self.line(self.svc_c)], payments=[
            {'payment_method': 'UPI', 'amount': '6300.00', 'tendered': '7000.00'}]), format='json')
        self.assertEqual(response.status_code, 400)

    def test_only_the_branch_s_methods(self):
        self.set('billing.payment_methods', 'Cash, UPI', center=self.center)
        refused = self.api.post('/billing/invoices/', self.payload([self.line(self.svc_c)], payments=[
            {'payment_method': 'Credit Card', 'amount': '6300.00'}]), format='json')
        self.assertEqual(refused.status_code, 400)
        self.assertIn('not a payment method at this branch', str(refused.data))


class PaymentChangeTests(Phase6Base):

    def bill(self, method='Cash'):
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]), method=method)
        return Invoice.objects.get(pk=response.data['id'])

    def change(self, invoice, method, **extra):
        return self.api.post(f'/billing/invoices/{invoice.id}/change_payment/',
                             {'payment_method': method, **extra}, format='json')

    def test_within_the_window_with_a_reference_and_a_sap_repost(self):
        invoice = self.bill()
        result = self.change(invoice, 'UPI', reference='UPI-1')
        self.assertEqual(result.status_code, 200, result.data)
        pay = invoice.payments.get()
        self.assertEqual((pay.payment_method, pay.reference), ('UPI', 'UPI-1'))
        self.assertTrue(SapPosting.objects.filter(key=f'repost:{invoice.id}:{pay.id}',
                                                  status=SapPosting.PENDING).exists())
        self.assertTrue(BackgroundJob.objects.filter(
            payload__object_type='payment_repost', payload__object_id=invoice.id).exists())

    def test_after_the_window_it_is_refused(self):
        invoice = self.bill()
        Invoice.objects.filter(pk=invoice.pk).update(
            finalized_at=timezone.now() - datetime.timedelta(hours=7))
        self.assertEqual(self.change(invoice, 'UPI').status_code, 400)
        self.set('billing.payment_change_hours', '8')
        self.assertEqual(self.change(invoice, 'UPI').status_code, 200)

    def test_a_value_card_cannot_be_swapped_in(self):
        card = ClientValueCard.objects.create(
            client=self.client_obj, balance=Decimal('100'), expiry_date=NEXT_YEAR,
            value_card=ValueCard.objects.create(title='V', value=Decimal('100'), expiry_days=1))
        self.assertEqual(self.change(self.bill(), 'Value Card', value_card_id=card.id).status_code, 400)


@override_settings(SAP_ENABLED=True)
class SapRepostTests(Phase6Base):
    """The SAP side of #41: cancel the incoming payment, post it again."""

    def test_cancel_and_post_again_with_a_fresh_reference(self):
        from pos_backend import sap_utils
        from pos_backend.tests_sap_integrity import FakeSap

        sap = FakeSap()
        with mock.patch('pos_backend.sap_utils._send_sap_request', sap), \
                mock.patch('pos_backend.sap_utils.get_hana_batches', return_value=[]):
            response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]))
            invoice = Invoice.objects.get(pk=response.data['id'])
            Invoice.objects.filter(pk=invoice.pk).update(sap_doc_entry=900)
            invoice.refresh_from_db()
            self.assertTrue(sap_utils.create_sap_incoming_payment(invoice))
            first = sap.posts('IncomingPayments')[-1][2]
            self.assertEqual(first['CashAccount'], app_settings.get('sap.account.cash'))

            pay = invoice.payments.get()
            pay.payment_method = 'UPI'
            pay.save()
            sap_utils.request_payment_repost(invoice, [pay.id])
            self.assertTrue(sap_utils.repost_incoming_payments(invoice))

        cancels = [c for c in sap.calls if c[1].endswith('/Cancel')]
        self.assertEqual(len(cancels), 1)
        again = sap.posts('IncomingPayments')[-1][2]
        self.assertEqual(again['TransferAccount'], app_settings.get('sap.account.upi'))
        self.assertNotEqual(again['Remarks'], first['Remarks'])
        self.assertTrue(SapPosting.objects.filter(key__startswith=f'inpayvoid:{invoice.id}:').exists())

    def test_each_method_posts_to_its_own_account(self):
        from pos_backend import sap_utils
        self.set('sap.account.method.credit_card', '1110100099')
        self.assertEqual(sap_utils.payment_account('Credit Card'), '1110100099')
        self.assertEqual(sap_utils.payment_account('Google Pay'), app_settings.get('sap.account.upi'))
        self.assertEqual(sap_utils.payment_account('Cheque'),
                         app_settings.get('sap.account.transfer_default'))


# ─── The bill as a tax document ───────────────────────────────────────────

class ReceiptTests(Phase6Base):

    def test_snapshots_biller_shift_and_codes(self):
        shift = Shift.objects.create(center=self.center, opened_by=self.user)
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_a), self.product_line()]))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual((invoice.customer_name, invoice.branch_gstin, invoice.place_of_supply,
                          invoice.document_type, invoice.created_by_id, invoice.shift_id),
                         ('Asha Rao', '07AAAAA0000A1Z5', '07-Delhi', 'tax_invoice', self.user.id, shift.id))
        codes = {i.description: (i.item_code, i.hsn_sac) for i in invoice.items.all()}
        self.assertEqual(codes['Bridal Makeup'][1], '999722')
        self.assertEqual(codes['Serum'][0], 'SER01')

        # A later rename never changes the issued bill.
        Center.objects.filter(pk=self.center.pk).update(display_name='Renamed')
        Client.objects.filter(pk=self.client_obj.pk).update(first_name='Changed')
        again = self.api.get(f'/billing/invoices/{invoice.id}/').data
        self.assertEqual((again['branch_name'], again['customer_name']), ('Saket', 'Asha Rao'))

    def test_a_branch_without_gst_issues_a_bill_of_supply(self):
        Center.objects.filter(pk=self.center.pk).update(gst_enabled=False)
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual((invoice.document_type, invoice.cgst, invoice.total_amount),
                         ('bill_of_supply', Decimal('0.00'), Decimal('6000.00')))

    def test_quote_and_saved_bill_agree(self):
        items = [self.line(self.svc_b), self.product_line(qty=2)]
        quote = self.quote(items, discount='333.00')
        saved = bill_and_pay(self.api, self.payload(items, discount='333.00'))
        for key in ('subtotal', 'discount', 'cgst', 'sgst', 'rounding', 'total_amount'):
            self.assertEqual(quote.data[key], saved.data[key], key)


class EInvoiceTests(Phase6Base):

    def test_a_b2b_bill_is_queued_and_waits_for_the_portal(self):
        from billing import einvoice
        Client.objects.filter(pk=self.client_obj.pk).update(gst_number='07BBBBB0000B1Z5')
        with self.captureOnCommitCallbacks(execute=True):
            response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.einvoice_status, Invoice.EINV_PENDING)
        job = BackgroundJob.objects.get(kind='einvoice.generate')

        einvoice.generate(job.payload)
        invoice.refresh_from_db()
        self.assertEqual(invoice.einvoice_status, Invoice.EINV_NOT_CONFIGURED)

        body = einvoice.build_payload(invoice)
        self.assertEqual((body['SellerDtls']['Pin'], body['BuyerDtls']['Gstin'],
                          body['ValDtls']['TotInvVal'], body['ItemList'][0]['HsnCd']),
                         (110017, '07BBBBB0000B1Z5', 6300.0, '999721'))

    def test_a_b2c_bill_needs_no_irn(self):
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]))
        self.assertEqual(Invoice.objects.get(pk=response.data['id']).einvoice_status, '')


@override_settings(SAP_ENABLED=True)
class ForfeitureJournalTests(Phase6Base):
    """F7 / D9: the unused balance of an expired card leaves the liability."""

    def expired_card(self):
        card = ValueCardTests.card(self, balance='3000', expiry_date=TODAY - datetime.timedelta(days=1))
        wallets.forfeit_expired_cards()
        card.refresh_from_db()
        return card

    def test_waits_until_the_income_account_is_named(self):
        from billing.background_jobs import queue_forfeiture_postings
        self.expired_card()
        self.assertEqual(queue_forfeiture_postings(), 0)
        self.set('sap.account.valuecard_forfeiture_income', '4290100001')
        self.assertEqual(queue_forfeiture_postings(), 1)
        self.assertTrue(BackgroundJob.objects.filter(payload__object_type='valuecard_forfeit').exists())

    def test_the_journal_moves_the_pre_tax_value(self):
        from pos_backend import sap_utils
        from pos_backend.tests_sap_integrity import FakeSap
        card = self.expired_card()
        self.set('sap.account.valuecard_forfeiture_income', '4290100001')
        sap = FakeSap()
        with mock.patch('pos_backend.sap_utils._send_sap_request', sap):
            self.assertTrue(sap_utils.create_sap_valuecard_forfeiture_je(card))
            self.assertTrue(sap_utils.create_sap_valuecard_forfeiture_je(card))   # once only
        (post,) = sap.posts('JournalEntries')
        lines = post[2]['JournalEntryLines']
        # Rs 3000 unused face x 20000/30000 = Rs 2000 pre-tax.
        self.assertEqual([(l['AccountCode'], l['Debit'], l['Credit']) for l in lines],
                         [(app_settings.get('sap.account.valuecard_liability'), 2000.0, 0.0),
                          ('4290100001', 0.0, 2000.0)])


class EInvoiceAddressTests(Phase6Base):

    def test_the_client_s_address_and_pin_reach_the_bill_and_the_portal(self):
        from billing import einvoice
        Client.objects.filter(pk=self.client_obj.pk).update(
            gst_number='07BBBBB0000B1Z5', address='4 Park Street, New Delhi', pin_code='110001')
        response = bill_and_pay(self.api, self.payload([self.line(self.svc_c)]))
        invoice = Invoice.objects.get(pk=response.data['id'])
        self.assertEqual(invoice.customer_address, '4 Park Street, New Delhi 110001')
        body = einvoice.build_payload(invoice)
        self.assertEqual((body['BuyerDtls']['Pin'], body['BuyerDtls']['Addr1']), (110001, '4 Park Street'))
        self.assertEqual(einvoice.problems(body), [])

    def test_a_pin_code_is_six_digits(self):
        result = self.api.patch(f'/clients/api/clients/{self.client_obj.id}/', {'pin_code': '1100'},
                                format='json')
        self.assertEqual(result.status_code, 400)
