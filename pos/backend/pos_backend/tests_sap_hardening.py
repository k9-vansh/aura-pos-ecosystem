"""SAP review fixes: nothing posts to the wrong branch, and a half-read pull is not "completed"."""
from unittest import mock

from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase

from pos_backend import sap_utils


class FakeList:
    def __init__(self, rows, status=200):
        self.status_code = status
        self._rows = rows
        self.text = ''

    def json(self):
        return {'value': self._rows}


class BranchResolutionTests(SimpleTestCase):
    def setUp(self):
        cache.delete('sap_bpl_map')
        self.addCleanup(cache.delete, 'sap_bpl_map')

    def test_unset_branch_uses_the_default(self):
        self.assertEqual(sap_utils._resolve_bpl_id(None), 1)
        self.assertEqual(sap_utils._resolve_bpl_id('No Branch', default=7), 7)

    def test_a_known_branch_resolves(self):
        with mock.patch.object(sap_utils, '_get_all', return_value=[{'BPLName': 'Pune', 'BPLID': 4}]):
            self.assertEqual(sap_utils._resolve_bpl_id('Pune'), 4)

    def test_an_unknown_branch_is_an_error_not_branch_one(self):
        with mock.patch.object(sap_utils, '_get_all', return_value=[{'BPLName': 'Pune', 'BPLID': 4}]):
            with self.assertRaises(ValueError):
                sap_utils._resolve_bpl_id('Mumbai')
            self.assertIsNone(sap_utils._resolve_bpl_id('Mumbai', default=None))

    def test_sap_unreachable_is_an_error_not_branch_one(self):
        with mock.patch.object(sap_utils, '_get_all', side_effect=RuntimeError('down')):
            with self.assertRaises(ValueError):
                sap_utils._resolve_bpl_id('Pune')
            self.assertIsNone(sap_utils._resolve_bpl_id('Pune', default=None))


class PullFromSapTests(TestCase):
    def test_a_page_that_cannot_be_read_stops_the_pull(self):
        with mock.patch('pos_backend.management.commands.pull_from_sap._send_sap_request',
                        side_effect=RuntimeError('timeout')):
            with self.assertRaises(CommandError):
                call_command('pull_from_sap')

    def test_a_non_200_page_stops_the_pull(self):
        with mock.patch('pos_backend.management.commands.pull_from_sap._send_sap_request',
                        return_value=FakeList([], status=500)):
            with self.assertRaises(CommandError):
                call_command('pull_from_sap')

    def test_pages_are_requested_in_a_fixed_order_and_followed_to_the_end(self):
        pages = [FakeList([{'CardCode': 'C1'}, {'CardCode': 'C2'}]), FakeList([{'CardCode': 'C3'}]), FakeList([])]
        from pos_backend.management.commands.pull_from_sap import Command
        with mock.patch('pos_backend.management.commands.pull_from_sap._send_sap_request',
                        side_effect=pages) as send:
            rows = list(Command()._paginate('BusinessPartners?$select=CardCode', 'CardCode'))
        self.assertEqual([r['CardCode'] for r in rows], ['C1', 'C2', 'C3'])
        urls = [c.args[1] for c in send.call_args_list]
        self.assertTrue(all('$orderby=CardCode' in u for u in urls))
        self.assertIn('$skip=2', urls[1])
        self.assertIn('$skip=3', urls[2])
