"""The analytics sections of the dashboard, checked against a small salon whose
every figure is worked out by hand below.

Period: the 20 days to today. Bills (all at one centre, all Paid unless noted):

  B1  Asha   5% service  Rs1000 less Rs100 discount, membership applied   sales 900  tax  45  UPI
  B2  Bina   service     Rs2000                                          sales 2000 tax 100  cash
  B3  Chitra service     Rs1000, covered by her value card (face 1000,
                         Rs50 card benefit)                              sales 950  tax 47.5 value card
  B4  Asha   same as B1, her second visit                                sales 900  tax  45  UPI
  B5  walk-in service Rs300 (no phone)                                   sales 300  tax  15  cash
  B6  Bina   product Rs500 at 18% (stock on hand is -1)                  sales 500  tax  90  UPI
  B7  Dev    service Rs700 - CANCELLED, so counted nowhere
  and Chitra's earlier bill, 101 days before B3: service Rs500, cash.

  sales value 5550 | redeemed 997.5 (the card) | collection excl. tax 4552.5
  GST 342.5 | total collection 4895 = 4552.5 + 342.5
"""
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem, Payment
from clients.models import Client, ClientMembership, ClientValueCard
from inventory.models import Product
from marketing.models import Membership, ValueCard
from salon_admin.models import Center
from services.models import ServiceMaster

D = Decimal


class AnalyticsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.today = date.today()
        cls.center = Center.objects.create(center_name='A', display_name='A')
        cls.owner = CustomUser.objects.create_user(
            email='o@test.com', password='x', full_name='Owner', is_superuser=True, center=cls.center)
        mk = lambda n, p, g='female': Client.objects.create(center=cls.center, first_name=n, phone=p, gender=g)
        cls.asha, cls.bina, cls.chitra, cls.dev = mk('Asha', '9000000001'), mk('Bina', '9000000002'), \
            mk('Chitra', '9000000003'), mk('Dev', '9000000004', 'male')
        cls.svc = ServiceMaster.objects.create(name='Haircut', category='Hair', default_price=1000)
        cls.prod = Product.objects.create(name='Shampoo', brand='Kerastase', category='Retail',
                                          center=cls.center, current_stock=D('-1'), mrp=D('600'))
        cls.ct_svc = ContentType.objects.get_for_model(ServiceMaster)
        cls.ct_prod = ContentType.objects.get_for_model(Product)

        mem = Membership.objects.create(name='Gold', expiry_days=365, pre_tax_price=2000)
        cls.membership = ClientMembership.objects.create(
            client=cls.asha, membership=mem, center=cls.center,
            start_date=cls.today - timedelta(days=60), expiry_date=cls.today + timedelta(days=10))
        tpl = ValueCard.objects.create(title='Elite', expiry_days=365, value=D('5000'))
        mk_card = lambda c, bal, exp: ClientValueCard.objects.create(
            client=c, value_card=tpl, balance=D(bal), face_value=D('5000'), center=cls.center, expiry_date=exp)
        mk_card(cls.chitra, '4000', cls.today + timedelta(days=300))     # live
        mk_card(cls.bina, '500', cls.today + timedelta(days=10))         # live, expiring
        mk_card(cls.dev, '200', cls.today - timedelta(days=5))           # expired with a balance

        ago = lambda n: datetime.combine(cls.today - timedelta(days=n), datetime.min.time()).replace(hour=11)
        cls.days = {'B1': 15, 'B2': 14, 'B3': 12, 'B4': 8, 'B5': 7, 'B6': 6}

        def line(inv, ct, obj, unit, disc, tax_pct, qty=1, **card):
            tp = D(unit) * qty - D(disc)
            tax = (tp * D(tax_pct) / 100).quantize(D('0.01'))
            InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=obj.pk, description=obj.name,
                                       unit_price=D(unit), quantity=qty, discount=D(disc), total_price=tp,
                                       tax_percentage=D(tax_pct), tax_amount=tax, **card)
            return tp, tax

        def make(client, when, ct, obj, unit, disc, tax_pct, method, member=None, status='paid', card=None):
            tp = D(unit) - D(disc)
            tax = (tp * D(tax_pct) / 100).quantize(D('0.01'))
            inv = Invoice.objects.create(client=client, center=cls.center, status=status, subtotal=tp,
                                         cgst=tax / 2, sgst=tax / 2, total_amount=tp + tax, paid_amount=tp + tax,
                                         membership_used=member)
            Invoice.objects.filter(pk=inv.pk).update(created_at=when)
            line(inv, ct, obj, unit, disc, tax_pct, **(card or {}))
            Payment.objects.create(invoice=inv, amount=tp + tax, payment_method=method,
                                   card_face=(card or {}).get('card_face'))
            return inv

        card_obj = ClientValueCard.objects.get(client=cls.chitra)
        make(cls.asha, ago(15), cls.ct_svc, cls.svc, 1000, 100, 5, 'UPI', member=cls.membership)
        make(cls.bina, ago(14), cls.ct_svc, cls.svc, 2000, 0, 5, 'Cash')
        make(cls.chitra, ago(12), cls.ct_svc, cls.svc, 1000, 50, 5, 'Value Card',
             card=dict(card_face=D('1000'), card_benefit=D('50'), card_taxable=D('950'), card_tax=D('47.5'),
                       value_card=card_obj))
        make(cls.asha, ago(8), cls.ct_svc, cls.svc, 1000, 100, 5, 'UPI', member=cls.membership)
        make(None, ago(7), cls.ct_svc, cls.svc, 300, 0, 5, 'Cash')
        make(cls.bina, ago(6), cls.ct_prod, cls.prod, 500, 0, 18, 'UPI')
        make(cls.dev, ago(5), cls.ct_svc, cls.svc, 700, 0, 5, 'Cash', status='cancelled')
        make(cls.chitra, ago(12 + 101), cls.ct_svc, cls.svc, 500, 0, 5, 'Cash')

    def setUp(self):
        from django.core.cache import cache
        cache.clear()

    def get(self, name, **extra):
        api = APIClient()
        api.force_authenticate(self.owner)
        params = {'start_date': (self.today - timedelta(days=19)).isoformat(), 'end_date': self.today.isoformat(), **extra}
        res = api.get(f'/salon_admin/api/analytics/{name}/', params)
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.data

    # ── Revenues ──
    def test_revenues_reconcile(self):
        d = self.get('revenues')
        r = d['reconciliation']
        self.assertEqual(d['kpis']['invoices'], 6)
        self.assertAlmostEqual(r['sales_value'], 5550.0)
        self.assertAlmostEqual(r['redeemed_value_cards'], 997.5)
        self.assertAlmostEqual(r['collection_excl_tax'], 4552.5)
        self.assertAlmostEqual(r['gst'], 342.5)
        self.assertAlmostEqual(r['total_collection'], 4895.0)
        self.assertAlmostEqual(r['collection_excl_tax'] + r['gst'], r['total_collection'])
        self.assertAlmostEqual(r['desk'], 4895.0)
        self.assertEqual((r['advance_in'], r['advance_used']), (0, 0))
        self.assertAlmostEqual(sum(x['value'] for x in d['daily']), 5550.0)
        self.assertAlmostEqual(sum(x['value'] for x in d['sales_by_type']), 5550.0)
        by = {x['type']: x for x in d['sales_by_type']}
        self.assertEqual((by['Services']['lines'], by['Services']['value']), (5, 5050.0))
        self.assertEqual((by['Products']['lines'], by['Products']['value']), (1, 500.0))
        self.assertEqual(by['Packages']['value'], 0)          # every type is always listed
        self.assertEqual(sum(h['invoices'] for h in d['hours']), 6)
        self.assertEqual({m['mode'] for m in d['modes']}, {'upi', 'cash'})

    def test_revenues_equal_the_finance_register_summary(self):
        from finance.views import compute_register_summary
        s = compute_register_summary(self.owner, None, (self.today - timedelta(days=19)).isoformat(), self.today.isoformat())
        d = self.get('revenues')
        self.assertAlmostEqual(d['kpis']['desk'], float(s['revenues']['including_tax']))          # "received at the desk" is Finance's Including Tax
        self.assertAlmostEqual(d['kpis']['gst'], float(s['revenues']['total_tax']))

    def test_advances_follow_the_business_definitions(self):
        from billing.models import AdvancePayment
        AdvancePayment.objects.create(client=self.bina, center=self.center, amount=D('1000'))
        AdvancePayment.objects.create(client=self.bina, center=self.center, amount=D('-200'))
        d = self.get('revenues')
        r = d['reconciliation']
        self.assertAlmostEqual(r['advance_in'], 1000.0)
        self.assertAlmostEqual(r['advance_used'], 200.0)
        self.assertAlmostEqual(r['sales_value'], 5550 + 1000)                          # advances deposited are part of what was sold
        self.assertAlmostEqual(r['collection_excl_tax'], 6550 - 997.5)                 # less cards; advances SETTLED stay in (an advance is money)
        self.assertAlmostEqual(r['total_collection'], 6550 - 997.5 + 342.5)
        self.assertAlmostEqual(r['desk'], 6550 - 997.5 + 342.5 - 200)                  # total collection less what earlier advances settled
        self.assertAlmostEqual(d['kpis']['advance_used'], 200.0)
        self.assertAlmostEqual(sum(x['value'] for x in d['sales_by_type']), r['sales_value'])      # the type table adds up to sales value
        self.assertAlmostEqual(sum(x['value'] for x in d['daily']), r['sales_value'])
        self.assertAlmostEqual(d['trend'][-1]['value'], r['collection_excl_tax'])      # this month's trend bar is the same collection
        # Finance subtracts advances settled one step earlier: its collection before tax is ours less them
        from finance.views import compute_register_summary
        s = compute_register_summary(self.owner, None, (self.today - timedelta(days=19)).isoformat(), self.today.isoformat())
        self.assertAlmostEqual(float(s['revenues']['collection_before_tax']), r['collection_excl_tax'] - 200)
        self.assertAlmostEqual(float(s['revenues']['including_tax']), r['desk'])

    def test_the_gst_inside_an_advance_is_not_counted_twice(self):
        """With 'charge GST on advances' on, the ledger credits Rs 1,050 (1,000 + 50 GST) while the bill's
        tax total already holds the 50. The deposit is therefore counted at 1,000; the 50 enters via the tax."""
        from billing.models import AdvancePayment
        AdvancePayment.objects.create(client=self.bina, center=self.center, amount=D('1050'),
                                      gst_amount=D('50'), gst_inclusive=True)
        d = self.get('revenues')
        self.assertAlmostEqual(d['reconciliation']['advance_in'], 1000.0)
        self.assertAlmostEqual(d['reconciliation']['sales_value'], 5550 + 1000)
        self.assertAlmostEqual(d['trend'][-1]['value'], 6550 - 997.5)
        self.assertEqual(next(t for t in d['sales_by_type'] if t['type'] == 'Advances')['lines'], 1)

    def test_refunds_are_shown_not_deducted(self):
        from billing.models import InvoiceRefund
        inv = Invoice.objects.filter(client=self.bina, status='paid').first()
        InvoiceRefund.objects.create(invoice=inv, center=self.center, amount=D('300'), refunded_on=self.today)
        r = self.get('revenues')['reconciliation']
        self.assertAlmostEqual(r['refunds'], 300.0)
        self.assertAlmostEqual(r['total_collection'], 4895.0)                          # not taken off any figure

    def test_previous_period_for_a_month_to_date_is_the_same_days_of_last_month(self):
        first = self.today.replace(day=1)
        d = self.get('revenues', start_date=first.isoformat(), end_date=self.today.isoformat())
        prev_first = (first - timedelta(days=1)).replace(day=1)
        self.assertEqual(d['period']['prev_start'], prev_first.isoformat())
        import calendar
        last = calendar.monthrange(prev_first.year, prev_first.month)[1]
        self.assertEqual(d['period']['prev_end'], prev_first.replace(day=min(self.today.day, last)).isoformat())

    def test_previous_period_for_any_other_range_is_the_equal_window_before(self):
        a, b = self.today - timedelta(days=45), self.today - timedelta(days=15)     # 31 days, not a month to date
        d = self.get('revenues', start_date=a.isoformat(), end_date=b.isoformat())
        self.assertEqual(d['period']['prev_end'], (a - timedelta(days=1)).isoformat())
        self.assertEqual(d['period']['prev_start'], (a - timedelta(days=31)).isoformat())

    def test_target_and_pace(self):
        self.center.monthly_target = D('100000')
        self.center.save()
        k = self.get('revenues')['kpis']
        self.assertEqual(k['target'], 100000.0)
        self.assertEqual(k['target_pct'], 5)                                          # 4552.5 of 100000
        self.assertTrue(0 < k['month_gone_pct'] <= 100)
        self.assertGreaterEqual(k['pace'], k['collection_excl_tax'])
        self.assertAlmostEqual(k['target_gap'], 100000 - 4552.5)                       # a finished month reads "short by ..."

    def test_only_advances_settled_sit_beside_the_money_modes(self):
        self.assertEqual(self.get('revenues')['from_balances'], [])                    # the value-card payment is not a money mode

    # ── scoping, edges, consistency ──
    def _other_centre_bill(self):
        other = Center.objects.create(center_name='B', display_name='B')
        inv = Invoice.objects.create(client=self.asha, center=other, status='paid', subtotal=D('777'),
                                     cgst=0, sgst=0, total_amount=D('777'), paid_amount=D('777'))
        Invoice.objects.filter(pk=inv.pk).update(created_at=datetime.combine(self.today - timedelta(days=3), datetime.min.time()).replace(hour=12))
        InvoiceItem.objects.create(invoice=inv, content_type=self.ct_svc, object_id=self.svc.pk, description='Haircut',
                                   unit_price=D('777'), quantity=1, total_price=D('777'))
        Payment.objects.create(invoice=inv, amount=D('777'), payment_method='Cash')
        return other

    def _centre_user(self, centre):
        from pos_backend.testing import full_access_permissions
        from salon_admin.models import Role
        perms = full_access_permissions()
        perms['all_centers'] = False                 # every module, but only their own centre
        role = Role.objects.create(name='Centre manager', permissions=perms)
        return CustomUser.objects.create_user(email='m@test.com', password='x', full_name='M', role=role, center=centre)

    def get_as(self, user, name, **extra):
        api = APIClient(); api.force_authenticate(user)
        params = {'start_date': (self.today - timedelta(days=19)).isoformat(), 'end_date': self.today.isoformat(), **extra}
        return api.get(f'/salon_admin/api/analytics/{name}/', params)

    def test_a_centre_user_sees_only_their_centre(self):
        other = self._other_centre_bill()
        owner_view = self.get('revenues')
        self.assertEqual(owner_view['kpis']['invoices'], 7)                      # the owner sees both centres
        mgr = self._centre_user(self.center)
        res = self.get_as(mgr, 'revenues')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['kpis']['invoices'], 6)                        # never centre B's Rs777 bill
        self.assertAlmostEqual(res.data['reconciliation']['sales_value'], 5550.0)
        # asking for another centre's id by hand does not open it up
        res = self.get_as(mgr, 'revenues', center_id=other.pk)
        self.assertEqual(res.data['kpis']['invoices'], 0)
        for name in ('clients', 'services', 'products', 'memberships', 'value-cards'):
            self.assertEqual(self.get_as(mgr, name, center_id=other.pk).status_code, 200)

    def test_owner_can_narrow_to_one_centre(self):
        other = self._other_centre_bill()
        self.assertEqual(self.get('revenues', center_id=other.pk)['kpis']['invoices'], 1)
        self.assertEqual(self.get('revenues', center_id=self.center.pk)['kpis']['invoices'], 6)

    def test_an_empty_period_is_all_zeros_not_an_error(self):
        far = {'start_date': '2001-01-01', 'end_date': '2001-01-31'}
        for name in ('revenues', 'clients', 'services', 'products', 'memberships', 'value-cards'):
            d = self.get(name, **far)
            self.assertIsInstance(d['kpis'], dict)
        d = self.get('revenues', **far)
        self.assertEqual((d['kpis']['invoices'], d['kpis']['avg_bill'], d['reconciliation']['total_collection']), (0, 0.0, 0.0))

    def test_bad_dates_fall_back_and_a_reversed_range_is_swapped(self):
        d = self.get('revenues', start_date='nonsense', end_date='also-nonsense')
        self.assertEqual(d['period']['end'], self.today.isoformat())
        a, b = (self.today - timedelta(days=19)).isoformat(), self.today.isoformat()
        d = self.get('revenues', start_date=b, end_date=a)
        self.assertEqual((d['period']['start'], d['period']['end']), (a, b))

    def test_no_warning_when_bills_match_their_lines(self):
        self.assertIsNone(self.get('revenues')['data_warning'])

    def test_warning_when_a_bill_total_disagrees_with_its_lines(self):
        inv = Invoice.objects.filter(client=self.bina, status='paid').first()
        Invoice.objects.filter(pk=inv.pk).update(subtotal=D('99999'))
        d = self.get('revenues')
        self.assertIsNotNone(d['data_warning'])
        # every figure still follows the lines, as Finance does
        self.assertAlmostEqual(d['reconciliation']['sales_value'], 5550.0)
        self.assertAlmostEqual(sum(x['value'] for x in d['daily']), 5550.0)

    def test_cancelled_bill_counts_nowhere(self):
        d = self.get('revenues')
        self.assertEqual(d['kpis']['invoices'], 6)             # the cancelled Rs700 bill is not one of them

    def test_previous_period_window(self):
        d = self.get('revenues')
        inv = next(r for r in d['versus_previous'] if r['label'] == 'Invoices')
        # The previous window is the 20 days before the period (20-39 days back); Chitra's old
        # bill, 113 days back, is in neither, so there is nothing to compare with.
        self.assertEqual((inv['cur'], inv['prev']), (6, 0))
        self.assertIsNone(inv['change'])

    # ── Clients ──
    def test_clients(self):
        d = self.get('clients')
        k = d['kpis']
        self.assertEqual((k['footfall'], k['unique']), (6, 3))            # the walk-in is footfall, not a client
        self.assertEqual((k['new'], k['repeat']), (2, 1))                 # Chitra was here 101 days earlier
        self.assertEqual(k['winback'], 1)
        self.assertAlmostEqual(k['visits_per_client'], 5 / 3, places=1)
        freq = {f['label']: f['clients'] for f in d['frequency']}
        self.assertEqual(freq['First visit ever'], 2)
        self.assertEqual(freq['Over 90 days · win-back'], 1)
        self.assertEqual(sum(w['unique'] for w in d['weeks']) >= 3, True)
        top = d['top_clients'][0]
        self.assertEqual(top['name'], 'Bina')                             # 2000 + 500
        self.assertAlmostEqual(top['sales'], 2500.0)
        self.assertEqual(top['visits'], 2)

    # ── Services / Products ──
    def test_services(self):
        d = self.get('services')
        k = d['kpis']
        self.assertEqual((k['done'], k['lines']), (5, 5))
        self.assertAlmostEqual(k['sales'], 5050.0)
        self.assertEqual(k['discounted_lines'], 2)                        # B1, B4; B3's discount is the card benefit
        self.assertEqual(d['top'][0]['name'], 'Haircut')
        self.assertEqual(d['categories'][0]['category'], 'Hair')

    def test_products(self):
        d = self.get('products')
        self.assertEqual(d['kpis']['units'], 1)
        self.assertAlmostEqual(d['kpis']['sales'], 500.0)
        self.assertAlmostEqual(d['kpis']['attach_rate'], 16.7, places=1)  # 1 of 6 bills
        self.assertEqual(d['kpis']['negative'], 1)
        self.assertEqual(d['problems'][0]['on_hand'], -1.0)

    # ── Memberships ──
    def test_memberships(self):
        d = self.get('memberships')
        k = d['kpis']
        self.assertEqual((k['members'], k['member_visits'], k['members_visited']), (1, 2, 1))
        self.assertAlmostEqual(k['saved'], 200.0)                         # two bills, Rs100 off each
        self.assertAlmostEqual(k['saved_per_visit'], 100.0)
        self.assertEqual(k['expiring_30'], 1)
        self.assertEqual(d['expiring'][0]['name'], 'Asha')
        self.assertEqual(d['expiring'][0]['days'], 10)
        self.assertTrue(d['expiring'][0]['phone'].endswith('0001'))
        self.assertTrue(d['expiring'][0]['phone'].startswith('X'))        # masked
        self.assertEqual(d['compare']['clients'], [1, 2])                 # Bina, Chitra are not members

    # ── Value cards ──
    def test_value_cards(self):
        d = self.get('value-cards')
        k = d['kpis']
        self.assertAlmostEqual(k['outstanding'], 4500.0)                  # 4000 + 500, not the expired card
        self.assertEqual(k['live_cards'], 2)
        self.assertAlmostEqual(k['spent'], 1000.0)                        # face value, not what the card settled
        self.assertEqual((k['spent_invoices'], k['redeeming_clients']), (1, 1))
        self.assertEqual((k['expiring_cards'], k['expired_cards']), (1, 1))
        self.assertAlmostEqual(k['expiring_value'], 500.0)
        self.assertAlmostEqual(k['expired_value'], 200.0)
        self.assertEqual(d['spend_categories'][0]['category'], 'Hair')
        self.assertAlmostEqual(d['spend_categories'][0]['face'], 1000.0)
        # Haircut sold 5 times (5100 incl. the card benefit); the one card-covered line is 1000 of it.
        self.assertAlmostEqual(d['services'][0]['of_sales'], 19.6, places=1)
        self.assertEqual(d['top_balances'][0]['name'], 'Chitra')

    def test_no_access_without_login(self):
        res = APIClient().get('/salon_admin/api/analytics/revenues/')
        self.assertIn(res.status_code, (401, 403))
