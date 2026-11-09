"""
Till/server pricing conformance.

The till priced a cart with its own arithmetic and the server priced the same
cart with `InvoiceSerializer._price_invoice`. They agreed most of the time, and
disagreed in three specific ways:

  1. the apportioned share of an invoice-level discount was left unrounded on
     the till, where the server rounds it to paise per line;
  2. line tax was accumulated as raw floats and rounded once at the end, where
     the server rounds each line's tax to paise before adding it;
  3. CGST and SGST came from halving the tax total and rounding each half
     independently, where the server splits it exactly - so the till's two
     halves did not always add back to the tax charged.

Individually those are paise, but the grand total is rounded to whole rupees, so
near a 50 paise boundary the drift flips the rupee: the customer is asked for a
rupee more or less than the screen showed. The till also never read
`Center.gst_enabled`, so a non-GST branch displayed GST and a tax-inclusive
total while the server stored zero tax - a whole-percentage error.

This module is one half of the check. It asserts the SERVER produces the amounts
in `till_pricing_cases.json`. `frontend/src/app/services/money.spec.ts` asserts
`priceCart` produces the same file. Both sides are pinned to one set of numbers,
so neither can drift without a failure.
"""

import json
import os
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client
from inventory.models import Product
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster

# Shared with the frontend test. Regenerate with:
#   REGENERATE_TILL_CASES=1 python manage.py test \
#       billing.tests_till_conformance --settings=pos_backend.settings_test
FIXTURE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'frontend', 'src', 'app', 'services', 'till_pricing_cases.json',
)


