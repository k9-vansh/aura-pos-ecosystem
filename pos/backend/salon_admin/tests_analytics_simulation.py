"""Six months of salon activity, created through the real billing endpoints, then every
analytics report checked against independent calculations.

Slow (minutes), so it only runs when asked:

    ANALYTICS_SIM=1 python manage.py test salon_admin.tests_analytics_simulation --settings=pos_backend.settings_test
    (SIM_DAYS=20 for a quick run; default 180)

What is created, all through POST /billing/invoices/ like the till: services with line and bill
discounts, products, value-card sales and redemptions, membership sales and member bills,
package sales and redemptions, advance sales (GST on at one centre, off at the other) and
advances spent, part-paid bills, cancellations. Each bill is then back-dated, with its
payments, wallet rows and perks, to a day in the last six months.

The checks are two kinds:
  * the reports against a slow, separately written calculation of the same definitions;
  * conservation rules that tie the reports to real money - e.g. "received at the desk" must equal
    the payments actually taken on the bills, less what part-paid bills still owe.
"""
import calendar
import datetime as dt
import os
import random
import unittest
from collections import defaultdict
from decimal import Decimal

from django.db.models import F
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing import wallets
from billing.models import (AdvancePayment, CashbackTransaction, Invoice, InvoiceItem, InvoiceRefund, Payment)
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from inventory.models import Product, StockTransaction
from marketing.models import Membership, Package, ValueCard
from pos_backend import app_settings
from pos_backend.money import money, quantize_ratio
from pos_backend.models import AppSetting
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import ServiceLog, StaffMember

D = Decimal
SIM = os.environ.get('ANALYTICS_SIM') == '1'
DAYS = int(os.environ.get('SIM_DAYS', '180'))
WALLET_METHODS = ('Advance', 'Value Card', 'Cashback Wallet')
FAILS, NOTES = [], []


def expect(label, got, want, tol=0.011):
    ok = abs(float(got or 0) - float(want or 0)) <= tol
    if not ok:
        FAILS.append(f'{label}: report={got} expected={want}')
    return ok


