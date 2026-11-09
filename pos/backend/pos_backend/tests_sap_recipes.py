"""What SAP is told for a service whose recipe the POS owns.

The A/R Invoice carries the service, and under it - at no price - the items that
bill used (the fixed items, the colour and shampoo chosen, any extras). SAP takes
its stock off that one document, so nothing is posted to SAP separately: no Goods
Issue. SAP's stock (a simulator that keeps stock from the documents it is sent)
ends up equal to the POS's. Nothing touches the network.
"""
from decimal import Decimal
from unittest import mock

from django.test import override_settings

from billing.models import Invoice
from inventory.models import Product, StockTransaction
from pos_backend import tests_numbers_everywhere as base
from pos_backend.tests_sap_billing_reconcile import drain
from pos_backend.tests_sap_inventory_reconcile import StockSim
from services.models import ServiceBOM, ServiceMaster, ServiceRecipeGroup

D = Decimal


@override_settings(SAP_ENABLED=True)
class SapRecipeTests(base.Everything):
    def setUp(self):
        super().setUp()
        self.center.sap_warehouse_code = 'WH-A'
        self.center.save()

        def product(code, category, stock, unit='ml'):
            return Product.objects.create(name=code, product_code=code, category=category, price=D('100'), mrp=D('100'),
                                          gst_percent=D('18'), current_stock=stock, unit=unit, center=self.center)

        self.towel = product('TOWEL', 'Consumable', 10, 'pcs')
        self.col_a = product('COL-A', 'Hair Colour', 100)
        self.col_b = product('COL-B', 'Hair Colour', 100)
        self.sham = product('SHAM-1', 'Shampoo', 50)
        self.products = [self.towel, self.col_a, self.col_b, self.sham]
        self.sim = StockSim({(p.product_code, 'WH-A'): D(p.current_stock) for p in self.products})
        for target, repl in (('pos_backend.sap_utils._send_sap_request', self.sim),
                             ('pos_backend.sap_utils.get_hana_batches', mock.Mock(return_value=[])),
                             ('pos_backend.sap_utils._resolve_bpl_id', mock.Mock(return_value=1))):
            patcher = mock.patch(target, repl)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.service = ServiceMaster.objects.create(name='Hair Colour', default_price=D('2000'),
                                                    tax_percentage=D('18'), recipe_source='pos', service_code='S000777')
        ServiceBOM.objects.create(service=self.service, product_code='TOWEL', quantity=D('1'))
        self.colour = ServiceRecipeGroup.objects.create(service=self.service, name='Colour')
        ServiceBOM.objects.create(service=self.service, group=self.colour, product_code='COL-A', quantity=D('30'))
        ServiceBOM.objects.create(service=self.service, group=self.colour, product_code='COL-B', quantity=D('40'))
        self.svc['hair'] = self.service

    def hair_line(self, colour, qty=1, **extra):
        line = self.line('hair', qty)
        line['recipe_choices'] = {'choices': {str(self.colour.pk): {'product_code': colour}}, 'extras': []}
        line['recipe_choices'].update(extra)
        return line

    def bill_and_post(self, items):
        callbacks = self.captureOnCommitCallbacks(execute=True)
        callbacks.__enter__()
        invoice = self.raise_bill(items)
        callbacks.__exit__(None, None, None)
        drain()
        return invoice

    def invoice_lines(self, number=0):
        return self.sim.posts('/Invoices')[number][2]['DocumentLines']

    def sap_stock(self, code):
        return self.sim.net().get((code, 'WH-A'))

    def test_the_invoice_lists_the_chosen_items_under_the_service(self):
        self.bill_and_post([self.hair_line('COL-A')])
        lines = self.invoice_lines()
        self.assertEqual(lines[0]['ItemCode'], 'S000777')
        self.assertEqual(lines[0]['TreeType'], 'iNotATree')          # its own row, not a bill of materials
        children = [(ln['ItemCode'], D(str(ln['Quantity'])), ln['UnitPrice'], ln['TreeType'], 'ParentLineNum' in ln,
                     '_Ingredient' in ln) for ln in lines[1:]]
        self.assertEqual(sorted(children), [('COL-A', D('30'), '0', 'iNotATree', False, False),
                                            ('TOWEL', D('1'), '0', 'iNotATree', False, False)])

    def test_only_the_chosen_option_is_on_the_invoice(self):
        self.bill_and_post([self.hair_line('COL-B')])
        codes = [ln['ItemCode'] for ln in self.invoice_lines()]
        self.assertIn('COL-B', codes)
        self.assertNotIn('COL-A', codes)

    def test_the_quantity_really_used_and_the_extras_are_on_the_invoice(self):
        line = self.hair_line('COL-A', extras=[{'product_code': 'TOWEL', 'quantity': '2'}])
        line['recipe_choices']['choices'][str(self.colour.pk)]['quantity'] = '45'
        self.bill_and_post([line])
        children = {ln['ItemCode']: D(str(ln['Quantity'])) for ln in self.invoice_lines()[1:]}
        self.assertEqual(children, {'COL-A': D('45'), 'TOWEL': D('3')})        # 1 fixed + 2 extra on one line

    def test_two_of_the_service_list_twice_the_quantity(self):
        self.bill_and_post([self.hair_line('COL-A', qty=2)])
        children = {ln['ItemCode']: D(str(ln['Quantity'])) for ln in self.invoice_lines()[1:]}
        self.assertEqual(children, {'COL-A': D('60'), 'TOWEL': D('2')})

    def test_nothing_is_posted_as_a_goods_issue(self):
        invoice = self.bill_and_post([self.hair_line('COL-A')])
        self.assertEqual(self.sim.posts('/InventoryGenExits'), [])
        rows = StockTransaction.objects.filter(invoice=invoice, transaction_type='SERVICE_USE')
        self.assertEqual(rows.count(), 2)
        self.assertTrue(all(r.sap_sync_status for r in rows))
        self.assertTrue(all('AR Invoice' in (r.sap_sync_message or '') for r in rows))

    def test_a_retry_of_a_ledger_row_never_posts_a_goods_issue(self):
        invoice = self.bill_and_post([self.hair_line('COL-A')])
        from pos_backend.sap_utils import create_sap_stock_transaction
        for row in StockTransaction.objects.filter(invoice=invoice, transaction_type='SERVICE_USE'):
            self.assertTrue(create_sap_stock_transaction(row))
        self.assertEqual(self.sim.posts('/InventoryGenExits'), [])

    def test_sap_stock_ends_up_equal_to_the_pos(self):
        self.bill_and_post([self.hair_line('COL-A', qty=3), self.hair_line('COL-B')])
        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(Decimal(str(self.sap_stock(product.product_code))), D(product.current_stock),
                             product.product_code)

    def test_cancelling_the_bill_keeps_sap_and_the_pos_in_step(self):
        callbacks = self.captureOnCommitCallbacks(execute=True)
        callbacks.__enter__()
        invoice = self.raise_bill([self.hair_line('COL-A')])
        cancel = self.api.post(f'/billing/invoices/{invoice.pk}/cancel/', {'reason': 'test'}, format='json')
        self.assertEqual(cancel.status_code, 200, cancel.data)
        callbacks.__exit__(None, None, None)
        drain()
        # The cancel waits for the bill's own postings to reach SAP (and the credit memo waits for
        # the cancel): each says "retry in 2 minutes". Let those retries come round.
        from django.utils import timezone
        from pos_backend.models import BackgroundJob
        for _ in range(8):
            waiting = BackgroundJob.objects.filter(status__in=['queued', 'failed'], run_after__gt=timezone.now())
            if not waiting.exists():
                break
            waiting.update(run_after=timezone.now())
            drain()
        credit_lines = self.sim.posts('/CreditNotes')[0][2]['DocumentLines']
        self.assertEqual([ln['ItemCode'] for ln in credit_lines], ['S000777'])              # the service, not the items
        for product in self.products:
            product.refresh_from_db()
            self.assertEqual(Decimal(str(self.sap_stock(product.product_code))), D(product.current_stock),
                             product.product_code)

    def test_a_technical_item_is_on_the_invoice_like_any_used_item(self):
        Product.objects.create(name='TOOL', product_code='TOOL', category='Technical', price=D('1'), mrp=D('1'),
                               current_stock=10, center=self.center)
        ServiceBOM.objects.create(service=self.service, product_code='TOOL', quantity=D('1'))
        self.bill_and_post([self.hair_line('COL-A')])
        tool = [ln for ln in self.invoice_lines() if ln['ItemCode'] == 'TOOL']
        self.assertEqual([(ln.get('TreeType'), str(ln['Quantity'])) for ln in tool], [('iNotATree', '1')])

    def test_a_service_with_nothing_to_deduct_is_a_plain_line(self):
        plain = ServiceMaster.objects.create(name='Threading', default_price=D('100'), tax_percentage=D('18'),
                                             recipe_source='pos', service_code='S000888')
        self.svc['thread2'] = plain
        self.bill_and_post([self.line('thread2')])
        lines = self.invoice_lines()
        self.assertEqual([(ln['ItemCode'], ln.get('TreeType')) for ln in lines], [('S000888', 'iNotATree')])

    def test_a_service_with_the_sap_recipe_also_goes_as_separate_rows(self):
        wash = ServiceMaster.objects.create(name='Wash', default_price=D('300'), tax_percentage=D('18'))
        ServiceBOM.objects.create(service=wash, product_code='SHAM-1', quantity=D('5'))
        self.svc['wash'] = wash
        self.bill_and_post([self.line('wash')])
        lines = self.invoice_lines()
        self.assertEqual([(ln['ItemCode'], ln.get('TreeType')) for ln in lines][1:], [('SHAM-1', 'iNotATree')])

    def test_the_sap_bom_pull_leaves_a_pos_recipe_alone(self):
        from pos_backend.management.commands.pull_from_sap import Command
        sap_tree = {'TreeType': 'iSalesTree', 'TreeCode': 'S000777',
                    'ProductTreeLines': [{'ItemCode': 'COL-A', 'Quantity': 1}]}
        command = Command()
        command.stdout = mock.Mock()
        with mock.patch.object(Command, '_paginate', return_value=[sap_tree]):
            command._sync_boms()
        self.assertEqual(ServiceBOM.objects.filter(service=self.service).count(), 3)       # unchanged
        self.service.refresh_from_db()
        self.assertEqual(self.service.recipe_source, 'pos')
