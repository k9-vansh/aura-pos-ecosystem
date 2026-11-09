"""
The two business policies that used to be accidents.

Neither of these was a decision anyone made; both fell out of how the code
happened to be written, and both change what a customer is charged or allowed to
spend. They are now named settings, defaulting to the existing behaviour, so the
choice is visible and reversible rather than buried.

  WALLET_SCOPE         - is an advance paid at one branch spendable at another?
  PACKAGE_GST_PERCENT  - is a prepaid package taxed when it is sold?
"""

from decimal import Decimal

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, CashbackTransaction
from billing.wallets import (
    advance_balance, cashback_balance, debit_advance, debit_cashback,
    wallet_scope,
)
from clients.models import Client
from marketing.models import Package
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class WalletScopeTests(TestCase):
    """A customer with money at two branches."""

    def setUp(self):
        self.branch_a = Center.objects.create(center_name='Branch A')
        self.branch_b = Center.objects.create(center_name='Branch B')

        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.branch_a)

        # Rs 1000 topped up at branch A, Rs 200 cashback at branch A.
        AdvancePayment.objects.create(
            client=self.customer, center=self.branch_a,
            amount=Decimal('1000.00'), notes='top up at A')
        CashbackTransaction.objects.create(
            client=self.customer, center=self.branch_a,
            amount=Decimal('200.00'), notes='earned at A')

    # ── the default: unchanged behaviour ─────────────────────────────────
    def test_the_default_is_global(self):
        self.assertEqual(wallet_scope(), 'global')

    def test_by_default_a_balance_is_spendable_at_another_branch(self):
        """This is what the system has always done. It is not a bug - but it is
        a policy, and the branches owe each other money because of it."""
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_b),
            Decimal('1000.00'))

        debit_advance(self.customer, Decimal('400.00'), center=self.branch_b)
        self.assertEqual(
            advance_balance(self.customer.id), Decimal('600.00'))

    @override_settings(WALLET_SCOPE='global')
    def test_global_is_explicit_too(self):
        self.assertEqual(
            cashback_balance(self.customer.id, center=self.branch_b),
            Decimal('200.00'))

    # ── the per-branch policy ────────────────────────────────────────────
    @override_settings(WALLET_SCOPE='center')
    def test_under_center_scope_the_other_branch_sees_nothing(self):
        self.assertEqual(wallet_scope(), 'center')
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_a),
            Decimal('1000.00'))
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_b),
            Decimal('0.00'))

    @override_settings(WALLET_SCOPE='center')
    def test_under_center_scope_spending_elsewhere_is_refused(self):
        from billing.wallets import InsufficientBalance

        with self.assertRaises(InsufficientBalance) as caught:
            debit_advance(self.customer, Decimal('100.00'), center=self.branch_b)

        # The refusal has to explain itself, or the counter thinks it is a bug.
        self.assertIn('per branch', str(caught.exception.detail))
        # Nothing was taken from the branch that holds the money. (Queried with
        # branch A: under this policy a balance query without a branch means
        # "rows with no branch recorded", which is the point of the policy.)
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_a),
            Decimal('1000.00'))

    @override_settings(WALLET_SCOPE='center')
    def test_under_center_scope_the_home_branch_still_works(self):
        debit_advance(self.customer, Decimal('250.00'), center=self.branch_a)
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_a),
            Decimal('750.00'))

    @override_settings(WALLET_SCOPE='center')
    def test_cashback_follows_the_same_policy(self):
        from billing.wallets import InsufficientBalance

        self.assertEqual(
            cashback_balance(self.customer.id, center=self.branch_b),
            Decimal('0.00'))
        with self.assertRaises(InsufficientBalance):
            debit_cashback(self.customer, Decimal('50.00'), center=self.branch_b)

    @override_settings(WALLET_SCOPE='center')
    def test_a_row_with_no_branch_is_not_spendable_anywhere(self):
        """Under a per-branch policy, an unattributed balance belongs to no
        branch. Treating it as global would defeat the policy."""
        AdvancePayment.objects.create(
            client=self.customer, center=None, amount=Decimal('500.00'),
            notes='no branch recorded')

        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_a),
            Decimal('1000.00'))
        self.assertEqual(
            advance_balance(self.customer.id, center=None),
            Decimal('500.00'))

    @override_settings(WALLET_SCOPE='NoNsEnSe')
    def test_an_unrecognised_value_falls_back_to_global(self):
        """A typo in an env var must not silently change what customers can
        spend, in either direction."""
        self.assertEqual(wallet_scope(), 'global')
        self.assertEqual(
            advance_balance(self.customer.id, center=self.branch_b),
            Decimal('1000.00'))


class PackageGstTests(TestCase):
    """Whether a prepaid package is taxed when sold."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner',
                                        permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)

        self.package = Package.objects.create(
            name='Haircut x5', price=Decimal('2000.00'), validity_days=365,
            services_json=[])

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def sell_package(self):
        response = self.api.post('/billing/invoices/', {
            'client': self.customer.id,
            'center': self.center.id,
            'status': 'draft',
            'discount': '0',
            'items': [{
                'content_type': 'marketing.package',
                'object_id': self.package.id,
                'quantity': 1,
                'unit_price': '2000.00',
                'discount': '0',
                'description': 'Haircut x5',
            }],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_by_default_a_package_sale_carries_no_gst(self):
        """Unchanged behaviour: 0%, at sale and at redemption both."""
        invoice = self.sell_package()
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(invoice['sgst'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('2000'))

    @override_settings(PACKAGE_GST_PERCENT='18')
    def test_a_configured_rate_is_charged(self):
        invoice = self.sell_package()
        # 2000 @ 18% = 360, split 180/180.
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('180.00'))
        self.assertEqual(Decimal(str(invoice['sgst'])), Decimal('180.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('2360'))

    @override_settings(PACKAGE_GST_PERCENT='junk')
    def test_an_unparseable_rate_falls_back_to_zero(self):
        invoice = self.sell_package()
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))

    @override_settings(PACKAGE_GST_PERCENT='150')
    def test_an_impossible_rate_falls_back_to_zero(self):
        """Better to charge nothing than to charge 150% GST."""
        invoice = self.sell_package()
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))

    @override_settings(PACKAGE_GST_PERCENT='18')
    def test_a_non_gst_branch_still_charges_nothing(self):
        """The branch switch outranks the package rate."""
        self.center.gst_enabled = False
        self.center.save(update_fields=['gst_enabled'])

        invoice = self.sell_package()
        self.assertEqual(Decimal(str(invoice['cgst'])), Decimal('0.00'))
        self.assertEqual(Decimal(str(invoice['total_amount'])), Decimal('2000'))
