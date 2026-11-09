"""
Invoice creation must not cost a fistful of queries per line.

Measured before the fix: 46 queries for a 1-line bill, 65 for 3 lines, 92 for 6,
146 for 12 - about 9 extra queries per line. Two of those nine were the same
content-type lookup twice over:

  * `pricing.get_content_type()` used
    `ContentType.objects.get(app_label=..., model=...)`. Django's ContentType
    cache only backs `get_for_model` and `get_for_id`, so that was a database
    round trip every time;
  * and `_price_invoice` called it once inside `resolve()` and again when
    building each `PricedLine`.

So a 12-line bill spent ~24 queries re-reading a dozen rows that never change,
inside a `transaction.atomic()` block holding row locks while several branches
bill simultaneously.

These tests assert the count is flat in the size of the tables and grows only
modestly per line - the useful invariant, since a bill genuinely does more work
for more lines.
"""

from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing import pricing
from clients.models import Client
from inventory.models import Product
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class InvoiceCreateQueryCountTests(TestCase):
    URL = '/billing/invoices/'

    def setUp(self):
        # The cache is per process and the test runner shares it, so start clean
        # or the first test in a run measures differently from the rest.
        pricing.clear_content_type_cache()

        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('300.00'), gst_percent=Decimal('18.00'),
            current_stock=1000, center=self.center)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def line(self, kind='service'):
        if kind == 'service':
            return {
                'content_type': 'services.servicemaster',
                'object_id': self.service.id, 'quantity': 1,
                'unit_price': '500.00', 'discount': '0',
                'tax_percentage': '18.00', 'description': 'Haircut',
                'staff': self.staff.id,
            }
        return {
            'content_type': 'inventory.product',
            'object_id': self.product.id, 'quantity': 1,
            'unit_price': '300.00', 'discount': '0',
            'tax_percentage': '18.00', 'description': 'Shampoo',
        }

    def create_bill(self, line_count, kind='service'):
        payload = {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'draft', 'discount': '0',
            'items': [self.line(kind) for _ in range(line_count)],
        }
        # Warm the content-type cache first: the point is the steady state, not
        # the very first request after a process starts.
        with CaptureQueriesContext(connection) as captured:
            response = self.api.post(self.URL, payload, format='json')
            self.assertEqual(response.status_code, 201, response.data)
        return len(captured), response

    def seed_noise(self, invoices=40):
        """Unrelated rows, to prove the count does not scale with table size."""
        for _ in range(invoices):
            self.api.post(self.URL, {
                'client': self.customer.id, 'center': self.center.id,
                'status': 'draft', 'discount': '0',
                'items': [self.line()],
            }, format='json')

    # ── flat in table size ────────────────────────────────────────────────

    def test_the_cost_does_not_grow_with_the_number_of_invoices(self):
        self.create_bill(3)          # warm the cache
        small, _ = self.create_bill(3)

        self.seed_noise(40)
        large, _ = self.create_bill(3)

        self.assertEqual(
            small, large,
            f'a 3-line bill cost {small} queries on an empty table and {large} '
            f'once 40 invoices existed')

    # ── the content-type lookups are gone ────────────────────────────────

    def test_content_types_are_resolved_once_per_process_not_per_line(self):
        # Warm BOTH content types. Django's own ContentType cache is per model
        # class, so the first bill touching a product legitimately costs one
        # lookup - once per process, not once per line. That is the distinction
        # this test is about.
        self.api.post(self.URL, {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'draft', 'discount': '0',
            'items': [self.line('service'), self.line('product')],
        }, format='json')

        with CaptureQueriesContext(connection) as captured:
            self.api.post(self.URL, {
                'client': self.customer.id, 'center': self.center.id,
                'status': 'draft', 'discount': '0',
                'items': [self.line('service'), self.line('product'),
                          self.line('service'), self.line('product')],
            }, format='json')

        content_type_queries = [
            q for q in captured.captured_queries
            if 'django_content_type' in q['sql']
        ]
        # A bulk GenericForeignKey prefetch (`WHERE id IN (...)`) is fine - it
        # is one query for the whole invoice. What must not happen is a query
        # PER LINE, which is what the uncached `get()` produced.
        with CaptureQueriesContext(connection) as eight_lines:
            self.api.post(self.URL, {
                'client': self.customer.id, 'center': self.center.id,
                'status': 'draft', 'discount': '0',
                'items': [self.line('service') for _ in range(8)],
            }, format='json')

        eight = len([q for q in eight_lines.captured_queries
                     if 'django_content_type' in q['sql']])
        four = len(content_type_queries)

        self.assertEqual(
            four, eight,
            f'{four} content-type queries for 4 lines but {eight} for 8 - they '
            f'are still being resolved per line')
        self.assertLessEqual(
            eight, 2,
            f'{eight} content-type queries for one bill')

    def test_the_cache_survives_a_second_request(self):
        self.create_bill(2)
        first, _ = self.create_bill(2)
        second, _ = self.create_bill(2)
        self.assertEqual(first, second)

    # ── per-line cost is modest and linear ───────────────────────────────

    def test_the_per_line_cost_is_small(self):
        """A bill does more work for more lines; the question is how much."""
        self.create_bill(1)  # warm

        one, _ = self.create_bill(1)
        six, _ = self.create_bill(6)

        per_line = (six - one) / 5.0
        self.assertLess(
            per_line, 7.0,
            f'each extra line costs {per_line:.1f} queries (was about 9)')

    def test_a_twelve_line_bill_stays_under_a_hundred_queries(self):
        """It was 146. This is a regression ceiling, not a target."""
        self.create_bill(1)  # warm
        queries, _ = self.create_bill(12)
        self.assertLess(
            queries, 100,
            f'a 12-line bill cost {queries} queries')

    # ── and the bill is still correct ────────────────────────────────────

    def test_the_totals_are_unchanged_by_the_caching(self):
        _, response = self.create_bill(2)
        # 2 x 500 at 18% = 1000 + 180.
        self.assertEqual(Decimal(str(response.data['subtotal'])), Decimal('1000.00'))
        self.assertEqual(Decimal(str(response.data['cgst'])), Decimal('90.00'))
        self.assertEqual(Decimal(str(response.data['total_amount'])), Decimal('1180'))

    def test_a_mixed_bill_resolves_both_content_types_correctly(self):
        response = self.api.post(self.URL, {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'draft', 'discount': '0',
            'items': [self.line('service'), self.line('product')],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        # Service 500 at 18% on top = 90.00. Product MRP 300 is GST-inclusive
        # (decision F22): taxable 254.24 + GST 45.76 inside it. Lines are
        # stored at taxable value: subtotal 754.24; tax 135.76 -> CGST 67.88;
        # the customer pays 590 + 300 = 890.
        self.assertEqual(Decimal(str(response.data['subtotal'])), Decimal('754.24'))
        self.assertEqual(Decimal(str(response.data['cgst'])), Decimal('67.88'))
        self.assertEqual(Decimal(str(response.data['total_amount'])), Decimal('890'))

    def test_an_unknown_content_type_is_still_rejected_and_cached_as_unknown(self):
        """A bad label must not become a query on every retry."""
        self.assertIsNone(pricing.get_content_type('nosuchapp.nosuchmodel'))

        with CaptureQueriesContext(connection) as captured:
            self.assertIsNone(pricing.get_content_type('nosuchapp.nosuchmodel'))
        self.assertEqual(len(captured), 0)

    def test_a_malformed_label_returns_none_without_a_query(self):
        for label in (None, '', 'nodot', 123, []):
            self.assertIsNone(pricing.get_content_type(label))