@unittest.skipUnless(SIM, 'slow: set ANALYTICS_SIM=1 to run the six-month simulation')
class SixMonthSimulation(TestCase):

    # ── data ─────────────────────────────────────────────────────────────
    @classmethod
    def setUpTestData(cls):
        cls.rnd = rnd = random.Random(20261020)
        cls.today = dt.date.today()
        cls.A = Center.objects.create(center_name='A Pvt', display_name='Saket', gst_number='07AAAAA0000A1Z5',
                                      sap_warehouse_code='WA', monthly_target=D('1500000'))
        cls.B = Center.objects.create(center_name='B Pvt', display_name='Noida', gst_number='09CCCCC0000C1Z5',
                                      sap_warehouse_code='WB', monthly_target=D('800000'))
        # Charge GST on advances at B (the default), not at A.
        AppSetting.objects.update_or_create(key='billing.gst_on_advance', scope=app_settings.branch_scope(cls.A),
                                            defaults={'value': 'false', 'center': cls.A})
        app_settings.invalidate()
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        cls.owner = CustomUser.objects.create_user(email='sim@test.com', password='x', full_name='Sim', role=role,
                                                   center=cls.A, is_superuser=True)
        cls.api = APIClient()
        cls.api.force_authenticate(cls.owner)
        cls.staff = {c.pk: StaffMember.objects.create(center=c, first_name='Ravi', designation='Stylist')
                     for c in (cls.A, cls.B)}

        catalogue = {
            'Hair Cut': [('Haircut Women', 1200, 5), ('Haircut Men', 800, 5), ('Beard Trim', 400, 5)],
            'Hair Color': [('Root Touchup', 2400, 18), ('Global Colour', 4500, 18), ('Highlights', 6000, 18)],
            'Body Waxing': [('Arms Wax', 700, 5), ('Legs Wax', 1100, 5)],
            'Threading': [('Eyebrows', 100, 5), ('Upper Lip', 60, 5)],
            'Manicure / Pedicure': [('Pedicure', 1500, 5), ('Manicure', 900, 5)],
            'Kerastase Ritual': [('Fusio Dose', 2600, 18), ('Express Ritual', 2000, 18)],
            'Facial': [('Glow Facial', 2400, 18), ('Cleanup', 1500, 18)],
        }
        cls.services = [ServiceMaster.objects.create(name=n, category=cat, default_price=D(p), tax_percentage=D(t))
                        for cat, rows in catalogue.items() for n, p, t in rows]
        cls.products = {}
        for c in (cls.A, cls.B):
            cls.products[c.pk] = [Product.objects.create(
                name=f'{n}', brand=b, category='Retail', price=D(m), mrp=D(m), gst_percent=D('18'),
                current_stock=D('800'), center=c, product_code=f'{n[:3].upper()}{c.pk}')
                for n, b, m in [('Shampoo 250ml', 'Kerastase', 1180), ('Serum 90ml', 'Kerastase', 2360),
                                ('Hair Mask 200ml', 'Olaplex', 1770), ('Conditioner', 'Thalgo', 944),
                                ('Hair Oil', 'LP', 590), ('Face Wash', 'Thalgo', 708)]]
        cls.cards = [ValueCard.objects.create(title='Silver', pre_tax_price=D('4000'), post_tax_price=D('4200'),
                                              value=D('5000'), gst_percent=D('5'), expiry_days=365),
                     ValueCard.objects.create(title='Gold', pre_tax_price=D('7500'), post_tax_price=D('7875'),
                                              value=D('10000'), gst_percent=D('5'), expiry_days=365),
                     ValueCard.objects.create(title='Short', pre_tax_price=D('2000'), post_tax_price=D('2100'),
                                              value=D('2500'), gst_percent=D('5'), expiry_days=60)]
        cls.memberships = [Membership.objects.create(name='Gold', pre_tax_price=D('2000'), gst_percent=D('18'),
                                                     discount_percent=D('20'), expiry_days=90),
                           Membership.objects.create(name='Silver', pre_tax_price=D('1000'), gst_percent=D('18'),
                                                     discount_percent=D('10'), expiry_days=45)]
        cls.package = Package.objects.create(name='Haircut x5', price=D('5000'), gst_percent=D('5'), validity_days=120,
                                             services_json=[{'id': cls.services[0].id, 'pkgQty': 5}])
        cls.clients = [Client.objects.create(
            center=rnd.choice((cls.A, cls.B)), first_name=f'Client{i}', phone=f'98{i:08d}',
            gender=rnd.choice(('female', 'female', 'male', 'female', ''))) for i in range(1, 161)]
        cls.created, cls.skipped, cls.cancelled, cls.kinds = 0, defaultdict(int), [], defaultdict(int)

        t0 = dt.datetime.now()
        for back in range(DAYS, -1, -1):
            day = cls.today - dt.timedelta(days=back)
            n = rnd.randint(5, 11) + (4 if day.weekday() >= 5 else 0)
            for _ in range(n):
                cls._make_bill(day)
        cls._checkouts()
        # Clients are registered at their first visit, as in real life, so "new in the period" means something.
        first_bill = {}
        for inv in Invoice.objects.exclude(client__isnull=True).order_by('created_at'):
            first_bill.setdefault(inv.client_id, inv.created_at)
        for c in cls.clients:
            Client.objects.filter(pk=c.pk).update(created_at=first_bill.get(c.pk, dt.datetime.combine(
                cls.today - dt.timedelta(days=cls.rnd.randint(0, DAYS)), dt.time(9, 0))))
        NOTES.append(f'{cls.created} bills created over {DAYS} days in {(dt.datetime.now() - t0).seconds}s; '
                     f'kinds: {dict(cls.kinds)}; cancelled: {len(cls.cancelled)}; refused: {dict(cls.skipped)}')
        print('\n' + NOTES[-1])

    # ── one bill ─────────────────────────────────────────────────────────
    @classmethod
    def _svc_line(cls, centre, svc, qty=1, disc=None, redemption=False):
        price = D(svc.default_price)
        return {'content_type': 'services.servicemaster', 'object_id': svc.id, 'description': svc.name,
                'unit_price': '0.00' if redemption else str(price), 'discount': str(disc or D('0')),
                'quantity': qty, 'staff_members': [cls.staff[centre.pk].id], **({'is_redemption': True} if redemption else {})}

    @classmethod
    def _prod_line(cls, centre, prod, qty=1):
        return {'content_type': 'inventory.product', 'object_id': prod.id, 'description': prod.name,
                'unit_price': str(prod.mrp), 'discount': '0.00', 'quantity': qty, 'staff_members': [cls.staff[centre.pk].id]}

    @classmethod
    def _post(cls, payload, pay_fraction=1.0):
        q = cls.api.post('/billing/invoices/quote/', payload, format='json')
        if q.status_code != 200:
            return None, f'quote {q.status_code}'
        payable = D(str(q.data.get('payable', q.data['total_amount'])))
        pay = (payable * D(str(pay_fraction))).quantize(D('0.01'))
        pays = []
        if pay > 0:
            methods = cls.rnd.choice((['Cash'], ['UPI'], ['Credit Card'], ['Debit Card'], ['UPI', 'Cash'], ['Cash', 'Credit Card']))
            if len(methods) == 1 or pay < 20:
                pays = [{'payment_method': methods[0], 'amount': str(pay)}]
            else:
                first = (pay * D('0.4')).quantize(D('0.01'))
                pays = [{'payment_method': methods[0], 'amount': str(first)},
                        {'payment_method': methods[1], 'amount': str(pay - first)}]
        if payload.get('_advance_pay'):
            adv = min(D(str(payload.pop('_advance_pay'))), pay)
            if adv > 0:
                rest = pay - adv
                pays = [{'payment_method': 'Advance', 'amount': str(adv)}] + (
                    [{'payment_method': 'Cash', 'amount': str(rest)}] if rest > 0 else [])
        body = {k: v for k, v in payload.items() if not k.startswith('_')}
        r = cls.api.post('/billing/invoices/', dict(body, status='paid', payments=pays), format='json')
        if r.status_code != 201:
            return None, f'create {r.status_code}: {str(r.data)[:90]}'
        return r.data, None

    @classmethod
    def _make_bill(cls, day):
        rnd = cls.rnd
        centre = cls.A if rnd.random() < 0.62 else cls.B
        client = rnd.choice(cls.clients) if rnd.random() > 0.08 else None
        kind = rnd.random()
        if rnd.random() < 0.35:
            # Favour clients who hold something to use, so cards, memberships and packages get real use.
            today = cls.today
            pool = (list(ClientValueCard.objects.filter(center=centre, is_active=True, balance__gt=200, expiry_date__gt=today).values_list('client_id', flat=True))
                    + list(ClientMembership.objects.filter(center=centre, is_active=True, expiry_date__gt=today).values_list('client_id', flat=True))
                    + list(ClientPackage.objects.filter(center=centre, is_active=True, expiry_date__gt=today).values_list('client_id', flat=True)))
            if pool:
                client = Client.objects.get(pk=rnd.choice(pool))
                kind = rnd.uniform(0.115, 0.34)
        base = {'client': client.id if client else None, 'center': centre.pk, 'discount': '0.00'}
        items, extra, fraction, label = [], {}, 1.0, 'normal'
        live_card = live_mem = pkg = None
        if client:
            live_card = ClientValueCard.objects.filter(client=client, center=centre, is_active=True, balance__gt=200,
                                                       expiry_date__gt=cls.today).first()
            live_mem = ClientMembership.objects.filter(client=client, center=centre, is_active=True,
                                                       expiry_date__gt=cls.today).first()
            pkg = ClientPackage.objects.filter(client=client, center=centre, is_active=True,
                                               expiry_date__gt=cls.today).first()
        advance_bal = wallets.advance_balance(client.id) if client else D('0')

        def some_services(n=None):
            return [cls._svc_line(centre, s, disc=(D(s.default_price) * D('0.1')).quantize(D('0.01')) if rnd.random() < 0.15 else None)
                    for s in rnd.sample(cls.services, n or rnd.choice((1, 1, 2, 3)))]

        if client and kind < 0.035:
            label = 'valuecard_sale'
            c = rnd.choice(cls.cards)
            items = [{'content_type': 'marketing.valuecard', 'object_id': c.id, 'description': c.title,
                      'unit_price': str(c.pre_tax_price), 'discount': '0.00', 'quantity': 1, 'staff_members': [cls.staff[centre.pk].id]}]
        elif client and kind < 0.065:
            label = 'membership_sale'
            m = rnd.choice(cls.memberships)
            items = [{'content_type': 'marketing.membership', 'object_id': m.id, 'description': m.name,
                      'unit_price': str(m.pre_tax_price), 'discount': '0.00', 'quantity': 1, 'staff_members': [cls.staff[centre.pk].id]}]
        elif client and kind < 0.085:
            label = 'package_sale'
            items = [{'content_type': 'marketing.package', 'object_id': cls.package.id, 'description': cls.package.name,
                      'unit_price': str(cls.package.price), 'discount': '0.00', 'quantity': 1, 'staff_members': [cls.staff[centre.pk].id]}]
        elif client and kind < 0.115:
            label = 'advance_sale'
            amount = rnd.choice(('2000.00', '5000.00', '10000.00'))
            items = [{'content_type': 'billing.advancepayment', 'object_id': None, 'description': 'Advance Payment',
                      'unit_price': amount, 'quantity': 1, 'tax_percentage': '5.00', 'staff_members': [cls.staff[centre.pk].id]}]
        elif live_card and kind < 0.22:
            label = 'card_redeem'
            items = some_services()
            extra['value_card_id'] = live_card.id
        elif live_mem and kind < 0.30:
            label = 'member_bill'
            items = some_services()
            extra['membership_id'] = live_mem.id
        elif pkg and kind < 0.34:
            label = 'package_redeem'
            sid = next(iter(pkg.services_remaining), None)
            svc = next((s for s in cls.services if str(s.id) == sid and pkg.services_remaining[sid] > 0), None)
            items = [cls._svc_line(centre, svc, redemption=True)] if svc else some_services()
        elif client and advance_bal > 300 and kind < 0.40:
            label = 'advance_pay'
            items = some_services()
            extra['_advance_pay'] = str(min(advance_bal, D('1500')))
        else:
            items = some_services()
            if rnd.random() < 0.25:
                items.append(cls._prod_line(centre, rnd.choice(cls.products[centre.pk]), rnd.choice((1, 1, 2))))
            if rnd.random() < 0.05:
                base['discount'] = str(rnd.choice((50, 100, 200)))
        data, err = cls._post({**base, 'items': items, **extra}, fraction)
        if err:
            cls.skipped[f'{label}: {err[:40]}'] += 1
            return
        cls.created += 1
        cls.kinds[label] += 1
        when = dt.datetime.combine(day, dt.time(rnd.choice((10, 11, 11, 12, 13, 14, 15, 16, 17, 17, 18, 18, 19, 20)), rnd.randint(0, 59)))
        if when > dt.datetime.now():
            when = dt.datetime.now() - dt.timedelta(minutes=5)
        # Bills can only be cancelled on the day they are made, so cancel now, before back-dating.
        cancelled = False
        if rnd.random() < 0.04 and label in ('normal', 'card_redeem', 'package_sale', 'member_bill', 'valuecard_sale'):
            r = cls.api.post(f"/billing/invoices/{data['id']}/cancel/", {'reason': 'entered wrongly'}, format='json')
            if r.status_code == 200:
                cancelled = True
                cls.cancelled.append(data['id'])
            else:
                cls.skipped[f'cancel refused ({label})'] += 1
        cls._backdate(data['id'], when)
        # Older data may hold part-paid bills (the till now insists on full payment, #37): make a few.
        if not cancelled and label == 'normal' and rnd.random() < 0.04:
            pays = list(Payment.objects.filter(invoice_id=data['id']).order_by('-id'))
            inv = Invoice.objects.get(pk=data['id'])
            if len(pays) >= 1 and inv.paid_amount == inv.total_amount and all(p.payment_method not in WALLET_METHODS for p in pays):
                last = pays[0]
                if last.amount < inv.total_amount:
                    Invoice.objects.filter(pk=inv.pk).update(paid_amount=inv.paid_amount - last.amount, status='partial')
                    last.delete()
                    cls.kinds['part-paid (injected)'] += 1

    @classmethod
    def _backdate(cls, invoice_id, when):
        inv = Invoice.objects.get(pk=invoice_id)
        shift_days = (when.date() - inv.created_at.date()).days
        Invoice.objects.filter(pk=invoice_id).update(created_at=when, **({'finalized_at': when} if inv.finalized_at else {}))
        Payment.objects.filter(invoice_id=invoice_id).update(created_at=when)
        AdvancePayment.objects.filter(invoice_id=invoice_id).update(created_at=when)
        CashbackTransaction.objects.filter(invoice_id=invoice_id).update(created_at=when)
        InvoiceRefund.objects.filter(invoice_id=invoice_id).update(refunded_on=when.date())
        ServiceLog.all_objects.filter(invoice_id=invoice_id).update(date=when.date(), time=when.time())
        for model in (ClientMembership, ClientValueCard, ClientPackage):
            for row in model.objects.filter(source_invoice_id=invoice_id):
                model.objects.filter(pk=row.pk).update(
                    created_at=when, start_date=row.start_date + dt.timedelta(days=shift_days),
                    expiry_date=row.expiry_date + dt.timedelta(days=shift_days))

    @classmethod
    def _checkouts(cls):
        """A few backbar check-outs, back-dated."""
        for _ in range(40):
            c = cls.rnd.choice((cls.A, cls.B))
            p = cls.rnd.choice(cls.products[c.pk])
            row = StockTransaction.objects.create(product=p, center=c, transaction_type='CHECKOUT', quantity_change=D(-cls.rnd.randint(1, 3)))
            StockTransaction.objects.filter(pk=row.pk).update(
                created_at=dt.datetime.combine(cls.today - dt.timedelta(days=cls.rnd.randint(0, DAYS)), dt.time(12, 0)))

    def setUp(self):
        # SIM_MUTATE=gross_advance puts the old advance-GST double count back, to prove the checks catch it.
        if os.environ.get('SIM_MUTATE') == 'gross_advance':
            from unittest import mock
            from django.db.models import Sum as _Sum, Q as _Q
            from salon_admin import analytics_endpoints as ae
            from salon_admin.dashboard_endpoints import _scope_advances

            def gross(request, start, end):
                qs = _scope_advances(request, AdvancePayment.objects.all()).filter(
                    created_at__gte=start, created_at__lt=end + dt.timedelta(days=1))
                a = qs.aggregate(pos=_Sum('amount', filter=_Q(amount__gt=0)), neg=_Sum('amount', filter=_Q(amount__lt=0)))
                return D(str(a['pos'] or 0)), qs.filter(amount__gt=0).count(), abs(D(str(a['neg'] or 0)))
            patcher = mock.patch.object(ae, '_advance_ledger', gross)
            patcher.start()
            self.addCleanup(patcher.stop)

    # ── independent calculation ──────────────────────────────────────────
    def window(self, start, end, centre=None):
        qs = Invoice.objects.filter(status__in=('paid', 'partial'), created_at__gte=start,
                                    created_at__lt=end + dt.timedelta(days=1))
        return qs.filter(center=centre) if centre else qs

    def ledger(self, start, end, centre=None):
        rows = [r for r in AdvancePayment.objects.filter(created_at__gte=start, created_at__lt=end + dt.timedelta(days=1))
                .select_related('invoice') if not (r.invoice and r.invoice.status in ('cancelled', 'refunded'))]
        # a compensating row and the row it compensates are both left out (as Finance does)
        reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
        rows = [r for r in rows if not r.reverses_id and r.pk not in reversed_ids]
        if centre:
            rows = [r for r in rows if (r.center_id or (r.client.center_id if r.client_id else None)) == centre.pk]
        pos = sum((r.amount - (r.gst_amount if r.gst_inclusive else D('0')) for r in rows if r.amount > 0), D('0'))
        neg = -sum((r.amount for r in rows if r.amount < 0), D('0'))
        return pos, len([r for r in rows if r.amount > 0]), neg

    def oracle_revenue(self, start, end, centre=None):
        invs = list(self.window(start, end, centre).prefetch_related('items__content_type', 'payments'))
        lines = gst = disc = rnd_ = vc = cb = cash_in = outstanding = D('0')
        for inv in invs:
            disc += inv.discount
            rnd_ += inv.rounding
            outstanding += max(D('0'), inv.total_amount - inv.paid_amount)
            for it in inv.items.all():
                if it.is_redemption:
                    continue
                gst += it.tax_amount
                if it.content_type and it.content_type.model == 'advancepayment':
                    continue
                lines += it.total_price
            for p in inv.payments.all():
                if p.is_tip:
                    continue
                if p.payment_method == 'Value Card':
                    vc += p.amount
                elif p.payment_method == 'Cashback Wallet':
                    cb += p.amount
                elif p.payment_method != 'Advance':
                    cash_in += p.amount
        adv_in, adv_n, adv_used = self.ledger(start, end, centre)
        sales = lines - disc + adv_in
        coll = sales - vc - cb
        total = coll + gst + rnd_
        refunds = sum((r.amount for r in InvoiceRefund.objects.filter(refunded_on__gte=start, refunded_on__lte=end)
                       if not centre or r.center_id == centre.pk), D('0'))
        return dict(bills=len(invs), sales=sales, coll=coll, gst=gst, total=total, desk=total - adv_used, vc=vc, cb=cb,
                    adv_in=adv_in, adv_used=adv_used, cash_in=cash_in, outstanding=outstanding, refunds=refunds)

    # ── the checks ───────────────────────────────────────────────────────
    def get(self, name, start, end, centre=None):
        p = {'start_date': start.isoformat(), 'end_date': end.isoformat()}
        if centre:
            p['center_id'] = centre.pk
        r = self.api.get(f'/salon_admin/api/analytics/{name}/', p)
        self.assertEqual(r.status_code, 200, (name, r.content[:200]))
        return r.data

    def ranges(self):
        t = self.today
        first = t.replace(day=1)
        last_end = first - dt.timedelta(days=1)
        return {'this month': (first, t), 'last month': (last_end.replace(day=1), last_end),
                'past 3 months': ((first - dt.timedelta(days=62)).replace(day=1), t),
                'past 6 months': ((first - dt.timedelta(days=155)).replace(day=1), t)}

    def test_revenues(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A, self.B):
                tag = f'[{rname} / {centre.display_name if centre else "all"}]'
                d, o = self.get('revenues', s, e, centre), self.oracle_revenue(s, e, centre)
                r = d['reconciliation']
                expect(f'{tag} invoices', d['kpis']['invoices'], o['bills'], 0)
                expect(f'{tag} sales value', r['sales_value'], o['sales'])
                expect(f'{tag} collection', r['collection_excl_tax'], o['coll'])
                expect(f'{tag} gst', r['gst'], o['gst'])
                expect(f'{tag} total collection', r['total_collection'], o['total'])
                expect(f'{tag} received at desk', r['desk'], o['desk'])
                expect(f'{tag} advances in', r['advance_in'], o['adv_in'])
                expect(f'{tag} advances used', r['advance_used'], o['adv_used'])
                expect(f'{tag} refunds', r['refunds'], o['refunds'])
                expect(f'{tag} daily sum', sum(x['value'] for x in d['daily']), o['sales'], 0.05)
                expect(f'{tag} type table sum', sum(x['value'] for x in d['sales_by_type']), o['sales'], 0.05)
                expect(f'{tag} hours sum', sum(h['invoices'] for h in d['hours']), o['bills'], 0)
                # Conservation: what the register says came in must be the money actually taken, less what
                # part-paid bills still owe. This is what exposes a wrong definition, not just wrong arithmetic.
                expect(f'{tag} CONSERVATION desk - owed = payments taken', o['desk'] - o['outstanding'], o['cash_in'], 0.05)

    def test_overview_tab_agrees(self):
        """The older Overview tab's headline figures against the same independent calculation."""
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.B):
                tag = f'[overview {rname} / {centre.display_name if centre else "all"}]'
                p = {'start_date': s.isoformat(), 'end_date': e.isoformat(), **({'center_id': centre.pk} if centre else {})}
                r = self.api.get('/salon_admin/api/dashboard/summary/', p)
                self.assertEqual(r.status_code, 200, r.content[:200])
                o = self.oracle_revenue(s, e, centre)
                mi = r.data['money_in']
                expect(f'{tag} bills', mi['bills'], o['bills'], 0)
                expect(f'{tag} total revenue = received at the desk', r.data['revenue'], o['desk'], 0.05)
                expect(f'{tag} money received = payments taken', mi['real_money_received'], o['cash_in'], 0.05)
                expect(f'{tag} value-card redemptions', mi['redeemed']['value_card'], o['vc'], 0.05)
                expect(f'{tag} GST collected', mi['tax']['cgst'] + mi['tax']['sgst'] + mi['tax']['igst'], o['gst'], 0.05)

    def test_clients(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[{rname} / {centre.display_name if centre else "all"}]'
                d = self.get('clients', s, e, centre)
                invs = list(self.window(s, e, centre))
                ids = {i.client_id for i in invs if i.client_id}
                prior = Invoice.objects.filter(status__in=('paid', 'partial'), created_at__lt=s, client_id__in=ids)
                if centre:
                    prior = prior.filter(center=centre)
                seen = {i.client_id for i in prior}
                last_before = defaultdict(lambda: None)
                for i in prior:
                    if last_before[i.client_id] is None or i.created_at > last_before[i.client_id]:
                        last_before[i.client_id] = i.created_at
                first_in = {}
                for i in invs:
                    if i.client_id and (i.client_id not in first_in or i.created_at < first_in[i.client_id]):
                        first_in[i.client_id] = i.created_at
                buckets = defaultdict(int)
                for cid, t in first_in.items():
                    if cid not in seen:
                        buckets['first'] += 1
                    else:
                        gap = (t.date() - last_before[cid].date()).days
                        buckets['d30' if gap <= 30 else 'd60' if gap <= 60 else 'd90' if gap <= 90 else 'over'] += 1
                k = d['kpis']
                expect(f'{tag} footfall', k['footfall'], len(invs), 0)
                expect(f'{tag} unique', k['unique'], len(ids), 0)
                expect(f'{tag} new', k['new'], buckets['first'], 0)
                expect(f'{tag} repeat', k['repeat'], len(ids) - buckets['first'], 0)
                expect(f'{tag} win-back', k['winback'], buckets['over'], 0)
                freq = {f['label']: f['clients'] for f in d['frequency']}
                expect(f'{tag} within 30', freq['Back within 30 days'], buckets['d30'], 0)

    def test_services_and_products(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.B):
                tag = f'[{rname} / {centre.display_name if centre else "all"}]'
                items = InvoiceItem.objects.filter(invoice__in=self.window(s, e, centre)).select_related('content_type')
                svc = [i for i in items if i.content_type and i.content_type.model == 'servicemaster']
                prd = [i for i in items if i.content_type and i.content_type.model == 'product' and not i.is_redemption]
                d = self.get('services', s, e, centre)
                expect(f'{tag} services done', d['kpis']['done'], sum(i.quantity for i in svc), 0)
                expect(f'{tag} service lines', d['kpis']['lines'], len(svc), 0)
                expect(f'{tag} service sales', d['kpis']['sales'], sum((i.total_price - i.bill_discount_share for i in svc), D('0')), 0.05)
                expect(f'{tag} distinct services', d['kpis']['distinct'], len({i.object_id for i in svc}), 0)
                d = self.get('products', s, e, centre)
                expect(f'{tag} units', d['kpis']['units'], sum(i.quantity for i in prd), 0)
                expect(f'{tag} retail sales', d['kpis']['sales'], sum((i.total_price - i.bill_discount_share for i in prd), D('0')), 0.05)
                expect(f'{tag} attach invoices', d['kpis']['attach_invoices'], len({i.invoice_id for i in prd}), 0)

    def test_memberships(self):
        t = self.today
        for rname, (s, e) in self.ranges().items():
            tag = f'[{rname}]'
            d = self.get('memberships', s, e)
            held = defaultdict(list)
            for m in ClientMembership.objects.exclude(source_invoice__status__in=('cancelled', 'refunded')):
                held[m.client_id].append((m.start_date, m.expiry_date))
            invs = list(self.window(s, e).exclude(client__isnull=True).prefetch_related('items'))
            member_visits = sum(1 for i in invs if any(a <= i.created_at.date() <= b for a, b in held[i.client_id]))
            saved = sum((i.discount + sum((it.discount - it.card_benefit for it in i.items.all()), D('0'))
                         for i in invs if i.membership_used_id), D('0'))
            expect(f'{tag} member visits', d['kpis']['member_visits'], member_visits, 0)
            expect(f'{tag} saved by members', d['kpis']['saved'], saved, 0.05)
            sold = InvoiceItem.objects.filter(invoice__in=self.window(s, e), content_type__model='membership')
            expect(f'{tag} memberships sold', d['kpis']['sold'], sum(i.quantity for i in sold), 0)
        d = self.get('memberships', *self.ranges()['this month'])
        valid = {c for c, rows in {m.client_id: 0 for m in ClientMembership.objects.filter(
            start_date__lte=t, expiry_date__gte=t).exclude(source_invoice__status__in=('cancelled', 'refunded'))}.items()}
        expect('members now', d['kpis']['members'], len(valid), 0)
        soon = ClientMembership.objects.filter(expiry_date__gte=t, expiry_date__lte=t + dt.timedelta(days=30), start_date__lte=t)
        expect('expiring in 30 days (rows)', d['kpis']['expiring_30'], soon.exclude(client_id__in=[
            m.client_id for m in ClientMembership.objects.filter(expiry_date__gt=t + dt.timedelta(days=30))]).count(), 0)

    def test_value_cards(self):
        t = self.today
        # integrity first: every card's balance must be its face value less what was spent from it
        for card in ClientValueCard.objects.select_related('source_invoice'):
            if card.source_invoice and card.source_invoice.status in ('cancelled', 'refunded'):
                # the bill that sold it was cancelled the same day: the card is withdrawn, nothing to spend
                expect(f'card {card.pk} (sold on a cancelled bill) is withdrawn', card.balance, 0, 0.001)
                continue
            spent = sum((p.card_face or p.amount for p in Payment.objects.filter(payment_method='Value Card', value_card_id=card.pk)
                         if p.invoice.status in ('paid', 'partial')), D('0'))
            face = card.face_value if card.face_value is not None else card.value_card.value
            expect(f'card {card.pk} balance = face - spent - forfeited', card.balance, face - spent - card.forfeited_amount, 0.02)
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[{rname} / {centre.display_name if centre else "all"}]'
                d = self.get('value-cards', s, e, centre)
                pays = [p for p in Payment.objects.filter(payment_method='Value Card', invoice__in=self.window(s, e, centre))]
                expect(f'{tag} spent (face)', d['kpis']['spent'], sum((p.card_face or p.amount for p in pays), D('0')), 0.05)
                expect(f'{tag} clients redeeming', d['kpis']['redeeming_clients'], len({p.invoice.client_id for p in pays}), 0)
                sold = InvoiceItem.objects.filter(invoice__in=self.window(s, e, centre), content_type__model='valuecard')
                expect(f'{tag} cards sold', d['kpis']['sold'], sum(i.quantity for i in sold), 0)
        d = self.get('value-cards', *self.ranges()['this month'])
        live = ClientValueCard.objects.filter(is_active=True, expiry_date__gte=t, balance__gt=0)
        expect('outstanding balance', d['kpis']['outstanding'], sum((c.balance for c in live), D('0')), 0.05)
        expect('live cards', d['kpis']['live_cards'], live.count(), 0)


    # ── the older tabs: Revenue drill-down, Clients trends, Finance, Services & Products, Staff, Overview ──
    def legacy(self, name, s, e, centre=None):
        p = {'start_date': s.isoformat(), 'end_date': e.isoformat(), **({'center_id': centre.pk} if centre else {})}
        r = self.api.get(f'/salon_admin/api/dashboard/{name}/', p)
        self.assertEqual(r.status_code, 200, (name, r.content[:200]))
        return r.data

    @staticmethod
    def mkey(d):
        return f'{calendar.month_abbr[d.month]}-{d.year}'

    def basis(self, inv):
        """Bill total less what was paid from a value card, advance or cashback (money in, GST included)."""
        return inv.total_amount - sum((p.amount for p in inv.payments.all()
                                       if not p.is_tip and p.payment_method in WALLET_METHODS), D('0'))

    def test_legacy_revenue_drilldown(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[drill-down {rname} / {centre.display_name if centre else "all"}]'
                d = self.legacy('revenues', s, e, centre)
                invs = list(self.window(s, e, centre).prefetch_related('payments'))
                monthly, daily, hourly = defaultdict(D), defaultdict(D), defaultdict(D)
                for i in invs:
                    v = self.basis(i)
                    monthly[self.mkey(i.created_at)] += v
                    daily[i.created_at.date().isoformat()] += v
                    hourly[i.created_at.hour] += v
                for row in d['monthly']:
                    expect(f"{tag} month {row['month']}", row['revenue'], monthly.get(row['month'], 0), 0.05)
                for row in d['daily']:
                    expect(f"{tag} day {row['day']}", row['revenue'], daily.get(row['day'], 0), 0.05)
                for row in d['hourly']:
                    expect(f"{tag} hour {row['hour']}", row['revenue'], hourly.get(int(row['hour'][:2]), 0), 0.05)
                expect(f'{tag} months add up to received at the desk', sum(r['revenue'] for r in d['monthly']),
                       self.oracle_revenue(s, e, centre)['desk'], 0.5)

    def held_on(self, windows, client_id, day):
        return any(a <= day <= b for a, b in windows.get(client_id, ()))

    def test_legacy_clients_trends(self):
        windows = defaultdict(list)
        for m in ClientMembership.objects.exclude(source_invoice__status__in=('cancelled', 'refunded')):
            windows[m.client_id].append((m.start_date, m.expiry_date))
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[clients trends {rname} / {centre.display_name if centre else "all"}]'
                d = self.legacy('clients', s, e, centre)
                invs = list(self.window(s, e, centre))
                prior = Invoice.objects.filter(status__in=('paid', 'partial'), created_at__lt=s)
                if centre:
                    prior = prior.filter(center=centre)
                seen = {i.client_id for i in prior if i.client_id}
                first_in = {}
                for i in sorted(invs, key=lambda x: x.created_at):
                    if i.client_id and i.client_id not in first_in:
                        first_in[i.client_id] = i.created_at
                new_ids = {c for c in first_in if c not in seen}
                ft = d['footfall']
                expect(f'{tag} footfall total', ft['total']['total'], len(first_in), 0)
                expect(f'{tag} footfall new', ft['new']['total'], len(new_ids), 0)
                expect(f'{tag} footfall repeat', ft['repeat']['total'], len(first_in) - len(new_ids), 0)
                by_month = defaultdict(lambda: {'inv': 0, 'member': 0, 'clients': set(), 'new': set()})
                for i in invs:
                    b = by_month[self.mkey(i.created_at)]
                    b['inv'] += 1
                    if i.client_id:
                        b['clients'].add(i.client_id)
                        if i.client_id in new_ids and self.mkey(first_in[i.client_id]) == self.mkey(i.created_at):
                            b['new'].add(i.client_id)
                        if self.held_on(windows, i.client_id, i.created_at.date()):
                            b['member'] += 1
                for mk, row in d['monthly_breakdown'].items():
                    b = by_month.get(mk)
                    if not b:
                        continue
                    expect(f'{tag} {mk} new clients', row['new'], len(b['new']), 0)
                    expect(f'{tag} {mk} repeat clients', row['repeat'], len(b['clients']) - len(b['new']), 0)
                    expect(f'{tag} {mk} member invoices', row['member'], b['member'], 0)
                    expect(f'{tag} {mk} non-member invoices', row['non_member'], b['inv'] - b['member'], 0)
                daily = defaultdict(set)
                for i in invs:
                    if i.client_id:
                        daily[i.created_at.date().isoformat()].add(i.client_id)
                for row in d['daily_footfall']:
                    expect(f"{tag} footfall {row['day']}", row['count'], len(daily.get(row['day'], ())), 0)

    def ledger_rows(self, start, end, centre=None):
        rows = [r for r in AdvancePayment.objects.filter(created_at__gte=start, created_at__lt=end + dt.timedelta(days=1))
                .select_related('invoice', 'client') if not (r.invoice and r.invoice.status in ('cancelled', 'refunded'))]
        reversed_ids = {r.reverses_id for r in rows if r.reverses_id}
        rows = [r for r in rows if not r.reverses_id and r.pk not in reversed_ids]
        if centre:
            rows = [r for r in rows if (r.center_id or (r.client.center_id if r.client_id else None)) == centre.pk]
        return rows

    def test_legacy_finance_charts(self):
        names = {'servicemaster': 'services', 'product': 'products', 'membership': 'memberships', 'package': 'packages', 'valuecard': 'value_cards'}
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.B):
                tag = f'[finance {rname} / {centre.display_name if centre else "all"}]'
                d = self.legacy('finance', s, e, centre)
                want = defaultdict(lambda: defaultdict(lambda: [D('0'), 0]))
                for it in InvoiceItem.objects.filter(invoice__in=self.window(s, e, centre), is_redemption=False).select_related('content_type', 'invoice'):
                    k = names.get(it.content_type.model if it.content_type else None)
                    if k:
                        cell = want[self.mkey(it.invoice.created_at)][k]
                        cell[0] += it.total_price - it.bill_discount_share
                        cell[1] += 1
                adv = defaultdict(lambda: D('0'))
                for r in self.ledger_rows(s, e, centre):
                    if r.amount > 0:
                        adv[self.mkey(r.created_at)] += r.amount - (r.gst_amount if r.gst_inclusive else D('0'))
                for mk, row in d.items():
                    for k in names.values():
                        expect(f'{tag} {mk} {k}', row[k]['revenue'], want[mk][k][0] if mk in want else 0, 0.05)
                        expect(f'{tag} {mk} {k} count', row[k]['count'], want[mk][k][1] if mk in want else 0, 0)
                    expect(f'{tag} {mk} advances (pre-tax, as on the Revenue tab)', row['advances']['revenue'], adv.get(mk, 0), 0.05)

    def test_legacy_services_and_products_lists(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[services/products lists {rname} / {centre.display_name if centre else "all"}]'
                d = self.legacy('services_products', s, e, centre)
                items = list(InvoiceItem.objects.filter(invoice__in=self.window(s, e, centre)).select_related('content_type'))
                for key, model, skip_redeem in (('services', 'servicemaster', False), ('products', 'product', True)):
                    agg = defaultdict(lambda: [D('0'), 0])
                    for it in items:
                        if it.content_type and it.content_type.model == model and not (skip_redeem and it.is_redemption):
                            agg[it.object_id][0] += it.total_price - it.bill_discount_share
                            agg[it.object_id][1] += it.quantity
                    expect(f'{tag} {key} total', sum(r['revenue'] for r in d[key]), sum((v[0] for v in agg.values()), D('0')), 0.1)
                    expect(f'{tag} {key} units', sum(r['count'] for r in d[key]), sum(v[1] for v in agg.values()), 0)
                    expect(f'{tag} {key} rows', len(d[key]), len(agg), 0)

    def test_legacy_staff_tab(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.B):
                tag = f'[staff {rname} / {centre.display_name if centre else "all"}]'
                d = self.legacy('staff', s, e, centre)
                invs = list(self.window(s, e, centre).prefetch_related('items__content_type', 'items__staff_members', 'payments'))
                per_staff = defaultdict(lambda: D('0'))
                for inv in invs:
                    wallet = sum((p.amount for p in inv.payments.all() if not p.is_tip and p.payment_method in WALLET_METHODS), D('0'))
                    real = inv.total_amount - wallet
                    if inv.total_amount <= 0 or real >= inv.total_amount:
                        ratio = D('1')
                    elif real <= 0:
                        ratio = D('0')
                    else:
                        ratio = quantize_ratio(real, inv.total_amount)
                    for it in inv.items.all():
                        if (it.content_type and it.content_type.model == 'advancepayment') or it.is_redemption:
                            continue
                        for m in it.staff_members.all():
                            per_staff[m.pk] += money(it.total_price * ratio)
                expect(f'{tag} staff revenue total', sum(r['revenue'] for r in d['table']), sum(per_staff.values(), D('0')), 0.05)
                expect(f'{tag} trend total', sum(float(r['revenue']) for r in d['trends']), sum(per_staff.values(), D('0')), 0.05)

    def test_overview_tab_in_full(self):
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[overview {rname} / {centre.display_name if centre else "all"}]'
                p = {'start_date': s.isoformat(), 'end_date': e.isoformat(), **({'center_id': centre.pk} if centre else {})}
                d = self.api.get('/salon_admin/api/dashboard/summary/', p).data
                invs = list(self.window(s, e, centre).prefetch_related('items__content_type'))
                wk = [0] * 7
                for i in invs:
                    wk[(i.created_at.weekday() + 1) % 7] += 1          # the tab runs Sunday first
                expect(f'{tag} weekday counts', sum(abs(a - b) for a, b in zip(d['weekday_counts'], wk)), 0, 0)
                ids = {i.client_id for i in invs if i.client_id}
                prior = Invoice.objects.filter(status__in=('paid', 'partial'), created_at__lt=s, client_id__in=ids)
                if centre:
                    prior = prior.filter(center=centre)
                new = len(ids - {i.client_id for i in prior})
                expect(f'{tag} new clients (first visit)', d['new_clients'], new, 0)
                expect(f'{tag} repeat clients', d['repeat_clients'], len(ids) - new, 0)
                expect(f'{tag} clients invoiced', d['total_clients'], len(ids), 0)
                bd = d['revenue_breakdown']
                tp = defaultdict(lambda: D('0'))
                for i in invs:
                    for it in i.items.all():
                        if it.content_type and not it.is_redemption:
                            tp[it.content_type.model] += it.total_price - it.bill_discount_share
                expect(f'{tag} services in breakdown', bd['service'], tp['servicemaster'], 0.05)
                expect(f'{tag} products in breakdown', bd['product'], tp['product'], 0.05)
                expect(f'{tag} value cards in breakdown', bd['card'], tp['valuecard'], 0.05)
                expect(f'{tag} advances in breakdown', bd['advance'], self.oracle_revenue(s, e, centre)['adv_in'], 0.05)

    def test_overview_money_balances_and_lists(self):
        t = self.today
        for rname, (s, e) in self.ranges().items():
            for centre in (None, self.A):
                tag = f'[overview2 {rname} / {centre.display_name if centre else "all"}]'
                p = {'start_date': s.isoformat(), 'end_date': e.isoformat(), **({'center_id': centre.pk} if centre else {})}
                d = self.api.get('/salon_admin/api/dashboard/summary/', p).data
                invs = list(self.window(s, e, centre).prefetch_related('items__content_type', 'payments'))
                mi = d['money_in']
                gross = sum((i.total_amount for i in invs), D('0'))
                expect(f'{tag} average bill', mi['avg_bill'], (gross / len(invs)) if invs else 0, 0.01)
                expect(f'{tag} discounts on bills', mi['discounts']['bill_level'], sum((i.discount for i in invs), D('0')), 0.05)
                line = sum((it.discount - it.card_benefit for i in invs for it in i.items.all() if not it.is_redemption), D('0'))
                expect(f'{tag} discounts on items', mi['discounts']['line_level'], line, 0.05)
                expect(f'{tag} still to collect', mi['to_collect'], sum((max(D('0'), i.total_amount - i.paid_amount) for i in invs), D('0')), 0.05)
                void = Invoice.objects.filter(status__in=('cancelled', 'refunded'), created_at__gte=s, created_at__lt=e + dt.timedelta(days=1))
                if centre:
                    void = void.filter(center=centre)
                expect(f'{tag} cancelled bills', mi['cancelled']['count'], void.count(), 0)
                paid = defaultdict(D)
                for i in invs:
                    for pay in i.payments.all():
                        if not pay.is_tip:
                            paid[pay.payment_method] += pay.amount
                got = {x['method']: x['amount'] for x in mi['payments']}
                expect(f'{tag} paid by cash', got.get('cash', 0), paid['Cash'], 0.05)
                expect(f'{tag} paid by UPI', got.get('upi', 0), paid['UPI'], 0.05)
                expect(f'{tag} paid by card', got.get('credit_card', 0), paid['Credit Card'] + paid['Debit Card'], 0.05)
                expect(f'{tag} paid from advances', got.get('advance', 0), paid['Advance'], 0.05)
                # top lists: the biggest counts must match
                counts = defaultdict(int)
                for i in invs:
                    for it in i.items.all():
                        if it.content_type and it.content_type.model == 'servicemaster' and not it.is_redemption:
                            counts[it.object_id] += it.quantity
                top = sorted(counts.values(), reverse=True)[:5]
                expect(f'{tag} top services counts', sum(x['count'] for x in d['top_services'][:5]), sum(top), 0)
                # balances held for clients, as of today, by the centre that sold the card / package / took the advance
                scope = (lambda q, f='center': q.filter(**{f: centre})) if centre else (lambda q, f='center': q)
                cards = scope(ClientValueCard.objects.filter(is_active=True, expiry_date__gte=t, balance__gt=0))
                expect(f'{tag} value cards held (amount)', d['balances']['value_cards']['amount'], sum((c.balance for c in cards), D('0')), 0.05)
                expect(f'{tag} value cards held (count)', d['balances']['value_cards']['count'], cards.count(), 0)
                bal = defaultdict(D)
                for r in scope(AdvancePayment.objects.all()):
                    bal[r.client_id] += r.amount
                expect(f'{tag} advances held (amount)', d['balances']['advances']['amount'], sum((v for v in bal.values() if v > 0), D('0')), 0.05)
                expect(f'{tag} advances held (clients)', d['balances']['advances']['count'], len([v for v in bal.values() if v > 0]), 0)
                stock = scope(Product.objects.all())
                expect(f'{tag} stock value', d['inventory']['amount'], sum((p.current_stock * (p.price or 0) for p in stock), D('0')), 0.5)
                # top staff are the same commissionable-sales figures as the Staff tab
                tab = self.legacy('staff', s, e, centre)
                expect(f'{tag} top staff agree with the Staff tab', sum(x['revenue'] for x in d['top_staff']),
                       sum(r['revenue'] for r in sorted(tab['table'], key=lambda r: -r['revenue'])[:5]), 0.05)

    def test_zz_report(self):
        print('\n' + '\n'.join(NOTES))
        print(f'\n{len(FAILS)} check(s) failed')
        import re
        groups = defaultdict(list)
        for f in FAILS:
            kind = re.sub(r'\d+', '#', re.sub(r'\[[^\]]*\]', '', f.split(': report=')[0])).strip()
            groups[kind].append(f)
        for kind, rows in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            print(f'  {len(rows):4d} x {kind}\n          e.g. {rows[0]}')
        self.assertEqual(FAILS, [])
