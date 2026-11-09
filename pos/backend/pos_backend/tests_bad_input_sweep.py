"""Nonsense numbers must never crash the server or be stored.

Every create below is first sent correctly, then once per number with that number turned into
-5, 99999999999 and "abc". Whatever the answer, it must not be a 500, and where the field is a
price, quantity or length the answer must be a refusal. At the end every list is read again:
a bad value that slipped into the database would crash it.
"""
import copy
import datetime
import itertools
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client
from inventory.models import Product, Vendor
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember

BAD = ('-5', '99999999999', 'abc')
TODAY = datetime.date.today()
LATER = TODAY + datetime.timedelta(days=30)

# A refusal is required (not just "no crash") for these: they are money, counts and lengths.
STRICT = {'quantity', 'unit_price', 'default_price', 'price', 'rate', 'mrp', 'duration_mins', 'expiry_days',
          'validity_days', 'amount', 'salary', 'tax_percent', 'gst_percent', 'tax_percentage'}


def leaves(node, path=()):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from leaves(v, path + (k,))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from leaves(v, path + (i,))
    else:
        yield path, node


def numeric(value):
    return not isinstance(value, bool) and (isinstance(value, (int, float)) or (
        isinstance(value, str) and value.lstrip('-').replace('.', '', 1).isdigit()))


def put(node, path, value):
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


