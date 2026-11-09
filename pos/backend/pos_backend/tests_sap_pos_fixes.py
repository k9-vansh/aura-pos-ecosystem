"""The POS-side SAP fixes of 2026-10-29, each pinned by what it must do and must NOT do.

  * a centre's SAP branch must be a business place SAP has (and `check_sap_mapping` finds the ones that are not);
  * a bill in SAP with no payment in SAP is found and its payment queued again, and a posting that gave up only
    because SAP was slow comes back - a business refusal never does;
  * a post waits for SAP long enough (not 15 seconds);
  * a B2B client / a vendor made in the POS carries its group, GSTIN on the address and branch assignments, and when
    SAP refuses those the plain partner still goes, with a message saying what is missing;
  * a B2B client's later edits reach SAP; a product's unit goes to SAP; a quantity SAP cannot hold is rounded.

SAP is FakeSap / mocks: no test reaches the network.
"""
import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest import mock

import requests
from django.core.cache import cache
from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Payment
from clients.models import Client
from inventory.models import Product, Vendor
from pos_backend import app_settings, hana_reads, sap_ledger, sap_recovery, sap_utils
from pos_backend.models import AppSetting, BackgroundJob
from pos_backend.tests_sap_integrity import FakeResponse, SapTestBase
from salon_admin.models import Center

GSTIN = '09ABTFA7333C1ZB'


def set_setting(key, value):
    AppSetting.objects.update_or_create(key=key, scope='global', defaults={'value': value})
    app_settings.invalidate()


