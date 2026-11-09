"""
The GST rate on memberships and value cards.

There was no rate column. `billing/pricing.py` inferred the rate from the gap
between `pre_tax_price` and `post_tax_price`, and the create/edit forms wrote
that pair with a hardcoded 1.05 in BOTH directions - typing in either field
rewrote the other to a 5% relationship. So the data-entry form decided the tax
rate as a side effect, 18% was physically unreachable through the UI, and every
membership and value card was invoiced at 5%.

Beauty and personal-care services under SAC 9997 attract 18%. On a Rs 25 lakh
annual membership book that is roughly Rs 3.2 lakh of output tax collected short,
recoverable from the salon with interest.

The rate is now its own field. These tests pin it, and pin the backfill that
keeps existing rows on whatever rate they were already being sold at.
"""

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing import pricing
from clients.models import Client
from marketing.models import Membership, ValueCard
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class PerkRateTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def sell(self, obj, content_type, unit_price, discount='0'):
        response = self.api.post('/billing/invoices/', {
            'client': self.customer.id,
            'center': self.center.id,
            'status': 'draft',
            'discount': '0',
            'items': [{
                'content_type': content_type,
                'object_id': obj.id,
                'quantity': 1,
                'unit_price': unit_price,
                'discount': discount,
                'description': getattr(obj, 'name', None) or obj.title,
            }],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    # ── the default ───────────────────────────────────────────────────────

    def test_a_new_membership_defaults_to_eighteen_percent(self):
        """5% was the only rate reachable before; 18 is the right default for a
        beauty service under SAC 9997."""
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'), expiry_days=365)
        self.assertEqual(membership.gst_percent, Decimal('18.00'))

    def test_a_new_value_card_defaults_to_eighteen_percent(self):
        card = ValueCard.objects.create(
            title='Silver', pre_tax_price=Decimal('2000.00'),
            value=Decimal('2500.00'), expiry_days=365)
        self.assertEqual(card.gst_percent, Decimal('18.00'))

    # ── the rate is honoured, not inferred ───────────────────────────────

    def test_eighteen_percent_is_now_reachable(self):
        """The whole point. This could not be configured at all before."""
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1180.00'), gst_percent=Decimal('18.00'),
            expiry_days=365)

        invoice = self.sell(membership, 'marketing.membership', '1000.00')
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('90.00'))
        self.assertEqual(Decimal(str(invoice['sgst'])), Decimal('90.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('1180'))

    def test_the_stated_rate_wins_over_the_price_pair(self):
        """An operator who rounds the post-tax price must not thereby change the
        tax rate on a legal document."""
        membership = Membership.objects.create(
            name='Odd', pre_tax_price=Decimal('1000.00'),
            # This pair implies 4.99%, not 18%.
            post_tax_price=Decimal('1049.90'), gst_percent=Decimal('18.00'),
            expiry_days=365)

        catalog = pricing.resolve('marketing.membership', membership.id,
                                  center=self.center)
        self.assertEqual(catalog.tax_percentage, Decimal('18.00'))

    def test_a_zero_rate_is_respected(self):
        membership = Membership.objects.create(
            name='Exempt', pre_tax_price=Decimal('500.00'),
            gst_percent=Decimal('0.00'), expiry_days=365)
        invoice = self.sell(membership, 'marketing.membership', '500.00')
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('500'))

    def test_an_impossible_rate_falls_back_to_zero(self):
        """Better to charge nothing than to put 150% GST on an invoice."""
        membership = Membership.objects.create(
            name='Broken', pre_tax_price=Decimal('500.00'),
            gst_percent=Decimal('150.00'), expiry_days=365)
        catalog = pricing.resolve('marketing.membership', membership.id,
                                  center=self.center)
        self.assertEqual(catalog.tax_percentage, Decimal('0'))

    # ── only a tax-inclusive price entered ───────────────────────────────

    def test_a_post_tax_only_price_is_worked_backwards(self):
        """Billing the inclusive figure AND adding tax would double-charge.

        Previously a row with no pre_tax_price was sold at the inclusive price
        with the rate silently 0, so the tax simply vanished.
        """
        membership = Membership.objects.create(
            name='Inclusive', pre_tax_price=None,
            post_tax_price=Decimal('1180.00'), gst_percent=Decimal('18.00'),
            expiry_days=365)

        catalog = pricing.resolve('marketing.membership', membership.id,
                                  center=self.center)
        self.assertEqual(catalog.list_price, Decimal('1000.00'))
        self.assertEqual(catalog.tax_percentage, Decimal('18.00'))

        invoice = self.sell(membership, 'marketing.membership', '1000.00')
        # The customer pays the inclusive price the operator meant.
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('1180'))

    # ── a discount no longer breaks the total ────────────────────────────

    def test_a_discounted_membership_reconciles(self):
        """The till used to show 950 while the server stored 945."""
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1180.00'), gst_percent=Decimal('18.00'),
            expiry_days=365)

        invoice = self.sell(membership, 'marketing.membership', '1000.00',
                            discount='100.00')
        self.assertEqual(Decimal(str(invoice['subtotal'])), Decimal('900.00'))
        # 900 @ 18% = 162, split 81/81.
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('81.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('1062'))

    def test_a_complimentary_membership_can_be_billed(self):
        """A 100% discount was rejected outright: the till sent a discount of
        1050 against a gross of 1000."""
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            post_tax_price=Decimal('1180.00'), gst_percent=Decimal('18.00'),
            expiry_days=365)

        invoice = self.sell(membership, 'marketing.membership', '1000.00',
                            discount='1000.00')
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('0'))
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))

    # ── a non-GST branch still overrides ─────────────────────────────────

    def test_a_non_gst_branch_charges_nothing(self):
        no_gst = Center.objects.create(center_name='No GST', gst_enabled=False)
        customer = Client.objects.create(
            first_name='Bela', phone='9990000002', center=no_gst)
        membership = Membership.objects.create(
            name='Gold', pre_tax_price=Decimal('1000.00'),
            gst_percent=Decimal('18.00'), expiry_days=365)

        response = self.api.post('/billing/invoices/', {
            'client': customer.id, 'center': no_gst.id, 'status': 'draft',
            'discount': '0',
            'items': [{
                'content_type': 'marketing.membership',
                'object_id': membership.id, 'quantity': 1,
                'unit_price': '1000.00', 'discount': '0',
                'description': 'Gold',
            }],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Decimal(str(response.data['cgst'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(response.data['total_amount'])), Decimal('1000'))

    # ── the API accepts and returns the rate ─────────────────────────────

    def test_the_rate_can_be_set_through_the_api(self):
        response = self.api.post('/marketing/api/memberships/', {
            'name': 'Platinum', 'pre_tax_price': '5000.00',
            'post_tax_price': '5900.00', 'gst_percent': '18.00',
            'expiry_days': 365, 'level': 'Organisation',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Decimal(str(response.data['gst_percent'])), Decimal('18.00'))

    def test_a_rate_over_a_hundred_is_refused_by_validation(self):
        response = self.api.post('/marketing/api/memberships/', {
            'name': 'Silly', 'pre_tax_price': '100.00', 'gst_percent': '150',
            'expiry_days': 365, 'level': 'Organisation',
        }, format='json')
        self.assertEqual(response.status_code, 400, response.data)


class BackfillTests(TestCase):
    """Existing rows keep the rate they were already being sold at.

    Defaulting them to 18 would silently reprice memberships customers already
    hold, and would make historical invoices disagree with the catalogue they
    were priced from. So the migration reads each row's own price pair.
    """

    def implied(self, pre, post):
        from importlib import import_module

        module = import_module(
            'marketing.migrations.0015_backfill_perk_gst_from_price_pair')
        return module.implied_rate(pre, post)

    def test_a_five_percent_pair_backfills_to_five(self):
        self.assertEqual(self.implied('1000.00', '1050.00'), Decimal('5.00'))

    def test_an_eighteen_percent_pair_backfills_to_eighteen(self):
        self.assertEqual(self.implied('1000.00', '1180.00'), Decimal('18.00'))

    def test_a_rounded_price_snaps_to_a_real_rate(self):
        """An operator who typed 1049.90 meant 5%, not 4.99%. A tax invoice must
        not carry a rate that does not exist."""
        self.assertEqual(self.implied('1000.00', '1049.90'), Decimal('5.00'))
        self.assertEqual(self.implied('1000.00', '1179.50'), Decimal('18.00'))

    def test_an_unusable_pair_backfills_to_zero_not_to_a_guess(self):
        """These rows were being sold at 0%. Taxing them here would raise a
        price behind the operator's back."""
        self.assertEqual(self.implied(None, '1050.00'), Decimal('0.00'))
        self.assertEqual(self.implied('1000.00', None), Decimal('0.00'))
        self.assertEqual(self.implied('1000.00', '900.00'), Decimal('0.00'))
        self.assertEqual(self.implied('0', '0'), Decimal('0.00'))

    def test_garbage_does_not_raise(self):
        self.assertEqual(self.implied('abc', 'def'), Decimal('0.00'))

    def test_a_genuinely_odd_rate_is_kept_as_is(self):
        """Only near-misses snap; a deliberate 9% stays 9%."""
        self.assertEqual(self.implied('1000.00', '1090.00'), Decimal('9.00'))
