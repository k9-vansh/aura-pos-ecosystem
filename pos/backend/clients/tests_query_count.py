"""
The customer list must not cost two queries per customer.

`ClientSerializer.get_advance_balance` checks for `advance_balance_annotated` and
otherwise falls back to `Client.advance_balance`, a model property that runs its
own `aggregate(Sum(...))`. Nothing ever set the annotation - `get_queryset` ended
with the comment "Removed Cartesian product annotations causing balance
inflation" - so the fallback fired for every row, twice (advance and cashback).

Measured before the fix: 107 queries for 50 customers, 507 for 250. Exactly 2.00
extra per row. At the 15,000 customers this business expects, one request to the
customer list would issue roughly 30,000 queries. The till hits this endpoint on
every customer search.

That old comment describes a real bug, and the fix must not bring it back:
annotating with JOINs multiplies rows when a customer has both advances and
cashback, and both sums come out inflated. Two independent correlated SUBQUERIES
cannot multiply rows. Both properties are asserted here - the query count AND the
arithmetic.
"""

from decimal import Decimal

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, CashbackTransaction
from clients.models import Client
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class ClientListQueryCountTests(TestCase):
    URL = '/clients/api/clients/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def seed(self, count, with_wallets=True):
        for i in range(count):
            customer = Client.objects.create(
                first_name=f'Customer{i}', phone=f'9{Client.objects.count():09d}', center=self.center)
            if with_wallets:
                AdvancePayment.objects.create(
                    client=customer, amount=Decimal('100.00'), notes='top up')
                CashbackTransaction.objects.create(
                    client=customer, amount=Decimal('50.00'), notes='earned')

    def count_queries(self, **params):
        with CaptureQueriesContext(connection) as captured:
            response = self.api.get(self.URL, params)
            self.assertEqual(response.status_code, 200, response.data)
        return len(captured), response

    # ── the query count must not grow with the number of customers ────────

    def test_the_query_count_is_flat_in_the_number_of_customers(self):
        """This is the whole finding: it was exactly 2.00 queries per row."""
        self.seed(20)
        small, _ = self.count_queries(page=1, page_size=20)

        self.seed(80)  # 100 total
        large, _ = self.count_queries(page=1, page_size=20)

        self.assertEqual(
            small, large,
            f'the customer list cost {small} queries for 20 customers and '
            f'{large} for 100 - it is still scaling per row')

    def test_a_bigger_page_does_not_cost_more_queries(self):
        self.seed(100)
        ten, _ = self.count_queries(page=1, page_size=10)
        hundred, _ = self.count_queries(page=1, page_size=100)

        self.assertEqual(
            ten, hundred,
            f'{ten} queries for 10 rows but {hundred} for 100 - the per-row '
            f'aggregate is still there')

    def test_the_page_costs_a_small_fixed_number_of_queries(self):
        """A regression guard with a real ceiling, not a vague one.

        It was 108 for a single page of 50. If this ever climbs back into the
        dozens, something has started querying per row again.
        """
        self.seed(50)
        queries, _ = self.count_queries(page=1, page_size=50)
        self.seed(200)
        big, _ = self.count_queries(page=1, page_size=50)
        self.assertEqual(queries, big, 'query count grew with table size')
        self.assertLess(
            queries, 15,
            f'a single page of 50 customers cost {queries} queries')

    # ── and the balances must still be right ──────────────────────────────

    def test_the_balances_are_correct_with_both_wallets_populated(self):
        """The inflation bug the old comment describes.

        A JOIN-based annotation multiplies rows when a customer has BOTH an
        advance and a cashback row, so each sum is multiplied by the other's
        count. Correlated subqueries do not.
        """
        customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        # Three of each: a join would give 3x3 = 9 rows and triple both sums.
        for _ in range(3):
            AdvancePayment.objects.create(
                client=customer, amount=Decimal('100.00'), notes='top up')
            CashbackTransaction.objects.create(
                client=customer, amount=Decimal('50.00'), notes='earned')

        _, response = self.count_queries(page=1, page_size=10)
        row = next(r for r in response.data['results'] if r['id'] == customer.id)

        self.assertEqual(Decimal(str(row['advance_balance'])), Decimal('300.00'))
        self.assertEqual(Decimal(str(row['cashback_balance'])), Decimal('150.00'))

    def test_the_annotated_balance_matches_the_model_property(self):
        """The annotation and the property must never disagree - one is used on
        the list, the other on the detail view."""
        customer = Client.objects.create(
            first_name='Bela', phone='9990000002', center=self.center)
        AdvancePayment.objects.create(
            client=customer, amount=Decimal('750.50'), notes='top up')
        AdvancePayment.objects.create(
            client=customer, amount=Decimal('-200.25'), notes='redeemed')
        CashbackTransaction.objects.create(
            client=customer, amount=Decimal('99.99'), notes='earned')

        _, response = self.count_queries(page=1, page_size=10)
        row = next(r for r in response.data['results'] if r['id'] == customer.id)

        fresh = Client.objects.get(pk=customer.pk)
        self.assertEqual(Decimal(str(row['advance_balance'])), fresh.advance_balance)
        self.assertEqual(Decimal(str(row['cashback_balance'])), fresh.cashback_balance)

    def test_a_customer_with_no_wallet_rows_reads_zero_not_null(self):
        Client.objects.create(first_name='Chitra', phone='9990000003',
                              center=self.center)
        _, response = self.count_queries(page=1, page_size=10)
        row = response.data['results'][0]
        self.assertEqual(Decimal(str(row['advance_balance'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(row['cashback_balance'])), Decimal('0.00'))

    def test_a_redeemed_wallet_can_go_to_zero(self):
        customer = Client.objects.create(
            first_name='Devi', phone='9990000004', center=self.center)
        AdvancePayment.objects.create(
            client=customer, amount=Decimal('500.00'), notes='top up')
        AdvancePayment.objects.create(
            client=customer, amount=Decimal('-500.00'), notes='spent')

        _, response = self.count_queries(page=1, page_size=10)
        row = next(r for r in response.data['results'] if r['id'] == customer.id)
        self.assertEqual(Decimal(str(row['advance_balance'])), Decimal('0.00'))

    def test_clients_are_global(self):
        """Owner decision #11: a client of any branch is found from every branch."""
        other = Center.objects.create(center_name='Other')
        Client.objects.create(first_name='Elsewhere', phone='9990000005',
                              center=other)
        Client.objects.create(first_name='Here', phone='9990000006',
                              center=self.center)

        limited_role = Role.objects.create(name='Manager', permissions={
            'admin': {'clients': {'read': True}}})
        manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=limited_role, center=self.center)
        manager.centers.set([self.center])

        client = APIClient()
        client.force_authenticate(manager)
        response = client.get(self.URL, {'page': 1, 'page_size': 50})
        self.assertEqual(response.status_code, 200, response.data)

        names = {r['first_name'] for r in response.data['results']}
        self.assertIn('Here', names)
        self.assertIn("Elsewhere", names)