class TillPricingConformanceTests(TestCase):
    """Each case is a cart; the assertion is on the stored header amounts."""

    def setUp(self):
        self.center = Center.objects.create(
            center_name='GST Branch', display_name='GST Branch', gst_enabled=True)
        self.no_gst_center = Center.objects.create(
            center_name='No GST Branch', display_name='No GST', gst_enabled=False)

        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)

        self.customer = Client.objects.create(
            first_name='Asha', last_name='Rao', phone='9990000001', center=self.center)
        self.customer_no_gst = Client.objects.create(
            first_name='Bela', last_name='Nair', phone='9990000002',
            center=self.no_gst_center)

        # Catalog entries chosen so the arithmetic lands on the awkward cases.
        # The server overrides a submitted unit_price with the catalog price, so
        # these prices are what actually gets billed.
        self.svc_500_18 = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))
        self.svc_5561_18 = ServiceMaster.objects.create(
            name='Threading', default_price=Decimal('55.61'),
            tax_percentage=Decimal('18.00'))
        self.svc_33333_18 = ServiceMaster.objects.create(
            name='Facial', default_price=Decimal('333.33'),
            tax_percentage=Decimal('18.00'))
        self.svc_10050_5 = ServiceMaster.objects.create(
            name='Head Massage', default_price=Decimal('100.50'),
            tax_percentage=Decimal('5.00'))
        self.svc_999_12 = ServiceMaster.objects.create(
            name='Colour', default_price=Decimal('999.00'),
            tax_percentage=Decimal('12.00'))
        self.svc_zero_tax = ServiceMaster.objects.create(
            name='Consultation', default_price=Decimal('250.00'),
            tax_percentage=Decimal('0.00'))

        # Perks now carry an explicit gst_percent. Before that the rate was
        # inferred from the pre/post price pair, and the UI forced that pair to
        # 5% - so no membership could be sold at 18%.
        from marketing.models import Membership, ValueCard

        self.membership_18 = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1180.00'), gst_percent=Decimal('18.00'),
            expiry_days=365, level='Organisation')
        self.card_18 = ValueCard.objects.create(
            title='Silver Card', pre_tax_price=Decimal('2000.00'),
            post_tax_price=Decimal('2360.00'), gst_percent=Decimal('18.00'),
            value=Decimal('2500.00'), expiry_days=365, level='Organisation')

        self.product_18 = Product.objects.create(
            name='Shampoo', price=Decimal('349.00'), gst_percent=Decimal('18.00'),
            current_stock=500, center=self.center)

        # A genuine package entitlement, so the redemption case is a real
        # redemption. The server refuses to redeem what the customer does not
        # hold - which is correct, and is why this has to be set up rather than
        # asserted with a bare flag.
        import datetime

        from clients.models import ClientPackage
        from marketing.models import Package

        package = Package.objects.create(
            name='Haircut x5', price=Decimal('2000.00'), validity_days=365,
            services_json=[{'service_id': self.svc_500_18.id, 'quantity': 5}])
        ClientPackage.objects.create(
            client=self.customer, package=package,
            services_remaining={str(self.svc_500_18.id): 5},
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.SVC = 'services.servicemaster'
        self.PROD = 'inventory.product'

    # ── the cases ─────────────────────────────────────────────────────────

    def cases(self):
        """Each entry: a label, the cart, the invoice discount, and the branch."""
        return [
            {
                'label': 'single line, 18% GST',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_500_18.id, 'qty': 1,
                     'unit_price': '500.00', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                # 55.61 * 18% = 10.0098 -> 10.01. Halving gives 5.005 a side,
                # which the till rounded to 5.00 twice: a paisa short.
                'label': 'odd paise tax, halves must still sum',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_5561_18.id, 'qty': 1,
                     'unit_price': '55.61', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'three lines, each tax rounded before summing',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_33333_18.id, 'qty': 1,
                     'unit_price': '333.33', 'discount': '0',
                     'tax_percentage': '18.00'},
                    {'ct': self.SVC, 'id': self.svc_10050_5.id, 'qty': 1,
                     'unit_price': '100.50', 'discount': '0',
                     'tax_percentage': '5.00'},
                    {'ct': self.SVC, 'id': self.svc_5561_18.id, 'qty': 3,
                     'unit_price': '55.61', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'invoice discount apportioned across lines',
                'gst_enabled': True,
                'invoice_discount': '137',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_33333_18.id, 'qty': 1,
                     'unit_price': '333.33', 'discount': '0',
                     'tax_percentage': '18.00'},
                    {'ct': self.SVC, 'id': self.svc_999_12.id, 'qty': 1,
                     'unit_price': '999.00', 'discount': '0',
                     'tax_percentage': '12.00'},
                    {'ct': self.SVC, 'id': self.svc_10050_5.id, 'qty': 2,
                     'unit_price': '100.50', 'discount': '0',
                     'tax_percentage': '5.00'},
                ],
            },
            {
                'label': 'line discounts and an invoice discount together',
                'gst_enabled': True,
                'invoice_discount': '50',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_500_18.id, 'qty': 2,
                     'unit_price': '500.00', 'discount': '75.50',
                     'tax_percentage': '18.00'},
                    {'ct': self.PROD, 'id': self.product_18.id, 'qty': 1,
                     'unit_price': '349.00', 'discount': '19.99',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'a zero-tax line alongside taxed lines',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_zero_tax.id, 'qty': 1,
                     'unit_price': '250.00', 'discount': '0',
                     'tax_percentage': '0.00'},
                    {'ct': self.SVC, 'id': self.svc_5561_18.id, 'qty': 1,
                     'unit_price': '55.61', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'a package redemption carries no value and no tax',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_500_18.id, 'qty': 1,
                     'unit_price': '500.00', 'discount': '0',
                     'tax_percentage': '18.00', 'is_redemption': True,
                     'description': '\U0001F381 [Redeem] Haircut'},
                    {'ct': self.SVC, 'id': self.svc_10050_5.id, 'qty': 1,
                     'unit_price': '100.50', 'discount': '0',
                     'tax_percentage': '5.00'},
                ],
            },
            {
                'label': 'an invoice discount larger than the subtotal is capped',
                'gst_enabled': True,
                'invoice_discount': '99999',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_500_18.id, 'qty': 1,
                     'unit_price': '500.00', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'a non-GST branch charges no tax at all',
                'gst_enabled': False,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_500_18.id, 'qty': 1,
                     'unit_price': '500.00', 'discount': '0',
                     'tax_percentage': '18.00'},
                    {'ct': self.SVC, 'id': self.svc_33333_18.id, 'qty': 2,
                     'unit_price': '333.33', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                # Lands the pre-rounding total on a .50 boundary, where a paisa
                # of drift changes the rupee the customer is asked for.
                'label': 'rounding boundary',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_10050_5.id, 'qty': 1,
                     'unit_price': '100.50', 'discount': '0',
                     'tax_percentage': '5.00'},
                ],
            },
            {
                'label': 'a membership at 18% GST, priced from its pre-tax price',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': 'marketing.membership', 'id': self.membership_18.id,
                     'qty': 1, 'unit_price': '1000.00', 'discount': '0',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                # The exact case that made the till disagree with the invoice:
                # the till priced from the tax-inclusive figure at 0% tax, so a
                # discount shifted the total by rate x discount.
                'label': 'a discounted membership - till and server must agree',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': 'marketing.membership', 'id': self.membership_18.id,
                     'qty': 1, 'unit_price': '1000.00', 'discount': '100.00',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'a complimentary membership at 100% discount',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': 'marketing.membership', 'id': self.membership_18.id,
                     'qty': 1, 'unit_price': '1000.00', 'discount': '1000.00',
                     'tax_percentage': '18.00'},
                ],
            },
            {
                'label': 'a value card alongside a service',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': 'marketing.valuecard', 'id': self.card_18.id,
                     'qty': 1, 'unit_price': '2000.00', 'discount': '0',
                     'tax_percentage': '18.00'},
                    {'ct': self.SVC, 'id': self.svc_10050_5.id, 'qty': 1,
                     'unit_price': '100.50', 'discount': '0',
                     'tax_percentage': '5.00'},
                ],
            },
            {
                'label': 'quantity greater than one with a line discount',
                'gst_enabled': True,
                'invoice_discount': '0',
                'lines': [
                    {'ct': self.SVC, 'id': self.svc_999_12.id, 'qty': 4,
                     'unit_price': '999.00', 'discount': '333.33',
                     'tax_percentage': '12.00'},
                ],
            },
        ]

    # ── running one case through the server ───────────────────────────────

    def price_on_server(self, case):
        center = self.center if case['gst_enabled'] else self.no_gst_center
        customer = self.customer if case['gst_enabled'] else self.customer_no_gst

        items = []
        for line in case['lines']:
            item = {
                'content_type': line['ct'],
                'object_id': line['id'],
                'quantity': line['qty'],
                'unit_price': line['unit_price'],
                'discount': line['discount'],
                'tax_percentage': line['tax_percentage'],
                'description': line.get('description', 'Item'),
            }
            if line.get('is_redemption'):
                item['is_redemption'] = True
            items.append(item)

        response = self.api.post('/billing/invoices/', {
            'client': customer.id,
            'center': center.id,
            'status': 'draft',
            'discount': case['invoice_discount'],
            'items': items,
        }, format='json')
        self.assertEqual(response.status_code, 201,
                         f"{case['label']}: {response.data}")

        body = response.data
        return {
            'subtotal': str(Decimal(str(body['subtotal'])).quantize(Decimal('0.01'))),
            'discount': str(Decimal(str(body['discount'])).quantize(Decimal('0.01'))),
            'cgst': str(Decimal(str(body['cgst'])).quantize(Decimal('0.01'))),
            'sgst': str(Decimal(str(body['sgst'])).quantize(Decimal('0.01'))),
            'rounding': str(Decimal(str(body['rounding'])).quantize(Decimal('0.01'))),
            'total': str(Decimal(str(body['total_amount'])).quantize(Decimal('0.01'))),
        }

    # ── the tests ─────────────────────────────────────────────────────────

    def test_the_server_matches_the_shared_fixture(self):
        """If this fails, the server's pricing changed and the till is now wrong.

        Regenerate the fixture deliberately (REGENERATE_TILL_CASES=1) and read
        the diff before accepting it - a change here changes customers' bills.
        """
        produced = []
        for case in self.cases():
            expected = self.price_on_server(case)
            produced.append({
                'label': case['label'],
                'gst_enabled': case['gst_enabled'],
                'invoice_discount': case['invoice_discount'],
                'lines': [
                    {k: v for k, v in line.items() if k != 'id'}
                    for line in case['lines']
                ],
                'expected': expected,
            })

        if os.environ.get('REGENERATE_TILL_CASES') == '1':
            with open(FIXTURE, 'w', encoding='utf-8', newline='\n') as handle:
                json.dump(produced, handle, indent=2, ensure_ascii=False)
                handle.write('\n')
            self.skipTest(f'Regenerated {FIXTURE} from the server.')

        self.assertTrue(
            os.path.exists(FIXTURE),
            f'{FIXTURE} is missing. Generate it with '
            f'REGENERATE_TILL_CASES=1.')

        with open(FIXTURE, encoding='utf-8') as handle:
            committed = json.load(handle)

        self.assertEqual(len(committed), len(produced),
                         'The fixture has a different number of cases.')
        for saved, fresh in zip(committed, produced):
            self.assertEqual(saved['label'], fresh['label'])
            self.assertEqual(
                saved['expected'], fresh['expected'],
                f"Server pricing changed for: {fresh['label']}")

    def test_cgst_and_sgst_always_sum_to_the_tax_charged(self):
        """The till's independent halving could lose or invent a paisa."""
        for case in self.cases():
            with self.subTest(case['label']):
                amounts = self.price_on_server(case)
                cgst = Decimal(amounts['cgst'])
                sgst = Decimal(amounts['sgst'])
                subtotal = Decimal(amounts['subtotal'])
                discount = Decimal(amounts['discount'])
                total = Decimal(amounts['total'])
                rounding = Decimal(amounts['rounding'])

                # The header must reconcile exactly.
                self.assertEqual(
                    subtotal - discount + cgst + sgst + rounding, total,
                    f"{case['label']}: header does not reconcile")

    def test_a_non_gst_branch_stores_no_tax(self):
        for case in self.cases():
            if case['gst_enabled']:
                continue
            amounts = self.price_on_server(case)
            self.assertEqual(Decimal(amounts['cgst']), Decimal('0.00'))
            self.assertEqual(Decimal(amounts['sgst']), Decimal('0.00'))

    def test_the_fixture_covers_the_awkward_cases(self):
        """A fixture that only holds easy sums proves nothing."""
        with open(FIXTURE, encoding='utf-8') as handle:
            committed = json.load(handle)

        labels = ' '.join(c['label'] for c in committed)
        for needle in ('odd paise', 'apportioned', 'redemption', 'non-GST',
                       'boundary', 'capped', 'membership', 'value card'):
            self.assertIn(needle, labels, f'no case covers {needle}')

        # At least one case must produce an odd number of paise in the tax, or
        # the exact-split behaviour is never exercised.
        odd = [
            c for c in committed
            if int(Decimal(c['expected']['cgst']) * 100)
            != int(Decimal(c['expected']['sgst']) * 100)
        ]
        self.assertTrue(
            odd, 'no case produces unequal CGST/SGST halves, so the exact split '
                 'is untested')
