"""
Staff module tests: credential exposure, app login, payroll controls.
"""

import datetime
from decimal import Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.money import money
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from staff.models import PayrollRecord, ServiceLog, StaffMember


class StaffTestBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.other_center = Center.objects.create(center_name='Other')

        self.owner_role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='pw', full_name='Owner',
            role=self.owner_role, center=self.center,
        )
        # A receptionist: can read the staff directory, nothing sensitive.
        self.recep_role = Role.objects.create(name='Receptionist', permissions={
            'staff': {'directory': {'read': True, 'create': False,
                                    'update': False, 'delete': False}},
        })
        self.receptionist = CustomUser.objects.create_user(
            email='recep@test.com', password='pw', full_name='Reception',
            role=self.recep_role, center=self.center,
        )

        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', last_name='Kumar',
            designation='Stylist', salary=Decimal('20000.00'),
            commission_percentage=Decimal('10.00'),
            aadhar_number='1234-5678-9012',
            app_password=make_password('4321'),
            phone='9990001111',
        )

        self.api = APIClient()


class CredentialExposureTests(StaffTestBase):
    """C10 - fields='__all__' leaked the PIN hash, Aadhaar and salary."""

    def test_receptionist_cannot_see_credentials_or_pii(self):
        self.api.force_authenticate(self.receptionist)
        response = self.api.get('/staff/api/members/')
        self.assertEqual(response.status_code, 200)
        rows = response.data if isinstance(response.data, list) else response.data['results']
        self.assertTrue(rows)
        row = rows[0]

        self.assertNotIn('app_password', row)
        self.assertNotIn('aadhar_number', row)
        self.assertNotIn('salary', row)
        self.assertNotIn('commission_percentage', row)
        # Non-sensitive fields still come through.
        self.assertIn('first_name', row)
        self.assertIn('designation', row)

    def test_owner_can_see_compensation(self):
        self.api.force_authenticate(self.owner)
        response = self.api.get('/staff/api/members/')
        rows = response.data if isinstance(response.data, list) else response.data['results']
        row = rows[0]
        self.assertIn('salary', row)
        self.assertIn('aadhar_number', row)

    def test_app_password_is_never_serialised_even_for_an_owner(self):
        self.api.force_authenticate(self.owner)
        response = self.api.get(f'/staff/api/members/{self.staff.id}/')
        self.assertNotIn('app_password', response.data)

    def test_app_password_cannot_be_written_directly(self):
        """C10: app_password was writable, so a plaintext string became a PIN."""
        self.api.force_authenticate(self.receptionist)
        original = self.staff.app_password
        self.api.patch(f'/staff/api/members/{self.staff.id}/',
                       {'app_password': '9999'}, format='json')
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.app_password, original)

    def test_receptionist_cannot_change_salary(self):
        self.api.force_authenticate(self.receptionist)
        response = self.api.patch(f'/staff/api/members/{self.staff.id}/',
                                  {'salary': '999999.00'}, format='json')
        self.assertIn(response.status_code, (400, 403))
        self.staff.refresh_from_db()
        self.assertEqual(money(self.staff.salary), Decimal('20000.00'))

    def test_owner_sets_pin_through_the_write_only_field_and_it_is_hashed(self):
        self.api.force_authenticate(self.owner)
        response = self.api.patch(f'/staff/api/members/{self.staff.id}/',
                                  {'new_app_pin': '5678'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.staff.refresh_from_db()
        self.assertNotEqual(self.staff.app_password, '5678')
        self.assertTrue(check_password('5678', self.staff.app_password))

    def test_non_owner_cannot_set_a_pin(self):
        self.api.force_authenticate(self.receptionist)
        response = self.api.patch(f'/staff/api/members/{self.staff.id}/',
                                  {'new_app_pin': '5678'}, format='json')
        self.assertIn(response.status_code, (400, 403))
        self.staff.refresh_from_db()
        self.assertFalse(check_password('5678', self.staff.app_password))


class ManualServiceLogTests(StaffTestBase):
    """M17 - manually recorded logs earned no commission."""

    def test_manual_log_appears_in_the_default_manager(self):
        log = ServiceLog.objects.create(
            staff=self.staff, center=self.center, client_name='Walk-in',
            service_name='Head Massage', service_type='Service',
            price=Decimal('500.00'), date=datetime.date.today(),
            time=datetime.time(11, 0), invoice=None,
        )
        self.assertIn(log, ServiceLog.objects.all())

    def test_manual_log_counts_toward_the_commission_report(self):
        ServiceLog.objects.create(
            staff=self.staff, center=self.center, client_name='Walk-in',
            service_name='Head Massage', service_type='Service',
            price=Decimal('500.00'), date=datetime.date.today(),
            time=datetime.time(11, 0), invoice=None,
        )
        self.api.force_authenticate(self.owner)
        today = datetime.date.today().isoformat()
        response = self.api.get('/staff/api/members/commission_report/',
                                {'start_date': today, 'end_date': today})
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data['results']
        self.assertTrue(rows)
        row = next(r for r in rows if r['staff_id'] == self.staff.id)
        self.assertEqual(money(row['service_revenue']), Decimal('500.00'))
        self.assertEqual(money(row['service_commission']), Decimal('50.00'))


class PayrollControlTests(StaffTestBase):
    """H29 / H27 - who may lock payroll, and net_pay integrity."""

    def make_payroll(self, **kw):
        defaults = dict(
            staff=self.staff, center=self.center, month=8, year=2026,
            base_salary=Decimal('20000.00'),
            service_commission=Decimal('3000.00'),
            product_commission=Decimal('500.00'),
            other_incentive=Decimal('800.00'),
            deductions=Decimal('300.00'),
        )
        defaults.update(kw)
        record = PayrollRecord(**defaults)
        record.net_pay = record.computed_net_pay
        record.save()
        return record

    def test_net_pay_must_match_its_components(self):
        """H27: net_pay was free-form and never cross-checked."""
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)
        response = self.api.patch(f'/staff/api/payrolls/{record.id}/',
                                  {'net_pay': '999999.00'}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('does not match', str(response.data))

    def test_net_pay_is_derived_when_omitted(self):
        self.api.force_authenticate(self.owner)
        response = self.api.post('/staff/api/payrolls/', {
            'staff': self.staff.id, 'center': self.center.id,
            'month': 9, 'year': 2026,
            'base_salary': '20000.00', 'service_commission': '1000.00',
            'product_commission': '0.00', 'other_incentive': '0.00',
            'deductions': '500.00',
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(money(response.data['net_pay']), Decimal('20500.00'))

    def test_receptionist_cannot_lock_payroll(self):
        """H29: lock/mark_paid needed nothing beyond being logged in."""
        record = self.make_payroll()
        self.api.force_authenticate(self.receptionist)
        response = self.api.post(f'/staff/api/payrolls/{record.id}/lock/')
        self.assertIn(response.status_code, (403, 404))
        record.refresh_from_db()
        self.assertEqual(record.status, 'Draft')

    def test_owner_can_lock_and_unlock(self):
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)

        locked = self.api.post(f'/staff/api/payrolls/{record.id}/lock/')
        self.assertEqual(locked.status_code, 200, locked.data)
        record.refresh_from_db()
        self.assertEqual(record.status, 'Locked')
        self.assertIsNotNone(record.locked_at)

        unlocked = self.api.post(f'/staff/api/payrolls/{record.id}/unlock/')
        self.assertEqual(unlocked.status_code, 200, unlocked.data)
        record.refresh_from_db()
        self.assertEqual(record.status, 'Draft')

    def test_status_cannot_be_changed_by_a_patch(self):
        """The lock used to be bypassable with a plain PATCH."""
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)
        self.api.post(f'/staff/api/payrolls/{record.id}/lock/')
        response = self.api.patch(f'/staff/api/payrolls/{record.id}/',
                                  {'status': 'Draft'}, format='json')
        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertEqual(record.status, 'Locked')

    def test_mark_paid_requires_locked_first(self):
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)
        response = self.api.post(f'/staff/api/payrolls/{record.id}/mark_paid/')
        self.assertEqual(response.status_code, 400)

    def test_paid_payroll_cannot_be_reopened(self):
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)
        self.api.post(f'/staff/api/payrolls/{record.id}/lock/')
        self.api.post(f'/staff/api/payrolls/{record.id}/mark_paid/')

        response = self.api.post(f'/staff/api/payrolls/{record.id}/unlock/')
        self.assertEqual(response.status_code, 400)
        record.refresh_from_db()
        self.assertEqual(record.status, 'Paid')

    def test_locked_payroll_cannot_be_deleted(self):
        record = self.make_payroll()
        self.api.force_authenticate(self.owner)
        self.api.post(f'/staff/api/payrolls/{record.id}/lock/')
        response = self.api.delete(f'/staff/api/payrolls/{record.id}/')
        self.assertEqual(response.status_code, 400)
        self.assertTrue(PayrollRecord.objects.filter(pk=record.id).exists())


