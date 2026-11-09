"""A recipe line can be written in ml, g or pcs; stock always moves in the product's own unit."""
from decimal import Decimal
from types import SimpleNamespace

from django.test import SimpleTestCase

from inventory.models import Product
from services import recipes, units
from pos_backend.testing import bill_and_pay
from services.tests_recipes import RecipeBase, D, SERVICES


def prod(unit='pcs', size=None, pack=''):
    return SimpleNamespace(name='Shampoo', unit=unit, pack_size=size, pack_unit=pack)


class ConversionTests(SimpleTestCase):
    def test_names_from_sap_and_the_till_are_one_unit(self):
        self.assertEqual([units.norm(u) for u in ('PCS', 'Ltr', 'GM', 'Litre', ' ML ', 'PACK')],
                         ['pcs', 'l', 'g', 'l', 'ml', 'pack'])

    def test_inside_one_family(self):
        self.assertEqual(units.to_stock_unit(500, 'ml', prod('l')), D('0.5'))
        self.assertEqual(units.to_stock_unit(2, 'kg', prod('g')), D('2000'))
        self.assertEqual(units.to_stock_unit(7, 'ml', prod('ml')), D('7'))

    def test_through_the_pack(self):
        bottle = prod('pcs', D('100'), 'ml')
        self.assertEqual(units.to_stock_unit(10, 'ml', bottle), D('0.1'))
        self.assertEqual(units.to_stock_unit(1, 'l', bottle), D('10'))
        self.assertEqual(units.to_stock_unit(3, 'pcs', bottle), D('3'))

    def test_a_unit_that_cannot_be_reached_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            units.to_stock_unit(10, 'ml', prod('pcs'))
        self.assertIn('holds', str(caught.exception))
        with self.assertRaises(ValueError):
            units.to_stock_unit(10, 'g', prod('pcs', D('100'), 'ml'))        # grams of a bottle of ml

    def test_the_choices_offered(self):
        self.assertEqual(units.options(prod('pcs')), ['pcs', 'dozen'])
        self.assertEqual(units.options(prod('pcs', D('100'), 'ml')), ['pcs', 'dozen', 'ml', 'cl', 'l'])   # its own family first, then the pack's
        self.assertEqual(units.options(prod('g')), ['g', 'mg', 'kg'])
        self.assertEqual(units.options(prod('pack')), ['pack'])


