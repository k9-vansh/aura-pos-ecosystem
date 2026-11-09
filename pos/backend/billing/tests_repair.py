"""
Tests for the repair_invoice_math management command.

This is the piece that rewrites real historical financial records, so it gets
its own tests: a dry run must change nothing, and an apply must produce
self-consistent invoices.
"""

import datetime
from decimal import Decimal
from io import StringIO

from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase

from billing.models import Invoice, InvoiceItem
from clients.models import Client
from inventory.models import Product
from pos_backend.money import D, money
from salon_admin.models import Center
from services.models import CenterService, ServiceMaster
from staff.models import ServiceLog, StaffMember


class RepairInvoiceMathTests(TestCase):
    """Reproduces the corruption C1 and H2 left behind, then repairs it."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main', display_name='Main')
        self.client_obj = Client.objects.create(
            first_name='Asha', phone='9990000001', center=self.center,
        )
        self.service = ServiceMaster.objects.create(
            name='Haircut', category='Cuts',
            default_price=Decimal('1000.00'), tax_percentage=Decimal('5.00'),
        )
        self.product = Product.objects.create(
            name='Shampoo', price=Decimal('500.00'), gst_percent=Decimal('18.00'),
            current_stock=50, center=self.center,
        )
        self.staff = StaffMember.objects.create(
            center=self.center, first_name='Ravi', designation='Stylist',
            salary=Decimal('20000.00'),
        )
        self.service_ct = ContentType.objects.get_for_model(ServiceMaster)
        self.product_ct = ContentType.objects.get_for_model(Product)

    # ─── fixtures reproducing the old corruption ──────────────────────────

    def make_corrupt_invoice(self):
        """An invoice exactly as the buggy code would have written it.

        Service line zeroed out by C1, product line stored tax-inclusive by H2,
        header carrying the client-supplied (correct) totals.
        """
        invoice = Invoice.objects.create(
            client=self.client_obj, center=self.center, status='paid',
            subtotal=Decimal('1500.00'),
            cgst=Decimal('70.00'), sgst=Decimal('70.00'),
            total_amount=Decimal('1640.00'), paid_amount=Decimal('1640.00'),
        )

        # C1: every service line was written with zeros.
        service_item = InvoiceItem.objects.create(
            invoice=invoice, content_type=self.service_ct, object_id=self.service.id,
            description='Haircut',
            unit_price=Decimal('0.00'), quantity=1,
            tax_percentage=Decimal('5.00'),
            tax_amount=Decimal('0.00'), total_price=Decimal('0.00'),
        )
        service_item.staff_members.set([self.staff])

        # H2: product lines carried a tax-INCLUSIVE total (500 + 18% = 590).
        product_item = InvoiceItem.objects.create(
            invoice=invoice, content_type=self.product_ct, object_id=self.product.id,
            description='Shampoo',
            unit_price=Decimal('500.00'), quantity=1,
            tax_percentage=Decimal('18.00'),
            tax_amount=Decimal('0.00'), total_price=Decimal('590.00'),
        )
        product_item.staff_members.set([self.staff])

        # C1 also zeroed the commission log.
        ServiceLog.objects.create(
            invoice=invoice, staff=self.staff, center=self.center,
            client_name='Asha', service_name='Haircut', service_type='Service',
            price=Decimal('0.00'),
            date=datetime.date.today(), time=datetime.time(11, 0),
        )
        return invoice

    def run_command(self, *args):
        out = StringIO()
        call_command('repair_invoice_math', *args, stdout=out, stderr=out)
        return out.getvalue()

    # ─── tests ────────────────────────────────────────────────────────────

    def test_dry_run_changes_nothing(self):
        invoice = self.make_corrupt_invoice()
        before = {
            'subtotal': money(invoice.subtotal),
            'total': money(invoice.total_amount),
            'items': [(money(i.unit_price), money(i.total_price))
                      for i in invoice.items.order_by('id')],
        }

        output = self.run_command('--dry-run')
        self.assertIn('DRY RUN', output)
        self.assertIn('Nothing was written', output)

        invoice.refresh_from_db()
        self.assertEqual(money(invoice.subtotal), before['subtotal'])
        self.assertEqual(money(invoice.total_amount), before['total'])
        self.assertEqual(
            [(money(i.unit_price), money(i.total_price))
             for i in invoice.items.order_by('id')],
            before['items'],
        )

    def test_dry_run_reports_what_it_would_change(self):
        self.make_corrupt_invoice()
        output = self.run_command('--dry-run', '--verbose')
        self.assertIn('line items repriced', output)
        self.assertIn('invoices changed', output)

    def test_apply_restores_the_service_price(self):
        """C1: the service line was stored at Rs 0."""
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply')

        service_item = invoice.items.get(content_type=self.service_ct)
        self.assertEqual(money(service_item.unit_price), Decimal('1000.00'))
        self.assertEqual(money(service_item.total_price), Decimal('1000.00'))
        self.assertEqual(money(service_item.tax_amount), Decimal('50.00'))

    def test_apply_converts_the_product_line_to_tax_exclusive(self):
        """H2: total_price held 590 (tax-inclusive) instead of 500."""
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply')

        product_item = invoice.items.get(content_type=self.product_ct)
        self.assertEqual(money(product_item.total_price), Decimal('500.00'))
        self.assertEqual(money(product_item.tax_amount), Decimal('90.00'))


    def test_apply_makes_the_invoice_self_consistent(self):
        """The invariant: sum(items) == subtotal, sum(tax) == cgst + sgst."""
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply')
        invoice.refresh_from_db()

        items = list(invoice.items.all())
        self.assertEqual(
            money(sum(D(i.total_price) for i in items)), money(invoice.subtotal)
        )
        self.assertEqual(
            money(sum(D(i.tax_amount) for i in items)),
            money(D(invoice.cgst) + D(invoice.sgst)),
        )
        # 1000 + 500 taxable; 50 + 90 tax; 1640 total.
        self.assertEqual(money(invoice.subtotal), Decimal('1500.00'))
        self.assertEqual(money(D(invoice.cgst) + D(invoice.sgst)), Decimal('140.00'))
        self.assertEqual(money(invoice.total_amount), Decimal('1640.00'))

    def test_apply_rebuilds_the_commission_log(self):
        """C1 left ServiceLog.price at zero, so no commission was ever paid."""
        invoice = self.make_corrupt_invoice()
        self.assertEqual(
            money(ServiceLog.all_objects.filter(invoice=invoice).first().price),
            Decimal('0.00'),
        )

        self.run_command('--apply')

        logs = ServiceLog.all_objects.filter(invoice=invoice)
        total = money(sum(D(log.price) for log in logs))
        # Rs 1000 service + Rs 500 product, both credited to one staff member.
        self.assertEqual(total, Decimal('1500.00'))

    def test_skip_service_logs_leaves_commissions_alone(self):
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply', '--skip-service-logs')
        logs = ServiceLog.all_objects.filter(invoice=invoice)
        self.assertEqual(money(sum(D(log.price) for log in logs)), Decimal('0.00'))

    def test_apply_assigns_a_gst_invoice_number(self):
        invoice = self.make_corrupt_invoice()
        self.assertIsNone(invoice.invoice_number)
        self.run_command('--apply')
        invoice.refresh_from_db()
        self.assertTrue(invoice.invoice_number)

    def test_skip_numbering_leaves_the_number_unset(self):
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply', '--skip-numbering')
        invoice.refresh_from_db()
        self.assertIsNone(invoice.invoice_number)

    def test_center_price_override_is_honoured(self):
        CenterService.objects.create(
            center=self.center, service=self.service,
            price=Decimal('1500.00'), is_active=True,
        )
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply')

        service_item = invoice.items.get(content_type=self.service_ct)
        self.assertEqual(money(service_item.unit_price), Decimal('1500.00'))

    def test_a_discount_on_the_line_is_preserved(self):
        invoice = self.make_corrupt_invoice()
        item = invoice.items.get(content_type=self.service_ct)
        item.discount = Decimal('200.00')
        item.save(update_fields=['discount'])

        self.run_command('--apply')
        item.refresh_from_db()
        self.assertEqual(money(item.discount), Decimal('200.00'))
        self.assertEqual(money(item.total_price), Decimal('800.00'))

    def test_deleted_catalog_item_keeps_its_stored_price(self):
        """A service that no longer exists must not be zeroed out again."""
        invoice = Invoice.objects.create(
            client=self.client_obj, center=self.center, status='paid',
            subtotal=Decimal('700.00'), total_amount=Decimal('700.00'),
            paid_amount=Decimal('700.00'),
        )
        InvoiceItem.objects.create(
            invoice=invoice, content_type=self.service_ct, object_id=999999,
            description='Deleted Service',
            unit_price=Decimal('700.00'), quantity=1,
            total_price=Decimal('700.00'),
        )
        self.run_command('--apply')
        item = invoice.items.first()
        self.assertEqual(money(item.unit_price), Decimal('700.00'))

    def test_redemption_lines_stay_at_zero(self):
        invoice = Invoice.objects.create(
            client=self.client_obj, center=self.center, status='paid',
            subtotal=Decimal('0.00'), total_amount=Decimal('0.00'),
        )
        InvoiceItem.objects.create(
            invoice=invoice, content_type=self.service_ct, object_id=self.service.id,
            description='Haircut (redeemed)', is_redemption=True,
            unit_price=Decimal('0.00'), quantity=1, total_price=Decimal('0.00'),
        )
        self.run_command('--apply')
        item = invoice.items.first()
        self.assertEqual(money(item.total_price), Decimal('0.00'))

    def test_drafts_are_left_alone(self):
        draft = Invoice.objects.create(
            client=self.client_obj, center=self.center, status='draft',
            subtotal=Decimal('100.00'), total_amount=Decimal('100.00'),
        )
        InvoiceItem.objects.create(
            invoice=draft, content_type=self.service_ct, object_id=self.service.id,
            description='Haircut', unit_price=Decimal('0.00'), quantity=1,
            total_price=Decimal('0.00'),
        )
        self.run_command('--apply')
        item = draft.items.first()
        self.assertEqual(money(item.unit_price), Decimal('0.00'))

    def test_running_twice_is_idempotent(self):
        invoice = self.make_corrupt_invoice()
        self.run_command('--apply')
        invoice.refresh_from_db()
        first = (money(invoice.subtotal), money(invoice.total_amount))
        commission_first = money(
            sum(D(l.price) for l in ServiceLog.all_objects.filter(invoice=invoice))
        )

        self.run_command('--apply')
        invoice.refresh_from_db()
        second = (money(invoice.subtotal), money(invoice.total_amount))
        commission_second = money(
            sum(D(l.price) for l in ServiceLog.all_objects.filter(invoice=invoice))
        )

        self.assertEqual(first, second)
        self.assertEqual(commission_first, commission_second)

    def test_date_range_scoping(self):
        invoice = self.make_corrupt_invoice()
        # Restrict to a window that excludes this invoice.
        self.run_command('--apply', '--from', '2020-01-01', '--to', '2020-12-31')
        item = invoice.items.get(content_type=self.service_ct)
        self.assertEqual(money(item.unit_price), Decimal('0.00'))

    def test_single_invoice_scoping(self):
        first = self.make_corrupt_invoice()
        second = self.make_corrupt_invoice()
        self.run_command('--apply', '--invoice', str(first.id))

        self.assertEqual(
            money(first.items.get(content_type=self.service_ct).unit_price),
            Decimal('1000.00'),
        )
        self.assertEqual(
            money(second.items.get(content_type=self.service_ct).unit_price),
            Decimal('0.00'),
        )

    def test_requires_an_explicit_mode(self):
        from django.core.management.base import CommandError
        with self.assertRaises(CommandError):
            call_command('repair_invoice_math', stdout=StringIO())
