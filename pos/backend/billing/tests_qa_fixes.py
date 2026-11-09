"""Regression tests for the 19 Oct 2026 QA report (C3, H2, H6, H7 and the approval defaults)."""
import io
from decimal import Decimal

from django.test import TestCase

from billing.tests import BillingTestBase


class ReceiveGoodsTests(BillingTestBase):
    """C3: a bad receipt used to close the order with nothing received."""

    def setUp(self):
        super().setUp()
        from inventory.models import PurchaseOrder, PurchaseOrderItem, Vendor
        vendor = Vendor.objects.create(name='Loreal', center=self.center)
        self.po = PurchaseOrder.objects.create(vendor=vendor, center=self.center, status='Ordered')
        self.item = PurchaseOrderItem.objects.create(
            purchase_order=self.po, product=self.product, quantity=10,
            rate=Decimal('100'), total_price=Decimal('1000'))
        self.url = f'/inventory/api/purchase-orders/{self.po.id}/receive_grpo/'

    def assert_untouched(self):
        self.po.refresh_from_db()
        self.item.refresh_from_db()
        self.product.refresh_from_db()
        self.assertEqual(self.po.status, 'Ordered')
        self.assertEqual(self.item.received_quantity, 0)
        self.assertEqual(self.product.current_stock, 20)

    def test_an_empty_receipt_is_refused(self):
        self.assertEqual(self.api.post(self.url, {'items': []}, format='json').status_code, 400)
        self.assert_untouched()

    def test_a_line_that_is_not_on_the_order_is_refused(self):
        r = self.api.post(self.url, {'items': [{'id': 999999, 'received_quantity': 3}]}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assert_untouched()

    def test_zero_and_negative_quantities_are_refused(self):
        for qty in (0, -3):
            r = self.api.post(self.url, {'items': [{'id': self.item.id, 'received_quantity': qty}]}, format='json')
            self.assertEqual(r.status_code, 400, qty)
        self.assert_untouched()

    def test_a_partial_receipt_is_not_delivered_and_the_rest_can_follow(self):
        r = self.api.post(self.url, {'items': [{'id': self.item.id, 'received_quantity': 4}]}, format='json')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data['status'], 'Partially Received')
        r = self.api.post(self.url, {'items': [{'id': self.item.id, 'received_quantity': 6}]}, format='json')
        self.assertEqual(r.data['status'], 'Delivered')


class BillLineTests(BillingTestBase):
    """H2: a line must be something the till sells and must exist."""

    def post(self, items, status='draft'):
        return self.api.post('/billing/invoices/', self.build_payload(items=items, status=status), format='json')

    def test_a_service_that_does_not_exist_is_refused(self):
        r = self.post([self.service_line(object_id=999999)])
        self.assertEqual(r.status_code, 400, r.data)

    def test_another_table_is_refused(self):
        r = self.post([self.service_line(content_type='accounts.customuser', object_id=self.owner.id)])
        self.assertEqual(r.status_code, 400, r.data)

    def test_an_empty_bill_cannot_be_completed(self):
        held = self.post([])
        if held.status_code == 201:
            r = self.api.put(f"/billing/invoices/{held.data['id']}/",
                             self.build_payload(items=[], status='paid'), format='json')
            self.assertEqual(r.status_code, 400, r.data)
        else:
            self.assertEqual(held.status_code, 400)

    def test_a_real_line_still_works(self):
        self.assertEqual(self.post([self.service_line()]).status_code, 201)


class ApprovalDefaultsTests(TestCase):
    """H1: a database that was never configured ships with the two till controls on."""

    def test_the_migration_switches_both_on_and_keeps_an_owners_choice(self):
        from importlib import import_module
        from django.apps import apps
        from approvals.models import ApprovalPolicy

        ApprovalPolicy.objects.all().delete()
        ApprovalPolicy.objects.create(kind='price_change', enabled=False)    # the owner switched it off
        import_module('approvals.migrations.0002_default_policies_on').switch_on(apps, None)
        self.assertTrue(ApprovalPolicy.objects.get(kind='discount').enabled)
        self.assertFalse(ApprovalPolicy.objects.get(kind='price_change').enabled)


