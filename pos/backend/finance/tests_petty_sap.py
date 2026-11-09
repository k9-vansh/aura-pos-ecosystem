"""Petty cash: categories are SAP's "Petty-..." accounts and each entry posts as a journal."""

from decimal import Decimal
from unittest import mock

from django.test import override_settings

from finance.models import PettyCashCategory, PettyCashEntry
from finance.tests import FinanceTestBase
from pos_backend import sap_utils

CHART = [
    {'Code': '5411000000', 'Name': 'Other Indirect Expenses', 'AccountLevel': 4, 'FatherAccountKey': '5410000000', 'ActiveAccount': 'tNO'},
    {'Code': '5411000003', 'Name': 'Petty-Client Refreshment', 'AccountLevel': 5, 'FatherAccountKey': '5411000000', 'ActiveAccount': 'tYES'},
    {'Code': '5411000012', 'Name': 'Petty-Repairs & Maintenance', 'AccountLevel': 5, 'FatherAccountKey': '5411000000', 'ActiveAccount': 'tYES'},
    {'Code': '5411000099', 'Name': 'Petty-Switched Off', 'AccountLevel': 5, 'FatherAccountKey': '5411000000', 'ActiveAccount': 'tNO'},
    {'Code': '5120100000', 'Name': 'Petty-Purchase Local-Product', 'AccountLevel': 4, 'FatherAccountKey': '5120000000', 'ActiveAccount': 'tNO'},
    {'Code': '5120100001', 'Name': 'Petty-Purchase Local-Product', 'AccountLevel': 5, 'FatherAccountKey': '5120100000', 'ActiveAccount': 'tYES'},
    {'Code': '5120100004', 'Name': 'Cost Of Good Sold A/C (COGS)', 'AccountLevel': 5, 'FatherAccountKey': '5120100000', 'ActiveAccount': 'tYES'},
    {'Code': '5632800001', 'Name': 'Event Expenses (Petty Cash)', 'AccountLevel': 5, 'FatherAccountKey': '5632800000', 'ActiveAccount': 'tYES'},
    {'Code': '5632800000', 'Name': 'Petty-Event Expense', 'AccountLevel': 4, 'FatherAccountKey': '5630000000', 'ActiveAccount': 'tNO'},
    {'Code': '2330100003', 'Name': 'Petty Cash', 'AccountLevel': 5, 'FatherAccountKey': '2330100000', 'ActiveAccount': 'tYES'},
    {'Code': '5511100001', 'Name': 'General Office Expenses', 'AccountLevel': 5, 'FatherAccountKey': '5511100000', 'ActiveAccount': 'tYES'},
]


class _Reply:
    def __init__(self, data):
        self._data, self.status_code, self.content = data, 200, b'x'

    def json(self):
        return self._data


class PettyCategoryTests(FinanceTestBase):
    def test_categories_are_the_petty_accounts_and_nothing_else(self):
        with mock.patch.object(sap_utils, '_send_sap_request', return_value=_Reply({'value': CHART})):
            accounts = sap_utils.petty_cash_accounts()
        codes = [code for code, _ in accounts]
        self.assertEqual(sorted(codes), ['5120100001', '5411000003', '5411000012'])
        self.assertNotIn('2330100003', codes)   # the Petty Cash balance account
        self.assertNotIn('5120100004', codes)   # COGS shares the Petty-Purchase group
        self.assertNotIn('5632800001', codes)   # no Petty- prefix of its own
        self.assertNotIn('5411000099', codes)   # inactive

    def test_sync_names_them_as_sap_does_and_switches_off_hand_made_ones(self):
        hand_made = PettyCashCategory.objects.create(name='refreshments')
        with mock.patch.object(sap_utils, '_send_sap_request', return_value=_Reply({'value': CHART})):
            created, updated, off = sap_utils.sync_petty_cash_categories()
            again = sap_utils.sync_petty_cash_categories()
        self.assertEqual((created, off), (3, 1))
        self.assertEqual(again, (0, 0, 0))
        hand_made.refresh_from_db()
        self.assertFalse(hand_made.is_active)
        self.assertEqual(PettyCashCategory.objects.get(sap_account='5411000003').name, 'Client Refreshment')


