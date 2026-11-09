"""Stock between two branches (two GSTINs) goes to SAP as an A/R invoice (when it leaves) and an A/P invoice (when it
arrives), with IGST worked out by SAP; stock inside one branch stays a plain Inventory Transfer.

SAP is a FakeSap that also answers the invoice reads/posts these need; HANA is mocked. No test reaches the network.
"""
import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock

import requests
from django.core.cache import cache
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from inventory import places
from inventory.models import Product, StockTransfer, StockTransferItem
from pos_backend import app_settings, hana_reads, sap_branch_transfer, sap_jobs, sap_utils
from pos_backend.management.commands import create_branch_partners as branch_command
from pos_backend.models import AppSetting, BackgroundJob
from pos_backend.tests_sap_integrity import FakeResponse, FakeSap, SapTestBase
from salon_admin.models import Center

DETAILS = {
    'New Delhi': {'id': 1, 'state': 'DL', 'gstin': '07AAHCG9333L1Z6'},
    'Haryana': {'id': 6, 'state': 'HR', 'gstin': '06AAHCG9333L1Z8'},
    'Uttar Pradesh': {'id': 8, 'state': 'UP', 'gstin': '09AAHCG9333L1Z2'},
    'ISD': {'id': 7, 'state': '', 'gstin': '07AAHCG9333L2Z5'},          # a second GSTIN in Delhi
    'Main': {'id': 2, 'state': '', 'gstin': ''},                          # SAP has no GSTIN for it
}


def set_setting(key, value):
    AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': value})
    app_settings.invalidate()


class BranchSap(FakeSap):
    """FakeSap that also numbers the invoices, like SAP does, and serves the A/R invoice back for the A/P."""

    def __init__(self):
        super().__init__()
        self.ar_lines = None
        self.missing_partners = set()
        self.partners = None            # {code: partner} once a test wants SAP to hold real partners (GET / PATCH / POST)

    def __call__(self, method, url, timeout=15, **kwargs):
        if "BusinessPartners('" in url:
            code = url.split("BusinessPartners('")[1].split("'")[0]
            if method == 'GET' and (code in self.missing_partners or (self.partners is not None and code not in self.partners)):
                self.calls.append((method, url, None))
                raise requests.exceptions.HTTPError(response=FakeResponse(404, {}))
            if method == 'GET' and self.partners is not None:
                self.calls.append((method, url, None))
                return FakeResponse(200, self.partners[code])
            if method == 'PATCH' and self.partners is not None:
                self.calls.append((method, url, kwargs.get('json')))
                self.partners[code].update(kwargs['json'])
                return FakeResponse(204, {})
        if method == 'POST' and url.endswith('/BusinessPartners') and self.partners is not None:
            self.partners[kwargs['json']['CardCode']] = dict(kwargs['json'])
        if method == 'GET' and 'Invoices(77)' in url:
            self.calls.append((method, url, None))
            return FakeResponse(200, {'DocNum': 9, 'DocTotal': 1180, 'VatSum': 180, 'DocumentLines': self.ar_lines or []})
        if method == 'POST' and url.endswith('/Invoices'):
            self.calls.append((method, url, kwargs.get('json')))
            return FakeResponse(201, {'DocEntry': 77, 'DocNum': 9, 'DocTotal': 1180, 'VatSum': 180})
        if method == 'POST' and url.endswith('/PurchaseInvoices'):
            self.calls.append((method, url, kwargs.get('json')))
            return FakeResponse(201, {'DocEntry': 88, 'DocNum': 5})
        return super().__call__(method, url, timeout=timeout, **kwargs)


