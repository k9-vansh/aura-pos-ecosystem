"""A service's recipe made in the POS: fixed items and pick-one groups.

The person billing chooses (say) the colour and shampoo; stock, the ledger and
the bill line follow the choice. Every way it can go wrong is refused with a
clear message and leaves stock and bills exactly as they were.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem
from clients.models import Client
from inventory.models import Product, StockTransaction
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceBOM, ServiceMaster, ServiceRecipeGroup
from staff.models import StaffMember

D = Decimal
SERVICES = '/services/api/master/'


class RecipeBase(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket',
                                            gst_number='07AAAAA0000A1Z5', sap_warehouse_code='WH-SAKET')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida', gst_number='09CCCCC0000C1Z5')
        role = Role.objects.create(name='Owner', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='o@test.com', password='pw', full_name='O',
                                                   role=role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.client_obj = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center)
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist')

        def product(code, category, stock, unit='pcs', center=None):
            return Product.objects.create(name=code, product_code=code, category=category, price=D('100'),
                                          mrp=D('100'), gst_percent=D('18'), current_stock=stock, unit=unit,
                                          center=center or self.center)

        self.towel = product('TOWEL', 'Consumable', 10)
        self.col_a = product('COL-A', 'Hair Colour', 100, 'ml')
        self.col_b = product('COL-B', 'Hair Colour', 100, 'ml')
        self.col_c = product('COL-C', 'Hair Colour', 0, 'ml')
        self.sham1 = product('SHAM-1', 'Shampoo', 50, 'ml')
        self.sham2 = product('SHAM-2', 'Shampoo', 50, 'ml')
        self.tool = product('TOOL', 'Technical', 0)
        self.retail = product('RETAIL', 'Retail', 20)
        product('COL-A', 'Hair Colour', 100, 'ml', center=self.other)
        self.service = ServiceMaster.objects.create(name='Hair Colour', default_price=D('2000'),
                                                    tax_percentage=D('18'))

    # the recipe the salon would set: a towel always, one colour, one shampoo
    def recipe(self, **over):
        data = {
            'fixed': [{'product_code': 'TOWEL', 'quantity': '1'}],
            'groups': [
                {'name': 'Colour', 'required': True, 'options': [
                    {'product_code': 'COL-A', 'quantity': '30'},
                    {'product_code': 'COL-B', 'quantity': '40'},
                    {'product_code': 'COL-C', 'quantity': '30'}]},
                {'name': 'Shampoo', 'required': True, 'options': [
                    {'product_code': 'SHAM-1', 'quantity': '10', 'is_default': True},
                    {'product_code': 'SHAM-2', 'quantity': '12'}]},
            ],
        }
        data.update(over)
        return data

    def set_recipe(self, **over):
        res = self.api.put(f'{SERVICES}{self.service.id}/recipe/', self.recipe(**over), format='json')
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def group_id(self, name):
        return str(ServiceRecipeGroup.objects.get(service=self.service, name=name).pk)

    def bill(self, choices=None, extras=None, qty=1, status=None, **line):
        item = {'content_type': 'services.servicemaster', 'object_id': self.service.id,
                'description': 'Hair Colour', 'unit_price': '2000', 'discount': '0.00', 'quantity': qty,
                'tax_percentage': '18', 'staff_members': [self.staff.id]}
        if choices is not None or extras is not None:
            item['recipe_choices'] = {'choices': choices or {}, 'extras': extras or []}
        item.update(line)
        payload = {'client': self.client_obj.id, 'center': self.center.id, 'status': status or 'paid',
                   'discount': '0.00', 'items': [item]}
        return payload

    def choose(self, colour, shampoo=None, **more):
        choices = {self.group_id('Colour'): {'product_code': colour}}
        if shampoo:
            choices[self.group_id('Shampoo')] = {'product_code': shampoo}
        choices.update(more)
        return choices

    def stock(self, product):
        product.refresh_from_db()
        return product.current_stock


class RecipeEditorTests(RecipeBase):
    def test_a_recipe_is_saved_and_read_back(self):
        saved = self.set_recipe()
        self.assertEqual(saved['source'], 'pos')
        self.assertEqual([f['product_code'] for f in saved['fixed']], ['TOWEL'])
        self.assertEqual([g['name'] for g in saved['groups']], ['Colour', 'Shampoo'])
        colour = saved['groups'][0]
        self.assertEqual([o['product_code'] for o in colour['options']], ['COL-A', 'COL-B', 'COL-C'])
        self.assertEqual(self.api.get(f'{SERVICES}{self.service.id}/recipe/').data, saved)
        self.service.refresh_from_db()
        self.assertEqual(self.service.recipe_source, 'pos')

    def test_saving_again_replaces_the_old_recipe(self):
        self.set_recipe()
        self.set_recipe(fixed=[], groups=[])
        self.assertEqual(ServiceBOM.objects.filter(service=self.service).count(), 0)
        self.assertEqual(ServiceRecipeGroup.objects.filter(service=self.service).count(), 0)

    def test_the_service_list_says_who_owns_the_recipe_and_how_long_it_is(self):
        before = next(s for s in self.api.get(SERVICES).data if s['id'] == self.service.id)
        self.assertEqual((before['recipe_source'], before['recipe_line_count']), ('sap', 0))
        self.set_recipe()
        after = next(s for s in self.api.get(SERVICES).data if s['id'] == self.service.id)
        self.assertEqual((after['recipe_source'], after['recipe_line_count']), ('pos', 6))

    def refused(self, **over):
        res = self.api.put(f'{SERVICES}{self.service.id}/recipe/', self.recipe(**over), format='json')
        self.assertEqual(res.status_code, 400, res.data)
        self.assertFalse(ServiceBOM.objects.filter(service=self.service).exists(), 'a refused recipe saved something')
        return str(res.data)

    def test_a_retail_product_is_never_in_a_recipe(self):
        self.assertIn('retail', self.refused(fixed=[{'product_code': 'RETAIL', 'quantity': '1'}]).lower())
        groups = [{'name': 'Pick', 'options': [{'product_code': 'RETAIL', 'quantity': '1'}]}]
        self.assertIn('retail', self.refused(groups=groups).lower())

    def test_bad_recipes_are_refused(self):
        self.refused(fixed=[{'product_code': 'TOWEL', 'quantity': '0'}])
        self.refused(fixed=[{'product_code': 'TOWEL', 'quantity': 'lots'}])
        self.refused(fixed=[{'product_code': 'TOWEL', 'quantity': '1'}, {'product_code': 'TOWEL', 'quantity': '2'}])
        self.refused(fixed=[{'product_code': '', 'quantity': '1'}])
        self.refused(groups=[{'name': '', 'options': [{'product_code': 'COL-A', 'quantity': '1'}]}])
        self.refused(groups=[{'name': 'X', 'options': []}])
        self.refused(groups=[{'name': 'X', 'options': [{'product_code': 'COL-A', 'quantity': '1'}]},
                             {'name': 'x', 'options': [{'product_code': 'COL-B', 'quantity': '1'}]}])
        self.refused(groups=[{'name': 'X', 'options': [{'product_code': 'COL-A', 'quantity': '1', 'is_default': True},
                                                         {'product_code': 'COL-B', 'quantity': '1', 'is_default': True}]}])
        self.refused(groups=[{'name': 'X', 'options': [{'product_code': 'COL-A', 'category': 'Hair Colour',
                                                          'quantity': '1'}]}])
        self.refused(groups=[{'name': 'X', 'options': [{'quantity': '1'}]}])

    def test_only_an_all_branches_role_changes_the_recipe(self):
        role = Role.objects.create(name='Manager', permissions={
            'admin': {'services': {'read': True, 'create': True, 'update': True, 'delete': True}}})
        manager = CustomUser.objects.create_user(email='m@test.com', password='pw', full_name='M', role=role,
                                                 center=self.center)
        manager.centers.add(self.center)
        api = APIClient()
        api.force_authenticate(manager)
        self.assertEqual(api.put(f'{SERVICES}{self.service.id}/recipe/', self.recipe(), format='json').status_code, 403)
        self.assertEqual(api.get(f'{SERVICES}{self.service.id}/recipe/').status_code, 200)

    def test_without_the_services_tick_the_recipe_cannot_be_read_or_changed(self):
        role = Role.objects.create(name='Clerk', permissions={'inventory': {'products': {'read': True}}})
        clerk = CustomUser.objects.create_user(email='c@test.com', password='pw', full_name='C', role=role,
                                               center=self.center)
        api = APIClient()
        api.force_authenticate(clerk)
        self.assertEqual(api.put(f'{SERVICES}{self.service.id}/recipe/', self.recipe(), format='json').status_code, 403)
        self.assertEqual(api.get(f'{SERVICES}{self.service.id}/recipe/').status_code, 403)

    def test_the_till_gets_the_options_with_stock(self):
        self.set_recipe()
        res = self.api.get(f'{SERVICES}till-recipes/?center_id={self.center.id}')
        self.assertEqual(res.status_code, 200, res.data)
        groups = res.data['recipes'][self.service.id]['groups']
        colour = next(g for g in groups if g['name'] == 'Colour')
        self.assertEqual([(o['product_code'], o['stock']) for o in colour['options']],
                         [('COL-A', '100'), ('COL-B', '100'), ('COL-C', '0')])
        shampoo = next(g for g in groups if g['name'] == 'Shampoo')
        self.assertEqual([o['product_code'] for o in shampoo['options'] if o['is_default']], ['SHAM-1'])
        extras = [e['product_code'] for e in res.data['extras']]
        self.assertIn('TOWEL', extras)                               # anything used up can be added as an extra
        self.assertNotIn('RETAIL', extras)                           # retail is sold, not used
        self.assertIn('TOOL', extras)                                # technical items are used up too

    def test_the_form_can_search_products_and_list_categories(self):
        res = self.api.get(f'{SERVICES}recipe-products/?q=col')
        self.assertEqual(res.status_code, 200, res.data)
        codes = [p['product_code'] for p in res.data['products']]
        self.assertEqual(sorted(codes), ['COL-A', 'COL-B', 'COL-C'])               # one per code, not one per branch
        self.assertNotIn('RETAIL', [p['product_code'] for p in self.api.get(f'{SERVICES}recipe-products/?q=RET').data['products']])
        categories = res.data['categories']
        self.assertIn('Hair Colour', categories)
        self.assertNotIn('Retail', categories)
        self.assertEqual(self.api.get(f'{SERVICES}recipe-products/?q=zzz').data['products'], [])

    def test_a_category_option_offers_every_product_of_that_category_and_new_ones_too(self):
        self.set_recipe(groups=[{'name': 'Colour', 'options': [{'category': 'hair colour', 'quantity': '30'}]}])
        till = lambda: self.api.get(f'{SERVICES}till-recipes/?center_id={self.center.id}').data['recipes'][self.service.id]
        offered = [o['product_code'] for o in till()['groups'][0]['options']]
        self.assertEqual(sorted(offered), ['COL-A', 'COL-B', 'COL-C'])
        Product.objects.create(name='COL-D', product_code='COL-D', category='Hair Colour', price=D('1'), mrp=D('1'),
                               current_stock=5, center=self.center)
        self.assertIn('COL-D', [o['product_code'] for o in till()['groups'][0]['options']])
        # another branch only sees what it stocks
        other = self.api.get(f'{SERVICES}till-recipes/?center_id={self.other.id}').data['recipes'][self.service.id]
        self.assertEqual([o['product_code'] for o in other['groups'][0]['options']], ['COL-A'])

    def test_a_category_never_offers_a_retail_product(self):
        Product.objects.filter(pk=self.retail.pk).update(category='Hair Colour')
        self.set_recipe(groups=[{'name': 'Colour', 'options': [{'category': 'Hair Colour', 'quantity': '30'}]}])
        self.retail.category = 'Retail'
        self.retail.save()
        offered = [o['product_code'] for o in
                   self.api.get(f'{SERVICES}till-recipes/?center_id={self.center.id}').data['recipes'][self.service.id]['groups'][0]['options']]
        self.assertNotIn('RETAIL', offered)


class RecipeBillingTests(RecipeBase):
    def setUp(self):
        super().setUp()
        self.set_recipe()

    def complete(self, **kwargs):
        return bill_and_pay(self.api, self.bill(**kwargs))

    def test_the_chosen_items_and_the_fixed_items_come_out_of_stock(self):
        res = self.complete(choices=self.choose('COL-A', 'SHAM-2'))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((self.stock(self.towel), self.stock(self.col_a), self.stock(self.sham2)),
                         (D('9'), D('70'), D('38')))
        self.assertEqual((self.stock(self.col_b), self.stock(self.sham1)), (D('100'), D('50')))   # not chosen: untouched
        rows = StockTransaction.objects.filter(transaction_type='SERVICE_USE')
        self.assertEqual(sorted((r.product.product_code, r.quantity_change) for r in rows),
                         [('COL-A', D('-30')), ('SHAM-2', D('-12')), ('TOWEL', D('-1'))])
        self.assertTrue(all(r.invoice_id == res.data['id'] for r in rows))

    def test_the_bill_line_keeps_what_was_chosen_and_what_was_used(self):
        res = self.complete(choices=self.choose('COL-B', 'SHAM-1'))
        item = InvoiceItem.objects.get(invoice_id=res.data['id'])
        self.assertEqual(item.recipe_choices['choices'][self.group_id('Colour')]['product_code'], 'COL-B')
        used = {(e['product_code'], e['quantity'], e['source'], e['group']) for e in item.consumables}
        self.assertEqual(used, {('TOWEL', '1', 'fixed', ''), ('COL-B', '40', 'choice', 'Colour'),
                                ('SHAM-1', '10', 'choice', 'Shampoo')})
        line = next(i for i in res.data['items'])
        self.assertEqual(len(line['consumables']), 3)

    def test_two_of_the_service_use_twice_as_much(self):
        self.complete(choices=self.choose('COL-A', 'SHAM-1'), qty=2)
        self.assertEqual((self.stock(self.towel), self.stock(self.col_a), self.stock(self.sham1)),
                         (D('8'), D('40'), D('30')))

    def test_a_required_choice_left_out_stops_the_bill(self):
        res = self.complete(choices={self.group_id('Shampoo'): {'product_code': 'SHAM-1'}})
        self.assertEqual(res.status_code, 400)
        self.assertIn('Choose the Colour', str(res.data))
        self.assertEqual(Invoice.objects.filter(status__in=['paid', 'partial']).count(), 0)
        self.assertEqual((self.stock(self.towel), self.stock(self.sham1)), (D('10'), D('50')))

    def test_a_default_is_used_when_nothing_is_chosen_for_that_group(self):
        res = self.complete(choices={self.group_id('Colour'): {'product_code': 'COL-A'}})     # shampoo left to its default
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.sham1), D('40'))

    def test_a_held_bill_and_a_quote_need_no_choices_yet(self):
        held = self.api.post('/billing/invoices/', self.bill(status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        payload = self.bill(status='paid')
        quote = self.api.post('/billing/invoices/quote/', payload, format='json')
        self.assertEqual(quote.status_code, 200, quote.data)               # totals show before the client chooses
        self.assertEqual((self.stock(self.towel), self.stock(self.col_a)), (D('10'), D('100')))   # nothing used yet

    def test_a_quote_still_refuses_an_option_that_is_not_on_the_recipe(self):
        quote = self.api.post('/billing/invoices/quote/', self.bill(choices=self.choose('SHAM-1')), format='json')
        self.assertEqual(quote.status_code, 400)

    def test_a_held_bill_is_completed_with_its_choices_later(self):
        held = self.api.post('/billing/invoices/', self.bill(choices=self.choose('COL-B', 'SHAM-2'), status='draft'),
                             format='json')
        self.assertEqual(held.status_code, 201, held.data)
        total = held.data['total_amount']
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            dict(self.bill(choices=self.choose('COL-B', 'SHAM-2'), status='paid'),
                                 payments=[{'payment_method': 'Cash', 'amount': total}]), format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual((self.stock(self.col_b), self.stock(self.sham2)), (D('60'), D('38')))

    def test_completing_a_held_bill_that_never_got_its_choices_is_refused(self):
        held = self.api.post('/billing/invoices/', self.bill(status='draft'), format='json')
        total = held.data['total_amount']
        done = self.api.put(f"/billing/invoices/{held.data['id']}/",
                            dict(self.bill(status='paid'), payments=[{'payment_method': 'Cash', 'amount': total}]),
                            format='json')
        self.assertEqual(done.status_code, 400, done.data)
        self.assertEqual(self.stock(self.towel), D('10'))

    def test_an_item_that_is_not_an_option_is_refused(self):
        self.assertEqual(self.complete(choices=self.choose('SHAM-1')).status_code, 400)        # a shampoo as the colour
        self.assertEqual(self.complete(choices=self.choose('NOPE')).status_code, 400)
        self.assertEqual(self.complete(choices=self.choose('COL-A', **{'9999': {'product_code': 'COL-A'}})).status_code,
                         400)                                                                  # a group of another service
        self.assertEqual(self.stock(self.col_a), D('100'))

    def test_an_option_with_no_stock_stops_the_bill_and_changes_nothing(self):
        res = self.complete(choices=self.choose('COL-C', 'SHAM-1'))
        self.assertEqual(res.status_code, 400)
        self.assertIn('COL-C', str(res.data))
        self.assertEqual((self.stock(self.towel), self.stock(self.sham1)), (D('10'), D('50')))
        self.assertEqual(StockTransaction.objects.filter(transaction_type='SERVICE_USE').count(), 0)

    def test_the_fixed_item_running_out_stops_the_bill(self):
        Product.objects.filter(pk=self.towel.pk).update(current_stock=0)
        res = self.complete(choices=self.choose('COL-A', 'SHAM-1'))
        self.assertEqual(res.status_code, 400)
        self.assertIn('TOWEL', str(res.data))
        self.assertEqual(self.stock(self.col_a), D('100'))

    def test_the_quantity_really_used_can_be_typed(self):
        choices = self.choose('COL-A', 'SHAM-1')
        choices[self.group_id('Colour')]['quantity'] = '45'
        res = self.complete(choices=choices)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.col_a), D('55'))
        self.assertEqual(self.stock(self.sham1), D('40'))                                       # the rest still follows the recipe
        for bad in ('0', '-3', 'lots'):
            c = self.choose('COL-A', 'SHAM-1')
            c[self.group_id('Colour')]['quantity'] = bad
            self.assertEqual(self.complete(choices=c).status_code, 400, bad)

    def test_an_extra_product_can_be_added(self):
        res = self.complete(choices=self.choose('COL-A', 'SHAM-1'), extras=[{'product_code': 'TOWEL', 'quantity': '2'}])
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.towel), D('7'))                                        # 1 fixed + 2 extra
        item = InvoiceItem.objects.get(invoice_id=res.data['id'])
        self.assertIn(('TOWEL', '2', 'extra'), {(e['product_code'], e['quantity'], e['source']) for e in item.consumables})

    def test_a_product_named_twice_is_one_stock_movement(self):
        res = self.complete(choices=self.choose('COL-A', 'SHAM-1'), extras=[{'product_code': 'TOWEL', 'quantity': '2'}])
        self.assertEqual(res.status_code, 201, res.data)
        rows = StockTransaction.objects.filter(transaction_type='SERVICE_USE', product=self.towel)
        self.assertEqual([r.quantity_change for r in rows], [D('-3')])                         # 1 fixed + 2 extra, once

    def test_an_extra_cannot_be_retail_or_unstocked(self):
        for extra in ({'product_code': 'RETAIL', 'quantity': '1'}, {'product_code': 'NOPE', 'quantity': '1'},
                      {'product_code': 'TOWEL', 'quantity': '0'}):
            res = self.complete(choices=self.choose('COL-A', 'SHAM-1'), extras=[extra])
            self.assertEqual(res.status_code, 400, extra)
        self.assertEqual(self.stock(self.retail), D('20'))

    def test_a_technical_item_is_deducted_like_any_other_used_item(self):
        Product.objects.filter(pk=self.tool.pk).update(current_stock=5)
        self.set_recipe(fixed=[{'product_code': 'TOOL', 'quantity': '1'}, {'product_code': 'TOWEL', 'quantity': '1'}])
        res = self.complete(choices=self.choose('COL-A', 'SHAM-1'))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.tool), D('4'))
        item = InvoiceItem.objects.get(invoice_id=res.data['id'])
        tool = next(e for e in item.consumables if e['product_code'] == 'TOOL')
        self.assertTrue(tool['deduct'])
        self.assertTrue(StockTransaction.objects.filter(product=self.tool).exists())

    def test_a_category_recipe_follows_the_choice_too(self):
        self.set_recipe(fixed=[], groups=[{'name': 'Colour', 'options': [{'category': 'Hair Colour', 'quantity': '25'}]}])
        res = self.complete(choices={self.group_id('Colour'): {'product_code': 'COL-B'}})
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.col_b), D('75'))

    def test_an_optional_group_can_be_skipped(self):
        self.set_recipe(fixed=[], groups=[{'name': 'Serum', 'required': False,
                                           'options': [{'product_code': 'SHAM-1', 'quantity': '5'}]}])
        res = self.complete()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.sham1), D('50'))

    def test_cancelling_the_bill_keeps_what_was_used_as_before(self):
        res = self.complete(choices=self.choose('COL-A', 'SHAM-1'))
        cancel = self.api.post(f"/billing/invoices/{res.data['id']}/cancel/", {'reason': 'test'}, format='json')
        self.assertEqual(cancel.status_code, 200, cancel.data)
        self.assertEqual(self.stock(self.col_a), D('70'))

    def test_a_service_without_a_pos_recipe_is_not_asked_for_anything(self):
        plain = ServiceMaster.objects.create(name='Threading', default_price=D('100'), tax_percentage=D('18'))
        payload = self.bill(object_id=plain.id, description='Threading')
        res = bill_and_pay(self.api, payload)
        self.assertEqual(res.status_code, 201, res.data)

    def test_the_old_sap_recipe_still_works(self):
        sap = ServiceMaster.objects.create(name='Wash', default_price=D('300'), tax_percentage=D('18'))
        ServiceBOM.objects.create(service=sap, product_code='SHAM-1', quantity=D('5'))
        res = bill_and_pay(self.api, self.bill(object_id=sap.id, description='Wash'))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.stock(self.sham1), D('45'))
        self.assertTrue(StockTransaction.objects.filter(transaction_type='BOM_CONSUMPTION').exists())

    def test_the_branch_is_the_one_that_pays_for_the_stock(self):
        # the recipe names COL-A; Noida has its own row, Saket's is the one used
        self.complete(choices=self.choose('COL-A', 'SHAM-1'))
        other = Product.objects.get(center=self.other, product_code='COL-A')
        self.assertEqual(other.current_stock, D('100'))
