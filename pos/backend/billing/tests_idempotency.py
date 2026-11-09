"""
A retried checkout must not become a second bill.

The guard was `cache.add(key, True, timeout=30)` against the default cache. That
cache is LocMemCache, which is per PROCESS. With more than one worker, two
identical submissions land on different workers, neither sees the other's key,
and BOTH complete: two invoices, two GST serial numbers, stock deducted twice,
and the customer's wallet debited twice. LocMemCache also culls a third of its
entries once it holds 300, so the key could be evicted mid-checkout even with a
single worker.

It is now a unique index in the database, which is atomic across processes,
survives a restart, and does not need Redis to be correct.

The key is the CALLER'S, not a hash of the request body. That distinction is the
whole design: a salon sells the same basket constantly - two walk-ins buying the
same haircut produce byte-identical payloads - so a body hash cannot tell a retry
from the next customer. Collapsing those would lose a real sale. The till sends
one key per checkout attempt and reuses it when it retries.
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import CheckoutIdempotency, Invoice
from clients.models import Client
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class CheckoutIdempotencyTests(TestCase):
    URL = '/billing/invoices/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.other_user = CustomUser.objects.create_user(
            email='second@test.com', password='StrongPass!234', full_name='Second',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('500.00'),
            tax_percentage=Decimal('18.00'))

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def payload(self):
        return {
            'client': self.customer.id, 'center': self.center.id,
            'status': 'draft', 'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id, 'quantity': 1,
                'unit_price': '500.00', 'discount': '0',
                'tax_percentage': '18.00', 'description': 'Haircut',
                'staff': self.staff.id,
            }],
        }

    def post(self, key=None, client=None, **overrides):
        body = self.payload()
        body.update(overrides)
        headers = {'HTTP_IDEMPOTENCY_KEY': key} if key else {}
        return (client or self.api).post(self.URL, body, format='json', **headers)

    # ── the retry ────────────────────────────────────────────────────────

    def test_a_retry_with_the_same_key_returns_the_same_bill(self):
        """The till's network dropped; it retries. It must not bill twice."""
        first = self.post(key='till-1-checkout-abc')
        self.assertEqual(first.status_code, 201, first.data)

        second = self.post(key='till-1-checkout-abc')
        self.assertEqual(second.status_code, 200, second.data)

        self.assertEqual(second.data['id'], first.data['id'])
        self.assertEqual(Invoice.objects.count(), 1)

    def test_the_retry_gets_the_bill_back_not_an_error(self):
        """An error would make staff bill the customer again by hand."""
        first = self.post(key='till-1-xyz')
        second = self.post(key='till-1-xyz')

        self.assertEqual(second.data['total_amount'], first.data['total_amount'])
        self.assertEqual(second.data['invoice_number'], first.data['invoice_number'])

    def test_only_one_gst_serial_is_consumed(self):
        """Two serials for one sale would break the consecutive numbering GST
        requires."""
        self.post(key='till-1-serial')
        self.post(key='till-1-serial')

        numbers = list(Invoice.objects.values_list('invoice_number', flat=True))
        self.assertEqual(len(numbers), 1, numbers)

    def test_three_retries_still_produce_one_bill(self):
        for _ in range(3):
            self.post(key='till-1-persistent')
        self.assertEqual(Invoice.objects.count(), 1)

    # ── the second customer ──────────────────────────────────────────────

    def test_an_identical_basket_with_a_new_key_is_a_new_sale(self):
        """The reason this is not keyed on the request body.

        Two walk-ins buying the same haircut send byte-identical payloads. They
        are two sales and must produce two bills.
        """
        first = self.post(key='till-1-customer-1')
        second = self.post(key='till-1-customer-2')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.data['id'], second.data['id'])
        self.assertEqual(Invoice.objects.count(), 2)

    def test_without_a_key_every_submission_is_a_new_sale(self):
        """No key means no safe way to tell a retry from the next customer.

        Going through is the correct trade: a duplicated bill is visible and
        refundable, a silently swallowed sale is neither.
        """
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(Invoice.objects.count(), 2)

    def test_the_key_is_scoped_to_the_user(self):
        """Two tills must not collide just by choosing the same key."""
        second_client = APIClient()
        second_client.force_authenticate(self.other_user)

        first = self.post(key='checkout-1')
        second = self.post(key='checkout-1', client=second_client)

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201, second.data)
        self.assertEqual(Invoice.objects.count(), 2)

    # ── it survives a failure ────────────────────────────────────────────

    def test_a_failed_attempt_does_not_block_a_corrected_retry(self):
        """Otherwise one bad submission locks that key out for good."""
        bad = self.post(key='till-1-oops', items=[{
            'content_type': 'services.servicemaster',
            'object_id': self.service.id,
            'quantity': 0,                      # rejected
            'unit_price': '500.00', 'discount': '0',
            'tax_percentage': '18.00', 'description': 'Haircut',
        }])
        self.assertEqual(bad.status_code, 400, bad.data)
        self.assertEqual(Invoice.objects.count(), 0)

        good = self.post(key='till-1-oops')
        self.assertEqual(good.status_code, 201, good.data)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_a_failed_attempt_leaves_no_claim_behind(self):
        self.post(key='till-1-failed', items=[{
            'content_type': 'services.servicemaster',
            'object_id': self.service.id, 'quantity': 0,
            'unit_price': '500.00', 'discount': '0',
            'tax_percentage': '18.00', 'description': 'x',
        }])
        self.assertFalse(CheckoutIdempotency.objects.exists())

    # ── the record itself ────────────────────────────────────────────────

    def test_a_successful_checkout_records_its_invoice(self):
        response = self.post(key='till-1-recorded')
        record = CheckoutIdempotency.objects.get()

        self.assertEqual(record.invoice_id, response.data['id'])
        self.assertIsNotNone(record.completed_at)

    def test_the_body_field_is_accepted_as_well_as_the_header(self):
        """Not every client can set a header easily."""
        first = self.post(idempotency_key='via-body-1')
        second = self.post(idempotency_key='via-body-1')

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(Invoice.objects.count(), 1)

    def test_a_blank_key_is_treated_as_no_key(self):
        for value in ('', '   '):
            with self.subTest(value):
                response = self.post(key=value)
                self.assertEqual(response.status_code, 201, response.data)

    def test_an_in_flight_claim_answers_with_a_conflict(self):
        """A claim with no invoice yet means the first attempt is still running."""
        CheckoutIdempotency.objects.create(
            key=__import__('hashlib').sha256(
                f'{self.owner.pk}:still-running'.encode('utf-8')).hexdigest())

        response = self.post(key='still-running')
        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(Invoice.objects.count(), 0)