class BranchBase(SapTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.sap = BranchSap()
        for p in (mock.patch('pos_backend.sap_utils._send_sap_request', self.sap),):
            p.start()
            self.addCleanup(p.stop)
        cache.set('sap_bpl_map', {'New Delhi': 1, 'Haryana': 6, 'Uttar Pradesh': 8, 'ISD': 7, 'Main': 2})
        self.delhi = Center.objects.create(center_name='Delhi A', sap_warehouse_code='DLW0001', sap_branch_name='New Delhi')
        self.delhi2 = Center.objects.create(center_name='Delhi B', sap_warehouse_code='DLW0002', sap_branch_name='New Delhi')
        self.haryana = Center.objects.create(center_name='Gurgaon', sap_warehouse_code='HRW0001', sap_branch_name='Haryana')
        self.product = Product.objects.create(name='Serum', product_code='SER1', price=Decimal('120'),
                                              gst_percent=Decimal('18'), center=self.delhi)
        self.details = mock.patch.object(places, 'business_place_details', return_value=DETAILS)
        self.details.start()
        self.addCleanup(self.details.stop)
        self.cost = Decimal('100')
        self.candidates = []            # rows of the "which partners look like a branch" HANA query
        self.hana = mock.patch.object(hana_reads, 'rows', side_effect=self.hana_rows)
        self.hana.start()
        self.addCleanup(self.hana.stop)

    def hana_rows(self, sql, params=()):
        if 'OITW' in sql:
            return [(self.cost,)]
        if 'OCRD' in sql:
            return self.candidates
        return []

    def transfer(self, to=None, status='In Transit', quantity=3, **fields):
        now = timezone.now()
        tr = StockTransfer.objects.create(from_center=self.delhi, to_center=to or self.haryana, status=status,
                                          dispatched_at=now, **fields)
        StockTransferItem.objects.create(transfer=tr, product=self.product, quantity=quantity)
        return tr

    def sale_body(self):
        (post,) = self.sap.posts('/Invoices')
        return post[2]


class SupplyKind(BranchBase):
    def kind(self, a, b):
        return places.supply_kind(a, b)

    def test_two_centres_of_one_branch_are_a_plain_transfer(self):
        self.assertEqual(self.kind(self.delhi, self.delhi2), ('plain', ''))

    def test_two_centres_with_no_branch_at_all_are_plain_as_before(self):
        a, b = Center.objects.create(center_name='X'), Center.objects.create(center_name='Y')
        self.assertEqual(self.kind(a, b), ('plain', ''))

    def test_different_states_are_igst(self):
        self.assertEqual(self.kind(self.delhi, self.haryana), ('igst', ''))

    def test_two_gstins_in_one_state_are_cgst_sgst(self):
        isd = Center.objects.create(center_name='ISD centre', sap_warehouse_code='ISW1', sap_branch_name='ISD')
        self.assertEqual(self.kind(self.delhi, isd), ('cgst_sgst', ''))

    def test_what_cannot_be_told_says_what_to_fix(self):
        main = Center.objects.create(center_name='General Warehouse', sap_warehouse_code='01', sap_branch_name='Main')
        kind, why = self.kind(main, self.haryana)
        self.assertEqual(kind, 'unknown')
        self.assertIn('"Main" has no GSTIN', why)
        nobranch = Center.objects.create(center_name='Loose', sap_warehouse_code='L1')
        self.assertEqual(self.kind(nobranch, self.haryana)[0], 'unknown')
        ghost = Center.objects.create(center_name='Ghost', sap_warehouse_code='G1', sap_branch_name='Ahmedabad')
        self.assertIn('no business place called "Ahmedabad"', self.kind(self.delhi, ghost)[1])

    def test_a_sap_that_cannot_be_read_is_unknown_not_plain(self):
        self.details.stop()
        with mock.patch.object(places, 'business_place_details', return_value={}):
            self.assertEqual(self.kind(self.delhi, self.haryana)[0], 'unknown')
        self.details.start()


class SaleAtDispatch(BranchBase):
    def test_the_a_r_invoice_is_for_the_sender_to_the_receiving_branchs_customer_with_igst_from_sap(self):
        tr = self.transfer()
        with mock.patch('pos_backend.sap_utils.issue_batches', return_value=[{'BatchNumber': 'B1', 'Quantity': 3.0}]):
            self.assertTrue(sap_branch_transfer.create_sap_branch_sale(tr))
        body = self.sale_body()
        self.assertEqual(body['CardCode'], 'IBC-6')                       # the receiving branch (Haryana) as a customer
        self.assertEqual(body['BPL_IDAssignedToInvoice'], 1)              # booked at the SENDER's business place
        self.assertEqual(body['NumAtCard'], f'ST-{tr.pk}')
        self.assertEqual(body['U_POSINNO'], f'ST-{tr.pk}')
        self.assertEqual(body['DocumentLines'], [{
            'ItemCode': 'SER1', 'Quantity': '3', 'WarehouseCode': 'DLW0001', 'UnitPrice': '100.0000',
            'TaxCode': 'IGST@18', 'BatchNumbers': [{'BatchNumber': 'B1', 'Quantity': 3.0}]}])
        self.assertNotIn('VatSum', str(body))                              # the POS never works the tax out
        tr.refresh_from_db()
        self.assertEqual((tr.supply_kind, tr.sap_ar_doc_entry, tr.sap_ar_doc_num), ('igst', 77, 9))
        self.assertEqual((tr.sap_taxable_value, tr.sap_tax_amount, tr.sap_invoice_total),
                         (Decimal('1000.00'), Decimal('180.00'), Decimal('1180.00')))
        self.assertIn('IGST 180', tr.sap_branch_message)

    def test_the_same_state_with_another_gstin_uses_cgst_sgst(self):
        isd = Center.objects.create(center_name='ISD centre', sap_warehouse_code='ISW1', sap_branch_name='ISD')
        tr = self.transfer(to=isd)
        sap_branch_transfer.create_sap_branch_sale(tr)
        self.assertEqual(self.sale_body()['DocumentLines'][0]['TaxCode'], 'CG+SG@18')
        self.assertEqual(self.sale_body()['CardCode'], 'IBC-7')

    def test_it_is_posted_once(self):
        tr = self.transfer()
        self.assertTrue(sap_branch_transfer.create_sap_branch_sale(tr))
        self.assertTrue(sap_branch_transfer.create_sap_branch_sale(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(len(self.sap.posts('/Invoices')), 1)

    def test_one_branch_needs_no_invoice(self):
        tr = self.transfer(to=self.delhi2)
        self.assertTrue(sap_branch_transfer.create_sap_branch_sale(tr))
        self.assertEqual(self.sap.posts('/Invoices'), [])
        tr.refresh_from_db()
        self.assertEqual(tr.supply_kind, 'plain')

    def refused(self, tr, text):
        self.assertFalse(sap_branch_transfer.create_sap_branch_sale(tr))
        self.assertEqual(self.sap.posts('/Invoices'), [])
        tr.refresh_from_db()
        self.assertFalse(tr.sap_sync_status)
        self.assertIn(text, tr.sap_sync_message)
        self.assertEqual(tr.sap_sync_message, tr.sap_branch_message)

    def test_a_transfer_whose_tax_cannot_be_told_posts_nothing(self):
        main = Center.objects.create(center_name='General Warehouse', sap_warehouse_code='01', sap_branch_name='Main')
        tr = self.transfer()
        StockTransfer.objects.filter(pk=tr.pk).update(from_center=main)
        self.refused(StockTransfer.objects.get(pk=tr.pk), '"Main" has no GSTIN')

    def test_a_missing_branch_partner_says_which_and_how_to_create_it(self):
        self.sap.missing_partners.add('IBC-6')
        self.refused(self.transfer(), 'SAP has no customer "IBC-6" for the branch "Haryana"')

    def test_a_zero_cost_is_never_invoiced(self):
        self.cost = Decimal('0')
        self.refused(self.transfer(), 'no value to invoice SER1')

    def test_a_gst_rate_with_no_sap_tax_code_is_refused(self):
        self.product.gst_percent = Decimal('3')
        self.product.save()
        self.refused(self.transfer(), 'GST 3.00% has no SAP tax code')

    def test_the_value_can_be_the_pos_price_instead_of_the_cost(self):
        set_setting('sap.transfer_price_basis', 'product_price')
        tr = self.transfer()
        def rows(sql, params=()):
            if 'OITW' in sql:                                   # the item's cost: must not be asked for in this mode
                raise AssertionError('SAP cost was read')
            return []
        with mock.patch.object(hana_reads, 'rows', side_effect=rows):
            sap_branch_transfer.create_sap_branch_sale(tr)
        self.assertEqual(self.sale_body()['DocumentLines'][0]['UnitPrice'], '120.0000')


class PurchaseAtReceipt(BranchBase):
    def setUp(self):
        super().setUp()
        self.sap.ar_lines = [{'ItemCode': 'SER1', 'Quantity': 3, 'UnitPrice': 100.0, 'TaxCode': 'IGST@18',
                              'WarehouseCode': 'DLW0001',
                              'BatchNumbers': [{'BatchNumber': 'B1', 'Quantity': 2}, {'BatchNumber': 'B2', 'Quantity': 1}]}]

    def received(self, accepted=None, **fields):
        tr = self.transfer(status='Completed', sap_ar_doc_entry=77, sap_ar_doc_num=9, supply_kind='igst', **fields)
        StockTransferItem.objects.filter(transfer=tr).update(received_quantity=accepted)
        StockTransfer.objects.filter(pk=tr.pk).update(received_at=timezone.now())
        return StockTransfer.objects.get(pk=tr.pk)

    def purchase_body(self):
        (post,) = self.sap.posts('/PurchaseInvoices')
        return post[2]

    def test_the_a_p_invoice_is_from_the_sending_branch_at_the_receiver_and_copies_the_tax_invoice(self):
        tr = self.received(accepted=3)
        self.assertTrue(sap_branch_transfer.create_sap_branch_purchase(tr))
        body = self.purchase_body()
        self.assertEqual(body['CardCode'], 'IBV-1')                         # the sending branch (New Delhi) as a vendor
        self.assertEqual(body['BPL_IDAssignedToInvoice'], 6)                # booked at the RECEIVER's business place
        self.assertEqual(body['NumAtCard'], '9')                            # the supplier's invoice number
        self.assertEqual(body['DocumentLines'], [{
            'ItemCode': 'SER1', 'Quantity': '3', 'WarehouseCode': 'HRW0001', 'UnitPrice': 100.0, 'TaxCode': 'IGST@18',
            'BatchNumbers': [{'BatchNumber': 'B1', 'Quantity': 2.0}, {'BatchNumber': 'B2', 'Quantity': 1.0}]}])
        tr.refresh_from_db()
        self.assertEqual((tr.sap_ap_doc_entry, tr.sap_ap_doc_num, tr.sap_sync_status), (88, 5, True))

    def test_a_shortage_is_invoiced_as_accepted_and_the_credit_memo_is_called_for(self):
        tr = self.received(accepted=2)
        sap_branch_transfer.create_sap_branch_purchase(tr)
        line = self.purchase_body()['DocumentLines'][0]
        self.assertEqual(line['Quantity'], '2')
        self.assertEqual(line['BatchNumbers'], [{'BatchNumber': 'B1', 'Quantity': 2.0}])      # the shortage comes off the last batch
        tr.refresh_from_db()
        self.assertIn('credit memo', tr.sap_branch_message)
        self.assertIn('SER1 1', tr.sap_branch_message)

    def test_nothing_accepted_posts_nothing(self):
        tr = self.received(accepted=0)
        self.assertTrue(sap_branch_transfer.create_sap_branch_purchase(tr))
        self.assertEqual(self.sap.posts('/PurchaseInvoices'), [])

    def test_it_needs_the_tax_invoice_first(self):
        tr = self.received(accepted=3)
        StockTransfer.objects.filter(pk=tr.pk).update(sap_ar_doc_entry=None)
        self.assertFalse(sap_branch_transfer.create_sap_branch_purchase(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(self.sap.posts('/PurchaseInvoices'), [])

    def test_it_is_posted_once(self):
        tr = self.received(accepted=3)
        sap_branch_transfer.create_sap_branch_purchase(tr)
        sap_branch_transfer.create_sap_branch_purchase(StockTransfer.objects.get(pk=tr.pk))
        self.assertEqual(len(self.sap.posts('/PurchaseInvoices')), 1)

    def test_a_missing_vendor_partner_says_so(self):
        self.sap.missing_partners.add('IBV-1')
        tr = self.received(accepted=3)
        self.assertFalse(sap_branch_transfer.create_sap_branch_purchase(tr))
        tr.refresh_from_db()
        self.assertIn('SAP has no vendor "IBV-1"', tr.sap_sync_message)


class HowTheTransferIsPosted(BranchBase):
    def test_a_transfer_between_branches_is_never_posted_as_a_plain_inventory_transfer(self):
        tr = self.transfer(status='Completed', sap_ar_doc_entry=77, sap_ar_doc_num=9, supply_kind='igst')
        self.sap.ar_lines = [{'ItemCode': 'SER1', 'Quantity': 3, 'UnitPrice': 100.0, 'TaxCode': 'IGST@18'}]
        StockTransfer.objects.filter(pk=tr.pk).update(received_at=timezone.now())
        self.assertTrue(sap_utils.create_sap_stock_transfer(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(self.sap.posts('StockTransfers'), [])
        self.assertEqual(len(self.sap.posts('/PurchaseInvoices')), 1)

    def test_a_transfer_inside_one_branch_is_still_a_plain_inventory_transfer(self):
        tr = self.transfer(to=self.delhi2, status='Completed')
        self.assertTrue(sap_utils.create_sap_stock_transfer(tr))
        self.assertEqual(len(self.sap.posts('StockTransfers')), 1)
        self.assertEqual(self.sap.posts('/PurchaseInvoices'), [])

    def test_a_transfer_whose_tax_cannot_be_told_waits_instead_of_posting_plain(self):
        main = Center.objects.create(center_name='General Warehouse', sap_warehouse_code='01', sap_branch_name='Main')
        tr = self.transfer(status='Completed')
        StockTransfer.objects.filter(pk=tr.pk).update(from_center=main)
        self.assertFalse(sap_utils.create_sap_stock_transfer(StockTransfer.objects.get(pk=tr.pk)))
        self.assertEqual(self.sap.posts('StockTransfers'), [])
        self.assertIn('"Main" has no GSTIN', StockTransfer.objects.get(pk=tr.pk).sap_sync_message)


class TheQueue(BranchBase):
    def test_dispatching_queues_the_tax_invoice(self):
        su = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        api = APIClient()
        api.force_authenticate(su)
        Product.objects.filter(pk=self.product.pk).update(current_stock=10)
        res = api.post('/inventory/api/stock_transfers/', {'from_center': self.delhi.id, 'to_center': self.haryana.id,
                       'items': [{'product': self.product.id, 'quantity': '3'}]}, format='json')
        self.assertEqual(res.status_code, 201, res.content)
        tid = res.json()['id']
        self.assertFalse(BackgroundJob.objects.filter(dedupe_key=f'sap:branch_sale:{tid}').exists())     # a draft posts nothing
        res = api.patch(f'/inventory/api/stock_transfers/{tid}/', {'status': 'In Transit'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:branch_sale:{tid}').exists())

    def test_the_receipt_waits_for_the_tax_invoice_and_queues_it_when_it_never_was(self):
        tr = self.transfer(status='Completed')
        with self.assertRaises(sap_jobs.WaitForInvoice):
            sap_jobs.invoice_dependency('stock_transfer', tr.pk)
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:branch_sale:{tr.pk}').exists())

    def test_a_given_up_tax_invoice_stops_the_receipt_from_spinning(self):
        tr = self.transfer(status='Completed')
        BackgroundJob.objects.create(kind='sap.sync', dedupe_key=f'sap:branch_sale:{tr.pk}', status=BackgroundJob.DEAD)
        with self.assertRaises(RuntimeError):
            sap_jobs.invoice_dependency('stock_transfer', tr.pk)

    def test_nothing_to_wait_for_when_the_invoice_is_in_sap_or_the_branch_is_the_same(self):
        done = self.transfer(status='Completed', sap_ar_doc_entry=77)
        plain = self.transfer(to=self.delhi2, status='Completed')
        self.assertEqual(sap_jobs.invoice_dependency('stock_transfer', done.pk), 'ready')
        self.assertEqual(sap_jobs.invoice_dependency('stock_transfer', plain.pk), 'ready')


def team_partner(code, name, gstin='', assigned=(1, 6)):
    """A partner as the SAP team made them: a ship-to / bill-to pair, no GSTIN on it unless one is given."""
    state = {'Haryana': 'HR', 'New Delhi': 'DL'}[name]
    return {'CardCode': code, 'CardName': f'{name} Branch',
            'BPAddresses': [{'RowNum': i, 'AddressName': state, 'AddressType': kind, 'State': state, 'Country': 'IN',
                             'Street': None, 'GSTIN': gstin or None, 'City': 'Gurugram',
                             'BuildingFloorRoom': 'Killa No. 36, Silokhra Village', 'BPCode': code,
                             'CreateDate': '2026-10-27T00:00:00Z', 'CreateTime': '12:38:49'}
                            for i, kind in enumerate(('bo_ShipTo', 'bo_BillTo'))],
            'BPBranchAssignment': [{'BPLID': b, 'DisabledForBP': 'tNO'} for b in assigned]}


class BranchPartnerLookup(BranchBase):
    """Which SAP partner stands for a branch: the ones the SAP team made are used as they are."""

    HARYANA_C = ('CUST000779', 'Haryana Branch', 'C', 'HR', '')
    HARYANA_V = ('VEND001274', 'Haryana Branch', 'S', 'HR', '')
    DELHI_C = ('CUST000775', 'New Delih Branch', 'C', 'DL', '')            # sic: the typo is in SAP
    DELHI_V = ('VEND001271', 'New Delhi Branch', 'S', 'DL', '')

    def lookup(self, kind, branch):
        cache.delete('sap_branch_partner_candidates')
        return sap_branch_transfer.branch_partner(kind, branch, DETAILS)

    def test_without_any_partner_in_sap_the_pos_code_is_asked_for(self):
        self.assertEqual(self.lookup('customer', 'Haryana'), 'IBC-6')
        self.assertEqual(self.lookup('vendor', 'New Delhi'), 'IBV-1')

    def test_the_partner_of_the_state_is_found_even_with_a_typo_in_its_name(self):
        self.candidates = [self.HARYANA_C, self.HARYANA_V, self.DELHI_C, self.DELHI_V]
        self.assertEqual(self.lookup('customer', 'Haryana'), 'CUST000779')
        self.assertEqual(self.lookup('vendor', 'Haryana'), 'VEND001274')
        self.assertEqual(self.lookup('customer', 'New Delhi'), 'CUST000775')
        self.assertEqual(self.lookup('vendor', 'New Delhi'), 'VEND001271')

    def test_a_customer_is_never_used_as_a_vendor(self):
        self.candidates = [self.HARYANA_C]
        self.assertEqual(self.lookup('vendor', 'Haryana'), 'IBV-6')

    def test_a_partner_the_pos_made_wins(self):
        self.candidates = [self.HARYANA_C, ('IBC-6', 'Haryana (own branch) - customer', 'C', 'HR', '06AAHCG9333L1Z8')]
        self.assertEqual(self.lookup('customer', 'Haryana'), 'IBC-6')

    def test_the_gstin_on_the_address_decides_first(self):
        other = ('CUST000999', 'Gurgaon Branch', 'C', 'HR', '06AAHCG9333L1Z8')
        self.candidates = [self.HARYANA_C, other]
        self.assertEqual(self.lookup('customer', 'Haryana'), 'CUST000999')

    def test_a_partner_carrying_another_branchs_gstin_is_ruled_out(self):
        wrong = ('CUST000998', 'Haryana Branch', 'C', 'HR', '09AAHCG9333L1Z2')
        self.candidates = [wrong]
        self.assertEqual(self.lookup('customer', 'Haryana'), 'IBC-6')

    def test_two_partners_for_one_state_are_refused_not_guessed(self):
        self.candidates = [self.HARYANA_C, ('CUST000888', 'Haryana 2 Branch', 'C', 'HR', '')]
        with self.assertRaisesRegex(ValueError, 'CUST000779, CUST000888'):
            self.lookup('customer', 'Haryana')

    def test_two_business_places_in_one_state_are_told_apart_by_name(self):
        details = dict(DETAILS, ISD={'id': 7, 'state': 'DL', 'gstin': '07AAHCG9333L2Z5'})
        cache.delete('sap_branch_partner_candidates')
        self.candidates = [self.DELHI_C, ('CUST000776', 'ISD Branch', 'C', 'DL', '')]
        self.assertEqual(sap_branch_transfer.branch_partner('customer', 'ISD', details), 'CUST000776')
        cache.delete('sap_branch_partner_candidates')
        self.assertEqual(sap_branch_transfer.branch_partner('customer', 'New Delhi', details), 'IBC-1')   # "New Delih" ≠ name

    def test_a_hana_that_cannot_be_read_falls_back_to_the_pos_code(self):
        self.hana.stop()
        with mock.patch.object(hana_reads, 'rows', side_effect=hana_reads.HanaUnavailable('down')):
            self.assertEqual(self.lookup('customer', 'Haryana'), 'IBC-6')
        self.hana.start()

    def test_the_tax_invoice_goes_to_the_partner_the_sap_team_made(self):
        self.candidates = [self.HARYANA_C, self.HARYANA_V, self.DELHI_C, self.DELHI_V]
        tr = self.transfer()
        self.assertTrue(sap_branch_transfer.create_sap_branch_sale(tr))
        self.assertEqual(self.sale_body()['CardCode'], 'CUST000779')            # Haryana as the customer
        self.sap.ar_lines = [{'ItemCode': 'SER1', 'Quantity': 3, 'UnitPrice': 100.0, 'TaxCode': 'IGST@18'}]
        tr.refresh_from_db()
        StockTransfer.objects.filter(pk=tr.pk).update(received_at=timezone.now())
        self.assertTrue(sap_branch_transfer.create_sap_branch_purchase(StockTransfer.objects.get(pk=tr.pk)))
        (post,) = self.sap.posts('/PurchaseInvoices')
        self.assertEqual(post[2]['CardCode'], 'VEND001271')                      # New Delhi as the vendor

    def test_an_ambiguous_branch_stops_the_posting_with_the_reason(self):
        self.candidates = [self.HARYANA_C, ('CUST000888', 'Haryana 2 Branch', 'C', 'HR', '')]
        tr = self.transfer()
        self.assertFalse(sap_branch_transfer.create_sap_branch_sale(tr))
        tr.refresh_from_db()
        self.assertIn('could stand for the branch "Haryana"', tr.sap_sync_message)
        self.assertEqual(self.sap.posts('/Invoices'), [])


class BranchPartners(BranchBase):
    def setUp(self):
        super().setUp()
        Center.objects.create(center_name='General Warehouse', sap_warehouse_code='01', sap_branch_name='Main')
        self.sap.partners = {}

    def command(self, *args):
        out = StringIO()
        call_command('create_branch_partners', *args, stdout=out)
        return out.getvalue()

    def patches(self):
        return [c for c in self.sap.calls if c[0] == 'PATCH']

    def test_it_lists_and_changes_nothing_by_default(self):
        text = self.command()
        self.assertIn('MISSING  IBC-6', text)
        self.assertIn('MISSING  IBV-1', text)
        self.assertIn('skipped  Main: no GSTIN in SAP', text)
        self.assertEqual(self.sap.posts('BusinessPartners'), [])
        self.assertEqual(self.patches(), [])

    def test_a_business_place_no_centre_uses_is_left_out(self):
        text = self.command()
        self.assertNotIn('ISD', text)
        self.assertNotIn('Uttar Pradesh', text)

    def test_apply_creates_a_customer_and_a_vendor_per_branch_with_the_gstin_on_the_address(self):
        self.command('--apply')
        posts = [p[2] for p in self.sap.posts('BusinessPartners')]
        self.assertEqual(len(posts), 4)                                       # 2 branches in use with a GSTIN x 2
        haryana = next(p for p in posts if p['CardCode'] == 'IBC-6')
        self.assertEqual((haryana['CardType'], haryana['FederalTaxID']), ('cCustomer', '06AAHCG9333L1Z8'))
        self.assertEqual({a['GSTIN'] for a in haryana['BPAddresses']}, {'06AAHCG9333L1Z8'})
        self.assertEqual({a['State'] for a in haryana['BPAddresses']}, {'HR'})
        self.assertEqual(haryana['GroupCode'], 118)
        vendor = next(p for p in posts if p['CardCode'] == 'IBV-6')
        self.assertEqual((vendor['CardType'], vendor['GroupCode']), ('cSupplier', 111))

    def test_the_address_state_is_the_one_the_business_place_has_in_sap(self):
        """SAP has two codes for Uttarakhand (UT and UK); the business place uses UK, so the partner must too."""
        place = {'id': 5, 'state': 'UK', 'gstin': '05AAHCG9333L1ZA'}
        for _, _, rich, _ in branch_command.partner_payloads('Uttara khand', place):
            self.assertEqual({a['State'] for a in rich['BPAddresses']}, {'UK'})

    def setup_team_partners(self, gstin=''):
        self.candidates = [('CUST000779', 'Haryana Branch', 'C', 'HR', gstin), ('VEND001274', 'Haryana Branch', 'S', 'HR', gstin),
                           ('CUST000775', 'New Delih Branch', 'C', 'DL', gstin), ('VEND001271', 'New Delhi Branch', 'S', 'DL', gstin)]
        self.sap.partners = {
            'CUST000779': team_partner('CUST000779', 'Haryana', gstin), 'VEND001274': team_partner('VEND001274', 'Haryana', gstin),
            'CUST000775': team_partner('CUST000775', 'New Delhi', gstin), 'VEND001271': team_partner('VEND001271', 'New Delhi', gstin)}

    def test_the_team_partners_are_used_and_none_is_created(self):
        self.setup_team_partners()
        text = self.command('--apply')
        self.assertEqual(self.sap.posts('BusinessPartners'), [])
        self.assertIn('have     CUST000779', text)
        self.assertIn('no GSTIN on its address', text)

    def test_apply_puts_the_gstin_on_the_addresses_that_have_none_and_keeps_the_rest(self):
        self.setup_team_partners()
        text = self.command('--apply')
        self.assertEqual(len(self.patches()), 4)
        haryana = self.sap.partners['CUST000779']['BPAddresses']
        self.assertEqual({a['GSTIN'] for a in haryana}, {'06AAHCG9333L1Z8'})
        self.assertEqual({a['GstType'] for a in haryana}, {'gstRegularTDSISD'})
        self.assertEqual([(a['RowNum'], a['AddressName'], a['AddressType'], a['State']) for a in haryana],
                         [(0, 'HR', 'bo_ShipTo', 'HR'), (1, 'HR', 'bo_BillTo', 'HR')])
        self.assertEqual((haryana[0]['City'], haryana[0]['BuildingFloorRoom']),     # what SAP held stays
                         ('Gurugram', 'Killa No. 36, Silokhra Village'))
        sent = next(c[2] for c in self.patches() if 'CUST000779' in c[1])['BPAddresses'][0]    # SAP only takes the rows
        # exactly as it returned them
        self.assertEqual({k: v for k, v in sent.items() if k not in ('GSTIN', 'GstType')},
                         {k: v for k, v in team_partner('CUST000779', 'Haryana')['BPAddresses'][0].items()
                          if k not in ('GSTIN', 'GstType')})
        self.assertIn('updated  CUST000779', text)
        self.assertEqual(self.sap.partners['VEND001271']['BPAddresses'][0]['GSTIN'], '07AAHCG9333L1Z6')

    def test_only_tries_one_partner(self):
        self.setup_team_partners()
        self.command('--apply', '--only', 'CUST000779')
        self.assertEqual([c[1].split("('")[1].split("'")[0] for c in self.patches()], ['CUST000779'])

    def test_a_partner_that_already_has_its_gstin_is_not_touched(self):
        self.setup_team_partners()
        self.sap.partners['CUST000779'] = team_partner('CUST000779', 'Haryana', '06AAHCG9333L1Z8')
        self.command('--apply')
        touched = [c[1] for c in self.patches()]
        self.assertFalse(any('CUST000779' in url for url in touched))
        self.assertEqual(len(touched), 3)

    def test_a_partner_missing_from_a_business_place_is_reported(self):
        self.setup_team_partners()
        self.sap.partners['CUST000779']['BPBranchAssignment'] = [{'BPLID': 1, 'DisabledForBP': 'tNO'}]
        text = self.command()
        self.assertIn('not available in business place Haryana', text)

    def test_an_ambiguous_branch_is_reported_and_nothing_is_changed_for_it(self):
        self.setup_team_partners()
        self.candidates.append(('CUST000888', 'Haryana 2 Branch', 'C', 'HR', ''))
        text = self.command('--apply')
        self.assertIn('AMBIGUOUS customer  Haryana', text)
        self.assertFalse(any('CUST000779' in c[1] for c in self.patches()))

    def test_a_refusal_from_sap_is_shown_and_the_others_go_on(self):
        self.setup_team_partners()
        real = self.sap.__call__

        def refuse_first(method, url, timeout=15, **kwargs):
            if method == 'PATCH' and 'CUST000775' in url:
                raise requests.exceptions.HTTPError(response=FakeResponse(400, {'error': {'message': {'value': 'no way'}}}))
            return real(method, url, timeout=timeout, **kwargs)

        with mock.patch('pos_backend.sap_utils._send_sap_request', refuse_first):
            text = self.command('--apply')
        self.assertIn('FAILED   CUST000775', text)
        self.assertIn('updated  CUST000779', text)