class WaitLongEnough(SapTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def test_the_default_is_a_minute_and_it_can_be_changed(self):
        self.assertEqual(sap_utils._post_timeout(), 60)
        set_setting('sap.post_timeout_seconds', 120)
        self.assertEqual(sap_utils._post_timeout(), 120)

    def test_no_document_post_still_waits_only_15_seconds(self):
        source = Path(sap_utils.__file__).read_text(encoding='utf-8')
        short = [line.strip() for line in source.splitlines()
                 if "_send_sap_request('POST'" in line and re.search(r'timeout=1[05]\)', line)]
        self.assertEqual(short, [])


class CentreBranchMustExistInSap(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        self.centre = Center.objects.create(center_name='Maple Tree', display_name='Maple Tree',
                                            sap_warehouse_code='GJW0002', sap_branch_name='Ahmedabad')

    def patch(self, branch):
        return self.api.patch(f'/salon_admin/api/centers/{self.centre.pk}/', {'sap_branch_name': branch}, format='json')

    def test_a_name_sap_does_not_have_is_refused_with_the_names_it_has(self):
        with mock.patch('inventory.places._business_place_states', return_value={'Gujarat': 'GJ', 'Main': ''}):
            res = self.patch('Ahmedabad2')
        self.assertEqual(res.status_code, 400)
        message = str(res.json()['sap_branch_name'])
        self.assertIn('Gujarat', message)
        self.assertIn('Main', message)

    def test_a_different_case_gets_a_did_you_mean(self):
        with mock.patch('inventory.places._business_place_states', return_value={'Gujarat': 'GJ'}):
            res = self.patch('gujarat')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Did you mean "Gujarat"', str(res.json()))

    def test_a_real_business_place_is_accepted(self):
        with mock.patch('inventory.places._business_place_states', return_value={'Gujarat': 'GJ'}):
            res = self.patch('Gujarat')
        self.assertEqual(res.status_code, 200, res.content)
        self.centre.refresh_from_db()
        self.assertEqual(self.centre.sap_branch_name, 'Gujarat')

    def test_a_sap_that_cannot_be_read_never_blocks_a_save(self):
        with mock.patch('inventory.places._business_place_states', return_value={}):
            self.assertEqual(self.patch('Anything').status_code, 200)

    def test_an_untouched_branch_is_not_re_checked(self):
        """A centre already saved with an old name can still be edited (other fields) while SAP is checked."""
        with mock.patch('inventory.places._business_place_states', return_value={'Gujarat': 'GJ'}):
            res = self.api.patch(f'/salon_admin/api/centers/{self.centre.pk}/',
                                 {'sap_branch_name': 'Ahmedabad', 'phone': '9999999999'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)


class MappingCheck(TestCase):
    def rows(self, warehouses, places):
        def rows(sql, params=()):
            return warehouses if 'OWHS' in sql else places
        return mock.patch.object(hana_reads, 'rows', side_effect=rows)

    def run_check(self):
        out = StringIO()
        try:
            call_command('check_sap_mapping', stdout=out)
            return True, out.getvalue()
        except CommandError:
            return False, out.getvalue()

    def test_every_problem_is_listed_and_a_clean_system_passes(self):
        Center.objects.create(center_name='A', sap_warehouse_code='W1', sap_branch_name='Gujarat')
        Center.objects.create(center_name='B', sap_warehouse_code='W2', sap_branch_name='Ahmedabad')
        Center.objects.create(center_name='C', sap_warehouse_code='W3', sap_branch_name='')
        Center.objects.create(center_name='D', sap_warehouse_code='W4', sap_branch_name='Odisha')
        Center.objects.create(center_name='E', sap_warehouse_code='NOPE', sap_branch_name='Gujarat')
        warehouses = [('W1', 13), ('W2', 13), ('W3', 2), ('W4', None)]
        places = [(13, 'Gujarat'), (2, 'Main'), (10, 'Odisha')]
        with self.rows(warehouses, places):
            ok, text = self.run_check()
        self.assertFalse(ok)
        self.assertNotIn('A:', text)
        self.assertIn('B: its SAP branch "Ahmedabad" is not a business place', text)
        self.assertIn('C: has no SAP branch', text)
        self.assertIn('D: warehouse W4 is not assigned to any business place', text)
        self.assertIn('E: its SAP warehouse "NOPE" does not exist', text)
        self.assertIn('4 of 5 active centres', text)

    def test_all_good(self):
        Center.objects.create(center_name='A', sap_warehouse_code='W1', sap_branch_name='Gujarat')
        with self.rows([('W1', 13)], [(13, 'Gujarat')]):
            ok, text = self.run_check()
        self.assertTrue(ok)
        self.assertIn('Every centre matches SAP', text)


class AutoRecovery(SapTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.old = timezone.now() - timedelta(hours=1)

    def bill(self, method='Cash', age=None, **kw):
        invoice = self.invoice(**kw)
        Payment.objects.create(invoice=invoice, amount=Decimal('1000'), payment_method=method)
        Invoice = type(invoice)
        Invoice.objects.filter(pk=invoice.pk).update(created_at=age or self.old)
        return Invoice.objects.get(pk=invoice.pk)

    def test_a_cash_bill_in_sap_with_no_payment_in_sap_is_found(self):
        invoice = self.bill()
        self.assertEqual([i.pk for i in sap_recovery.missing_payment_invoices()], [invoice.pk])

    def test_not_found_once_the_payment_is_recorded(self):
        invoice = self.bill()
        sap_ledger.mark_done(f'inpay:{invoice.pk}:ar:p1', '10', amount=1000)
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])

    def test_a_balance_only_payment_posts_no_incoming_payment_so_it_is_not_a_gap(self):
        self.bill(method='Value Card')
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])

    def test_a_bill_raised_a_moment_ago_is_left_to_the_normal_route(self):
        self.bill(age=timezone.now() - timedelta(minutes=2))
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])

    def test_a_bill_that_is_not_in_sap_yet_is_not_a_payment_gap(self):
        self.bill(sap_doc_entry=None, sap_sync_status=False)
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])

    def test_a_payment_job_that_is_working_or_gave_up_is_left_alone(self):
        invoice = self.bill()
        job = BackgroundJob.objects.create(kind='sap.sync', dedupe_key=f'sap:incoming_payment:{invoice.pk}',
                                           status=BackgroundJob.DEAD, payload={})
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])
        BackgroundJob.objects.filter(pk=job.pk).update(status=BackgroundJob.FAILED)
        self.assertEqual(sap_recovery.missing_payment_invoices(), [])

    def test_recover_queues_the_payment_and_reopens_a_finished_job_that_did_nothing(self):
        invoice = self.bill()
        BackgroundJob.objects.create(kind='sap.sync', dedupe_key=f'sap:incoming_payment:{invoice.pk}',
                                     status=BackgroundJob.DONE, payload={}, attempts=3, finished_at=self.old)
        self.assertEqual(sap_recovery.recover(), {'payments': 1, 'invoices': 0})
        job = BackgroundJob.objects.get(dedupe_key=f'sap:incoming_payment:{invoice.pk}')
        self.assertEqual((job.status, job.attempts), (BackgroundJob.QUEUED, 0))
        # and a second sweep does not pile up another one
        sap_recovery.recover()
        self.assertEqual(BackgroundJob.objects.filter(dedupe_key=f'sap:incoming_payment:{invoice.pk}').count(), 1)

    def test_a_bill_nobody_queued_is_queued(self):
        invoice = self.bill(sap_doc_entry=None, sap_sync_status=False)
        self.assertEqual(sap_recovery.recover()['invoices'], 1)
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:invoice:{invoice.pk}').exists())
        self.assertEqual(sap_recovery.recover()['invoices'], 0)        # it has a job now

    def dead(self, error, revived=0, age=None):
        return BackgroundJob.objects.create(
            kind='sap.sync', status=BackgroundJob.DEAD, last_error=error, finished_at=age or self.old,
            payload={'object_type': 'invoice', 'object_id': 1, 'revived': revived}, attempts=10, max_attempts=10)

    def test_a_posting_that_only_timed_out_is_given_another_go(self):
        job = self.dead("SapPostingFailed: HTTPConnectionPool(host='sap', port=50001): Read timed out. (read timeout=15)")
        self.assertEqual(sap_recovery.revive_transient_failures(), 1)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts, job.payload['revived']), (BackgroundJob.QUEUED, 0, 1))

    def test_a_login_failure_counts_as_sap_being_down(self):
        self.dead('SapPostingFailed: SAP login failed: Max retries exceeded with url: /b1s/v1/Login')
        self.dead('SapPostingFailed: Could not obtain SAP session')
        self.assertEqual(sap_recovery.revive_transient_failures(), 2)

    def test_what_sap_refused_is_never_retried_by_itself(self):
        self.dead('SapPostingFailed: SAP holds only 0 of DG000009 in batches at 01; 2 is needed.')
        self.dead('SapPostingFailed: -5002: SAP has no business place named Ahmedabad')
        self.assertEqual(sap_recovery.revive_transient_failures(), 0)

    def test_it_gives_up_after_three_goes_and_waits_half_an_hour_first(self):
        self.dead('Read timed out', revived=3)
        self.dead('Read timed out', age=timezone.now() - timedelta(minutes=5))
        self.assertEqual(sap_recovery.revive_transient_failures(), 0)

    def test_the_background_job_is_off_with_the_setting_and_reports_what_it_queued(self):
        from pos_backend.background_jobs import sap_auto_recovery
        invoice = self.bill()
        set_setting('sap.auto_recovery', False)
        self.assertEqual(sap_auto_recovery(), 'off')
        self.assertFalse(BackgroundJob.objects.filter(dedupe_key=f'sap:incoming_payment:{invoice.pk}').exists())
        set_setting('sap.auto_recovery', True)
        self.assertIn('1 payment(s)', sap_auto_recovery())


