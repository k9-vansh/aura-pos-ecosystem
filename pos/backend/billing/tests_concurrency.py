"""
Many tills billing at once must never collide.

These run real threads against real commits (TransactionTestCase), so they are
only meaningful on MySQL - SQLite serialises writers and would pass anyway:

    TEST_DB_ENGINE=mysql DB_ENGINE=mysql manage.py test billing.tests_concurrency \
        --settings=pos_backend.settings_test

What must hold however the requests interleave:
  * every bill at a branch gets a unique, gap-free GST number;
  * stock goes down by exactly what was sold, and never below zero;
  * a branch's last unit is sold once, not twice;
  * a database deadlock is never shown to a cashier as a failed bill.
"""

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from django.db import connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from inventory.models import Product, StockTransaction
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role


class ConcurrentBillingTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor == 'sqlite':
            self.skipTest('needs a server database with row-level locking (MySQL)')
        # TransactionTestCase flushes content types between tests; drop the
        # ids cached by the previous one.
        from django.contrib.contenttypes.models import ContentType
        from billing import pricing
        ContentType.objects.clear_cache()
        pricing.clear_content_type_cache()
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner', role=self.role)
        self.centers = [Center.objects.create(center_name=f'Branch {i}', gst_enabled=True)
                        for i in range(4)]
        self.clients = {
            c.id: [Client.objects.create(first_name=f'C{i}', phone=f'99900{c.id:03d}{i:02d}', center=c)
                   for i in range(30)]
            for c in self.centers}

    def product(self, center, stock):
        return Product.objects.create(name='Shampoo', price=Decimal('300.00'), gst_percent=Decimal('18.00'),
                                      current_stock=stock, center=center)

    def bill(self, center, product, client, quantity=1):
        api = APIClient()
        api.force_authenticate(self.owner)
        try:
            return bill_and_pay(api, {
                'client': client.id, 'center': center.id, 'status': 'paid', 'discount': '0',
                'items': [{'content_type': 'inventory.product', 'object_id': product.id,
                           'quantity': quantity, 'unit_price': '300.00', 'discount': '0',
                           'tax_percentage': '18.00', 'description': 'Shampoo'}],
            })
        finally:
            connections.close_all()

    def run_all(self, jobs):
        barrier = threading.Barrier(len(jobs))

        def go(job):
            barrier.wait()
            return self.bill(*job)

        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            return list(pool.map(go, jobs))

    def serials(self, center):
        nums = list(Invoice.objects.filter(center=center).exclude(invoice_number__isnull=True)
                    .values_list('invoice_number', flat=True))
        return sorted(int(re.search(r'-(\d+)$', n).group(1)) for n in nums)

    def test_one_branch_billing_at_once_gets_unique_gapless_numbers_and_exact_stock(self):
        center = self.centers[0]
        product = self.product(center, 100)
        results = self.run_all([(center, product, self.clients[center.id][i]) for i in range(16)])

        self.assertEqual([r.status_code for r in results], [201] * 16, [r.data for r in results if r.status_code != 201])
        self.assertEqual(self.serials(center), list(range(1, 17)))
        product.refresh_from_db()
        self.assertEqual(product.current_stock, 84)
        self.assertEqual(StockTransaction.objects.filter(product=product, transaction_type='SALE').count(), 16)

    def test_four_branches_at_once_do_not_disturb_each_other(self):
        products = {c.id: self.product(c, 50) for c in self.centers}
        jobs = [(c, products[c.id], self.clients[c.id][i]) for i in range(8) for c in self.centers]
        results = self.run_all(jobs)

        self.assertEqual([r.status_code for r in results], [201] * 32, [r.data for r in results if r.status_code != 201])
        for c in self.centers:
            self.assertEqual(self.serials(c), list(range(1, 9)))
            products[c.id].refresh_from_db()
            self.assertEqual(products[c.id].current_stock, 42)

    def test_the_last_units_are_sold_once_not_twice(self):
        center = self.centers[1]
        product = self.product(center, 5)
        results = self.run_all([(center, product, self.clients[center.id][i]) for i in range(12)])

        codes = sorted(r.status_code for r in results)
        self.assertEqual(codes, [201] * 5 + [400] * 7, [r.data for r in results if r.status_code not in (201, 400)])
        product.refresh_from_db()
        self.assertEqual(product.current_stock, 0)
        self.assertEqual(Invoice.objects.filter(center=center, status='paid').count(), 5)
        self.assertEqual(self.serials(center), list(range(1, 6)))
