"""
The Usage screen.

Its purpose is to answer "are these perks actually being used, or only sold?"
It could not answer that, because it only ever counted sales:

  * a package sold once and drawn down eight times read as 1 - a redemption
    writes an InvoiceItem for the SERVICE with `is_redemption=True`, which the
    report never looked at;
  * paying with a value card writes a Payment row, not an InvoiceItem, so card
    spend was invisible;
  * the status filter included 'draft', so unfinished bills still open in the
    till were counted as sales - and every other report in the POS counts
    ['paid', 'partial'], so Usage and Finance disagreed about the same month;
  * the branch filter read a `Center-Id` header that nothing sends;
  * a non-owner was scoped by `user.center` alone, ignoring the `centers`
    many-to-many;
  * `Count('id')` counted invoice lines, so three memberships on one line was 1.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem, Payment
from clients.models import Client, ClientPackage, ClientValueCard
from marketing.models import Membership, Package, ValueCard
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster


class UsageReportTests(TestCase):
    URL = '/marketing/api/promotions/usage_report/'

    def setUp(self):
        self.branch_a = Center.objects.create(
            center_name='Branch A', display_name='A', gst_enabled=True)
        self.branch_b = Center.objects.create(
            center_name='Branch B', display_name='B', gst_enabled=True)

        self.owner_role = Role.objects.create(
            name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.branch_a)

        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.branch_a)

        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))

        self.package = Package.objects.create(
            name='Haircut x5', price=Decimal('2000.00'), validity_days=365,
            services_json=[{'service_id': self.service.id, 'quantity': 5}],
            level='Organisation')
        self.membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('5000.00'), expiry_days=365,
            level='Organisation')
        # ValueCard's label field is `title`, not `name`.
        self.card = ValueCard.objects.create(
            title='Silver Card', pre_tax_price=Decimal('1000.00'),
            value=Decimal('1200.00'), expiry_days=365, level='Organisation')

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    # ── helpers ───────────────────────────────────────────────────────────

    def invoice(self, center=None, status=Invoice.STATUS_PAID):
        return Invoice.objects.create(
            client=self.customer, center=center or self.branch_a,
            status=status, subtotal=Decimal('0'), total_amount=Decimal('0'))

    def sell(self, obj, invoice=None, quantity=1, price='1000.00'):
        from django.contrib.contenttypes.models import ContentType

        invoice = invoice or self.invoice()
        return InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(obj),
            object_id=obj.id,
            description=getattr(obj, 'name', None) or obj.title,
            unit_price=Decimal(price), quantity=quantity,
            total_price=Decimal(price) * quantity,
        )

    def redeem_package(self, client_package, invoice=None, quantity=1):
        from django.contrib.contenttypes.models import ContentType

        invoice = invoice or self.invoice()
        return InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(self.service),
            object_id=self.service.id,
            description='\U0001F381 [Redeem] Haircut',
            unit_price=Decimal('500.00'), quantity=quantity,
            total_price=Decimal('0'),
            is_redemption=True,
            redeemed_from_package=client_package,
        )

    def client_package(self):
        return ClientPackage.objects.create(
            client=self.customer, package=self.package,
            services_remaining={str(self.service.id): 5},
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

    def fetch(self, **params):
        response = self.api.get(self.URL, params)
        self.assertEqual(response.status_code, 200, response.data)
        return {row['id']: row for row in response.data}

    # ── sales ─────────────────────────────────────────────────────────────

    def test_a_perk_with_no_activity_still_appears(self):
        rows = self.fetch()
        self.assertIn(f'package_{self.package.id}', rows)
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)
        self.assertEqual(rows[f'package_{self.package.id}']['used'], 0)

    def test_a_sale_is_counted(self):
        self.sell(self.package, price='2000.00')
        rows = self.fetch()
        row = rows[f'package_{self.package.id}']
        self.assertEqual(row['sold'], 1)
        self.assertEqual(Decimal(str(row['revenue'])), Decimal('2000.00'))

    def test_quantity_is_counted_not_lines(self):
        """Three memberships on one line used to count as one."""
        self.sell(self.membership, quantity=3, price='5000.00')
        rows = self.fetch()
        self.assertEqual(rows[f'membership_{self.membership.id}']['sold'], 3)

    def test_a_draft_invoice_is_not_a_sale(self):
        """A bill still open in the till is not revenue, and every other report
        in the POS agrees. Usage was the only screen that counted it."""
        draft = self.invoice(status=Invoice.STATUS_DRAFT)
        self.sell(self.package, invoice=draft, price='2000.00')

        rows = self.fetch()
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)
        self.assertEqual(Decimal(str(rows[f'package_{self.package.id}']['revenue'])),
                         Decimal('0'))

    def test_a_cancelled_invoice_is_not_a_sale(self):
        cancelled = self.invoice(status=Invoice.STATUS_CANCELLED)
        self.sell(self.package, invoice=cancelled, price='2000.00')
        rows = self.fetch()
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)

    def test_a_partial_invoice_is_a_sale(self):
        partial = self.invoice(status=Invoice.STATUS_PARTIAL)
        self.sell(self.package, invoice=partial, price='2000.00')
        rows = self.fetch()
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 1)

    # ── the flow gap: redemptions ─────────────────────────────────────────

    def test_package_redemptions_are_counted_as_use(self):
        """Sold once, drawn down three times: it used to read as 1."""
        held = self.client_package()
        self.sell(self.package, price='2000.00')
        self.redeem_package(held)
        self.redeem_package(held)
        self.redeem_package(held)

        rows = self.fetch()
        row = rows[f'package_{self.package.id}']
        self.assertEqual(row['sold'], 1)
        self.assertEqual(row['used'], 3)

    def test_a_redemption_adds_no_revenue(self):
        held = self.client_package()
        self.redeem_package(held)
        rows = self.fetch()
        self.assertEqual(
            Decimal(str(rows[f'package_{self.package.id}']['revenue'])), Decimal('0'))

    def test_a_redemption_on_a_draft_is_not_counted(self):
        held = self.client_package()
        self.redeem_package(held, invoice=self.invoice(status=Invoice.STATUS_DRAFT))
        rows = self.fetch()
        self.assertEqual(rows[f'package_{self.package.id}']['used'], 0)

    def test_a_redemption_is_not_double_counted_as_a_sale(self):
        """The sale query excludes redemption lines."""
        held = self.client_package()
        self.redeem_package(held)
        rows = self.fetch()
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)

    def test_value_card_spend_is_counted_as_use(self):
        """Paying by card writes a Payment, not an InvoiceItem."""
        held_card = ClientValueCard.objects.create(
            client=self.customer, value_card=self.card,
            balance=Decimal('1000.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

        invoice = self.invoice()
        Payment.objects.create(
            invoice=invoice, payment_method=Payment.VALUE_CARD,
            amount=Decimal('300.00'), value_card_id=held_card.id)

        rows = self.fetch()
        self.assertEqual(rows[f'valuecard_{self.card.id}']['used'], 1)

    def test_membership_holders_are_reported(self):
        from clients.models import ClientMembership

        ClientMembership.objects.create(
            client=self.customer, membership=self.membership,
            start_date=datetime.date.today(),
            expiry_date=datetime.date.today() + datetime.timedelta(days=365))

        rows = self.fetch()
        self.assertEqual(rows[f'membership_{self.membership.id}'].get('holders'), 1)

    # ── branch scoping ────────────────────────────────────────────────────

    def test_the_center_id_query_parameter_filters(self):
        """The filter read a `Center-Id` header, which nothing sends."""
        self.sell(self.package, invoice=self.invoice(center=self.branch_a),
                  price='2000.00')
        self.sell(self.package, invoice=self.invoice(center=self.branch_b),
                  price='2000.00')

        both = self.fetch()
        self.assertEqual(both[f'package_{self.package.id}']['sold'], 2)

        only_a = self.fetch(center_id=self.branch_a.id)
        self.assertEqual(only_a[f'package_{self.package.id}']['sold'], 1)

    def test_a_junk_center_id_is_not_a_500(self):
        for value in ('abc', 'null', 'undefined', ''):
            response = self.api.get(self.URL, {'center_id': value})
            self.assertEqual(response.status_code, 200, value)

    def test_a_manager_of_two_branches_sees_both(self):
        """Scoping by `user.center` alone showed a multi-branch manager one."""
        role = Role.objects.create(name='Manager', permissions={
            'marketing': {'promotions': {'read': True, 'create': False, 'update': False, 'delete': False},
                          'usage': {'read': True, 'create': False, 'update': False, 'delete': False}},
        })
        manager = CustomUser.objects.create_user(
            email='m@test.com', password='StrongPass!234', full_name='M',
            role=role, center=self.branch_a)
        manager.centers.set([self.branch_a, self.branch_b])

        self.sell(self.package, invoice=self.invoice(center=self.branch_a),
                  price='2000.00')
        self.sell(self.package, invoice=self.invoice(center=self.branch_b),
                  price='2000.00')

        client = APIClient()
        client.force_authenticate(manager)
        response = client.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)

        rows = {row['id']: row for row in response.data}
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 2)

    def test_a_manager_does_not_see_another_branch(self):
        role = Role.objects.create(name='Manager B', permissions={
            'marketing': {'promotions': {'read': True, 'create': False, 'update': False, 'delete': False},
                          'usage': {'read': True, 'create': False, 'update': False, 'delete': False}},
        })
        manager = CustomUser.objects.create_user(
            email='mb@test.com', password='StrongPass!234', full_name='MB',
            role=role, center=self.branch_a)
        manager.centers.set([self.branch_a])

        self.sell(self.package, invoice=self.invoice(center=self.branch_b),
                  price='2000.00')

        client = APIClient()
        client.force_authenticate(manager)
        rows = {row['id']: row for row in client.get(self.URL).data}
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)

    def test_the_branch_breakdown_is_one_row_per_branch(self):
        self.sell(self.package, invoice=self.invoice(center=self.branch_a),
                  price='2000.00')
        self.sell(self.package, invoice=self.invoice(center=self.branch_a),
                  price='2000.00')

        rows = self.fetch()
        breakdown = rows[f'package_{self.package.id}']['center_breakdown']
        self.assertEqual(len(breakdown), 1)
        self.assertEqual(breakdown[0]['sold'], 2)

    def test_sold_and_used_appear_in_the_branch_breakdown(self):
        held = self.client_package()
        self.sell(self.package, invoice=self.invoice(center=self.branch_a),
                  price='2000.00')
        self.redeem_package(held, invoice=self.invoice(center=self.branch_a))

        rows = self.fetch()
        breakdown = rows[f'package_{self.package.id}']['center_breakdown'][0]
        self.assertEqual(breakdown['sold'], 1)
        self.assertEqual(breakdown['used'], 1)
        self.assertEqual(breakdown['count'], 2)

    # ── date filtering ────────────────────────────────────────────────────

    def test_dates_filter_both_sales_and_redemptions(self):
        held = self.client_package()
        self.sell(self.package, price='2000.00')
        self.redeem_package(held)

        today = datetime.date.today().isoformat()
        rows = self.fetch(start_date=today, end_date=today)
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 1)
        self.assertEqual(rows[f'package_{self.package.id}']['used'], 1)

        past = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
        rows = self.fetch(start_date=past, end_date=past)
        self.assertEqual(rows[f'package_{self.package.id}']['sold'], 0)
        self.assertEqual(rows[f'package_{self.package.id}']['used'], 0)

    def test_a_junk_date_does_not_break_the_report(self):
        response = self.api.get(self.URL, {'start_date': 'not-a-date'})
        self.assertEqual(response.status_code, 200)

    def test_an_anonymous_caller_is_refused(self):
        response = APIClient().get(self.URL)
        self.assertIn(response.status_code, (401, 403))


class MarketingListBranchFilterTests(TestCase):
    """The branch filter on every marketing list.

    `MarketingBaseViewSet.get_queryset` did:

        center_id = coerce_center_id(request.query_params.get('center_id'))
        if center_id and center_id.lower() != 'null':

    `coerce_center_id` returns an int or None, so `.lower()` on it raised
    AttributeError. The guard was left over from when the value was a raw
    string. Any owner - the person most likely to use a branch filter - got a
    500 on the cards, memberships, packages and coupons lists.
    """

    ENDPOINTS = (
        '/marketing/api/memberships/',
        '/marketing/api/packages/',
        '/marketing/api/cards/',
        '/marketing/api/promotions/',
    )

    def setUp(self):
        self.branch = Center.objects.create(center_name='Branch A', display_name='A')
        self.owner_role = Role.objects.create(
            name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner2@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.branch)

        Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('5000.00'), expiry_days=365,
            level='Organisation')
        Package.objects.create(
            name='Pack', price=Decimal('2000.00'), validity_days=365,
            level='Organisation')
        ValueCard.objects.create(
            title='Card', pre_tax_price=Decimal('1000.00'),
            value=Decimal('1200.00'), expiry_days=365, level='Organisation')

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_filtering_by_branch_does_not_500(self):
        for url in self.ENDPOINTS:
            with self.subTest(url):
                response = self.api.get(url, {'center_id': self.branch.id})
                self.assertEqual(response.status_code, 200, url)

    def test_a_junk_branch_filter_does_not_500(self):
        for url in self.ENDPOINTS:
            for value in ('null', 'undefined', 'abc', ''):
                with self.subTest(url=url, value=value):
                    response = self.api.get(url, {'center_id': value})
                    self.assertEqual(response.status_code, 200, f'{url} {value}')

    def test_no_filter_still_lists(self):
        for url in self.ENDPOINTS:
            with self.subTest(url):
                response = self.api.get(url)
                self.assertEqual(response.status_code, 200, url)