class B2bClientAndVendor(SapTestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        cache.set('sap_bpl_map', {'Main': 2, 'Gujarat': 13})
        self.b2b = Client.objects.create(first_name='Acme', last_name='Salon', phone='9990000009',
                                         email='a@acme.in', gst_number=GSTIN, center=self.center)

    def bp_post(self):
        (post,) = self.sap.posts('BusinessPartners')
        return post[2]

    def test_a_b2b_client_carries_group_gstin_on_both_addresses_and_every_branch(self):
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False):
            self.assertTrue(sap_utils.create_sap_business_partner(self.b2b))
        body = self.bp_post()
        self.assertEqual(body['GroupCode'], 118)
        self.assertEqual([a['GSTIN'] for a in body['BPAddresses']], [GSTIN, GSTIN])
        self.assertEqual({a['GstType'] for a in body['BPAddresses']}, {'gstRegularTDSISD'})
        self.assertEqual({a['State'] for a in body['BPAddresses']}, {'UP'})
        self.assertEqual(body['BPBranchAssignment'], [{'BPLID': 2, 'DisabledForBP': 'tNO'},
                                                      {'BPLID': 13, 'DisabledForBP': 'tNO'}])
        self.assertEqual(body['FederalTaxID'], GSTIN)
        self.b2b.refresh_from_db()
        self.assertEqual(self.b2b.sap_sync_message, 'Success')

    def test_the_group_is_the_one_in_settings(self):
        set_setting('sap.b2b_customer_group', 119)
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False):
            sap_utils.create_sap_business_partner(self.b2b)
        self.assertEqual(self.bp_post()['GroupCode'], 119)

    def refusing(self, sent):
        def send(method, url, timeout=15, **kwargs):
            sent.append((method, url, kwargs.get('json')))
            body = kwargs.get('json') or {}
            if method == 'POST' and 'BusinessPartners' in url and ('GroupCode' in body):
                raise requests.exceptions.HTTPError(response=FakeResponse(400, {'error': {'code': -1, 'message': {'value': 'Invalid GroupCode'}}}))
            return FakeResponse(201, {'DocEntry': 1})
        return send

    def test_when_sap_refuses_the_extras_the_plain_partner_still_goes_and_says_so(self):
        sent = []
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False), \
                mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=self.refusing(sent)):
            self.assertTrue(sap_utils.create_sap_business_partner(self.b2b))
        posts = [s[2] for s in sent if s[0] == 'POST']
        self.assertEqual(len(posts), 2)
        self.assertNotIn('GroupCode', posts[1])
        self.assertEqual(posts[1]['CardName'], 'Acme Salon')
        self.b2b.refresh_from_db()
        self.assertTrue(self.b2b.sap_sync_status)
        self.assertIn('WITHOUT group', self.b2b.sap_sync_message)
        self.assertIn('Invalid GroupCode', self.b2b.sap_sync_message)

    def test_a_duplicate_is_not_mistaken_for_a_refused_extra(self):
        dup = requests.exceptions.HTTPError(response=FakeResponse(400, {'error': {'code': -2035, 'message': {'value': 'already exists'}}}))
        calls = []

        def send(method, url, timeout=15, **kwargs):
            calls.append(method)
            if method == 'POST':
                raise dup
            return FakeResponse(200, {})
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', side_effect=[False, True]), \
                mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=send):
            self.assertTrue(sap_utils.create_sap_business_partner(self.b2b))
        self.assertEqual(calls.count('POST'), 1)                        # no second, "plain" post

    def test_a_partner_that_exists_is_updated_with_what_the_pos_says_now(self):
        self.b2b.phone = '9876543210'
        self.b2b.save()
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=True):
            self.assertTrue(sap_utils.create_sap_business_partner(self.b2b))
        (call,) = [c for c in self.sap.calls if c[0] == 'PATCH']
        self.assertIn(f'C{self.b2b.pk:06d}', call[1])
        self.assertEqual(call[2], {'CardName': 'Acme Salon', 'Phone1': '9876543210', 'EmailAddress': 'a@acme.in',
                                   'FederalTaxID': GSTIN})
        self.assertEqual(self.sap.posts('BusinessPartners'), [])

    def test_an_edit_of_a_b2b_client_is_queued_and_an_edit_of_a_b2c_client_is_not(self):
        admin = CustomUser.objects.create_superuser(email='su@t.com', password='pw')
        api = APIClient()
        api.force_authenticate(admin)
        res = api.patch(f'/clients/api/clients/{self.b2b.pk}/', {'phone': '9811111111'}, format='json')
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:client:{self.b2b.pk}').exists())
        walk_in = Client.objects.create(first_name='Walk', phone='9990000010', center=self.center)
        BackgroundJob.objects.all().delete()
        api.patch(f'/clients/api/clients/{walk_in.pk}/', {'phone': '9822222222'}, format='json')
        self.assertFalse(BackgroundJob.objects.filter(dedupe_key=f'sap:client:{walk_in.pk}').exists())

    def test_a_vendor_carries_group_branches_and_its_gstin_on_the_addresses(self):
        vendor = Vendor.objects.create(name='Loreal India', center=self.center, cst_number='07AAACL1234C1Z5',
                                       address='12 Market Rd', city='Delhi', pin_code='110001')
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False):
            self.assertTrue(sap_utils.create_sap_vendor(vendor))
        body = self.bp_post()
        self.assertEqual(body['GroupCode'], 111)
        self.assertEqual({a['GSTIN'] for a in body['BPAddresses']}, {'07AAACL1234C1Z5'})
        self.assertEqual({a['State'] for a in body['BPAddresses']}, {'DL'})
        self.assertEqual({a['Street'] for a in body['BPAddresses']}, {'12 Market Rd'})
        self.assertEqual(len(body['BPBranchAssignment']), 2)

    def test_a_vendor_without_a_gstin_gets_no_made_up_address(self):
        vendor = Vendor.objects.create(name='Local Supplier', center=self.center, cst_number='OLD-CST-7')
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False):
            sap_utils.create_sap_vendor(vendor)
        self.assertNotIn('BPAddresses', self.bp_post())

    def test_a_vendor_refused_the_extras_is_still_created(self):
        vendor = Vendor.objects.create(name='Loreal India', center=self.center, cst_number='07AAACL1234C1Z5')
        sent = []
        with mock.patch('pos_backend.sap_utils.check_sap_bp_exists', return_value=False), \
                mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=self.refusing(sent)):
            self.assertTrue(sap_utils.create_sap_vendor(vendor))
        vendor.refresh_from_db()
        self.assertTrue(vendor.sap_sync_status)
        self.assertIn('WITHOUT group', vendor.sap_sync_message)


