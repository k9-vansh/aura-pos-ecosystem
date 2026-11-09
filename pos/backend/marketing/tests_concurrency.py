"""Two tills, one coupon, one customer, at the same moment.

Needs a real multi-connection database, so it runs on MySQL only
(`DB_ENGINE=mysql manage.py test marketing.tests_concurrency`) and is skipped on
SQLite, which serialises every writer.
"""
import datetime
import threading
from decimal import Decimal

from django.db import connection, connections
from django.test import TransactionTestCase, skipUnlessDBFeature
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice
from clients.models import Client
from marketing.models import Promotion, PromotionUsage
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class SimultaneousCheckoutTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != 'mysql':
            self.skipTest('needs MySQL: SQLite serialises all writers')
        self.center = Center.objects.create(center_name='A', display_name='A', gst_enabled=True)
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='O',
                                                   role=role, center=self.center)
        self.client_obj = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='M', designation='S')
        self.svc = ServiceMaster.objects.create(name='Cut', default_price=Decimal('1000'), tax_percentage=Decimal('18'))
        self.promo = Promotion.objects.create(
            name='Once', promo_type='Discount', discount_type='Percentage', discount_value=Decimal('10'),
            level='Organisation', max_usage_per_client=1, config={},
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=5))

    def payload(self):
        return {'client': self.client_obj.id, 'center': self.center.id, 'status': 'paid', 'discount': '0',
                'promo_id': self.promo.id,
                'items': [{'content_type': 'services.servicemaster', 'object_id': self.svc.id,
                           'description': 'Cut', 'unit_price': '1000', 'discount': '0', 'quantity': 1,
                           'tax_percentage': '18', 'staff_members': [self.staff.id]}]}

    def test_a_one_per_customer_coupon_works_once_when_two_tills_race(self):
        results, barrier = [], threading.Barrier(2)

        def till():
            try:
                api = APIClient()
                api.force_authenticate(self.user)
                barrier.wait(timeout=10)
                r = bill_and_pay(api, self.payload())
                results.append(r.status_code)
            except Exception as exc:                       # a crash is a failure, recorded
                results.append(repr(exc)[:120])
            finally:
                connections.close_all()

        threads = [threading.Thread(target=till) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(sorted(map(str, results)).count('201'), 1, results)
        self.assertEqual(PromotionUsage.objects.filter(promotion=self.promo, is_reversed=False).count(), 1)
        self.assertEqual(Invoice.objects.filter(status='paid', promotion=self.promo).count(), 1)


class SimultaneousCouponTests(TransactionTestCase):
    """One brand coupon code typed at two tills in the same moment: one bill gets it."""

    def setUp(self):
        if connection.vendor != 'mysql':
            self.skipTest('needs MySQL: SQLite serialises all writers')
        from django.contrib.contenttypes.models import ContentType
        from marketing import coupons
        # The previous test's flush gave every content type a new id; the cached ones are gone.
        from billing import pricing
        ContentType.objects.clear_cache()
        pricing.clear_content_type_cache()
        self.center = Center.objects.create(center_name='A', display_name='A', gst_enabled=True)
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@t.com', password='pw', full_name='O',
                                                   role=role, center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='M', designation='S')
        self.svc = ServiceMaster.objects.create(name='Cut', default_price=Decimal('1000'), tax_percentage=Decimal('18'))
        self.promo = Promotion.objects.create(
            name='Blinkit', promo_type='Coupon', level='Organisation',
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=5),
            config={'brand': 'Blinkit', 'coupon_value_type': 'Flat', 'coupon_value': 500})
        coupons.import_codes(self.promo, 'RACE-0001')

    def payload(self):
        return {'center': self.center.id, 'status': 'paid', 'discount': '0',
                'payments': [{'payment_method': 'Gift Card', 'amount': '500', 'reference': 'RACE-0001'},
                             {'payment_method': 'Cash', 'amount': '680'}],
                'items': [{'content_type': 'services.servicemaster', 'object_id': self.svc.id,
                           'description': 'Cut', 'unit_price': '1000', 'discount': '0', 'quantity': 1,
                           'tax_percentage': '18', 'staff_members': [self.staff.id]}]}

    def test_a_code_typed_at_two_tills_at_once_pays_one_bill_only(self):
        from marketing.models import CouponCode
        results, barrier = [], threading.Barrier(2)

        def till():
            try:
                api = APIClient()
                api.force_authenticate(self.user)
                barrier.wait(timeout=10)
                results.append(api.post('/billing/invoices/', self.payload(), format='json').status_code)
            except Exception as exc:                       # a crash is a failure, recorded
                results.append(repr(exc)[:120])
            finally:
                connections.close_all()

        threads = [threading.Thread(target=till) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        self.assertEqual(sorted(map(str, results)).count('201'), 1, results)
        code = CouponCode.objects.get(code_key='RACE0001')
        self.assertTrue(code.is_used)
        self.assertEqual(Invoice.objects.filter(status='paid').count(), 1)
        self.assertEqual(code.invoice_id, Invoice.objects.get(status='paid').id)