class BadInputSweep(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        owner = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='Owner',
                                               role=role, center=self.center, is_superuser=True)
        self.api = APIClient()
        self.api.force_authenticate(owner)
        from finance.models import PettyCashCategory
        self.petty_category = PettyCashCategory.objects.create(name='Tea', sap_account='5411000003')
        self.service = ServiceMaster.objects.create(name='Cut', default_price=Decimal('500'),
                                                    tax_percentage=Decimal('18'), duration_mins=30)
        self.product = Product.objects.create(name='Serum', price=Decimal('300'), mrp=Decimal('500'),
                                              gst_percent=Decimal('18'), current_stock=50, center=self.center,
                                              category='Retail')
        self.vendor = Vendor.objects.create(name='V', center=self.center)
        self.client_row = Client.objects.create(first_name='Asha', phone='9811100001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Meena', designation='Stylist',
                                                salary=Decimal('20000'))
        self.n = itertools.count(1)

    def creates(self):
        """(url, payload factory). A factory takes a counter so every payload is unique."""
        c, s, p, v = self.center.id, self.service.id, self.product.id, self.vendor.id
        line = lambda: {'content_type': 'services.servicemaster', 'object_id': s, 'description': 'x',
                        'unit_price': '500', 'quantity': 1, 'discount': '0'}
        return [
            ('/services/api/master/', lambda n: {'name': f'S{n}', 'category': 'Hair', 'default_price': '100',
                                                  'tax_percentage': '18', 'duration_mins': 30}),
            ('/services/api/center/override/', lambda n: {'center_id': c, 'service_id': s, 'price': '450'}),
            ('/inventory/api/products/', lambda n: {'name': f'P{n}', 'product_code': f'C{n}', 'center': c, 'price': '100',
                                                    'mrp': '118', 'gst_percent': '18', 'reorder_level': 5,
                                                    'reorder_quantity': 10}),
            ('/inventory/api/purchase-orders/', lambda n: {'vendor': v, 'center': c, 'invoice_number': f'I{n}', 'items': [
                {'product': p, 'quantity': '5', 'rate': '100', 'discount_percent': '0', 'tax_percent': '18'}]}),
            ('/inventory/api/stock_transfers/', lambda n: {'from_center': c, 'to_center': self.other.id, 'items': [
                {'product': p, 'quantity': '2'}]}),
            ('/inventory/api/stock_requests/', lambda n: {'requesting_center': c, 'stock_type': 'Retail', 'items': [
                {'product': p, 'quantity': '2'}]}),
            ('/inventory/api/products/audit/', lambda n: {'center_id': c, 'items': [{'product_id': p, 'quantity': 40}]}),
            ('/billing/invoices/quote/', lambda n: {'center': c, 'client': self.client_row.id, 'status': 'draft',
                                                     'discount': '0', 'items': [line()]}),
            ('/billing/invoices/', lambda n: {'center': c, 'client': self.client_row.id, 'status': 'draft',
                                              'discount': '0', 'items': [line()]}),
            ('/marketing/api/promotions/', lambda n: {'name': f'Promo{n}', 'promo_type': 'Discount', 'level': 'Organisation',
                                                      'discount_type': 'Percentage', 'discount_value': '10',
                                                      'start_date': str(TODAY), 'end_date': str(LATER), 'config': {}}),
            ('/marketing/api/memberships/', lambda n: {'name': f'M{n}', 'expiry_days': 365, 'post_tax_price': '1180',
                                                       'pre_tax_price': '1000', 'gst_percent': '18',
                                                       'discount_percent': '10'}),
            ('/marketing/api/cards/', lambda n: {'title': f'Card{n}', 'expiry_days': 365, 'post_tax_price': '1180',
                                                  'pre_tax_price': '1000', 'gst_percent': '18', 'value': '1500',
                                                  'benefit_percent': '10'}),
            ('/marketing/api/packages/', lambda n: {'name': f'Pk{n}', 'price': '1200', 'validity_days': 90,
                                                    'gst_percent': '18', 'services_json': [{'id': s, 'pkgQty': 3}]}),
            ('/inventory/api/vendors/', lambda n: {'name': f'Vendor{n}', 'center': c, 'phone': '9810000001'}),
            ('/staff/api/members/', lambda n: {'first_name': f'St{n}', 'designation': 'Stylist', 'center': c,
                                               'salary': '20000', 'commission_percentage': '10'}),
            ('/staff/api/tools/', lambda n: {'tool_name': f'T{n}', 'date_taken': str(TODAY), 'staff': self.staff.id,
                                              'amount': 100, 'details': 'x'}),
        ]

    def test_bad_numbers_never_crash_and_money_is_never_stored_wrong(self):
        crashes, accepted = [], []
        for url, make in self.creates():
            base = make(next(self.n))
            base_ok = self.api.post(url, base, format='json').status_code in (200, 201)
            paths = [(path, value) for path, value in leaves(base)
                     if path and numeric(value) and not str(path[-1]).endswith('id')
                     and path[-1] not in ('center', 'client', 'product', 'vendor', 'staff', 'stock_type')]
            for path, _ in paths:
                for bad in BAD:
                    body = copy.deepcopy(make(next(self.n)))
                    put(body, path, bad)
                    res = self.api.post(url, body, format='json')
                    label = f'{url} {".".join(map(str, path))}={bad}'
                    if res.status_code >= 500:
                        crashes.append(f'{label} -> {res.status_code}')
                    elif base_ok and res.status_code in (200, 201) and path[-1] in STRICT:
                        accepted.append(f'{label} -> {res.status_code}')
        self.assertEqual(crashes, [], 'crashed:\n' + '\n'.join(crashes))
        self.assertEqual(accepted, [], 'accepted bad money/quantity:\n' + '\n'.join(accepted))

        # Nothing odd reached the database: every list still reads.
        for url in ('/services/api/master/', '/services/api/center/', '/inventory/api/products/', '/inventory/api/purchase-orders/',
                    '/inventory/api/stock_transfers/', '/billing/invoices/', '/marketing/api/promotions/',
                    '/marketing/api/memberships/', '/marketing/api/cards/', '/marketing/api/packages/',
                    '/finance/api/petty-cash/', '/staff/api/members/'):
            self.assertEqual(self.api.get(url).status_code, 200, url)
        quote = self.api.post('/billing/invoices/quote/', {
            'center': self.center.id, 'client': self.client_row.id, 'status': 'draft', 'discount': '0',
            'items': [{'content_type': 'services.servicemaster', 'object_id': self.service.id, 'description': 'x',
                       'unit_price': '500', 'quantity': 1, 'discount': '0'}]}, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)
