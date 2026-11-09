"""Phase 9: the closing is worked out by the server (X4)."""

import datetime
import tempfile
from decimal import Decimal

from django.test import override_settings

from billing.models import Payment
from finance.models import DailyClosing, PettyCashEntry, Shift
from finance.tests import FinanceTestBase
from pos_backend.testing import bill_picture

DAY = datetime.date.today()


class ClosingTests(FinanceTestBase):
    def take(self, method, amount, **kw):
        invoice = self.make_invoice(total=amount, **kw)
        Payment.objects.create(invoice=invoice, amount=Decimal(amount), payment_method=method)
        return invoice

    def test_compute_gives_the_drawer_figure(self):
        DailyClosing.objects.create(center=self.center, date=DAY - datetime.timedelta(days=1),
                                    closing_balance=Decimal('1000.00'))
        self.take('Cash', '2500.00')
        self.take('UPI', '700.00')
        self.take('Cash', '999.00', status='cancelled')          # a cancelled bill is not counted
        PettyCashEntry.objects.create(center=self.center, description='Tea', amount=Decimal('200.00'))
        res = self.api.get('/finance/api/daily-closing/compute/',
                           {'center_id': self.center.id, 'date': str(DAY)})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(Decimal(str(res.data['system_cash'])), Decimal('2500.00'))
        self.assertEqual(Decimal(str(res.data['upi'])), Decimal('700.00'))
        self.assertEqual(Decimal(str(res.data['todays_expenses'])), Decimal('200.00'))
        self.assertEqual(Decimal(str(res.data['expected_cash'])), Decimal('3300.00'))

    def test_the_browsers_figures_are_ignored(self):
        self.take('Cash', '2000.00')
        res = self.api.post('/finance/api/daily-closing/', {
            'center': self.center.id, 'date': str(DAY),
            'system_cash': '999999', 'todays_expenses': '5', 'difference': '0', 'upi': '123',
            'opening_balance': '777', 'closing_balance': '1',
            'cash_in_hand': '1900.00', 'cash_deposit': '500.00'}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        row = DailyClosing.objects.get()
        self.assertEqual(row.system_cash, Decimal('2000.00'))
        self.assertEqual(row.upi, Decimal('0.00'))
        self.assertEqual(row.opening_balance, Decimal('0.00'))
        self.assertEqual(row.difference, Decimal('-100.00'))         # counted 1900, expected 2000
        self.assertEqual(row.closing_balance, Decimal('1500.00'))     # 2000 - 500 deposited

    def test_an_amendment_is_recomputed_too(self):
        self.take('Cash', '2000.00')
        self.api.post('/finance/api/daily-closing/', {
            'center': self.center.id, 'date': str(DAY), 'cash_in_hand': '2000', 'cash_deposit': '0'},
            format='json')
        row = DailyClosing.objects.get()
        res = self.api.patch(f'/finance/api/daily-closing/{row.id}/',
                             {'cash_in_hand': '1950', 'system_cash': '1'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        row.refresh_from_db()
        self.assertEqual((row.system_cash, row.difference), (Decimal('2000.00'), Decimal('-50.00')))


class ShiftTests(FinanceTestBase):
    def test_expected_cash_is_the_servers_not_the_browsers(self):
        shift = Shift.objects.create(center=self.center, opened_by=self.owner,
                                     starting_float=Decimal('500.00'))
        invoice = self.make_invoice(total='1200.00')
        Payment.objects.create(invoice=invoice, amount=Decimal('1200.00'), payment_method='Cash')
        res = self.api.post(f'/finance/api/shifts/{shift.id}/close_shift/',
                            {'actual_cash': '1650', 'expected_cash': '0'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        shift.refresh_from_db()
        self.assertEqual(shift.expected_cash, Decimal('1700.00'))
        self.assertEqual(shift.variance, Decimal('-50.00'))


class FlatSplitTests(FinanceTestBase):
    def test_weighted_split_adds_up_exactly(self):
        from finance.incentives import weighted_split
        self.assertEqual(weighted_split(Decimal('100.00'), [70, 30]), [Decimal('70.00'), Decimal('30.00')])
        parts = weighted_split(Decimal('100.00'), [1, 1, 1])
        self.assertEqual(sum(parts), Decimal('100.00'))
        parts = weighted_split(Decimal('10.01'), [33.3, 33.3, 33.4])
        self.assertEqual(sum(parts), Decimal('10.01'))
        self.assertEqual(weighted_split(Decimal('50'), [0, 0]), [Decimal('25.00'), Decimal('25.00')])


class StaffPhase9Tests(FinanceTestBase):
    def test_aadhaar_keeps_only_the_last_four_digits(self):
        res = self.api.patch(f'/staff/api/members/{self.stylist.id}/',
                             {'aadhar_number': '1234 5678 9012'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.stylist.refresh_from_db()
        self.assertEqual(self.stylist.aadhar_number, 'XXXXXXXX9012')

    def test_a_salary_change_is_written_to_the_activity_log(self):
        from audit_logs.models import SystemLog
        res = self.api.patch(f'/staff/api/members/{self.stylist.id}/', {'salary': '25000.00'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        log = SystemLog.objects.filter(entity_type='Staff Pay', entity_id=str(self.stylist.id)).first()
        self.assertIsNotNone(log)
        self.assertIn('20000.00 -> 25000.00', log.description)

    def test_a_manual_service_log_needs_its_own_tick(self):
        from accounts.models import CustomUser
        from salon_admin.models import Role
        role = Role.objects.create(name='Logs', permissions={'staff': {'logs': {'read': True, 'create': True}}})
        user = CustomUser.objects.create_user(email='l@test.com', password='pw', full_name='L',
                                              role=role, center=self.center)
        from rest_framework.test import APIClient
        api = APIClient()
        api.force_authenticate(user)
        body = {'staff': self.stylist.id, 'center': self.center.id, 'client_name': 'X',
                'service_name': 'Cut', 'price': '500.00', 'date': str(DAY), 'time': '10:00:00'}
        self.assertEqual(api.post('/staff/api/logs/', body, format='json').status_code, 403)
        res = self.api.post('/staff/api/logs/', body, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        from audit_logs.models import SystemLog
        self.assertTrue(SystemLog.objects.filter(entity_type='Service Log').exists())


class LeftoverTests(FinanceTestBase):
    def test_designation_rules_can_name_several_designations(self):
        from finance.incentives import rule_applies_to_role

        class R:
            applicable_role = 'Senior Stylist, Colourist'
        self.assertTrue(rule_applies_to_role(R, 'senior stylist'))
        self.assertTrue(rule_applies_to_role(R, 'Colourist'))
        self.assertFalse(rule_applies_to_role(R, 'Beautician'))

    def test_tax_report_has_the_return_sections(self):
        from django.contrib.contenttypes.models import ContentType
        from billing.models import InvoiceItem
        inv = self.make_invoice(total='1050.00')
        InvoiceItem.objects.create(invoice=inv, description='Cut', unit_price=Decimal('1000'), quantity=1,
                                   tax_percentage=Decimal('5'), tax_amount=Decimal('50'),
                                   total_price=Decimal('1000'), hsn_sac='999722')
        res = self.api.get('/finance/api/reports/tax/', {'center_id': self.center.id})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['b2c']['invoices'], 1)
        self.assertEqual(res.data['b2b']['invoices'], 0)
        hsn = res.data['hsn_summary'][0]
        self.assertEqual((hsn['hsn_sac'], hsn['rate'], hsn['taxable_value'], hsn['tax']),
                         ('999722', '5', Decimal('1000.00'), Decimal('50.00')))
        self.assertEqual(res.data['rate_wise'][0]['rate'], '5')

    def test_petty_cash_categories(self):
        from finance.models import PettyCashCategory
        # Categories are SAP's accounts: not made by hand, and not usable without an account.
        res = self.api.post('/finance/api/petty-cash-categories/', {'name': 'Tea & snacks'}, format='json')
        self.assertEqual(res.status_code, 405, res.data)
        hand_made = PettyCashCategory.objects.create(name='Tea & snacks')
        cat = PettyCashCategory.objects.create(name='Client Refreshment', sap_account='5411000003')
        body = {'center': self.center.id, 'description': 'Tea', 'amount': '120.00'}
        self.assertEqual(self.api.post('/finance/api/petty-cash/', body, format='json').status_code, 400)
        self.assertEqual(self.api.post('/finance/api/petty-cash/', {**body, 'category': hand_made.id},
                                       format='json').status_code, 400)
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='pos-test-media-')):
            entry = self.api.post('/finance/api/petty-cash/', {**body, 'category': cat.id, 'bill_image': bill_picture()},
                                  format='multipart')
        self.assertEqual(entry.status_code, 201, entry.data)
        self.assertEqual(entry.data['category_name'], 'Client Refreshment')
        self.assertEqual(entry.data['user_name'], 'Owner')
        self.assertFalse(entry.data['sap_sync_status'])

    def test_two_tills_can_have_shifts_but_not_the_same_till_twice(self):
        body = {'center': self.center.id, 'starting_float': '500', 'till': 'Till 2'}
        self.assertEqual(self.api.post('/finance/api/shifts/', {**body, 'till': 'Till 1'}, format='json').status_code, 201)
        self.assertEqual(self.api.post('/finance/api/shifts/', body, format='json').status_code, 201)
        self.assertEqual(self.api.post('/finance/api/shifts/', body, format='json').status_code, 400)

    def test_x_report_and_z_report_and_variance_alert(self):
        from notifications.models import Notification
        from notifications.models import NotificationRule
        NotificationRule.objects.create(event='closing.variance', user_ids=[self.owner.id])
        shift = Shift.objects.create(center=self.center, opened_by=self.owner, starting_float=Decimal('100'))
        invoice = self.make_invoice(total='400.00')
        Payment.objects.create(invoice=invoice, amount=Decimal('400.00'), payment_method='Cash')
        x = self.api.get(f'/finance/api/shifts/{shift.id}/x_report/')
        self.assertEqual((x.status_code, Decimal(str(x.data['expected_cash']))), (200, Decimal('500.00')))
        self.assertEqual(Shift.objects.get().status, 'Open')                 # X does not close
        z = self.api.post(f'/finance/api/shifts/{shift.id}/close_shift/', {'actual_cash': '480'}, format='json')
        self.assertEqual(z.status_code, 200, z.data)
        self.assertEqual(Decimal(str(z.data['z_report']['expected_cash'])), Decimal('500.00'))
        self.assertTrue(Notification.objects.filter(event='closing.variance', user=self.owner).exists())

    def test_reconciliation_shows_billed_but_not_in_sap(self):
        a = self.make_invoice(total='1000.00')
        b = self.make_invoice(total='500.00')
        from billing.models import Invoice
        Invoice.objects.filter(pk=a.pk).update(sap_sync_status=True)
        res = self.api.get('/finance/api/reports/sap-reconciliation/', {'center_id': self.center.id})
        self.assertEqual(res.status_code, 200, res.data)
        t = res.data['totals']
        self.assertEqual((t['billed'], t['in_sap'], t['not_in_sap']),
                         (Decimal('1500.00'), Decimal('1000.00'), Decimal('500.00')))