class RangeTests(BillingTestBase):
    """H7: values that make no sense are refused."""

    def post(self, url, body):
        return self.api.post(url, body, format='json')

    def test_promotion_dates_and_discount(self):
        import datetime
        today = datetime.date.today()
        base = {'name': 'P', 'level': 'Organisation', 'promo_type': 'Discount', 'discount_type': 'Percentage',
                'start_date': today.isoformat(), 'end_date': (today + datetime.timedelta(days=5)).isoformat(),
                'discount_value': '10'}
        self.assertEqual(self.post('/marketing/api/promotions/', base).status_code, 201)
        for bad in ({'end_date': (today - datetime.timedelta(days=1)).isoformat()},
                    {'discount_value': '150'}, {'discount_value': '-5'}):
            self.assertEqual(self.post('/marketing/api/promotions/', {**base, 'name': 'Q', **bad}).status_code,
                             400, bad)

    def test_membership_and_card_values(self):
        for url, base, bad in (
            ('/marketing/api/memberships/', {'name': 'M', 'expiry_days': 30, 'pre_tax_price': '100'},
             {'discount_percent': '300'}),
            ('/marketing/api/cards/', {'title': 'V', 'expiry_days': 30, 'pre_tax_price': '100'},
             {'pre_tax_price': '-100'}),
        ):
            self.assertEqual(self.post(url, {**base, **bad}).status_code, 400, (url, bad))

    def test_petty_cash_must_be_positive(self):
        for amount in ('-100', '0'):
            r = self.post('/finance/api/petty-cash/', {'center': self.center.id, 'amount': amount,
                                                       'description': 'x', 'comments': 'x'})
            self.assertEqual(r.status_code, 400, amount)

    def test_incentive_percent_over_100_is_refused(self):
        r = self.post('/finance/api/incentives/', {'center': self.center.id, 'name': 'I', 'category': 'services',
                                                   'custom_percent': '250'})
        self.assertEqual(r.status_code, 400)


class FinanceReportScreenTests(BillingTestBase):
    """The Tax Report, Service Drilldown and Staff Performance screens read these."""

    def setUp(self):
        super().setUp()
        from pos_backend.testing import bill_and_pay
        r = bill_and_pay(self.api, self.build_payload(items=[self.service_line(qty=2)]))
        self.assertEqual(r.status_code, 201, r.data)
        self.window = {'center_id': self.center.id}

    def test_staff_performance_credits_a_line_that_only_lists_staff_members(self):
        r = self.api.get('/finance/api/reports/staff-performance/', self.window)
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual([row['staff_id'] for row in r.data], [self.staff.id])
        self.assertEqual(r.data[0]['services_done'], 2)
        self.assertEqual(Decimal(str(r.data[0]['revenue_generated'])), Decimal('2000.00'))

    def test_service_drilldown_and_tax_report_agree_with_the_bill(self):
        drill = self.api.get('/finance/api/reports/services/', self.window)
        self.assertEqual(drill.status_code, 200)
        self.assertEqual(drill.data[0]['count'], 2)
        tax = self.api.get('/finance/api/reports/tax/', self.window)
        self.assertEqual(tax.status_code, 200)
        self.assertEqual(len(tax.data['invoices']), 1)
        self.assertEqual(Decimal(str(tax.data['summary']['total_tax'])), Decimal('100.00'))


class SpreadsheetFormulaTests(TestCase):
    """H6: customer-typed text that starts with "=" must not become a formula."""

    def test_text_that_looks_like_a_formula_is_stored_as_text(self):
        import openpyxl
        from pos_backend import xlsx_safe
        xlsx_safe.install()
        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet('t')
        ws.append(['=HYPERLINK("http://x","click")', 5])
        out = io.BytesIO()
        wb.save(out)
        sheet = openpyxl.load_workbook(io.BytesIO(out.getvalue())).worksheets[0]
        formula, number = sheet[1]
        self.assertEqual(formula.data_type, 's')
        self.assertEqual(number.data_type, 'n')
