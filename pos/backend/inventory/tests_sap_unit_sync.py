"""A unit changed on an item in SAP reaches the POS products."""
from decimal import Decimal
from unittest import mock

from django.test import TestCase

from inventory.models import Product
from inventory.sap_unit_sync import sync_units_from_sap
from salon_admin.models import Center


def rows(*items):
    return mock.patch('pos_backend.management.commands.pull_from_sap.Command._paginate',
                      return_value=iter([dict(zip(('ItemCode', 'InventoryUOM', 'InventoryItem'), i)) for i in items]))


class UnitSyncTests(TestCase):
    def setUp(self):
        self.a = Center.objects.create(center_name='A', display_name='A', gst_number='07AAAAA0000A1Z5')
        self.b = Center.objects.create(center_name='B', display_name='B', gst_number='09CCCCC0000C1Z5')
        self.foam = [Product.objects.create(name='FOAM', product_code='F1', unit='pcs', center=c) for c in (self.a, self.b)]
        self.mask = Product.objects.create(name='MASK', product_code='M1', unit='pcs', center=self.a,
                                           pack_size=Decimal('250'), pack_unit='ml')

    def test_a_unit_changed_in_sap_changes_every_branchs_copy(self):
        with rows(('F1', 'ML', 'tYES'), ('M1', 'PCS', 'tYES')):
            self.assertEqual(sync_units_from_sap(), 2)
        self.assertEqual({p.unit for p in Product.objects.filter(product_code='F1')}, {'ml'})

    def test_the_pack_note_stays_while_the_unit_is_unchanged_and_goes_when_it_changes(self):
        with rows(('M1', 'PCS', 'tYES')):
            sync_units_from_sap()
        self.mask.refresh_from_db()
        self.assertEqual((self.mask.unit, self.mask.pack_size), ('pcs', Decimal('250.000')))
        with rows(('M1', 'ML', 'tYES')):
            sync_units_from_sap()
        self.mask.refresh_from_db()
        self.assertEqual((self.mask.unit, self.mask.pack_size, self.mask.pack_unit), ('ml', None, ''))

    def test_services_marketing_items_and_unknown_codes_are_left_alone(self):
        with rows(('GS1', 'ML', 'tNO'), ('VC000001', 'ML', 'tYES'), ('NOPE', 'ML', 'tYES')):
            self.assertEqual(sync_units_from_sap(), 0)
