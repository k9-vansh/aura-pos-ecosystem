"""
Tests for the seven multi-salon report endpoints.

These had no tests at all, which is how two guaranteed 500s survived: both
drilldown searches used lookups that cannot work (a join on a CharField, and a
filter on a GenericForeignKey). Every endpoint is now exercised, including with
a search term.
"""

import datetime
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, Invoice, InvoiceItem
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from inventory.models import Product
from marketing.models import Membership, Package, ValueCard
from pos_backend.money import D, money
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class MultiSalonTestBase(TestCase):
    def setUp(self):
        self.center_a = Center.objects.create(center_name='Andheri', display_name='Andheri')
        self.center_b = Center.objects.create(center_name='Bandra', display_name='Bandra')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center_a,
        )
        self.limited_role = Role.objects.create(name='Manager A', permissions={
            'finance': {'multi_salon': {'read': True}},
        })
        self.manager_a = CustomUser.objects.create_user(
            email='a@test.com', password='pw', full_name='Manager A',
            role=self.limited_role, center=self.center_a,
        )

        # Deliberately no last name: this is what produced "Asha None".
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center_a,
        )

        self.service = ServiceMaster.objects.create(
            name='Keratin Treatment', category='Hair', sub_category='Smoothening',
            brand='Loreal', hsn_code='999721',
            default_price=Decimal('2000.00'), tax_percentage=Decimal('18.00'),
        )
        self.product = Product.objects.create(
            name='Argan Shampoo', brand='Moroccanoil', category='Haircare',
            barcode='8901234567890', sac_code='330510',
            price=Decimal('900.00'), gst_percent=Decimal('18.00'),
            current_stock=40, center=self.center_a,
        )
        self.staff = StaffMember.objects.create(
            center=self.center_a, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'),
        )

        self.service_ct = ContentType.objects.get_for_model(ServiceMaster)
        self.product_ct = ContentType.objects.get_for_model(Product)

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.invoice = self._invoice()
        self._line(self.invoice, self.service, self.service_ct, '2000.00', '18.00')
        self._line(self.invoice, self.product, self.product_ct, '900.00', '18.00')

    def _invoice(self, center=None, client=None, total='3422.00', paid=None):
        invoice = Invoice.objects.create(
            center=center or self.center_a,
            client=client if client is not None else self.customer,
            status='paid', subtotal=Decimal('2900.00'),
            cgst=Decimal('261.00'), sgst=Decimal('261.00'),
            total_amount=Decimal(total),
            paid_amount=Decimal(paid if paid is not None else total),
        )
        invoice.assign_invoice_number()
        return invoice

    def _line(self, invoice, obj, content_type, price, tax, quantity=1,
              is_redemption=False, staff=None):
        taxable = Decimal(price) * quantity
        item = InvoiceItem.objects.create(
            invoice=invoice, content_type=content_type, object_id=obj.id,
            description=getattr(obj, 'name', 'Item'),
            unit_price=Decimal(price), quantity=quantity,
            tax_percentage=Decimal(tax),
            tax_amount=money(taxable * Decimal(tax) / Decimal('100')),
            total_price=money(taxable) if not is_redemption else Decimal('0.00'),
            is_redemption=is_redemption,
        )
        item.staff_members.set([staff or self.staff])
        return item

    def get(self, path, **params):
        return self.api.get(path, params)


class DrilldownSearchTests(MultiSalonTestBase):
    """R1 / R2 - both searches were guaranteed 500s."""

    SERVICE_URL = '/finance/api/reports/multi_salon/drilldown/services/'
    PRODUCT_URL = '/finance/api/reports/multi_salon/drilldown/products/'

    def test_service_drilldown_without_a_search(self):
        response = self.get(self.SERVICE_URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data[0]['amount']), Decimal('2000.00'))

    def test_service_search_does_not_500(self):
        """`category__name__icontains` on a CharField raised FieldError."""
        response = self.get(self.SERVICE_URL, search='keratin')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data[0]['amount']), Decimal('2000.00'))

    def test_service_search_matches_the_category(self):
        response = self.get(self.SERVICE_URL, search='Hair')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data)

    def test_service_search_matches_the_brand_and_subcategory(self):
        for term in ['Loreal', 'Smoothening']:
            with self.subTest(term=term):
                response = self.get(self.SERVICE_URL, search=term)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data, f'no match for {term}')

    def test_service_search_with_no_match_returns_empty(self):
        response = self.get(self.SERVICE_URL, search='zzzznothing')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, [])

    def test_product_search_does_not_500(self):
        """`content_object__brand__name` on a GenericForeignKey raised FieldError."""
        response = self.get(self.PRODUCT_URL, search='argan')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data[0]['amount']), Decimal('900.00'))

    def test_product_search_matches_brand_category_and_barcode(self):
        for term in ['Moroccanoil', 'Haircare', '8901234567890']:
            with self.subTest(term=term):
                response = self.get(self.PRODUCT_URL, search=term)
                self.assertEqual(response.status_code, 200, response.data)
                self.assertTrue(response.data, f'no match for {term}')

    def test_drilldowns_exclude_redemptions(self):
        """R4: a Rs 0 redeemed service is not a sale."""
        self._line(self.invoice, self.service, self.service_ct, '0.00', '0.00',
                   is_redemption=True)
        response = self.get(self.SERVICE_URL)
        self.assertEqual(int(response.data[0]['count']), 1)

    def test_null_center_id_does_not_500(self):
        """R7: the frontend sends the literal string 'null'."""
        for url in (self.SERVICE_URL, self.PRODUCT_URL):
            with self.subTest(url=url):
                self.assertEqual(self.get(url, center_id='null').status_code, 200)
                self.assertEqual(self.get(url, center_id='abc').status_code, 200)