@override_settings(SAP_ENABLED=True)
class PettyJournalTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        self.center.sap_branch_name = 'Main'
        self.center.save()
        self.cat = PettyCashCategory.objects.create(name='Client Refreshment', sap_account='5411000003')
        self.entry = PettyCashEntry.objects.create(center=self.center, description='Milk', amount=Decimal('120.50'),
                                                   voucher_number='V9', category=self.cat)

    cost_centre = 'CGPAO001'

    def post(self, entry):
        posted = {}

        def fake(key, payload, total):
            posted.update(key=key, payload=payload, total=total)
            return 77
        with mock.patch.object(sap_utils, '_post_je_once', fake), \
                mock.patch.object(sap_utils, '_resolve_bpl_id', return_value=4), \
                mock.patch.object(sap_utils, 'cost_centre_code', return_value=self.cost_centre), \
                mock.patch.object(sap_utils, '_sap', side_effect=lambda key: {
                    'sap.account.petty_cash': '1110200001', 'sap.account.cash': '1110100007'}[key]):
            ok = sap_utils.create_sap_petty_cash_je(entry)
        return ok, posted

    def test_debits_the_category_account_and_credits_cash_on_the_centres_branch(self):
        ok, posted = self.post(self.entry)
        self.assertTrue(ok)
        self.assertEqual(posted['key'], f'je:petty:{self.entry.id}')
        debit, credit = posted['payload']['JournalEntryLines']
        self.assertEqual((debit['AccountCode'], debit['Debit'], debit['Credit'], debit['BPLID']),
                         ('5411000003', 120.5, 0.0, 4))
        self.assertEqual((credit['AccountCode'], credit['Debit'], credit['Credit'], credit['BPLID']),
                         ('1110200001', 0.0, 120.5, 4))   # Cash in Hand, not the till's bank account
        self.entry.refresh_from_db()
        self.assertTrue(self.entry.sap_sync_status)

    def test_references_remarks_and_cost_centre_are_on_the_journal_and_every_line(self):
        PettyCashEntry.objects.filter(pk=self.entry.pk).update(comments='Guests came without notice')
        ok, posted = self.post(PettyCashEntry.objects.get(pk=self.entry.pk))
        payload = posted['payload']
        self.assertEqual((payload['Reference'], payload['Reference2'], payload['Reference3'], payload['Memo']),
                         (f'PC{self.entry.id}', 'V9', 'Milk', 'Guests came without notice'))
        for line in payload['JournalEntryLines']:
            self.assertEqual((line['Reference1'], line['Reference2'], line['AdditionalReference'],
                              line['LineMemo'], line['CostingCode']),
                             (f'PC{self.entry.id}', 'V9', 'Milk', 'Guests came without notice', 'CGPAO001'))

    def test_empty_voucher_and_comments_are_left_out_not_sent_blank(self):
        PettyCashEntry.objects.filter(pk=self.entry.pk).update(voucher_number='', comments='')
        ok, posted = self.post(PettyCashEntry.objects.get(pk=self.entry.pk))
        payload = posted['payload']
        self.assertNotIn('Reference2', payload)
        self.assertEqual(payload['Memo'], f'Petty Cash POS {self.entry.id} - Client Refreshment')
        self.assertTrue(all('Reference2' not in l for l in payload['JournalEntryLines']))

    def test_long_text_is_cut_to_what_sap_holds(self):
        PettyCashEntry.objects.filter(pk=self.entry.pk).update(description='d' * 255, comments='c' * 400)
        ok, posted = self.post(PettyCashEntry.objects.get(pk=self.entry.pk))
        self.assertEqual((len(posted['payload']['Reference3']), len(posted['payload']['Memo'])), (200, 254))

    def test_a_centre_without_a_cost_centre_still_posts_without_one(self):
        PettyJournalTests.cost_centre = None
        try:
            ok, posted = self.post(self.entry)
        finally:
            PettyJournalTests.cost_centre = 'CGPAO001'
        self.assertTrue(ok)
        self.assertTrue(all('CostingCode' not in l for l in posted['payload']['JournalEntryLines']))

    def test_the_cost_centre_is_the_rule_named_after_the_centre(self):
        from django.core.cache import cache
        cache.delete('sap_cost_centres')
        rows = [{'FactorCode': 'CGPAO001', 'FactorDescription': 'ADDRESS ONE', 'InWhichDimension': 1, 'Active': 'tYES'},
                {'FactorCode': 'OLD', 'FactorDescription': 'MAIN', 'InWhichDimension': 1, 'Active': 'tNO'}]
        centre = type('C', (), {'display_name': 'Address One', 'center_name': 'Address One'})()
        main = type('C', (), {'display_name': 'Main', 'center_name': 'Main'})()
        with mock.patch.object(sap_utils, '_get_all', return_value=rows):
            self.assertEqual(sap_utils.cost_centre_code(centre), 'CGPAO001')
            self.assertIsNone(sap_utils.cost_centre_code(main))   # its rule is switched off
        cache.delete('sap_cost_centres')

    def test_an_entry_without_a_sap_category_is_not_posted(self):
        self.entry.category = None
        self.entry.save()
        ok, posted = self.post(self.entry)
        self.assertFalse(ok)
        self.assertEqual(posted, {})
        self.entry.refresh_from_db()
        self.assertIn('no SAP category', self.entry.sap_sync_message)

    def test_a_centre_without_a_branch_name_is_not_posted(self):
        self.center.sap_branch_name = ''
        self.center.save()
        ok, posted = self.post(PettyCashEntry.objects.get(pk=self.entry.pk))
        self.assertFalse(ok)
        self.assertEqual(posted, {})

    def test_the_queue_knows_the_journal(self):
        from pos_backend import sap_jobs
        self.assertEqual(sap_jobs.queue_object(sap_utils.create_sap_petty_cash_je, (self.entry,)),
                         ('petty_cash_je', self.entry.id))
        manager, fn = sap_jobs._loader('petty_cash_je')
        self.assertIs(fn, sap_utils.create_sap_petty_cash_je)

    def test_a_posted_entry_cannot_be_changed_or_deleted(self):
        PettyCashEntry.objects.filter(pk=self.entry.pk).update(sap_sync_status=True)
        url = f'/finance/api/petty-cash/{self.entry.id}/'
        self.assertEqual(self.api.patch(url, {'amount': '5'}, format='json').status_code, 403)
        self.assertEqual(self.api.delete(url).status_code, 403)

    def test_an_entry_not_yet_posted_can_be_corrected(self):
        url = f'/finance/api/petty-cash/{self.entry.id}/'
        res = self.api.patch(url, {'amount': '99.00'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)


@override_settings(SAP_ENABLED=True)
class PettyCategoryRefreshTests(FinanceTestBase):
    def setUp(self):
        super().setUp()
        from django.core.cache import cache
        cache.delete('petty_categories_synced')

    def names(self):
        res = self.api.get('/finance/api/petty-cash-categories/')
        rows = res.data['results'] if isinstance(res.data, dict) else res.data
        return sorted(r['name'] for r in rows if r['is_active'])

    def test_a_change_in_sap_shows_on_the_next_read_and_nothing_is_lost_when_sap_is_silent(self):
        from django.core.cache import cache
        with mock.patch.object(sap_utils, 'petty_cash_accounts',
                               return_value=[('5411000003', 'Petty-Client Refreshment')]):
            self.assertEqual(self.names(), ['Client Refreshment'])
        cache.delete('petty_categories_synced')
        with mock.patch.object(sap_utils, 'petty_cash_accounts',
                               return_value=[('5411000003', 'Petty-Client Refreshment'),
                                             ('5411000006', 'Petty-Internet Expenses')]):
            self.assertEqual(self.names(), ['Client Refreshment', 'Internet Expenses'])
        cache.delete('petty_categories_synced')
        with mock.patch.object(sap_utils, 'petty_cash_accounts', return_value=[]):
            self.assertEqual(self.names(), ['Client Refreshment', 'Internet Expenses'])
        cache.delete('petty_categories_synced')
        with mock.patch.object(sap_utils, 'petty_cash_accounts', side_effect=RuntimeError('down')):
            self.assertEqual(self.names(), ['Client Refreshment', 'Internet Expenses'])