class PayrollGenerationTests(StaffTestBase):
    """H27 - nothing used to write PayrollRecord at all."""

    def test_generate_creates_draft_records_from_the_engine(self):
        from django.contrib.contenttypes.models import ContentType
        from billing.models import Invoice, InvoiceItem
        from finance.models import IncentiveRule
        from services.models import ServiceMaster

        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
        )
        invoice = Invoice.objects.create(
            center=self.center, status='paid',
            subtotal=Decimal('10000.00'), total_amount=Decimal('10000.00'),
            paid_amount=Decimal('10000.00'),
        )
        item = InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(ServiceMaster),
            object_id=service.id, description='Cut',
            unit_price=Decimal('1000.00'), quantity=10,
            total_price=Decimal('10000.00'),
        )
        item.staff_members.set([self.staff])

        IncentiveRule.objects.create(
            name='services 10%', category='services', rule_type='percentage',
            flat_percent=Decimal('10.00'), applicable_role='all', center=self.center,
        )

        today = datetime.date.today()
        self.api.force_authenticate(self.owner)
        response = self.api.post('/staff/api/payrolls/generate/', {
            'month': today.month, 'year': today.year, 'center_id': self.center.id,
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertGreaterEqual(response.data['created'], 1)

        record = PayrollRecord.objects.get(
            staff=self.staff, month=today.month, year=today.year
        )
        self.assertEqual(money(record.service_commission), Decimal('1000.00'))
        self.assertEqual(money(record.net_pay), record.computed_net_pay)
        self.assertIn('total_sales', record.breakdown)

    def _record_sales(self, amount='10000.00'):
        """Give self.staff some sales in the current month."""
        from django.contrib.contenttypes.models import ContentType
        from billing.models import Invoice, InvoiceItem
        from services.models import ServiceMaster

        service = ServiceMaster.objects.create(
            name='Cut', category='Cuts', default_price=Decimal('1000.00'),
        )
        invoice = Invoice.objects.create(
            center=self.center, status='paid',
            subtotal=Decimal(amount), total_amount=Decimal(amount),
            paid_amount=Decimal(amount),
        )
        item = InvoiceItem.objects.create(
            invoice=invoice,
            content_type=ContentType.objects.get_for_model(ServiceMaster),
            object_id=service.id, description='Cut',
            unit_price=Decimal('1000.00'), quantity=10,
            total_price=Decimal(amount),
        )
        item.staff_members.set([self.staff])
        return invoice

    def test_generate_skips_locked_periods(self):
        """A locked period must not be silently overwritten by a regeneration."""
        today = datetime.date.today()
        self._record_sales()

        existing = PayrollRecord(
            staff=self.staff, center=self.center, month=today.month, year=today.year,
            base_salary=Decimal('20000.00'), status='Locked',
        )
        existing.net_pay = existing.computed_net_pay
        existing.save()
        frozen_net_pay = money(existing.net_pay)

        self.api.force_authenticate(self.owner)
        response = self.api.post('/staff/api/payrolls/generate/', {
            'month': today.month, 'year': today.year,
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data['skipped'], response.data)
        self.assertIn('Locked', str(response.data['skipped']))

        existing.refresh_from_db()
        self.assertEqual(money(existing.net_pay), frozen_net_pay)
        self.assertEqual(existing.status, 'Locked')

    def test_generate_updates_a_draft_period(self):
        today = datetime.date.today()
        self._record_sales()

        draft = PayrollRecord(
            staff=self.staff, center=self.center, month=today.month, year=today.year,
            base_salary=Decimal('1.00'),
        )
        draft.net_pay = draft.computed_net_pay
        draft.save()

        self.api.force_authenticate(self.owner)
        response = self.api.post('/staff/api/payrolls/generate/', {
            'month': today.month, 'year': today.year,
        }, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['updated'], 1)

        draft.refresh_from_db()
        self.assertEqual(money(draft.base_salary), Decimal('20000.00'))

    def test_receptionist_cannot_generate_payroll(self):
        self.api.force_authenticate(self.receptionist)
        response = self.api.post('/staff/api/payrolls/generate/', {
            'month': 8, 'year': 2026,
        }, format='json')
        self.assertEqual(response.status_code, 403)
