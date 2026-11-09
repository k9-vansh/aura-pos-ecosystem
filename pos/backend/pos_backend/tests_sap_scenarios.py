"""
The accountant's value-card / advance scenarios, replayed through the real code.

Each test sells a card (or takes an advance), bills a service, redeems, then
reads the journal lines the POS would send to SAP - SAP itself is never called
(the posting function is replaced by one that records what it was given) - and
compares them with the Excel sheet line by line.

Accounts, as in the sheet:
    Paytm Virtual Parking   1110100008      Advance from customer   2320100001
    Paytm Bank              1110100009
    Output CGST advance     2341100024      Output SGST advance     2341100025

UPI is posted to the parking account; the next-day settlement journal moves it
to the bank (Scenario 04).
"""
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem
from clients.models import Client, ClientValueCard
from marketing.models import ValueCard
from pos_backend import app_settings, sap_utils
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

BANK, ADVANCE, CGST, SGST = '1110100008', '2320100001', '2341100024', '2341100025'   # BANK = UPI parking
UPI_BANK = '1110100009'


def money(x):
    return Decimal(str(x)).quantize(Decimal('0.01'))


class SheetScenarioTests(TestCase):
    def setUp(self):
        from pos_backend.testing import pin_gate_off
        pin_gate_off()                      # these tests spend wallets without caring about the PIN
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.center = Center.objects.create(center_name='Saket', display_name='Saket',
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='b@test.com', password='pw', full_name='B',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client_obj = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist')
        self.posted = []

        def record(key, payload, total):
            self.posted.append((key, payload))
            return 'posted'

        patcher = mock.patch.object(sap_utils, '_post_je_once', record)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, ret in [('_resolve_bpl_id', 1), ('_get_je_udf_fields', {}),
                          ('_resolve_card_code_for_invoice', 'B2C')]:
            patcher = mock.patch.object(sap_utils, name, mock.Mock(return_value=ret))
            patcher.start()
            self.addCleanup(patcher.stop)

    # ── helpers ──────────────────────────────────────────────────────────
    def bill(self, items, **extra):
        payload = {'client': self.client_obj.id, 'center': self.center.id, 'status': 'paid',
                   'discount': '0.00', 'items': items}
        payload.update(extra)
        return payload

    def service_line(self, price, rate):
        svc = ServiceMaster.objects.create(name=f'Svc{price}', default_price=Decimal(price),
                                           tax_percentage=Decimal(rate))
        return {'content_type': 'services.servicemaster', 'object_id': svc.id, 'description': svc.name,
                'unit_price': str(price), 'discount': '0.00', 'quantity': 1,
                'staff_members': [self.staff.id]}

    def sell_card(self, pre='20000', face='20000', rate='5', method='UPI'):
        tpl = ValueCard.objects.create(title='Card', pre_tax_price=Decimal(pre),
                                       post_tax_price=Decimal(pre) * (1 + Decimal(rate) / 100),
                                       value=Decimal(face), gst_percent=Decimal(rate), expiry_days=365)
        res = bill_and_pay(self.api, self.bill([{
            'content_type': 'marketing.valuecard', 'object_id': tpl.id, 'description': 'Card',
            'unit_price': str(pre), 'discount': '0.00', 'quantity': 1, 'staff_members': [self.staff.id]}]),
            method=method)
        self.assertEqual(res.status_code, 201, res.data)
        invoice = Invoice.objects.get(pk=res.data['id'])
        item = invoice.items.get()
        sap_utils.create_sap_valuecard_je(invoice, item)
        return ClientValueCard.objects.get(value_card=tpl), self.lines()

    def lines(self):
        """The (account, debit, credit) lines of the journal posted last."""
        key, payload = self.posted[-1]
        out = []
        for ln in payload['JournalEntryLines']:
            out.append((ln.get('AccountCode') or ln.get('ShortName'),
                        money(ln['Debit']), money(ln['Credit'])))
        return out

    def redeem(self, card, items, extra_payment=None):
        payments = [{'payment_method': 'Value Card', 'value_card_id': card.id}]
        payload = self.bill(items, value_card_id=card.id)
        quote = self.api.post('/billing/invoices/quote/', payload, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        covered = Decimal(quote.data['value_card']['settled'])
        pay_now = Decimal(quote.data['payable'])
        body = dict(payload, payments=([{'payment_method': 'UPI', 'amount': str(pay_now)}] if pay_now else []))
        held = self.api.post('/billing/invoices/', dict(payload, status='draft'), format='json')
        done = self.api.put(f"/billing/invoices/{held.data['id']}/", body, format='json')
        self.assertEqual(done.status_code, 200, done.data)
        invoice = Invoice.objects.get(pk=done.data['id'])
        vc = invoice.payments.get(payment_method='Value Card')
        before = len(self.posted)
        sap_utils.create_sap_valuecard_redemption_je(invoice, float(vc.amount),
                                                      float(card.gst_percent), payment_id=vc.id)
        self.assertEqual(len(self.posted), before + 1)
        return invoice, covered, pay_now, self.lines()

    # ── Scenario 02: card fully used, bill exactly covered ───────────────
    def test_scenario_02_sale_then_full_use(self):
        card, sale = self.sell_card()
        self.assertEqual(sorted(sale), sorted([
            (BANK, money(21000), money(0)),
            (ADVANCE, money(0), money(20000)),
            (CGST, money(0), money(500)),
            (SGST, money(0), money(500))]))

        invoice, covered, pay_now, je = self.redeem(card, [self.service_line(20000, 5)])
        self.assertEqual((invoice.total_amount, pay_now), (money(21000), money(0)))
        self.assertEqual(sorted(je), sorted([
            ('B2C', money(0), money(21000)),
            (ADVANCE, money(20000), money(0)),
            (CGST, money(500), money(0)),
            (SGST, money(500), money(0))]))

    # ── Scenario 01: 50,000 bill, card covers 21,000, rest paid ──────────
    def test_scenario_01_card_plus_payment(self):
        card, _ = self.sell_card()
        invoice, covered, pay_now, je = self.redeem(card, [self.service_line(50000, 5)])
        self.assertEqual((invoice.total_amount, covered, pay_now),
                         (money(52500), money(21000), money(31500)))
        # sheet: Dr Advance 20,000 + Dr GST advance 1,000 (+ Dr bank 31,500 as a receipt) = Cr B2C 52,500
        self.assertEqual(sorted(je), sorted([
            ('B2C', money(0), money(21000)),
            (ADVANCE, money(20000), money(0)),
            (CGST, money(500), money(0)),
            (SGST, money(500), money(0))]))
        self.assertEqual(invoice.payments.get(payment_method='UPI').amount, money(31500))

    # ── Scenario 01.1: 36,000 bill ───────────────────────────────────────
    def test_scenario_01_1_smaller_bill(self):
        card, _ = self.sell_card()
        invoice, covered, pay_now, je = self.redeem(card, [self.service_line(36000, 5)])
        self.assertEqual((invoice.total_amount, covered, pay_now),
                         (money(37800), money(21000), money(16800)))
        self.assertEqual(sorted(je), sorted([
            ('B2C', money(0), money(21000)),
            (ADVANCE, money(20000), money(0)),
            (CGST, money(500), money(0)),
            (SGST, money(500), money(0))]))

    # ── Scenario 03: only part of the card is used ───────────────────────
    def test_scenario_03_partial_use(self):
        card, _ = self.sell_card()
        invoice, covered, pay_now, je = self.redeem(card, [self.service_line(10000, 5)])
        self.assertEqual((invoice.total_amount, covered, pay_now),
                         (money(10500), money(10500), money(0)))
        # The sheet shows 500 + 500 GST here, which does not balance against 10,500:
        # 5% of 10,000 is 500 in total, 250 for each of CGST and SGST.
        self.assertEqual(sorted(je), sorted([
            ('B2C', money(0), money(10500)),
            (ADVANCE, money(10000), money(0)),
            (CGST, money(250), money(0)),
            (SGST, money(250), money(0))]))
        card.refresh_from_db()
        self.assertEqual(card.balance, money(10000))

    # ── every journal balances, whatever the amounts ─────────────────────
    def test_every_journal_balances(self):
        card, sale = self.sell_card(pre='18333.33', face='25000.50')
        for lines in (sale,):
            self.assertEqual(sum(d for _, d, _ in lines), sum(c for _, _, c in lines))
        invoice, covered, pay_now, je = self.redeem(card, [self.service_line(7777.77, 5)])
        self.assertEqual(sum(d for _, d, _ in je), sum(c for _, _, c in je))

    # ── The advance version of Scenario 01.1 (sheet: "Advance" / Inv / JV) ─
    def take_advance(self, amount='20000', rate='5'):
        res = bill_and_pay(self.api, self.bill([{
            'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance',
            'unit_price': amount, 'discount': '0.00', 'quantity': 1, 'tax_percentage': rate,
            'staff_members': [self.staff.id]}]), method='UPI')
        self.assertEqual(res.status_code, 201, res.data)
        from billing.models import AdvancePayment
        return AdvancePayment.objects.get(invoice_id=res.data['id'])

    def test_advance_is_credited_with_its_gst_and_posts_the_sheet_journal(self):
        adv = self.take_advance()
        # The client paid 21,000, so 21,000 is what they hold.
        self.assertEqual((adv.amount, adv.gst_amount, adv.gst_inclusive),
                         (money(21000), money(1000), True))
        sap_utils.create_sap_advance_je(adv)
        self.assertEqual(sorted(self.lines()), sorted([
            (BANK, money(21000), money(0)),
            (ADVANCE, money(0), money(20000)),
            (CGST, money(0), money(500)),
            (SGST, money(0), money(500))]))

    def test_scenario_01_1_advance_spent_on_a_36000_bill(self):
        from billing import wallets
        adv = self.take_advance()
        res = bill_and_pay(self.api, self.bill(
            [self.service_line(36000, 5)],
            payments=[{'payment_method': 'Advance', 'amount': '21000'},
                      {'payment_method': 'UPI', 'amount': '16800'}]))
        self.assertEqual(res.status_code, 201, res.data)
        invoice = Invoice.objects.get(pk=res.data['id'])
        self.assertEqual(invoice.total_amount, money(37800))
        self.assertEqual(wallets.advance_balance(self.client_obj.id), money(0))

        advance_payment = invoice.payments.get(payment_method='Advance')
        sap_utils.create_sap_advance_adjustment_je(invoice, float(advance_payment.amount),
                                                   payment_id=advance_payment.id)
        # sheet: Dr Advance 20,000, Dr GST advance 1,000, Cr B2C (the 21,000 the
        # advance settles; the other 16,800 is the bank's incoming payment).
        self.assertEqual(sorted(self.lines()), sorted([
            ('B2C', money(0), money(21000)),
            (ADVANCE, money(20000), money(0)),
            (CGST, money(500), money(0)),
            (SGST, money(500), money(0))]))

    def test_an_advance_taken_before_gst_was_counted_spends_without_gst(self):
        from billing.models import AdvancePayment
        AdvancePayment.objects.create(client=self.client_obj, center=self.center, amount=Decimal('20000'),
                                      gst_amount=Decimal('1000'), gst_inclusive=False)
        res = bill_and_pay(self.api, self.bill([self.service_line(10000, 0)],
                                               payments=[{'payment_method': 'Advance', 'amount': '10000'}]))
        self.assertEqual(res.status_code, 201, res.data)
        invoice = Invoice.objects.get(pk=res.data['id'])
        p = invoice.payments.get(payment_method='Advance')
        sap_utils.create_sap_advance_adjustment_je(invoice, float(p.amount), payment_id=p.id)
        self.assertEqual(sorted(self.lines()), sorted([
            ('B2C', money(0), money(10000)), (ADVANCE, money(10000), money(0))]))

    # ── Scenario 01.1 as ONE journal: card part + cash/UPI part together ──
    def test_scenario_01_1_card_and_upi_post_as_one_journal(self):
        self.one_journal_for_card_plus_upi(service_price=36000, face='20000')

    def test_scenario_01_1_a_46000_service_with_a_30000_face_value_card(self):
        # A card bought for 20,000 carries 30,000 of face value: its 10,000 benefit
        # is the difference between the 46,000 list price and the sheet's 36,000
        # revenue (36,000 + 5% GST = 37,800; the card settles 21,000, UPI 16,800).
        self.one_journal_for_card_plus_upi(service_price=46000, face='30000')

    def one_journal_for_card_plus_upi(self, service_price, face):
        from pos_backend import sap_ledger
        card, _ = self.sell_card(pre='20000', face=face)
        payload = self.bill([self.service_line(service_price, 5)], value_card_id=card.id)
        quote = self.api.post('/billing/invoices/quote/', payload, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        held = self.api.post('/billing/invoices/', dict(payload, status='draft'), format='json')
        done = self.api.put(f"/billing/invoices/{held.data['id']}/", dict(
            payload, payments=[{'payment_method': 'UPI', 'amount': quote.data['payable']}]), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        invoice = Invoice.objects.get(pk=done.data['id'])
        Invoice.objects.filter(pk=invoice.pk).update(sap_doc_entry=77)
        invoice.refresh_from_db()
        sap_ledger.mark_done(f"invoice:{invoice.id}", 77)

        def post_like_sap(key, payload, total):
            # As the real posting: a journal recorded as done is never sent again.
            if sap_ledger.posted(key):
                return sap_ledger.posted(key)
            self.posted.append((key, payload))
            sap_ledger.mark_done(key, 'JE1')
            return 'JE1'

        before = len(self.posted)
        with mock.patch.object(sap_utils, '_post_je_once', post_like_sap), \
                mock.patch.object(sap_utils, '_invoice_card_code', return_value='B2C'), \
                mock.patch.object(sap_utils, '_send_sap_request',
                                  side_effect=AssertionError('no separate incoming payment')):
            self.assertTrue(sap_utils.create_sap_incoming_payment(invoice))
            self.assertEqual(len(self.posted), before + 1)        # exactly one journal
            self.assertEqual(sorted(self.lines()), sorted([
                ('B2C', money(0), money(37800)),
                (ADVANCE, money(20000), money(0)),
                (CGST, money(500), money(0)),
                (SGST, money(500), money(0)),
                (BANK, money(16800), money(0))]))             # BANK = the UPI parking account
            # Run again (a retry): nothing more is posted.
            self.assertTrue(sap_utils.create_sap_incoming_payment(invoice))
            self.assertEqual(len(self.posted), before + 1)

    def test_scenario_01_1_advance_and_upi_post_as_one_journal(self):
        from pos_backend import sap_ledger
        self.take_advance()
        res = bill_and_pay(self.api, self.bill(
            [self.service_line(36000, 5)],
            payments=[{'payment_method': 'Advance', 'amount': '21000'},
                      {'payment_method': 'UPI', 'amount': '16800'}]))
        self.assertEqual(res.status_code, 201, res.data)
        invoice = Invoice.objects.get(pk=res.data['id'])
        Invoice.objects.filter(pk=invoice.pk).update(sap_doc_entry=78)
        invoice.refresh_from_db()
        sap_ledger.mark_done(f"invoice:{invoice.id}", 78)

        def post_like_sap(key, payload, total):
            if sap_ledger.posted(key):
                return sap_ledger.posted(key)
            self.posted.append((key, payload))
            sap_ledger.mark_done(key, 'JE2')
            return 'JE2'

        before = len(self.posted)
        with mock.patch.object(sap_utils, '_post_je_once', post_like_sap), \
                mock.patch.object(sap_utils, '_invoice_card_code', return_value='B2C'), \
                mock.patch.object(sap_utils, '_send_sap_request',
                                  side_effect=AssertionError('no separate incoming payment')):
            self.assertTrue(sap_utils.create_sap_incoming_payment(invoice))
            self.assertEqual(len(self.posted), before + 1)
            self.assertEqual(sorted(self.lines()), sorted([
                ('B2C', money(0), money(37800)),
                (ADVANCE, money(20000), money(0)),
                (CGST, money(500), money(0)),
                (SGST, money(500), money(0)),
                (BANK, money(16800), money(0))]))
            self.assertTrue(sap_utils.create_sap_incoming_payment(invoice))
            self.assertEqual(len(self.posted), before + 1)

    # ── The next-day settlement: parking -> bank ─────────────────────────
    def test_upi_settles_from_parking_to_the_bank_the_next_day(self):
        from datetime import timedelta
        from django.utils import timezone
        t0 = timezone.now() - timedelta(hours=1)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=t0), 0)   # stamps the start
        self.take_advance()                                                 # 21,000 by UPI
        self.sell_card(method='Cash')                                       # paid in cash: not settled
        Invoice.objects.update(sap_sync_status=True)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=timezone.now()), 0)   # same day: too early
        before = len(self.posted)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=timezone.now() + timedelta(days=1)), 1)
        self.assertEqual(len(self.posted), before + 1)
        self.assertEqual(sorted(self.lines()), sorted([
            (UPI_BANK, money(21000), money(0)), (BANK, money(0), money(21000))]))
        payload = self.posted[-1][1]
        self.assertEqual(payload['Reference'], 'UPI-SETTLE')
        self.assertEqual(payload['Reference2'], str(timezone.now().date()))
        self.assertEqual(payload['Reference3'], 'Saket')
        self.assertIn('1 pmt', payload['Memo'])
        for line in payload['JournalEntryLines']:
            self.assertEqual(line['U_B2C_NAME'], 'Asha')
            self.assertEqual(line['U_B2C_PH'], '9990000001')
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=timezone.now() + timedelta(days=1)), 0)
        self.assertEqual(len(self.posted), before + 1)                       # never twice

    def test_upi_settles_after_one_minute_in_demo_mode(self):
        from datetime import timedelta
        from django.utils import timezone
        from pos_backend.models import AppSetting
        AppSetting.objects.update_or_create(key='sap.upi_settlement_minutes', scope='global',
                                            defaults={'value': '1'})
        app_settings.invalidate()
        t0 = timezone.now() - timedelta(hours=1)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=t0), 0)   # stamps the start
        self.take_advance()
        Invoice.objects.update(sap_sync_status=True)
        taken = timezone.now()
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=taken), 0)             # too early
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=taken + timedelta(seconds=30)), 0)
        before = len(self.posted)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=taken + timedelta(minutes=2)), 1)
        self.assertEqual(len(self.posted), before + 1)
        self.assertEqual(sap_utils.create_sap_upi_settlements(now=taken + timedelta(minutes=3)), 0)

    # ── Scenario 04: a parking account in front of the bank ──────────────
    def set_account(self, key, value):
        from pos_backend.models import AppSetting
        AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': value})
        app_settings.invalidate()

    def test_scenario_04_parking_account_is_what_the_sale_debits(self):
        # A sale is journalled to where the money went: UPI to the UPI parking account...
        self.set_account('sap.account.upi', '1110100099')
        _, sale = self.sell_card(method='UPI')
        self.assertIn(('1110100099', money(21000), money(0)), sale)

    def test_scenario_04b_a_cash_sale_debits_the_cash_account(self):
        # ...and cash to the cash account, whatever the parking account is.
        self.set_account('sap.account.upi', '1110100099')
        self.set_account('sap.account.cash', '1110100055')
        _, sale = self.sell_card(method='Cash')
        self.assertIn(('1110100055', money(21000), money(0)), sale)
        self.assertNotIn('1110100099', [account for account, _, _ in sale])
