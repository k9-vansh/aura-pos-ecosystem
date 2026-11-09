"""Walk-in bills name the shared B2C customer's address for the branch's state."""
from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from pos_backend import sap_utils

PLACES = [{'BPLID': 5, 'BPLName': 'Uttara khand', 'State': 'UK'}, {'BPLID': 6, 'BPLName': 'Haryana', 'State': 'HR'},
          {'BPLID': 2, 'BPLName': 'Main', 'State': None}]


def partner(*names):
    rows = []
    for name in names:
        rows += [{'AddressName': name, 'AddressType': 'bo_ShipTo'}, {'AddressName': name, 'AddressType': 'bo_BillTo'}]
    return mock.Mock(json=lambda: {'CardCode': 'B2C', 'BPAddresses': rows})


class WalkInAddressTests(TestCase):
    def setUp(self):
        cache.clear()

    def call(self, card, branch, addresses=('UK', 'DL')):
        with mock.patch.object(sap_utils, '_get_all', return_value=PLACES), \
                mock.patch.object(sap_utils, '_send_sap_request', return_value=partner(*addresses)), \
                mock.patch.object(sap_utils, '_sap', return_value='B2C'):
            return sap_utils._walk_in_address(card, branch)

    def test_a_walk_in_names_the_address_of_the_branchs_state(self):
        self.assertEqual(self.call('B2C', 'Uttara khand'), {'ShipToCode': 'UK', 'PayToCode': 'UK'})

    def test_a_client_with_a_partner_of_their_own_is_left_to_sap(self):
        self.assertEqual(self.call('C000012', 'Uttara khand'), {})

    def test_a_branch_whose_state_has_no_address_waits_with_what_to_add(self):
        with self.assertRaises(ValueError) as caught:
            self.call('B2C', 'Haryana')
        text = str(caught.exception)
        self.assertIn("named 'HR'", text)
        self.assertIn('Haryana', text)

    def test_a_business_place_with_no_state_is_reported(self):
        with self.assertRaises(ValueError) as caught:
            self.call('B2C', 'Main')
        self.assertIn('has no State', str(caught.exception))

    def test_an_unset_branch_adds_nothing(self):
        self.assertEqual(self.call('B2C', ''), {})
        self.assertEqual(self.call('B2C', 'No Branch'), {})
