"""
Do billing, the dashboards and Finance say the same thing, to the paisa?

One month of awkward bills is raised through the real API (odd paise, invoice
and line discounts, mixed payments, a value card sold and used, a membership, a
part-paid bill, a cancelled bill, a draft, an advance). Every screen is then read
and compared with figures added up straight from the invoice rows.
"""
import datetime
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client, ClientValueCard
from inventory.models import Product
from marketing.models import Membership, ValueCard
from pos_backend import app_settings
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

Q = Decimal('0.01')


def m(x):
    return Decimal(str(x or 0)).quantize(Q)


class Everything(TestCase):
    def setUp(self):
        from pos_backend.testing import pin_gate_off
        pin_gate_off()                      # these tests spend wallets without caring about the PIN
        cache.clear()
        app_settings.invalidate()
        self.center = Center.objects.create(center_name='Saket', display_name='Saket', gst_enabled=True,
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='Owner',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.asha = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center, gender='Female')
        self.ravi_c = Client.objects.create(first_name='Ravi', phone='9990000002', center=self.center, gender='Male')
        self.staff1 = StaffMember.objects.create(center=self.center, first_name='Meena', designation='Stylist')
        self.staff2 = StaffMember.objects.create(center=self.center, first_name='Karan', designation='Stylist')
        self.today = datetime.date.today()
        self.first = self.today.replace(day=1)
        self.svc = {
            'cut': ServiceMaster.objects.create(name='Haircut', default_price=Decimal('555.55'), tax_percentage=Decimal('18')),
            'thread': ServiceMaster.objects.create(name='Threading', default_price=Decimal('55.61'), tax_percentage=Decimal('18')),
            'massage': ServiceMaster.objects.create(name='Massage', default_price=Decimal('1234.50'), tax_percentage=Decimal('5')),
            'colour': ServiceMaster.objects.create(name='Colour', default_price=Decimal('3999.00'), tax_percentage=Decimal('12')),
        }
        self.product = Product.objects.create(name='Shampoo', price=Decimal('349.00'), mrp=Decimal('349.00'),
                                              gst_percent=Decimal('18'), current_stock=100, center=self.center)
        self.membership = Membership.objects.create(name='Gold', pre_tax_price=Decimal('1000'), post_tax_price=Decimal('1180'),
                                                    gst_percent=Decimal('18'), expiry_days=365, level='Organisation')
        self.card = ValueCard.objects.create(title='Silver', pre_tax_price=Decimal('2000'), post_tax_price=Decimal('2360'),
                                             gst_percent=Decimal('18'), value=Decimal('2500'), expiry_days=365)

    # ── helpers ──────────────────────────────────────────────────────────
    def line(self, key, qty=1, discount='0', staff=None):
        s = self.svc[key]
        return {'content_type': 'services.servicemaster', 'object_id': s.id, 'description': s.name,
                'unit_price': str(s.default_price), 'discount': str(discount), 'quantity': qty,
                'tax_percentage': str(s.tax_percentage), 'staff_members': [(staff or self.staff1).id]}

    def prod_line(self, qty=1, discount='0'):
        return {'content_type': 'inventory.product', 'object_id': self.product.id, 'description': 'Shampoo',
                'unit_price': '349.00', 'discount': str(discount), 'quantity': qty, 'tax_percentage': '18',
                'staff_members': [self.staff1.id]}

    def head(self, client=None, status='paid', discount='0'):
        return {'client': (client or self.asha).id, 'center': self.center.id, 'status': status, 'discount': str(discount)}

    def raise_bill(self, items, client=None, discount='0'):
        res = bill_and_pay(self.api, dict(self.head(client, discount=discount), items=items))
        self.assertEqual(res.status_code, 201, res.data)
        return Invoice.objects.get(pk=res.data['id'])

    def pay(self, items, payments_for, client=None, discount='0', status='paid', **extra):
        """Hold, read the server's total, then complete with `payments_for(total)`."""
        held = self.api.post('/billing/invoices/', dict(self.head(client, 'draft', discount), items=items, **extra), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        total = Decimal(str(held.data.get('paid_now', held.data['total_amount'])))
        done = self.api.put(f"/billing/invoices/{held.data['id']}/", dict(
            self.head(client, status, discount), items=items, payments=payments_for(total), **extra), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        return Invoice.objects.get(pk=done.data['id'])

    def build(self):
        # 1. odd paise, three lines, cash
        self.raise_bill([self.line('cut'), self.line('thread', 3)])
        # 2. invoice discount + line discount, paid in three methods
        self.pay([self.massage_colour_line(), self.line('colour', discount='400.25')],
                 lambda t: [{'payment_method': 'Cash', 'amount': '1000.00'},
                            {'payment_method': 'UPI', 'amount': '1500.50'},
                            {'payment_method': 'Credit Card', 'amount': str(t - Decimal('2500.50'))}],
                 client=self.ravi_c, discount='137')
        # 3. product (MRP inclusive) + service by another staff
        self.raise_bill([self.prod_line(2, '19.99'), self.line('cut', staff=self.staff2)])
        # 4. membership with a discount
        self.raise_bill([{'content_type': 'marketing.membership', 'object_id': self.membership.id, 'description': 'Gold',
                          'unit_price': '1000', 'discount': '100', 'quantity': 1, 'tax_percentage': '18',
                          'staff_members': [self.staff1.id]}])
        # 5. value card sold (cash), then a bill part-settled by the card
        self.raise_bill([{'content_type': 'marketing.valuecard', 'object_id': self.card.id, 'description': 'Silver',
                          'unit_price': '2000', 'discount': '0', 'quantity': 1, 'tax_percentage': '18',
                          'staff_members': [self.staff1.id]}])
        cv = ClientValueCard.objects.get(value_card=self.card)
        quote = self.api.post('/billing/invoices/quote/', dict(
            self.head(), items=[self.line('massage', 2)], value_card_id=cv.id), format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
        pay_now = Decimal(quote.data['payable'])
        self.pay([self.line('massage', 2)],
                 lambda t: ([{'payment_method': 'UPI', 'amount': str(pay_now)}] if pay_now else []),
                 value_card_id=cv.id)
        # 6. a part-paid bill. New bills must be paid in full (#37), so this is an
        # older one: raised in full, then left with only Rs 1,000 collected.
        old = self.raise_bill([self.line('colour')], client=self.ravi_c)
        old.payments.update(amount=Decimal('1000.00'))
        Invoice.objects.filter(pk=old.pk).update(status='partial', paid_amount=Decimal('1000.00'))
        # 7. a bill that is then cancelled
        gone = self.raise_bill([self.line('cut')])
        r = self.api.post(f'/billing/invoices/{gone.pk}/cancel/', {'reason': 'test'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        # 8. a draft left in the till
        self.api.post('/billing/invoices/', dict(self.head(status='draft'), items=[self.line('colour')]), format='json')
        # 9. an advance taken as a line on a bill (how the till sells it), paid in cash
        self.raise_bill([{'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance',
                          'unit_price': '5000.00', 'discount': '0', 'quantity': 1, 'tax_percentage': '0',
                          'staff_members': [self.staff1.id]}])
        # 10. a later bill paid out of that advance
        self.pay([self.line('cut'), self.line('thread', 2)], lambda t: [{'payment_method': 'Advance', 'amount': str(t)}])

    def massage_colour_line(self):
        return self.line('massage')

    def get(self, url, **extra):
        cache.clear()   # the register and monthly screens keep an answer for 60 s
        params = {'start_date': self.first.isoformat(), 'end_date': self.today.isoformat(),
                  'center_id': self.center.id}
        params.update(extra)
        r = self.api.get(url, params)
        self.assertEqual(r.status_code, 200, (url, r.data))
        return r.data

    # ── the check ────────────────────────────────────────────────────────
    def test_report(self):
        self.build()
        live = list(Invoice.objects.filter(status__in=['paid', 'partial']))
        problems = []

        def check(label, shown, truth):
            shown, truth = m(shown), m(truth)
            ok = shown == truth
            print(f"{'ok  ' if ok else 'DIFF'} {label:58s} shown {shown:>12}  truth {truth:>12}"
                  + ('' if ok else f'  (off by {shown - truth})'))
            if not ok:
                problems.append(label)

        for inv in live:
            items = list(inv.items.all())
            tax_i = m(inv.cgst) + m(inv.sgst) + m(inv.igst)
            check(f'inv {inv.pk} sub-disc+tax+round = total', m(inv.subtotal) - m(inv.discount) + tax_i + m(inv.rounding), inv.total_amount)
            check(f'inv {inv.pk} sum(item price) = subtotal', sum(m(i.total_price) for i in items), inv.subtotal)
            check(f'inv {inv.pk} sum(item tax) = cgst+sgst', sum(m(i.tax_amount) for i in items), tax_i)
            check(f'inv {inv.pk} sum(bill share) = discount', sum(m(i.bill_discount_share) for i in items), inv.discount)
            check(f'inv {inv.pk} payments = paid_amount', sum(m(p.amount) for p in inv.payments.filter(is_tip=False)), inv.paid_amount)

        def T(f):
            return sum(m(getattr(i, f)) for i in live)
        total, sub, disc = T('total_amount'), T('subtotal'), T('discount')
        tax = T('cgst') + T('sgst') + T('igst')
        rnd = T('rounding')
        print(f'\nTRUTH  total {total}  subtotal {sub}  invoice discount {disc}  tax {tax}  rounding {rnd}  collected {T("paid_amount")}\n')

        cat = {}
        adv_lines = Decimal(0)
        for inv in live:
            for it in inv.items.select_related('content_type'):
                if it.is_redemption:
                    continue
                k = it.content_type.model
                if k == 'advancepayment':
                    adv_lines += m(it.total_price)
                    continue
                cat[k] = cat.get(k, Decimal(0)) + m(it.total_price)

        pays = {}
        for inv in live:
            for p in inv.payments.filter(is_tip=False):
                pays[p.payment_method] = pays.get(p.payment_method, Decimal(0)) + m(p.amount)
        liab = pays.get('Advance', 0) + pays.get('Value Card', 0) + pays.get('Cashback Wallet', 0)
        d = self.get('/salon_admin/api/dashboard/summary/')
        check('dashboard revenue (net of card/advance payments)', d['revenue'], total - liab)
        mi = d['money_in']
        check('dashboard gross billed', mi['gross_billed'], total)
        check('dashboard redeemed', mi['redeemed']['total'], liab)
        check('dashboard bills counted', mi['bills'], len(live))
        check('dashboard tax', sum(m(v) for v in mi['tax'].values()), tax)
        check('dashboard round-off', mi['round_off'], rnd)
        check('dashboard bill-level discount', mi['discounts']['bill_level'], disc)
        check('dashboard real money received', mi['real_money_received'], sum(v for k, v in pays.items() if k not in ('Advance', 'Value Card', 'Cashback Wallet')))
        check('dashboard to collect', mi['to_collect'], total - T('paid_amount'))
        check('dashboard cancelled count', mi['cancelled']['count'], 1)
        check('dashboard payment split adds to payments', sum(m(x['amount']) for x in mi['payments']), sum(pays.values()))
        check('dashboard revenue_without_tax', d['revenue_without_tax'], total - liab - tax - rnd)
        rb = d['revenue_breakdown']
        check('dashboard breakdown services', rb['service'], cat.get('servicemaster', 0))
        check('dashboard breakdown products', rb['product'], cat.get('product', 0))
        check('dashboard breakdown memberships', rb['membership'], cat.get('membership', 0))
        check('dashboard breakdown value cards', rb['card'], cat.get('valuecard', 0))
        check('dashboard breakdown advances (money received)', rb['advance'], 5000)
        revs = self.get('/salon_admin/api/dashboard/revenues/')
        check('dashboard daily total', sum(m(x['revenue']) for x in revs['daily']), total - liab)
        check('dashboard hourly total', sum(m(x['revenue']) for x in revs['hourly']), total - liab)
        check('dashboard monthly total', sum(m(x['revenue']) for x in revs['monthly']), total - liab)
        fin = self.get('/salon_admin/api/dashboard/finance/')
        mon = fin[next(iter(fin))]
        check('dashboard finance services', mon['services']['revenue'], cat.get('servicemaster', 0))
        check('dashboard finance value cards', mon['value_cards']['revenue'], cat.get('valuecard', 0))
        sp = self.get('/salon_admin/api/dashboard/services_products/')
        check('dashboard services table total', sum(m(x['revenue']) for x in sp['services']), cat.get('servicemaster', 0))
        check('dashboard products table total', sum(m(x['revenue']) for x in sp['products']), cat.get('product', 0))

        reg = self.get('/finance/api/register_summary/')
        rv = reg['revenues']
        check('register services', rv['services']['amount'], cat.get('servicemaster', 0))
        check('register products', rv['products']['amount'], cat.get('product', 0))
        check('register memberships', rv['memberships']['amount'], cat.get('membership', 0))
        check('register value cards', rv['value_cards']['amount'], cat.get('valuecard', 0))
        check('register advances', rv['advances']['amount'], 5000)
        check('register discounts', -m(rv['discounts']['amount']), disc)
        check('register total tax', rv['total_tax'], tax)
        check('register tax parts add to total tax',
              sum(m(rv[k]['tax']) for k in ('services', 'products', 'value_cards', 'memberships', 'packages', 'other')), tax)
        check('register taxable value', rv['taxable_value'], sub - adv_lines - disc)
        pm = reg['payment_methods']
        check('register cash', pm['cash']['amount'], pays.get('Cash', 0))
        check('register upi', pm['upi']['amount'], pays.get('UPI', 0))
        check('register credit card', pm['credit_card']['amount'], pays.get('Credit Card', 0))
        check('register value card', pm['value_card']['amount'], pays.get('Value Card', 0))
        rc = reg['reconciliation']
        check('register total_billed', rc['total_billed'], total)
        check('register total_collected', rc['total_collected'], T('paid_amount'))
        check('register receivable', rc['outstanding_receivable'], total - T('paid_amount'))
        check('register incl-tax = billed - liability payments', rv['including_tax'], total - liab)
        check('dashboard revenue = register incl-tax', d['revenue'], rv['including_tax'])
        real = sum(v for k, v in pays.items() if k not in ('Advance', 'Value Card', 'Cashback Wallet'))
        check('register real money received', rc['real_money_received'], real)
        check('register expected settlement', rc['expected_settlement'], real)
        check('register advance redemptions', reg['payment_redemptions']['advance'], pays.get('Advance', 0))

        from finance import closing
        day = closing.compute_day(self.center.pk, self.today)
        check('day closing system cash', day['system_cash'], pays.get('Cash', 0))
        check('day closing upi', day['upi'], pays.get('UPI', 0))
        check('day closing credit card', day['credit_card'], pays.get('Credit Card', 0))

        inc = self.get('/finance/api/reports/incentive-calculation/', frequency='monthly')
        check('incentives: services billed = Finance services', sum(m(r['services_billed']) for r in inc), cat.get('servicemaster', 0))
        check('incentives: services earning + paid from cards = billed',
              sum(m(r['services_revenue']) + m(r['services_from_cards']) for r in inc), sum(m(r['services_billed']) for r in inc))
        check('incentives: products billed = Finance products', sum(m(r['products_billed']) for r in inc), cat.get('product', 0))
        check('incentives: cards sold = Finance value cards', sum(m(r['cards_revenue']) for r in inc), cat.get('valuecard', 0))
        check('incentives: services paid from cards = card/advance payments on service bills',
              sum(m(r['services_from_cards']) for r in inc) + sum(m(r['products_from_cards']) for r in inc),
              sum(m(i.total_price) for i in __import__('billing.models', fromlist=['InvoiceItem']).InvoiceItem.objects.filter(
                  invoice__in=live, is_redemption=False, invoice__payments__payment_method__in=['Advance', 'Value Card'])
                  .filter(content_type__model__in=['servicemaster', 'product']).distinct()))

        mo = self.get('/finance/api/monthly_sales/')
        row = mo[0] if isinstance(mo, list) else mo['results'][0]
        check('monthly services', row['services'], cat.get('servicemaster', 0))
        check('monthly total tax', row['total_tax'], tax)
        check('monthly taxable value', row['taxable_value'], sub - adv_lines - disc)
        check('monthly collection_before_tax = register', row['collection_before_tax'], rv['collection_before_tax'])
        check('monthly including_tax = register', row['including_tax'], rv['including_tax'])

        det = self.get('/finance/api/detailed_revenues/')
        rows = det['results']
        check('detailed grand total', sum(m(x['grand_total']) for x in rows), total)
        check('detailed gst', sum(m(x['total_gst']) for x in rows), tax)
        check('detailed net + gst = grand (all bills)', sum(m(x['net']) + m(x['total_gst']) - m(x['grand_total']) for x in rows), 0)

        tx = self.get('/finance/api/reports/tax/')
        check('tax report total', tx['summary']['total_tax'], tax)
        check('tax report b2c total', tx['b2c']['total'], total)
        check('tax report b2c taxable (advance lines included at 0%)', tx['b2c']['taxable_value'], sub - disc)
        check('tax report rate-wise taxable', sum(m(x['taxable_value']) for x in tx['rate_wise']), sub - disc)
        check('tax report rate-wise tax', sum(m(x['tax']) for x in tx['rate_wise']), tax)

        print('\nPROBLEMS:', problems)


class Reversals(Everything):
    """Cancelled bills must leave no trace in the money screens."""
    test_report = None

    def seen(self, label, shown, truth):
        shown, truth = m(shown), m(truth)
        print(f"{'ok  ' if shown == truth else 'DIFF'} {label:62s} shown {shown:>10}  truth {truth:>10}")
        self.problems.append(label) if shown != truth else None

    def test_cancelled_advance_and_card_bills(self):
        self.problems = []
        # An advance is sold (5000), part of it spent on a bill, then that bill is cancelled.
        self.raise_bill([{'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance',
                          'unit_price': '5000.00', 'discount': '0', 'quantity': 1, 'tax_percentage': '0',
                          'staff_members': [self.staff1.id]}])
        spent = self.pay([self.line('cut')], lambda t: [{'payment_method': 'Advance', 'amount': str(t)}])
        r = self.api.post(f'/billing/invoices/{spent.pk}/cancel/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        reg = self.get('/finance/api/register_summary/')
        d = self.get('/salon_admin/api/dashboard/summary/')
        self.seen('register advances received after a spent-advance bill is cancelled', reg['revenues']['advances']['amount'], 5000)
        self.seen('register advance redemptions after that cancel', reg['payment_redemptions']['advance'], 0)
        self.seen('dashboard advances after that cancel', d['revenue_breakdown']['advance'], 5000)

        # The bill that SOLD the advance is cancelled too (allowed while unspent).
        sold = Invoice.objects.filter(items__content_type__model='advancepayment').first()
        r = self.api.post(f'/billing/invoices/{sold.pk}/cancel/', {'reason': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        reg = self.get('/finance/api/register_summary/')
        d = self.get('/salon_admin/api/dashboard/summary/')
        self.seen('register advances after the selling bill is cancelled', reg['revenues']['advances']['amount'], 0)
        self.seen('register advance redemptions after that', reg['payment_redemptions']['advance'], 0)
        self.seen('register incl tax with everything cancelled', reg['revenues']['including_tax'], 0)
        self.seen('dashboard revenue with everything cancelled', d['revenue'], 0)
        self.seen('dashboard advances with everything cancelled', d['revenue_breakdown']['advance'], 0)
        print('PROBLEMS:', self.problems)


class Branches(Everything):
    """Several branches, a restricted user, day boundaries, packages."""
    test_report = None

    def seen(self, label, shown, truth):
        shown, truth = m(shown), m(truth)
        print(f"{'ok  ' if shown == truth else 'DIFF'} {label:62s} shown {shown:>10}  truth {truth:>10}")
        if shown != truth:
            self.problems.append(label)

    def test_branches_scope_and_edges(self):
        self.problems = []
        # a second branch that does not charge GST
        noida = Center.objects.create(center_name='Noida', display_name='Noida', gst_enabled=False,
                                      sap_warehouse_code='WH-NOIDA')
        nclient = Client.objects.create(first_name='Neha', phone='9990000009', center=noida)
        nstaff = StaffMember.objects.create(center=noida, first_name='Zoya', designation='Stylist')
        a = self.raise_bill([self.line('cut'), self.line('thread', 3)])
        res = bill_and_pay(self.api, {'client': nclient.id, 'center': noida.id, 'status': 'paid', 'discount': '0',
                                      'items': [dict(self.line('massage'), staff_members=[nstaff.id])]})
        self.assertEqual(res.status_code, 201, res.data)
        b = Invoice.objects.get(pk=res.data['id'])
        self.assertEqual(m(b.cgst) + m(b.sgst), 0)

        both = self.get('/salon_admin/api/dashboard/summary/', center_id='')
        one = self.get('/salon_admin/api/dashboard/summary/', center_id=self.center.id)
        two = self.get('/salon_admin/api/dashboard/summary/', center_id=noida.id)
        self.seen('dashboard: all branches = Saket + Noida', both['revenue'], m(one['revenue']) + m(two['revenue']))
        self.seen('dashboard Saket = its bill', one['revenue'], a.total_amount)
        self.seen('dashboard Noida = its bill (no GST)', two['revenue'], b.total_amount)
        r_all = self.get('/finance/api/register_summary/', center_id='')
        r_one = self.get('/finance/api/register_summary/', center_id=self.center.id)
        r_two = self.get('/finance/api/register_summary/', center_id=noida.id)
        for k in ('including_tax', 'total_tax', 'taxable_value', 'collection_before_tax', 'rounding'):
            sh = r_all['revenues'][k]
            sh = sh['amount'] if isinstance(sh, dict) else sh
            parts = [x['revenues'][k]['amount'] if isinstance(x['revenues'][k], dict) else x['revenues'][k] for x in (r_one, r_two)]
            self.seen(f'register {k}: all = Saket + Noida', sh, sum(m(x) for x in parts))
        self.seen('Noida charged no tax', r_two['revenues']['total_tax'], 0)

        # a user who can only see Saket
        role = Role.objects.create(name='Saket only', permissions={k: v for k, v in full_access_permissions().items() if k != 'all_centers'})
        lim = CustomUser.objects.create_user(email='s@test.com', password='pw', full_name='S', role=role, center=self.center)
        lim.centers.set([self.center])
        api = APIClient(); api.force_authenticate(lim)
        cache.clear()
        got = api.get('/salon_admin/api/dashboard/summary/', {'start_date': self.first.isoformat(), 'end_date': self.today.isoformat(), 'center_id': noida.id}).data
        self.seen('restricted user asking for Noida sees', got['revenue'], 0)
        got = api.get('/finance/api/register_summary/', {'start_date': self.first.isoformat(), 'end_date': self.today.isoformat()}).data
        self.seen('restricted user, no branch picked: sees only Saket', got['revenues']['including_tax'], r_one['revenues']['including_tax'])

        # day boundaries: one bill at 23:59:59 yesterday, one at 00:00:00 today
        yesterday = self.today - datetime.timedelta(days=1)
        edge = self.raise_bill([self.line('cut')])
        Invoice.objects.filter(pk=edge.pk).update(created_at=datetime.datetime.combine(yesterday, datetime.time(23, 59, 59)))
        edge2 = self.raise_bill([self.line('cut')])
        Invoice.objects.filter(pk=edge2.pk).update(created_at=datetime.datetime.combine(self.today, datetime.time(0, 0, 0)))
        for label, day, want in (('yesterday', yesterday, edge.total_amount), ('today', self.today, a.total_amount + edge2.total_amount)):
            q = dict(start_date=day.isoformat(), end_date=day.isoformat(), center_id=self.center.id)
            self.seen(f'dashboard revenue for {label} only', self.get('/salon_admin/api/dashboard/summary/', **q)['revenue'], want)
            self.seen(f'register incl tax for {label} only', self.get('/finance/api/register_summary/', **q)['revenues']['including_tax'], want)
            det = self.get('/finance/api/detailed_revenues/', **q)
            self.seen(f'detailed bills for {label} only', sum(m(x['grand_total']) for x in det['results']), want)
            from finance import closing
            self.seen(f'day closing cash for {label}', closing.compute_day(self.center.pk, day)['system_cash'], want)
        print('PROBLEMS:', self.problems)


class Packages(Everything):
    """Selling a package, then using it."""
    test_report = None

    def test_package_sale_and_use(self):
        from marketing.models import Package
        pkg = Package.objects.create(name='Cut x5', price=Decimal('2000.00'), validity_days=365,
                                     services_json=[{'id': self.svc['cut'].id, 'pkgQty': 5}])
        sale = self.raise_bill([{'content_type': 'marketing.package', 'object_id': pkg.id, 'description': 'Cut x5',
                                 'unit_price': '2000', 'discount': '0', 'quantity': 1, 'tax_percentage': '0',
                                 'staff_members': [self.staff1.id]}])
        use = dict(self.line('cut'), is_redemption=True, description='\U0001F381 [Redeem] Haircut')
        used = self.raise_bill([use, self.line('thread')])
        print('sale total', sale.total_amount, 'use total', used.total_amount, 'status', used.status)
        d = self.get('/salon_admin/api/dashboard/summary/')
        sp = self.get('/salon_admin/api/dashboard/services_products/')
        reg = self.get('/finance/api/register_summary/')
        print('dash top_services', [(x['name'], x['count'], x['revenue']) for x in d['top_services']])
        print('dash services table', [(x['name'], x['count'], x['revenue']) for x in sp['services']])
        print('dash packages', d['revenue_breakdown']['package'], [(x['name'], x['count'], x['revenue']) for x in d['top_packages']])
        print('register packages', reg['revenues']['packages'], 'services', reg['revenues']['services'])
        sl = self.get('/finance/api/reports/services/')
        print('services drilldown', str(sl)[:700])
        from staff.models import ServiceLog
        print('service logs', [(l.service_name.encode('ascii','replace').decode(), str(l.price)) for l in ServiceLog.objects.all()])
        self.assertEqual(used.total_amount, m(self.svc['thread'].default_price * Decimal('1.18')).quantize(Decimal('1')))