class BalancesTests(MultiSalonTestBase):
    URL = '/finance/api/reports/multi_salon/balances/'

    def setUp(self):
        super().setUp()
        card_def = ValueCard.objects.create(
            title='Prestige', pre_tax_price=Decimal('5000.00'),
            value=Decimal('6000.00'), expiry_days=365,
        )
        self.card = ClientValueCard.objects.create(
            client=self.customer, value_card=card_def, balance=Decimal('4500.00'),
            expiry_date=datetime.date.today() + datetime.timedelta(days=200),
        )
        AdvancePayment.objects.create(client=self.customer, amount=Decimal('3000.00'))
        AdvancePayment.objects.create(client=self.customer, amount=Decimal('-800.00'))

        self.package_def = Package.objects.create(
            name='5 Keratin', price=Decimal('9000.00'), validity_days=180,
            services_json=[{'id': self.service.id, 'pkgQty': 5}],
        )

    def test_returns_cards_advances_and_packages(self):
        ClientPackage.objects.create(
            client=self.customer, package=self.package_def,
            services_remaining={str(self.service.id): 3},
            expiry_date=datetime.date.today() + datetime.timedelta(days=100),
        )
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        kinds = {row['type'] for row in response.data}
        self.assertEqual(kinds, {'Value Card', 'Advance', 'Package'})

    def test_client_name_has_no_trailing_none(self):
        """R6: `f"{first} {last}"` rendered "Asha None"."""
        response = self.get(self.URL)
        for row in response.data:
            self.assertNotIn('None', row['client'], row)
            self.assertEqual(row['client'], 'Asha')

    def test_advance_reports_net_topup_and_redeemed_separately(self):
        """R11: one netted figure hid whether it was busy or quiet."""
        response = self.get(self.URL)
        advance = next(r for r in response.data if r['type'] == 'Advance')
        self.assertEqual(money(advance['value']), Decimal('2200.00'))
        self.assertEqual(money(advance['topped_up']), Decimal('3000.00'))
        self.assertEqual(money(advance['redeemed']), Decimal('800.00'))

    def test_package_carries_a_rupee_value(self):
        """R12: packages reported value '-', a string in a numeric column."""
        ClientPackage.objects.create(
            client=self.customer, package=self.package_def,
            services_remaining={str(self.service.id): 3},
            expiry_date=datetime.date.today() + datetime.timedelta(days=100),
        )
        response = self.get(self.URL)
        package = next(r for r in response.data if r['type'] == 'Package')
        self.assertEqual(package['count'], 3)
        # 3 remaining Keratin services at the Rs 2000 catalog price.
        self.assertEqual(money(package['value']), Decimal('6000.00'))

    def test_string_quantities_in_the_json_do_not_500(self):
        """R5: `sum(dict.values())` raised TypeError on string quantities."""
        ClientPackage.objects.create(
            client=self.customer, package=self.package_def,
            services_remaining={str(self.service.id): '2'},
            expiry_date=datetime.date.today() + datetime.timedelta(days=100),
        )
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        package = next(r for r in response.data if r['type'] == 'Package')
        self.assertEqual(package['count'], 2)

    def test_a_user_only_sees_their_own_center(self):
        other = Client.objects.create(
            first_name='Bina', phone='9990000002', center=self.center_b,
        )
        AdvancePayment.objects.create(client=other, amount=Decimal('5000.00'))

        self.api.force_authenticate(self.manager_a)
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(all(r['center_name'] == 'Andheri' for r in response.data),
                        response.data)


