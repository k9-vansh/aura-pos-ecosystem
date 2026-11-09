"""A perk that cannot be issued stops the bill with a clear 400, not a 500 (found on the full-size bulk database, where a package master with no
services made every till that sold it fail with a server error)."""
from decimal import Decimal

from billing.models import Invoice
from billing.services import PerkNotIssuable
from billing.tests import BillingTestBase
from marketing.models import Package


class PerkNotIssuableTests(BillingTestBase):
    def sell(self, package):
        line = {'content_type': 'marketing.package', 'object_id': package.id, 'description': package.name, 'unit_price': '2000.00',
                'discount': '0.00', 'quantity': 1, 'staff_members': [self.staff.id]}
        payload = self.build_payload(items=[line])
        held = self.api.post('/billing/invoices/', {**payload, 'status': 'draft'}, format='json')
        self.assertEqual(held.status_code, 201, getattr(held, 'data', None))
        total = Decimal(str(held.data.get('paid_now', held.data['total_amount'])))
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            {**payload, 'status': 'paid', 'payments': [{'payment_method': 'Cash', 'amount': str(total)}]}, format='json')
        return held.data['id'], done

    def test_a_package_with_no_services_is_a_400_and_the_bill_stays_unpaid(self):
        empty = Package.objects.create(name='Empty pack', price=Decimal('2000'), validity_days=90, services_json=[], level='Organisation')
        invoice_id, done = self.sell(empty)
        self.assertEqual(done.status_code, 400, getattr(done, 'data', None))
        self.assertIn('contains no services', str(done.data))
        self.assertNotEqual(Invoice.objects.get(pk=invoice_id).status, 'paid')

    def test_it_is_still_a_value_error_for_code_that_catches_that(self):
        self.assertTrue(issubclass(PerkNotIssuable, ValueError))
