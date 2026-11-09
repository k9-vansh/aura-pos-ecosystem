"""
Three tabs of one Finance screen must not disagree about the same month.

REC-1: "refunds" had three incompatible definitions.

  Refunds tab      InvoiceRefund rows, by `refunded_on`, summing `amount`
  Register Summary the same
  Monthly Sales    CANCELLED INVOICES, by `created_at`, summing `total_amount`
                   - and then the figure was thrown away and a literal 0 was
                     emitted, so the column always read zero

Three separate errors in one column:
  * wrong month - a January bill refunded in March landed in January;
  * wrong amount - the bill TOTAL rather than what was actually returned, so a
    partly-paid bill overstated the refund by whatever was never collected;
  * and the column showed 0 regardless, while the Refunds tab showed a figure.

The owner's complaint about this class of bug is the one that matters most: if
two screens disagree, neither can be trusted, and there is no way to tell which
is wrong.
"""

import datetime
from pos_backend.testing import bill_and_pay
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceRefund
from clients.models import Client
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


class RefundReconciliationTests(TestCase):
    def setUp(self):
        # Monthly sales caches its result for 60s per branch and dates; clear
        # it so this test does not read an earlier test's figures.
        cache.clear()
        self.center = Center.objects.create(center_name='Main', display_name='Main',
                                            gst_enabled=True)
        self.role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.role, center=self.center)
        self.customer = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center)
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('1000.00'),
            tax_percentage=Decimal('5.00'))
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'))

        self.api = APIClient()
        self.api.force_authenticate(self.owner)

        self.today = datetime.date.today()
        self.first_of_month = self.today.replace(day=1)

    def bill(self, paid, status=Invoice.STATUS_PARTIAL):
        """A bill of Rs 1,050 with `paid` collected."""
        response = bill_and_pay(self.api, {
            'client': self.customer.id,
            'center': self.center.id,
            'status': 'paid' if paid == Decimal('1050.00') else 'partial',
            'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id,
                'quantity': 1, 'unit_price': '1000.00', 'discount': '0',
                'tax_percentage': '5.00', 'description': 'Haircut',
                'staff': self.staff.id,
            }],
            'payments': [{'payment_method': 'Cash', 'amount': str(paid)}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        return response.data['id']

    def refund(self, invoice_id):
        response = self.api.post(f'/billing/invoices/{invoice_id}/cancel/',
                                 {'reason': 'customer unhappy'}, format='json')
        self.assertEqual(response.status_code, 200, response.data)

    def month_params(self):
        return {
            'start_date': self.first_of_month.isoformat(),
            'end_date': self.today.isoformat(),
            'center_id': self.center.id,
        }

    def refunds_tab_total(self):
        response = self.api.get('/finance/api/refunds/', self.month_params())
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data.get('refunds', response.data)
        if isinstance(rows, dict):
            rows = rows.get('results', [])
        return sum(Decimal(str(r.get('amount') or 0)) for r in rows)

    def monthly_sales_refunds(self):
        response = self.api.get('/finance/api/monthly_sales/', self.month_params())
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data if isinstance(response.data, list) else response.data.get('results', [])
        # Reported negative (a refund reduces the month's sales).
        return sum(abs(Decimal(str(r.get('refunds') or 0))) for r in rows)

    # ── the reconciliation ────────────────────────────────────────────────

    def test_the_refunds_tab_and_monthly_sales_agree(self):
        """Monthly Sales reported 0 no matter what."""
        invoice_id = self.bill(Decimal('1050.00'))
        self.refund(invoice_id)

        tab = self.refunds_tab_total()
        monthly = self.monthly_sales_refunds()

        self.assertEqual(tab, Decimal('1050.00'))
        self.assertEqual(
            monthly, tab,
            f'Monthly Sales says {monthly} and the Refunds tab says {tab} for the '
            f'same month')

    def test_both_report_what_was_returned_not_the_bill_total(self):
        """A partly-paid bill: only Rs 400 ever changed hands, so only Rs 400
        can come back. Summing total_amount claimed Rs 1,050."""
        # Bills are paid in full now (#37); part-paid ones exist from before.
        invoice_id = self.bill(Decimal('1050.00'))
        from billing.models import Invoice, Payment
        Payment.objects.filter(invoice_id=invoice_id).update(amount=Decimal('400.00'))
        Invoice.objects.filter(pk=invoice_id).update(paid_amount=Decimal('400.00'), status='partial')
        self.refund(invoice_id)

        self.assertEqual(self.refunds_tab_total(), Decimal('400.00'))
        self.assertEqual(self.monthly_sales_refunds(), Decimal('400.00'))

        refunded = InvoiceRefund.objects.get(invoice_id=invoice_id)
        self.assertEqual(refunded.amount, Decimal('400.00'))

    def test_a_refund_lands_in_the_month_the_money_went_back(self):
        """A January bill refunded in March belongs to March.

        Bucketing by `created_at` put it in January, which also meant a closed
        month's figures changed after the fact.
        """
        invoice_id = self.bill(Decimal('1050.00'))
        self.refund(invoice_id)

        # Backdate the BILL to two months ago, leaving the refund today.
        old_date = self.first_of_month - datetime.timedelta(days=60)
        Invoice.objects.filter(pk=invoice_id).update(
            created_at=datetime.datetime.combine(old_date, datetime.time(10, 0)))

        # This month still shows the refund...
        self.assertEqual(self.monthly_sales_refunds(), Decimal('1050.00'))

        # ...and the month the bill was raised does not.
        response = self.api.get('/finance/api/monthly_sales/', {
            'start_date': old_date.replace(day=1).isoformat(),
            'end_date': (old_date.replace(day=28)).isoformat(),
            'center_id': self.center.id,
        })
        rows = response.data if isinstance(response.data, list) else response.data.get('results', [])
        self.assertEqual(
            sum(abs(Decimal(str(r.get('refunds') or 0))) for r in rows),
            Decimal('0'),
            'the refund was counted in the month the bill was raised')

    def test_no_refunds_means_zero_everywhere(self):
        self.bill(Decimal('1050.00'))
        self.assertEqual(self.refunds_tab_total(), Decimal('0'))
        self.assertEqual(self.monthly_sales_refunds(), Decimal('0'))

    def test_two_refunds_are_both_counted(self):
        first = self.bill(Decimal('1050.00'))
        second = self.bill(Decimal('1050.00'))
        self.refund(first)
        self.refund(second)

        self.assertEqual(self.refunds_tab_total(), Decimal('2100.00'))
        self.assertEqual(self.monthly_sales_refunds(), Decimal('2100.00'))

    def test_a_refund_at_another_branch_is_not_counted_here(self):
        other = Center.objects.create(center_name='Other', display_name='Other')
        other_customer = Client.objects.create(
            first_name='Bela', phone='9990000002', center=other)

        response = bill_and_pay(self.api, {
            'client': other_customer.id, 'center': other.id, 'status': 'paid',
            'discount': '0',
            'items': [{
                'content_type': 'services.servicemaster',
                'object_id': self.service.id, 'quantity': 1,
                'unit_price': '1000.00', 'discount': '0',
                'tax_percentage': '5.00', 'description': 'Haircut',
            }],
            'payments': [{'payment_method': 'Cash', 'amount': '1050.00'}],
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        self.refund(response.data['id'])

        # Filtered to our branch, both views must ignore it.
        self.assertEqual(self.refunds_tab_total(), Decimal('0'))
        self.assertEqual(self.monthly_sales_refunds(), Decimal('0'))

    def test_a_junk_date_does_not_break_either_view(self):
        for view in ('/finance/api/refunds/', '/finance/api/monthly_sales/'):
            with self.subTest(view):
                response = self.api.get(view, {'start_date': 'not-a-date',
                                               'center_id': self.center.id})
                self.assertLess(response.status_code, 500, view)