class CategoriesTests(MultiSalonTestBase):
    URL = '/finance/api/reports/multi_salon/categories/'

    def test_service_and_product_tables_populate(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(money(response.data['services'][0]['amount']), Decimal('2000.00'))
        self.assertEqual(money(response.data['products'][0]['amount']), Decimal('900.00'))

    def test_products_get_a_tax_code(self):
        """R3: it read `hsn_code` off Product, which has no such field."""
        response = self.get(self.URL)
        product = response.data['products'][0]
        self.assertEqual(product['hsn'], '330510')  # Product.sac_code

    def test_services_get_their_hsn_code(self):
        response = self.get(self.URL)
        self.assertEqual(response.data['services'][0]['hsn'], '999721')

    def test_one_service_with_varied_labels_is_a_single_row(self):
        """R15: grouping by description split it across rows."""
        second = self._invoice()
        item = self._line(second, self.service, self.service_ct, '2000.00', '18.00')
        item.description = 'Keratin Treatment (Senior)'
        item.save(update_fields=['description'])

        response = self.get(self.URL)
        rows = [r for r in response.data['services'] if r['item_id'] == self.service.id]
        self.assertEqual(len(rows), 1, response.data['services'])
        self.assertEqual(money(rows[0]['amount']), Decimal('4000.00'))
        self.assertEqual(rows[0]['name'], 'Keratin Treatment')

    def test_redemptions_are_excluded_from_counts(self):
        """R4: free redeemed services inflated the sold count."""
        before = self.get(self.URL).data['services'][0]['count']
        self._line(self.invoice, self.service, self.service_ct, '0.00', '0.00',
                   quantity=3, is_redemption=True)
        after = self.get(self.URL).data['services'][0]['count']
        self.assertEqual(after, before)

    def test_advances_split_topups_from_redemptions(self):
        AdvancePayment.objects.create(
            client=self.customer, invoice=self.invoice, amount=Decimal('2000.00'))
        AdvancePayment.objects.create(
            client=self.customer, invoice=self.invoice, amount=Decimal('-500.00'))

        response = self.get(self.URL)
        advances = next(c for c in response.data['categories'] if c['type'] == 'Advances')
        self.assertEqual(money(advances['amount']), Decimal('2000.00'))
        self.assertEqual(money(advances['redeemed']), Decimal('500.00'))
        self.assertEqual(advances['count'], 1)  # one top-up, not two rows

    def test_null_center_id_does_not_500(self):
        self.assertEqual(self.get(self.URL, center_id='null').status_code, 200)


class SalesExportTests(MultiSalonTestBase):
    URL = '/finance/api/reports/multi_salon/sales_export/'

    def test_service_export_is_a_real_xlsx(self):
        response = self.get(self.URL, item_type='service')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b'PK'))

    def test_every_valid_item_type_exports(self):
        for item_type in ['service', 'product', 'membership', 'package',
                          'valuecard', 'giftcard', 'advances']:
            with self.subTest(item_type=item_type):
                response = self.get(self.URL, item_type=item_type)
                self.assertEqual(response.status_code, 200, item_type)

    def test_an_unknown_item_type_is_rejected(self):
        """R8: it silently exported EVERY line type instead."""
        response = self.get(self.URL, item_type='nonsense')
        self.assertEqual(response.status_code, 400)
        self.assertIn('valid', response.data)

    def test_a_missing_item_type_is_rejected(self):
        self.assertEqual(self.get(self.URL).status_code, 400)

    def test_null_center_id_does_not_500(self):
        response = self.get(self.URL, item_type='service', center_id='null')
        self.assertEqual(response.status_code, 200)