class ItemUnit(SapTestBase):
    def test_the_unit_goes_to_sap_so_ml_stays_ml(self):
        cache.clear()
        product = Product.objects.create(name='Serum', product_code='SER9', unit='ml', center=self.center,
                                         price=Decimal('10'))
        with mock.patch('pos_backend.sap_utils.check_sap_item_exists', return_value=False):
            self.assertTrue(sap_utils.create_sap_item(product))
        (post,) = self.sap.posts('/Items')
        self.assertEqual(post[2]['InventoryUOM'], 'ML')

    def test_a_piece_product_says_pcs(self):
        product = Product.objects.create(name='Gel', product_code='GEL9', center=self.center)
        sap_utils.create_sap_item(product)
        self.assertEqual(self.sap.posts('/Items')[0][2]['InventoryUOM'], 'PCS')

    def test_when_sap_does_not_take_the_unit_the_item_is_still_created_and_says_so(self):
        product = Product.objects.create(name='Serum', product_code='SER8', unit='ml', center=self.center)
        sent = []

        def send(method, url, timeout=15, **kwargs):
            sent.append(kwargs.get('json'))
            if method == 'POST' and 'InventoryUOM' in (kwargs.get('json') or {}):
                raise requests.exceptions.HTTPError(response=FakeResponse(400, {'error': {'message': {'value': "Property 'InventoryUOM' is invalid"}}}))
            return FakeResponse(201, {})
        with mock.patch('pos_backend.sap_utils._send_sap_request', side_effect=send):
            self.assertTrue(sap_utils.create_sap_item(product))
        product.refresh_from_db()
        self.assertIn('unit of measure', product.sap_sync_message)
        self.assertNotIn('InventoryUOM', sent[-1])


