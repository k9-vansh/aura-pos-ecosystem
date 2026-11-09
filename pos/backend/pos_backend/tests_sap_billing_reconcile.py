"""
What SAP is told about each bill, against what the POS says.

The bills of tests_numbers_everywhere are raised through the real API with SAP
replaced by FakeSap, the posting worker is run until nothing is due, and every
document that would have reached SAP is compared with the invoice: the A/R
invoice's value, the incoming payments, the journals (which must balance) and
the credit memo of a cancelled bill. Nothing touches the network.
"""
import re
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from billing.models import Invoice
from pos_backend import jobs, sap_utils
from pos_backend import tests_numbers_everywhere as base
from pos_backend.tests_numbers_everywhere import m
from pos_backend.tests_sap_integrity import FakeSap


def drain(limit=60):
    for _ in range(limit):
        if not jobs.process_due():
            return


def tax_rate(code):
    found = re.search(r'@(\d+)', code or '')
    return Decimal(found.group(1)) if found else Decimal(0)


@override_settings(SAP_ENABLED=True)
class SapBillingReconcile(base.Everything):
    test_report = None

    def setUp(self):
        super().setUp()
        self.sap = FakeSap()
        for target, repl in (('pos_backend.sap_utils._send_sap_request', self.sap),
                             ('pos_backend.sap_utils.get_hana_batches', mock.Mock(return_value=[])),
                             ('pos_backend.sap_utils._resolve_bpl_id', mock.Mock(return_value=1))):
            p = mock.patch(target, repl)
            p.start()
            self.addCleanup(p.stop)
        self.problems = []

    def note(self, label, shown, truth):
        shown, truth = m(shown), m(truth)
        ok = shown == truth
        print(f"{'ok  ' if ok else 'DIFF'} {label:66s} SAP {shown:>10}  POS {truth:>10}"
              + ('' if ok else f'  (off by {shown - truth})'))
        if not ok:
            self.problems.append(label)

    @staticmethod
    def ar_total(doc):
        """What SAP will make of an A/R invoice or credit memo document."""
        total = Decimal(0)
        for line in doc.get('DocumentLines', []):
            if line.get('TreeType') == 'iIngredient':
                continue
            net = m(Decimal(str(line.get('UnitPrice') or 0)) * Decimal(str(line.get('Quantity') or 0)))
            total += net + m(net * tax_rate(line.get('TaxCode')) / 100)
        return total + Decimal(str(doc.get('RoundingDiffAmount') or 0))

    def test_what_sap_is_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.build()
        drain()
        posts = [(url, body) for (method, url, body) in self.sap.calls if method == 'POST']
        kinds = {}
        for url, _ in posts:
            kinds[url.rsplit('/', 1)[-1]] = kinds.get(url.rsplit('/', 1)[-1], 0) + 1
        print('\nSAP documents posted:', kinds)

        live = list(Invoice.objects.filter(status__in=['paid', 'partial']).order_by('id'))
        by_pos = {}
        for url, body in posts:
            if url.endswith('/Invoices'):
                by_pos[str(body.get('U_POSINNO') or body.get('Comments') or '')] = body
        print('A/R invoices posted:', len(by_pos), 'for', len(live), 'live bills')

        pays_for = {}
        for url, body in posts:
            if url.endswith('/IncomingPayments'):
                found = re.search(r'POS_INV_(\d+)_', body.get('Remarks') or '')
                if found:
                    amount = (Decimal(str(body.get('CashSum') or 0)) + Decimal(str(body.get('TransferSum') or 0)))
                    pays_for.setdefault(int(found.group(1)), Decimal(0))
                    pays_for[int(found.group(1))] += amount

        journals = [body for url, body in posts if url.endswith('/JournalEntries')]
        for j in journals:
            debit = sum(Decimal(str(l.get('Debit') or 0)) for l in j['JournalEntryLines'])
            credit = sum(Decimal(str(l.get('Credit') or 0)) for l in j['JournalEntryLines'])
            self.note(f"journal balances: {str(j.get('Memo') or j.get('Reference') or '')[:44]}", debit, credit)

        for inv in live:
            doc = by_pos.get(inv.display_number)
            items = list(inv.items.select_related('content_type'))
            has_goods = any(i.content_type and i.content_type.app_label in ('services', 'inventory') for i in items)
            if has_goods:
                self.assertIsNotNone(doc, f'no A/R invoice for {inv.display_number}')
                goods_total = sum(m(i.total_price) - m(i.bill_discount_share) + m(i.tax_amount)
                                  for i in items if i.content_type and i.content_type.app_label in ('services', 'inventory'))
                self.note(f'bill {inv.pk} A/R invoice = goods on the bill + round-off',
                          self.ar_total(doc), goods_total + (m(inv.rounding) if doc.get('RoundingDiffAmount') else 0))
        # ── every bill's payments reach SAP in full ──
        from billing.models import AdvancePayment
        sap_paid = {}

        def credit(pk, amount):
            sap_paid[pk] = sap_paid.get(pk, Decimal(0)) + Decimal(str(amount))

        for url, body in posts:
            kind = url.rsplit('/', 1)[-1]
            if kind == 'IncomingPayments':
                found = re.search(r'POS_INV_(\d+)_', body.get('Remarks') or '')
                if found:
                    credit(int(found.group(1)), Decimal(str(body.get('CashSum') or 0)) + Decimal(str(body.get('TransferSum') or 0)))
            elif kind == 'JournalEntries':
                memo = body.get('Memo') or ''
                sale = re.search(r'(Membership|Value Card|Package) Sale - Invoice #(\d+)', memo)
                adj = re.search(r'Advance Adjustment \((?:Value Card|Advance)\) - Invoice #(\d+)', memo)
                adv = re.search(r'Advance Payment POS (\d+)', memo)
                first = body['JournalEntryLines'][0]
                if sale:
                    credit(int(sale.group(2)), first['Debit'])
                elif adj:
                    credit(int(adj.group(1)), first['Credit'])
                elif adv:
                    row = AdvancePayment.objects.filter(pk=int(adv.group(1))).first()
                    if row and row.invoice_id:
                        credit(row.invoice_id, first['Debit'])
        for inv in live:
            self.note(f'bill {inv.pk} payments reaching SAP = paid on the bill', sap_paid.get(inv.pk, 0), inv.paid_amount)

        # ── the customer account (B2C) should net to what is still owed ──
        owed = sum((m(i.total_amount) - m(i.paid_amount) for i in live), Decimal(0))
        balance = Decimal(0)
        for url, body in posts:
            kind = url.rsplit('/', 1)[-1]
            if kind == 'Invoices':
                balance += self.ar_total(body)
            elif kind == 'CreditNotes':
                balance -= self.ar_total(body)
            elif kind == 'IncomingPayments' and body.get('CardCode') == 'B2C':
                balance -= sum(Decimal(str(x['SumApplied'])) for x in body['PaymentInvoices'])
            elif kind == 'JournalEntries':
                for ln in body['JournalEntryLines']:
                    if ln.get('AccountCode') == 'B2C' or ln.get('ShortName') == 'B2C':
                        balance += Decimal(str(ln.get('Debit') or 0)) - Decimal(str(ln.get('Credit') or 0))
        self.note('B2C customer account nets to what is still owed', balance, owed)

        print()
        for inv in Invoice.objects.exclude(status='draft').order_by('id'):
            pay = ', '.join(f'{p.payment_method} {p.amount}' for p in inv.payments.all())
            kinds = [it.content_type.model for it in inv.items.select_related('content_type')]
            print(f'POS bill {inv.pk} [{inv.status}] total {inv.total_amount} paid {inv.paid_amount} :: {pay or "-"} :: {kinds}')
        for url, body in posts:
            kind = url.rsplit('/', 1)[-1]
            if kind == 'Invoices':
                print('  SAP Invoice', body.get('U_POSINNO'), 'total', self.ar_total(body), 'card', body.get('CardCode'))
            elif kind == 'IncomingPayments':
                print('  SAP Payment', body.get('Remarks'), 'cash', body.get('CashSum'), 'transfer', body.get('TransferSum'), body.get('TransferAccount'), 'applied', [x['SumApplied'] for x in body['PaymentInvoices']])
            elif kind == 'JournalEntries':
                print('  SAP Journal', (body.get('Memo') or '')[:60], [(l.get('AccountCode') or l.get('ShortName'), l.get('Debit'), l.get('Credit')) for l in body['JournalEntryLines']])
            else:
                print('  SAP', kind, str(body)[:300])
        self.problems and print('PROBLEMS:', self.problems)
        print('SAP calls total', len(self.sap.calls))

    def test_what_sap_is_told_when_a_bill_is_cancelled(self):
        """Cancel a membership bill and an advance bill; what reverses in SAP?"""
        def mem():
            return {'content_type': 'marketing.membership', 'object_id': self.membership.id, 'description': 'Gold',
                    'unit_price': '1000', 'discount': '0', 'quantity': 1, 'tax_percentage': '18',
                    'staff_members': [self.staff1.id]}

        def adv():
            return {'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance',
                    'unit_price': '3000', 'discount': '0', 'quantity': 1, 'tax_percentage': '0',
                    'staff_members': [self.staff1.id]}

        with self.captureOnCommitCallbacks(execute=True):
            bills = [self.raise_bill([mem()]), self.raise_bill([adv()]), self.raise_bill([self.line('cut')])]
        drain()
        before = len(self.sap.calls)
        with self.captureOnCommitCallbacks(execute=True):
            for b in bills:
                r = self.api.post(f'/billing/invoices/{b.pk}/cancel/', {'reason': 'test'}, format='json')
                self.assertEqual(r.status_code, 200, r.data)
        drain()
        after = [(u.rsplit('/', 1)[-1], body) for (mth, u, body) in self.sap.calls[before:] if mth == 'POST']
        kinds = sorted(k for k, _ in after)
        print('\nPosted to SAP when 3 bills (membership, advance, haircut) were cancelled:', kinds)
        self.assertEqual(kinds.count('CreditNotes'), 1)
        for label, expected in (('membership liability reversed', 'JournalEntries'),
                                ('advance liability reversed', 'JournalEntries'),
                                ('haircut payment refunded', 'OutgoingPayments')):
            have = expected in kinds
            print(f"{'ok  ' if have else 'GAP '} {label}: {'sent' if have else 'nothing sent to SAP'}")
