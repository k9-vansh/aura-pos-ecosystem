"""A whole month of Marketing, run through Billing and read back from SAP's side.

Everything is created the way an operator does it - the Marketing screens' API for
cards, memberships, packages and promotions, the Clients API for B2C customers -
and then sold, used, cancelled and reconciled:

    value card    sold (UPI) -> part-pays a later bill
    membership    sold (cash) -> its % comes off a later bill
    package       sold (card) -> sessions redeemed on later bills
    promotions    percent, flat, flat price, trigger deal, cashback (earned and spent)
    cancellation  of a promo bill, a cashback bill and a card sale

SAP itself is never called: `_send_sap_request` is replaced by a recorder, the
posting worker is run until nothing is due, and every document that would have
reached SAP is compared with the POS bill it belongs to.
"""
import datetime
import re
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import CashbackTransaction, Invoice
from billing.wallets import cashback_balance
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from marketing.models import PromotionUsage
from pos_backend import app_settings, jobs
from pos_backend.sap_utils import _resolve_sap_card_code
from pos_backend import tests_sap_billing_reconcile as _reconcile
drain = _reconcile.drain
from pos_backend.tests_sap_integrity import FakeSap
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


def m(x):
    return Decimal(str(x)).quantize(Decimal('0.01'))


def day(offset=0):
    return str(datetime.date.today() + datetime.timedelta(days=offset))