class TaxCodeFollowsTheBranchState(TestCase):
    """CGST+SGST or IGST on a bill is decided by the state the bill is raised from: the centre's GSTIN, else the one of
    its SAP business place (no POS centre has a GSTIN, so every centre used to be taken for Delhi)."""

    PLACES = {'New Delhi': {'id': 1, 'state': 'DL', 'gstin': '07AAHCG9333L1Z6'},
              'Haryana': {'id': 6, 'state': 'HR', 'gstin': '06AAHCG9333L1Z8'},
              'Main': {'id': 2, 'state': '', 'gstin': ''}}

    def setUp(self):
        from inventory import places
        cache.clear()
        patch = mock.patch.object(places, 'business_place_details', return_value=self.PLACES)
        patch.start()
        self.addCleanup(patch.stop)

    def code(self, branch, client_gstin=None, own_gstin='', rate=18):
        from types import SimpleNamespace
        centre = Center.objects.create(center_name=f'C{Center.objects.count()}', sap_branch_name=branch, gst_number=own_gstin)
        client = SimpleNamespace(gst_number=client_gstin) if client_gstin else None
        return sap_utils._apply_invoice_tax_code(SimpleNamespace(center=centre, client=client), {}, rate)['TaxCode']

    def test_a_haryana_centre_selling_to_a_haryana_business_is_cgst_sgst(self):
        self.assertEqual(self.code('Haryana', '06AAAAA0000A1Z5'), 'CG+SG@18')

    def test_a_haryana_centre_selling_to_a_delhi_business_is_igst(self):
        self.assertEqual(self.code('Haryana', '07AAAAA0000A1Z5'), 'IGST@18')

    def test_a_delhi_centre_selling_to_a_haryana_business_is_igst(self):
        self.assertEqual(self.code('New Delhi', '06AAAAA0000A1Z5'), 'IGST@18')

    def test_a_walk_in_customer_is_always_local(self):
        self.assertEqual(self.code('Haryana'), 'CG+SG@18')

    def test_a_centre_with_its_own_gstin_uses_that_one(self):
        self.assertEqual(self.code('New Delhi', '09AAAAA0000A1Z5', own_gstin='09AAHCG9333L1Z2'), 'CG+SG@18')

    def test_with_no_state_known_it_is_delhi_as_before(self):
        self.assertEqual(self.code('Main', '07AAAAA0000A1Z5'), 'CG+SG@18')
        self.assertEqual(self.code('Main', '06AAAAA0000A1Z5'), 'IGST@18')
        self.assertEqual(self.code('', '07AAAAA0000A1Z5'), 'CG+SG@18')