class ClientsReportTests(MultiSalonTestBase):
    URL = '/finance/api/reports/multi_salon/clients/'

    def test_reports_visits_and_spend(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data if r['id'] == self.customer.id)
        self.assertEqual(row['visits'], 1)
        self.assertEqual(money(row['total_spend']), Decimal('3422.00'))
        self.assertEqual(row['name'], 'Asha')

    def test_service_balance_is_computed(self):
        """R12: it was hardcoded to 0.0."""
        package_def = Package.objects.create(
            name='5 Keratin', price=Decimal('9000.00'), validity_days=180,
        )
        ClientPackage.objects.create(
            client=self.customer, package=package_def,
            services_remaining={str(self.service.id): 2},
            expiry_date=datetime.date.today() + datetime.timedelta(days=90),
        )
        response = self.get(self.URL)
        row = next(r for r in response.data if r['id'] == self.customer.id)
        self.assertEqual(money(row['serv_balance']), Decimal('4000.00'))

    def test_an_expired_membership_does_not_count_as_a_member(self):
        """R13: expiry was ignored, so a lapsed membership still said Yes."""
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('5000.00'),
            post_tax_price=Decimal('5000.00'), expiry_days=30,
        )
        ClientMembership.objects.create(
            client=self.customer, membership=membership, is_active=True,
            start_date=datetime.date.today() - datetime.timedelta(days=60),
            expiry_date=datetime.date.today() - datetime.timedelta(days=30),
        )
        response = self.get(self.URL)
        row = next(r for r in response.data if r['id'] == self.customer.id)
        self.assertEqual(row['is_member'], 'No')

    def test_a_live_membership_does_count(self):
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('5000.00'),
            post_tax_price=Decimal('5000.00'), expiry_days=365,
        )
        ClientMembership.objects.create(
            client=self.customer, membership=membership, is_active=True,
            expiry_date=datetime.date.today() + datetime.timedelta(days=300),
        )
        response = self.get(self.URL)
        row = next(r for r in response.data if r['id'] == self.customer.id)
        self.assertEqual(row['is_member'], 'Yes')

    def test_walk_in_invoices_do_not_break_the_report(self):
        walk_in = self._invoice(client=None)
        self._line(walk_in, self.service, self.service_ct, '2000.00', '18.00')
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)

    def test_string_quantities_do_not_500(self):
        package_def = Package.objects.create(
            name='Bundle', price=Decimal('1000.00'), validity_days=90,
        )
        ClientPackage.objects.create(
            client=self.customer, package=package_def,
            services_remaining={str(self.service.id): '4'},
            expiry_date=datetime.date.today() + datetime.timedelta(days=60),
        )
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)


class StaffReportTests(MultiSalonTestBase):
    URL = '/finance/api/reports/multi_salon/staff/'

    def test_attributes_revenue_to_staff(self):
        response = self.get(self.URL)
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data if r['staff_id'] == self.staff.id)
        self.assertEqual(money(row['revenue']), Decimal('2900.00'))
        self.assertEqual(money(row['services']), Decimal('2000.00'))
        self.assertEqual(money(row['products']), Decimal('900.00'))

    def test_a_split_line_loses_no_paise(self):
        """R14: float division leaked paise on odd splits."""
        second = StaffMember.objects.create(
            center=self.center_a, first_name='Sita', designation='Stylist')
        third = StaffMember.objects.create(
            center=self.center_a, first_name='Meena', designation='Stylist')

        invoice = self._invoice()
        item = self._line(invoice, self.service, self.service_ct, '1000.00', '0.00')
        item.staff_members.set([self.staff, second, third])

        response = self.get(self.URL)
        total = money(sum(D(r['revenue']) for r in response.data))
        # 2900 from setUp plus exactly 1000 split three ways.
        self.assertEqual(total, Decimal('3900.00'))

    def test_labels_itself_as_gross_sales(self):
        """It is not the commissionable figure; the two used to look alike."""
        response = self.get(self.URL)
        self.assertEqual(response.data[0]['basis'], 'gross sales')

    def test_redemptions_are_excluded(self):
        before = money(self.get(self.URL).data[0]['revenue'])
        self._line(self.invoice, self.service, self.service_ct, '0.00', '0.00',
                   is_redemption=True)
        after = money(self.get(self.URL).data[0]['revenue'])
        self.assertEqual(after, before)

    def test_null_center_id_does_not_500(self):
        self.assertEqual(self.get(self.URL, center_id='null').status_code, 200)


class MultiSalonPermissionTests(MultiSalonTestBase):
    """These endpoints previously required only IsAuthenticated."""

    URLS = [
        '/finance/api/reports/multi_salon/balances/',
        '/finance/api/reports/multi_salon/categories/',
        '/finance/api/reports/multi_salon/clients/',
        '/finance/api/reports/multi_salon/staff/',
        '/finance/api/reports/multi_salon/drilldown/services/',
        '/finance/api/reports/multi_salon/drilldown/products/',
    ]

    def test_a_user_without_the_permission_is_refused(self):
        role = Role.objects.create(name='Reception', permissions={
            'billing': {'invoices': {'read': True}},
        })
        user = CustomUser.objects.create_user(
            email='recep@test.com', password='pw', full_name='Reception',
            role=role, center=self.center_a,
        )
        self.api.force_authenticate(user)
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 403, url)

    def test_a_user_with_the_permission_is_allowed(self):
        self.api.force_authenticate(self.manager_a)
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertEqual(self.get(url).status_code, 200, url)

    def test_an_unauthenticated_request_is_refused(self):
        anon = APIClient()
        for url in self.URLS:
            with self.subTest(url=url):
                self.assertIn(anon.get(url).status_code, (401, 403), url)