@override_settings(SAP_ENABLED=True)
class MarketingToSapJourney(TestCase):
    def setUp(self):
        cache.clear()
        app_settings.invalidate()
        self.sap = FakeSap()
        for target, repl in (('pos_backend.sap_utils._send_sap_request', self.sap),
                             ('pos_backend.sap_utils.get_hana_batches', mock.Mock(return_value=[])),
                             ('pos_backend.sap_utils._resolve_bpl_id', mock.Mock(return_value=1))):
            patcher = mock.patch(target, repl)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.center = Center.objects.create(center_name='Saket', display_name='Saket', gst_enabled=True,
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='Owner',
                                                    role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Meena', designation='Stylist')

        self.svc = {
            'cut': ServiceMaster.objects.create(name='Haircut', default_price=Decimal('1000'), tax_percentage=Decimal('18')),
            'spa': ServiceMaster.objects.create(name='Spa', default_price=Decimal('2500'), tax_percentage=Decimal('18')),
            'thread': ServiceMaster.objects.create(name='Threading', default_price=Decimal('100'), tax_percentage=Decimal('18')),
        }
        self.problems = []

    # ── helpers ──────────────────────────────────────────────────────────
    def post(self, url, body, expect=201):
        r = self.api.post(url, body, format='json')
        self.assertEqual(r.status_code, expect, (url, r.data))
        return r.data

    def line(self, key, qty=1, **extra):
        s = self.svc[key]
        line = {'content_type': 'services.servicemaster', 'object_id': s.id, 'description': s.name,
                'unit_price': str(s.default_price), 'discount': '0', 'quantity': qty,
                'tax_percentage': str(s.tax_percentage), 'staff_members': [self.staff.id]}
        line.update(extra)
        return line

    def perk_line(self, label, obj, price):
        return {'content_type': f'marketing.{label}', 'object_id': obj['id'], 'description': obj.get('name') or obj.get('title'),
                'unit_price': str(price), 'discount': '0', 'quantity': 1, 'tax_percentage': '18',
                'staff_members': [self.staff.id]}

    def head(self, client, status='paid'):
        return {'client': client['id'], 'center': self.center.id, 'status': status, 'discount': '0'}

    def sell(self, client, items, method='Cash', **extra):
        res = bill_and_pay(self.api, dict(self.head(client), items=items, **extra), method=method)
        self.assertEqual(res.status_code, 201, res.data)
        return Invoice.objects.get(pk=res.data['id'])

    def pay_with(self, client, items, payments_for, **extra):
        held = self.api.post('/billing/invoices/', dict(self.head(client, 'draft'), items=items, **extra), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        total = Decimal(str(held.data.get('paid_now', held.data['total_amount'])))
        # Spending a card, advance or cashback needs the client's PIN: this is the pass the till gets after typing it.
        from clients import pins
        from clients.models import Client
        pass_ = pins.issue_token(Client.objects.get(pk=client['id']), self.center, None, 'client')
        done = self.api.put(f"/billing/invoices/{held.data['id']}/", dict(
            self.head(client), items=items, payments=payments_for(total), pin_token=pass_, **extra), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        return Invoice.objects.get(pk=done.data['id'])

    def cancel(self, invoice):
        r = self.api.post(f'/billing/invoices/{invoice.pk}/cancel/', {'reason': 'journey test'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def customer(self, first, phone, **extra):
        return self.post('/clients/api/clients/', {
            'first_name': first, 'last_name': 'Test', 'phone': phone, 'center': self.center.id,
            'gender': 'Female', **extra})

    def flag(self, label, ok, detail=''):
        print(f"{'ok  ' if ok else 'DIFF'} {label} {detail}")
        if not ok:
            self.problems.append(f'{label} {detail}'.strip())

    # ── the journey ──────────────────────────────────────────────────────
    def test_the_whole_month(self):
        # 1. Marketing items, made the way the Marketing screens make them
        card = self.post('/marketing/api/cards/', {
            'title': 'Silver Card', 'level': 'Organisation', 'pre_tax_price': '2000', 'post_tax_price': '2360',
            'gst_percent': '18', 'value': '2500', 'benefit_percent': '5.93', 'expiry_days': 365})
        gold = self.post('/marketing/api/memberships/', {
            'name': 'Gold', 'level': 'Organisation', 'pre_tax_price': '1000', 'post_tax_price': '1180',
            'gst_percent': '18', 'discount_percent': '10', 'expiry_days': 365})
        pack = self.post('/marketing/api/packages/', {
            'name': '3 Spa', 'level': 'Organisation', 'price': '6000', 'gst_percent': '18', 'validity_days': 180,
            'services_json': [{'id': self.svc['spa'].id, 'pkgQty': 3}]})
        promos = {
            'percent': self.post('/marketing/api/promotions/', {
                'name': '10% off', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Percentage',
                'discount_value': '10', 'start_date': day(), 'end_date': day(30), 'config': {}}),
            'flat': self.post('/marketing/api/promotions/', {
                'name': 'Rs 250 off', 'promo_type': 'Discount', 'level': 'Organisation', 'discount_type': 'Flat',
                'discount_value': '250', 'start_date': day(), 'end_date': day(30), 'max_usage_per_client': 1,
                'config': {'min_bill_amount': 500}}),
            'flatprice': self.post('/marketing/api/promotions/', {
                'name': 'Haircut at 700', 'promo_type': 'FlatPrice', 'level': 'Organisation',
                'start_date': day(), 'end_date': day(30), 'config': {'flat_price': 700, 'specific_service': 'Haircut'}}),
            'trigger': self.post('/marketing/api/promotions/', {
                'name': 'Cut+Spa', 'promo_type': 'Trigger', 'level': 'Organisation', 'start_date': day(),
                'end_date': day(30), 'config': {'trigger_services': ['Haircut', 'Spa'], 'trigger_discount': 20}}),
            'cashback': self.post('/marketing/api/promotions/', {
                'name': '10% cashback', 'promo_type': 'Cashback', 'level': 'Organisation', 'start_date': day(),
                'end_date': day(30), 'config': {'cashback_discount': 10, 'cashback_validity_days': 30}}),
        }

        # 2. B2C customers, made through the Clients API
        asha = self.customer('Asha', '9990000001')
        bina = self.customer('Bina', '9990000002')
        cara = self.customer('Cara', '9990000003')
        dev = self.customer('Dev', '9990000004')
        esha = self.customer('Esha', '9990000005')
        fay = self.customer('Fay', '9990000006')

        bills = {}
        # 3. Perks sold
        bills['gold'] = self.sell(asha, [self.perk_line('membership', gold, 1000)], 'Cash')
        bills['card'] = self.sell(bina, [self.perk_line('valuecard', card, 2000)], 'UPI')
        bills['pack'] = self.sell(cara, [self.perk_line('package', pack, 6000)], 'Credit Card')
        self.assertTrue(ClientMembership.objects.filter(client_id=asha['id'], membership_id=gold['id']).exists())
        self.assertTrue(ClientValueCard.objects.filter(client_id=bina['id']).exists())
        pkg = ClientPackage.objects.get(client_id=cara['id'])
        self.assertEqual(int(pkg.services_remaining[str(self.svc['spa'].id)]), 3)

        # 4. Perks used
        cm = ClientMembership.objects.get(client_id=asha['id'])
        quote = self.api.post('/billing/invoices/quote/', dict(
            self.head(asha), items=[self.line('cut')], membership_id=cm.id), format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        bills['member_cut'] = self.pay_with(asha, [self.line('cut')], lambda t: [{'payment_method': 'Cash', 'amount': str(t)}],
                                            membership_id=cm.id)
        self.flag('membership took 10% off the haircut', m(bills['member_cut'].discount) == Decimal('100.00'),
                  f"discount {bills['member_cut'].discount}")

        cv = ClientValueCard.objects.get(client_id=bina['id'])
        qv = self.api.post('/billing/invoices/quote/', dict(
            self.head(bina), items=[self.line('spa')], value_card_id=cv.id), format='json')
        self.assertEqual(qv.status_code, 200, qv.data)
        due = Decimal(qv.data['payable'])
        bills['card_spa'] = self.pay_with(bina, [self.line('spa')],
                                          lambda t: ([{'payment_method': 'UPI', 'amount': str(due)}] if due else []),
                                          value_card_id=cv.id)
        cv.refresh_from_db()
        spa_bill = bills['card_spa']
        print('DEBUG card bill: total', spa_bill.total_amount, 'paid', spa_bill.paid_amount, 'UPI due', due,
              'card balance', cv.balance, 'payments', [(p.payment_method, str(p.amount)) for p in spa_bill.payments.all()])
        # The Rs 2500 face value covers the whole Rs 2500 service; the customer only ever paid
        # Rs 2360 for the card, and that is what the bill is worth in cash terms.
        self.flag('value card spent down to nothing by the Rs 2500 service', m(cv.balance) == Decimal('0.00'),
                  f'balance {cv.balance}')
        self.flag('the bill is settled entirely by the card', due == 0 and m(spa_bill.paid_amount) == m(spa_bill.total_amount))

        redeem = self.line('spa', 2, unit_price='0.00', is_redemption=True, description='Spa (redeemed)')
        bills['redeem'] = self.sell(cara, [redeem])
        pkg.refresh_from_db()
        self.flag('package sessions left after redeeming 2 of 3',
                  int(pkg.services_remaining[str(self.svc['spa'].id)]) == 1)

        # 5. Promotions on real bills
        bills['percent'] = self.sell(dev, [self.line('cut'), self.line('thread', 2)], 'UPI', promo_id=promos['percent']['id'])
        bills['flat'] = self.sell(dev, [self.line('spa')], 'Cash', promo_id=promos['flat']['id'])
        bills['flatprice'] = self.sell(esha, [self.line('cut', 2)], 'Cash', promo_id=promos['flatprice']['id'])
        bills['trigger'] = self.sell(esha, [self.line('cut'), self.line('spa')], 'Credit Card', promo_id=promos['trigger']['id'])
        bills['cashback'] = self.sell(fay, [self.line('spa')], 'UPI', promo_id=promos['cashback']['id'])
        earned = cashback_balance(fay['id'])
        self.flag('cashback earned = 10% of the taxable Rs 2500', earned == Decimal('250.00'), f'wallet {earned}')
        bills['cashback_spend'] = self.pay_with(
            fay, [self.line('cut')], lambda t: [{'payment_method': 'Cashback Wallet', 'amount': '250.00'},
                                                 {'payment_method': 'Cash', 'amount': str(t - Decimal('250.00'))}])

        # 6. Cancellations
        bills['cancel_promo'] = self.sell(asha, [self.line('spa')], 'Cash', promo_id=promos['percent']['id'])
        self.cancel(bills['cancel_promo'])
        bills['cancel_card'] = self.sell(dev, [self.perk_line('valuecard', card, 2000)], 'Cash')
        self.cancel(bills['cancel_card'])
        self.flag('cancelled promo bill is marked reversed',
                  PromotionUsage.objects.get(invoice=bills['cancel_promo']).is_reversed)

        # 7. Post everything and read SAP's side
        with self.captureOnCommitCallbacks(execute=True):
            pass
        drain(200)
        self.reconcile(bills)
        print('\nPROBLEMS:', self.problems)
        self.assertEqual(self.problems, [])

    # ── SAP's side ───────────────────────────────────────────────────────
    def reconcile(self, bills):
        posts = [(url.rsplit('/', 1)[-1], body) for (method, url, body) in self.sap.calls if method == 'POST']
        kinds = {}
        for kind, _ in posts:
            kinds[kind] = kinds.get(kind, 0) + 1
        print('\nSAP documents posted:', kinds)

        # every B2C customer reached SAP as the shared B2C partner (no private partner is made)
        partners = [b for k, b in posts if k == 'BusinessPartners']
        print('business partners created for B2C customers:', len(partners))
        self.flag('B2C customers do not each get a partner of their own', not partners, f'({len(partners)} created)')

        # every journal balances
        for kind, body in posts:
            if kind == 'JournalEntries':
                debit = sum(Decimal(str(x.get('Debit') or 0)) for x in body['JournalEntryLines'])
                credit = sum(Decimal(str(x.get('Credit') or 0)) for x in body['JournalEntryLines'])
                self.flag(f"journal balances: {(body.get('Memo') or '')[:50]}", m(debit) == m(credit), f'{debit} vs {credit}')

        by_pos = {str(b.get('U_POSINNO') or ''): b for k, b in posts if k == 'Invoices'}
        credit_notes = [b for k, b in posts if k == 'CreditNotes']

        for label, inv in bills.items():
            inv.refresh_from_db()
            items = list(inv.items.select_related('content_type'))
            goods = [i for i in items if i.content_type and i.content_type.app_label in ('services', 'inventory')
                     and not i.is_redemption]
            doc = by_pos.get(inv.display_number)
            if goods:
                self.flag(f'[{label}] A/R invoice reached SAP', doc is not None)
                if doc is not None:
                    expected = sum(m(i.total_price) - m(i.bill_discount_share) + m(i.tax_amount) for i in goods)
                    shown = _reconcile.SapBillingReconcile.ar_total(doc)
                    rounding = m(inv.rounding) if doc.get('RoundingDiffAmount') else 0
                    self.flag(f'[{label}] A/R invoice = goods net of discount + tax',
                              m(shown) == m(expected + rounding), f'SAP {shown} vs POS {expected + rounding}')
                    self.flag(f'[{label}] customer is the B2C partner', doc.get('CardCode') == _resolve_sap_card_code(inv.client),
                              doc.get('CardCode'))
            perk = [i for i in items if i.content_type and i.content_type.app_label == 'marketing']
            for i in perk:
                word = {'membership': 'Membership', 'valuecard': 'Value Card', 'package': 'Package'}[i.content_type.model]
                found = [b for k, b in posts if k == 'JournalEntries'
                         and re.search(rf'{word} Sale - Invoice #{inv.pk}\b', b.get('Memo') or '')]
                self.flag(f'[{label}] {word} sale journal reached SAP', bool(found))
            if inv.status == 'cancelled' and (goods or perk):
                if goods:
                    cn = [b for b in credit_notes if str(b.get('U_POSINNO') or b.get('NumAtCard') or '').find(str(inv.display_number)) >= 0]
                    self.flag(f'[{label}] credit memo reached SAP', bool(cn) or len(credit_notes) >= 1)

        # What a cancelled bill leaves behind in SAP: every journal and payment it created
        # must have been cancelled with SAP's own Cancel action, once.
        cancels = [url for (method, url, body) in self.sap.calls if method == 'POST' and url.endswith('/Cancel')]
        print('cancel calls:', cancels)
        for label in ('cancel_promo', 'cancel_card'):
            inv = bills[label]
            from pos_backend.sap_utils import sap_documents_of_bill
            journals, payments = sap_documents_of_bill(inv)
            for n in journals:
                self.flag(f'[{label}] journal {n} cancelled in SAP', any(u.endswith(f'/JournalEntries({n})/Cancel') for u in cancels))
            for n in payments:
                self.flag(f'[{label}] payment {n} cancelled in SAP', any(u.endswith(f'/IncomingPayments({n})/Cancel') for u in cancels))
            self.flag(f'[{label}] something was posted for it and then cancelled', bool(journals or payments))
        live_docs = set()
        for label, inv in bills.items():
            if inv.status in ('paid', 'partial'):
                from pos_backend.sap_utils import sap_documents_of_bill
                j, p = sap_documents_of_bill(inv)
                live_docs.update(('je', n) for n in j)
                live_docs.update(('inpay', n) for n in p)
        for u in cancels:
            hit = re.search(r'/(JournalEntries|IncomingPayments)\((\d+)\)/Cancel', u)
            kind = 'je' if hit.group(1) == 'JournalEntries' else 'inpay'
            self.flag(f'cancel {u.rsplit("/", 2)[-2]} touches no live bill', (kind, int(hit.group(2))) not in live_docs)
        self.flag('each document is cancelled once only', len(cancels) == len(set(cancels)))

        # payments reaching SAP == paid on each live bill (cash/bank + card/advance journals)
        sap_paid = {}

        def credit(pk, amount):
            sap_paid[pk] = sap_paid.get(pk, Decimal(0)) + Decimal(str(amount))

        for kind, body in posts:
            if kind == 'IncomingPayments':
                found = re.search(r'POS_INV_(\d+)_', body.get('Remarks') or '')
                if found:
                    credit(int(found.group(1)), Decimal(str(body.get('CashSum') or 0)) + Decimal(str(body.get('TransferSum') or 0)))
            elif kind == 'JournalEntries':
                memo = body.get('Memo') or ''
                first = body['JournalEntryLines'][0]
                sale = re.search(r'(Membership|Value Card|Package) Sale - Invoice #(\d+)', memo)
                adj = re.search(r'Advance Adjustment \((?:Value Card|Advance|Bill)\) - Invoice #(\d+)', memo)
                if sale:
                    credit(int(sale.group(2)), first['Debit'])
                elif adj:
                    credit(int(adj.group(1)), first['Credit'])
        for label, inv in bills.items():
            if inv.status in ('paid', 'partial'):
                self.flag(f'[{label}] money reaching SAP = paid on the bill',
                          m(sap_paid.get(inv.pk, 0)) == m(inv.paid_amount),
                          f'SAP {m(sap_paid.get(inv.pk, 0))} vs POS {m(inv.paid_amount)}')
        for label in ('card_spa', 'cashback_spend'):
            inv = bills[label]
            print(f'DEBUG {label}: total {inv.total_amount} paid {inv.paid_amount} payments',
                  [(p.payment_method, str(p.amount)) for p in inv.payments.all()])
            for kind, body in posts:
                text = str(body)
                if f'POS_INV_{inv.pk}_' in text or f'Invoice #{inv.pk}' in text:
                    print('   SAP', kind, {k: v for k, v in body.items() if k in (
                        'CashSum', 'TransferSum', 'TransferAccount', 'Memo', 'Remarks', 'PaymentInvoices',
                        'JournalEntryLines', 'DocTotal')})

        # POS-side invariants on every bill
        for label, inv in bills.items():
            if inv.status in ('paid', 'partial'):
                items = list(inv.items.all())
                self.flag(f'[{label}] lines add up to the subtotal',
                          m(sum(m(i.total_price) for i in items)) == m(inv.subtotal))
                self.flag(f'[{label}] line tax adds up to the header tax',
                          m(sum(m(i.tax_amount) for i in items)) == m(inv.cgst) + m(inv.sgst))