class QuantityDecimals(TestCase):
    def setUp(self):
        cache.clear()
        app_settings.invalidate()

    def test_sap_counts_in_two_decimals_by_default(self):
        from inventory.quantities import to_sap
        self.assertEqual(to_sap('0.033'), Decimal('0.03'))
        self.assertEqual(to_sap('0.035'), Decimal('0.04'))               # half up
        self.assertEqual(to_sap('12'), Decimal('12.00'))
        self.assertEqual(to_sap('0.1'), Decimal('0.10'))

    def test_the_setting_changes_it(self):
        from inventory.quantities import to_sap
        set_setting('sap.quantity_decimals', 3)
        self.assertEqual(to_sap('0.033'), Decimal('0.033'))

    def test_a_recipe_line_is_rounded_the_way_sap_counts_it(self):
        from services import recipes
        product = Product(name='Mask', product_code='M1', unit='pcs', pack_size=Decimal('300'), pack_unit='ml',
                          category='Consumable')
        entry = recipes._entry('M1', product, Decimal('10'), '', 'fixed', 'ml')       # 10 ml of a 300 ml pack
        self.assertEqual(entry['quantity'], '0.03')
        self.assertEqual(entry['used'], '10 ml')

    def test_whole_and_one_decimal_recipes_are_unchanged(self):
        from services import recipes
        product = Product(name='Towel', product_code='T1', unit='pcs', category='Technical')
        self.assertEqual(recipes._entry('T1', product, Decimal('1'), '', 'fixed')['quantity'], '1')
        self.assertEqual(recipes._entry('T1', product, Decimal('0.1'), '', 'fixed')['quantity'], '0.1')

    def test_an_amount_too_small_for_sap_to_count_is_refused_not_taken_as_zero(self):
        from rest_framework.exceptions import ValidationError
        from services import recipes
        product = Product(name='Oil', product_code='O1', unit='pcs', pack_size=Decimal('1000'), pack_unit='ml',
                          category='Consumable')
        with self.assertRaises(ValidationError) as caught:
            recipes._entry('O1', product, Decimal('3'), '', 'fixed', 'ml')               # 0.003 of a bottle
        self.assertIn('less than SAP can count', str(caught.exception.detail))
