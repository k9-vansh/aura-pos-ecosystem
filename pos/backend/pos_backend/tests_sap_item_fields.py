"""What an item carries between SAP and the POS: group (category), tax rate, brand, sub group, HSN."""
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from pos_backend import sap_hsn, sap_utils
from pos_backend.management.commands.pull_from_sap import item_attributes

GROUPS = {121: 'RETAIL', 122: 'TECHNICAL', 123: 'TOOL', 128: 'CONSUMABLE-R'}


class PulledItemTests(TestCase):
    def test_the_category_is_sap_s_item_group_not_retail_for_everything(self):
        got = {code: item_attributes({'ItemsGroupCode': code}, GROUPS)['category'] for code in GROUPS}
        self.assertEqual(got, {121: 'Retail', 122: 'Technical', 123: 'Tool', 128: 'Consumable-R'})

    def test_tax_brand_and_sub_group_come_from_the_item_fields(self):
        a = item_attributes({'ItemsGroupCode': 122, 'U_TAXRT': '18', 'U_BRAND': ' Loreal ', 'U_SUB_GR': 'LP LOREAL'}, GROUPS)
        self.assertEqual((a['gst'], a['brand'], a['sub_category']), (Decimal('18'), 'Loreal', 'LP LOREAL'))
        self.assertEqual(item_attributes({'U_TAXRT': '5'}, GROUPS)['gst'], Decimal('5'))

    def test_a_missing_or_nonsense_tax_rate_is_zero_not_a_crash(self):
        for bad in (None, '', 'abc', '-5', '250'):
            self.assertEqual(item_attributes({'U_TAXRT': bad}, GROUPS)['gst'], Decimal('0'), bad)

    def test_an_item_with_no_group_has_no_category(self):
        self.assertEqual(item_attributes({'ItemsGroupCode': 999}, GROUPS)['category'], '')


class SentItemTests(TestCase):
    def setUp(self):
        cache.clear()

    def product(self, **kw):
        return mock.Mock(category=kw.get('category'), gst_percent=kw.get('gst', Decimal('18')),
                         brand=kw.get('brand'), sub_category=kw.get('sub'), sac_code=kw.get('hsn'))

    def test_the_group_follows_the_category(self):
        rows = [{'Number': 121, 'GroupName': 'RETAIL'}, {'Number': 122, 'GroupName': 'TECHNICAL'}]
        with mock.patch.object(sap_utils, '_get_all', return_value=rows), \
                mock.patch.object(sap_utils, '_sap', return_value=121):
            self.assertEqual(sap_utils._item_group_code('Technical'), 122)
            self.assertEqual(sap_utils._item_group_code('retail'), 121)
            self.assertEqual(sap_utils._item_group_code('Something Else'), 121)     # falls back to Settings
            self.assertEqual(sap_utils._item_group_code(''), 121)

    def test_the_item_fields_carry_tax_rate_brand_and_sub_group(self):
        udfs = sap_utils._item_udfs(self.product(brand='Loreal', sub='Serum'))
        self.assertEqual(udfs, {'U_TAXRT': '18', 'U_BRAND': 'Loreal', 'U_SUB_GR': 'Serum'})
        self.assertEqual(sap_utils._item_udfs(self.product(gst=Decimal('5.00'))), {'U_TAXRT': '5'})

    def test_the_hsn_is_sent_as_sap_s_row_number(self):
        lists = {'hsn': {10: '33049990', 1: '33059090'}, 'sac': {}}
        with mock.patch.object(sap_hsn, 'lists', return_value=lists):
            self.assertEqual(sap_utils._item_hsn(self.product(hsn='33049990')), {'ChapterID': 10})
            self.assertEqual(sap_utils._item_hsn(self.product(hsn='3305.90.90')), {'ChapterID': 1})   # dots ignored
            self.assertEqual(sap_utils._item_hsn(self.product(hsn='12345678')), {})                   # not in SAP's list
            self.assertEqual(sap_utils._item_hsn(self.product(hsn='')), {})


class HsnListTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_codes_are_found_both_ways(self):
        lists = {'hsn': {8: '33059019'}, 'sac': {-14: '00999721'}}
        with mock.patch.object(sap_hsn, 'lists', return_value=lists):
            self.assertEqual(sap_hsn.hsn_code(8), '33059019')
            self.assertEqual(sap_hsn.hsn_code(-1), '')            # SAP's "none"
            self.assertEqual(sap_hsn.hsn_code(None), '')
            self.assertEqual(sap_hsn.sac_code(-14), '00999721')
            self.assertEqual(sap_hsn.hsn_entry('3305 9019'), 8)

    def test_an_unreadable_hana_gives_empty_lists_not_a_crash(self):
        with mock.patch.object(sap_hsn, '_read', side_effect=RuntimeError('host down')):
            self.assertEqual(sap_hsn.lists(), {'hsn': {}, 'sac': {}})
            self.assertEqual(sap_hsn.hsn_code(8), '')
            self.assertIsNone(sap_hsn.hsn_entry('33059019'))