class RecipeUnitTests(RecipeBase):
    def setUp(self):
        super().setUp()
        Product.objects.filter(product_code='SHAM-1').update(unit='pcs', pack_size=D('250'), pack_unit='ml',
                                                             current_stock=D('20'))

    def line(self, **over):
        row = {'product_code': 'SHAM-1', 'quantity': '50', 'unit': 'ml'}
        row.update(over)
        return row

    def test_a_line_in_ml_is_saved_and_shown_back_in_ml(self):
        data = self.set_recipe(fixed=[self.line()], groups=[])
        self.assertEqual(data['fixed'][0]['unit'], 'ml')
        self.assertEqual(data['fixed'][0]['quantity'], '50')
        self.assertIn('pcs', data['fixed'][0]['unit_options'])
        self.assertIn('ml', data['fixed'][0]['unit_options'])

    def test_the_bill_takes_the_stock_in_the_products_own_unit(self):
        self.set_recipe(fixed=[self.line()], groups=[])
        self.service.refresh_from_db()
        entries = recipes.resolve(self.service, self.center, 2)
        self.assertEqual(entries[0]['unit'], 'pcs')
        self.assertEqual(entries[0]['quantity'], '0.4')            # 2 services x 50 ml of a 250 ml bottle
        self.assertEqual(entries[0]['used'], '100 ml')
        self.assertEqual(recipes.deductions(entries), {'SHAM-1': D('0.4')})

    def test_the_pick_one_options_can_have_their_own_units(self):
        self.set_recipe(fixed=[], groups=[{'name': 'Shampoo', 'required': True, 'options': [
            self.line(is_default=True), {'product_code': 'SHAM-2', 'quantity': '15', 'unit': 'ml', 'is_default': False}]}])
        self.service.refresh_from_db()
        entries = recipes.resolve(self.service, self.center, 1)
        self.assertEqual(entries[0]['quantity'], '0.2')
        shams = {o['product_code']: o for o in recipes.till_recipes(self.center)['recipes'][self.service.pk]['groups'][0]['options']}
        self.assertEqual(shams['SHAM-1']['unit'], 'ml')
        self.assertEqual(shams['SHAM-2']['unit'], 'ml')

    def test_ml_on_a_product_with_no_pack_size_is_refused(self):
        res = self.api.put(f'{SERVICES}{self.service.id}/recipe/',
                           {'fixed': [self.line(product_code='TOWEL')], 'groups': []}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('holds', str(res.data))

    def test_the_recipe_can_say_what_one_unit_holds_and_it_reaches_every_branch(self):
        row = {'product_code': 'TOWEL', 'quantity': '5', 'unit': 'g', 'pack_size': '250', 'pack_unit': 'g'}
        data = self.set_recipe(fixed=[row], groups=[])
        self.assertEqual(data['fixed'][0]['unit'], 'g')
        self.assertTrue(Product.objects.filter(product_code='TOWEL').exclude(pack_size=D('250')).count() == 0)
        self.service.refresh_from_db()
        self.assertEqual(recipes.resolve(self.service, self.center, 1)[0]['quantity'], '0.02')

    def test_a_nonsense_pack_is_refused(self):
        for bad in ({'pack_size': '0', 'pack_unit': 'g'}, {'pack_size': 'x', 'pack_unit': 'g'},
                    {'pack_size': '10', 'pack_unit': 'pcs'}):
            res = self.api.put(f'{SERVICES}{self.service.id}/recipe/',
                               {'fixed': [{'product_code': 'TOWEL', 'quantity': '5', 'unit': 'g', **bad}], 'groups': []},
                               format='json')
            self.assertEqual(res.status_code, 400, bad)

    def test_the_products_own_unit_is_stored_as_empty(self):
        data = self.set_recipe(fixed=[self.line(unit='pcs', quantity='1')], groups=[])
        self.assertEqual(data['fixed'][0]['unit'], 'pcs')
        self.assertEqual(self.service.bom_items.get().unit, '')

    def test_billing_with_a_unit_recipe_moves_the_right_stock(self):
        self.set_recipe(fixed=[self.line()], groups=[])
        before = Product.objects.get(product_code='SHAM-1', center=self.center).current_stock
        res = bill_and_pay(self.api, self.bill())
        self.assertIn(res.status_code, (200, 201), res.data)
        after = Product.objects.get(product_code='SHAM-1', center=self.center).current_stock
        self.assertEqual(before - after, D('0.2'))


class ServiceToSapTests(RecipeBase):
    """A service made in the POS must exist in SAP, or a bill for it cannot be posted."""

    def make(self, **over):
        body = {'name': 'Head Massage', 'category': 'Hair', 'default_price': '600', 'tax_percentage': '18',
                'duration_mins': 30, 'level': 'Organisation'}
        body.update(over)
        return self.api.post('/services/api/master/', body, format='json')

    def test_a_new_service_is_queued_for_sap(self):
        from pos_backend.models import BackgroundJob
        res = self.make()
        self.assertEqual(res.status_code, 201, res.data)
        row = BackgroundJob.objects.filter(payload__object_type='service').first()
        self.assertIsNotNone(row, 'no SAP job was queued for the new service')
        self.assertEqual(row.payload['object_id'], res.data['id'])

    def test_the_sap_item_is_a_non_stock_sales_item_with_its_sac_and_tax(self):
        from unittest import mock
        from pos_backend import sap_utils
        from services.models import ServiceMaster
        service = ServiceMaster.objects.create(name='Head Massage', category='Hair', tax_percentage=D('18'),
                                               default_price=D('600'), hsn_code='999721', brand='Loreal')
        sent = []

        def fake(method, url, **kw):
            sent.append((method, url, kw.get('json')))
            return mock.Mock(status_code=201)

        with mock.patch.object(sap_utils, '_send_sap_request', side_effect=fake), \
                mock.patch.object(sap_utils, 'check_sap_item_exists', return_value=False), \
                mock.patch.object(sap_utils, '_item_group_code', return_value=125), \
                mock.patch('pos_backend.sap_hsn.sac_entry', return_value=-15):
            self.assertTrue(sap_utils.create_sap_service(service))
        payload = sent[0][2]
        self.assertEqual((payload['InventoryItem'], payload['SalesItem'], payload['PurchaseItem']), ('tNO', 'tYES', 'tNO'))
        self.assertEqual((payload['ItemsGroupCode'], payload['SACEntry'], payload['U_TAXRT'], payload['U_BRAND']),
                         (125, -15, '18', 'Loreal'))
        service.refresh_from_db()
        self.assertTrue(service.sap_sync_status)
        self.assertEqual(service.service_code, f'S{service.id:06d}')

    def test_a_service_already_in_sap_is_updated_not_duplicated(self):
        from unittest import mock
        from pos_backend import sap_utils
        from services.models import ServiceMaster
        service = ServiceMaster.objects.create(name='Facial', category='Skin', service_code='GS1', default_price=D('1'))
        calls = []
        with mock.patch.object(sap_utils, '_send_sap_request', side_effect=lambda m, u, **k: calls.append(m) or mock.Mock(status_code=204)), \
                mock.patch.object(sap_utils, 'check_sap_item_exists', return_value=True), \
                mock.patch.object(sap_utils, '_item_group_code', return_value=126), \
                mock.patch('pos_backend.sap_hsn.sac_entry', return_value=None):
            self.assertTrue(sap_utils.create_sap_service(service))
        self.assertEqual(calls, ['PATCH'])


class ProductPackTests(RecipeBase):
    def test_what_one_unit_holds_needs_a_measure(self):
        res = self.api.patch(f'/inventory/api/products/{self.towel.id}/', {'pack_size': '100', 'pack_unit': 'box'}, format='json')
        self.assertEqual(res.status_code, 400)
        res = self.api.patch(f'/inventory/api/products/{self.towel.id}/', {'pack_size': '100', 'pack_unit': 'ML'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.towel.refresh_from_db()
        self.assertEqual((self.towel.pack_size, self.towel.pack_unit), (D('100'), 'ml'))

    def test_no_size_clears_the_unit(self):
        res = self.api.patch(f'/inventory/api/products/{self.towel.id}/', {'pack_size': None, 'pack_unit': ''}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        res = self.api.patch(f'/inventory/api/products/{self.towel.id}/', {'pack_unit': 'ml'}, format='json')
        self.assertEqual(res.status_code, 400)
