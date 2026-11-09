"""Phase 2: the SAP pipeline.

Pins the blueprint's Phase 2 items so they cannot come back:
  * the outbox - a posting is a job written with the bill, worked by the
    background worker, waiting (not failing) while SAP is off or the invoice
    it depends on is not in SAP yet;
  * failures carry SAP's own message, give up into "needs attention" and come
    back when retried or when new work arrives for the same document;
  * X7 - no 12% -> 5% rewrite, no forced HSN, credit memo linked to its
    invoice, one card-code rule;
  * the SAP mapping in Settings drives the accounts;
  * the six legacy 'unknown' rows are mapped and held for review;
  * Admin > SAP Sync: who can see, retry and resolve what.

SAP is the FakeSap from tests_sap_integrity; nothing reaches the network.
"""

import importlib
from datetime import timedelta
from decimal import Decimal
from unittest import mock

import requests
from django.apps import apps as django_apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem, Payment
from clients.models import Client
from pos_backend import app_settings, jobs, sap_jobs, sap_utils
from pos_backend.models import AppSetting, BackgroundJob, SapPosting, SapSyncQueue
from pos_backend.tests_sap_integrity import FakeResponse, SapTestBase
from salon_admin.models import Center, Role
from services.models import ServiceMaster


def drain(limit=20):
    """Run the worker until nothing is due (bounded)."""
    for _ in range(limit):
        if not jobs.process_due():
            return


def make_due(job_ids=None):
    qs = BackgroundJob.objects.all() if job_ids is None else BackgroundJob.objects.filter(pk__in=job_ids)
    qs.update(run_after=timezone.now() - timedelta(seconds=1))


class Phase2Base(SapTestBase):
    def setUp(self):
        super().setUp()
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.service = ServiceMaster.objects.create(
            name='Haircut', default_price=Decimal('1000'), tax_percentage=Decimal('18'))
        self.service_ct = ContentType.objects.get_for_model(ServiceMaster)

    def bill(self, tax='18', client='b2c', **kw):
        kw.setdefault('sap_doc_entry', None)
        if client != 'b2c':
            kw['client'] = client  # None (walk-in) or a specific client
        inv = self.invoice(**kw)
        InvoiceItem.objects.create(
            invoice=inv, content_type=self.service_ct, object_id=self.service.id,
            description='Haircut', quantity=1, total_price=Decimal('1000'),
            tax_percentage=Decimal(tax))
        return inv


# ─── The outbox ───────────────────────────────────────────────────────────

class OutboxTests(Phase2Base):
    def test_a_posting_is_a_job_written_with_the_bill(self):
        inv = self.bill()
        with transaction.atomic():
            sap_utils.run_sap_sync_async(sap_utils.create_sap_invoice, inv)
            self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:invoice:{inv.id}').exists())
        self.assertEqual(self.sap.calls, [], 'nothing is sent from the request')

    def test_a_bill_that_rolls_back_leaves_no_posting(self):
        inv = self.bill()
        try:
            with transaction.atomic():
                sap_utils.run_sap_sync_async(sap_utils.create_sap_invoice, inv)
                raise RuntimeError('the bill failed to save')
        except RuntimeError:
            pass
        self.assertFalse(BackgroundJob.objects.filter(kind='sap.sync').exists())

    def test_the_worker_posts_the_invoice_then_its_payment(self):
        inv = self.bill()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        sap_jobs.enqueue('invoice', inv.id, obj=inv)
        drain()
        self.assertEqual(len(self.sap.posts('/Invoices')), 1)
        self.assertEqual(len(self.sap.posts('IncomingPayments')), 1)
        left = list(BackgroundJob.objects.exclude(status=BackgroundJob.DONE)
                    .values_list('payload', 'status', 'attempts', 'last_error', 'run_after'))
        self.assertEqual(left, [])
        # The monitor shows where each one went in SAP.
        inv.refresh_from_db()
        job = BackgroundJob.objects.get(payload__object_type='invoice', payload__object_id=inv.id)
        self.assertEqual(job.payload['sap_ref'], str(inv.sap_doc_entry))
        pay = BackgroundJob.objects.get(payload__object_type='incoming_payment', payload__object_id=inv.id)
        self.assertTrue(pay.payload['sap_ref'])

    def test_a_payment_waits_for_its_invoice_without_spending_attempts(self):
        inv = self.bill()
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        pay = sap_jobs.enqueue('incoming_payment', inv.id, obj=inv)
        jobs.process_due()
        pay.refresh_from_db()
        self.assertEqual((pay.status, pay.attempts), (BackgroundJob.FAILED, 0))
        self.assertIn('Waiting for the A/R invoice', pay.last_error)
        # ... and the invoice it waits for was queued.
        self.assertTrue(BackgroundJob.objects.filter(dedupe_key=f'sap:invoice:{inv.id}').exists())
        self.assertEqual(self.sap.posts('IncomingPayments'), [])
        drain()
        make_due([pay.pk])
        drain()
        self.assertEqual(len(self.sap.posts('IncomingPayments')), 1)

    def test_a_bill_that_posts_no_invoice_has_no_payment_to_post(self):
        """A bill selling only a value card posts journals, not an A/R invoice.
        Its payment used to fail forever looking for one."""
        inv = self.invoice(sap_doc_entry=None, sap_sync_status=True)
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        job = sap_jobs.enqueue('incoming_payment', inv.id, obj=inv)
        drain()
        job.refresh_from_db()
        self.assertEqual(job.status, BackgroundJob.DONE)
        self.assertEqual(self.sap.posts('IncomingPayments'), [])

    @override_settings(SAP_ENABLED=False)
    def test_with_sap_off_postings_wait_and_nothing_is_sent(self):
        inv = self.bill()
        job = sap_jobs.enqueue('invoice', inv.id, obj=inv)
        jobs.process_due()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (BackgroundJob.FAILED, 0))
        self.assertEqual(self.sap.calls, [])


class FailureTests(Phase2Base):
    def refuse(self, fragment, message):
        real = self.sap

        def send(method, url, timeout=15, **kw):
            if method == 'POST' and fragment in url:
                body = {'error': {'code': -5002, 'message': {'lang': 'en-us', 'value': message}}}
                raise requests.exceptions.HTTPError(response=FakeResponse(400, body))
            return real(method, url, timeout, **kw)

        patcher = mock.patch('pos_backend.sap_utils._send_sap_request', send)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_refusal_shows_sap_s_own_message(self):
        self.refuse('/Invoices', 'Tax code IGST@12 not defined')
        inv = self.bill(tax='12')
        job = sap_jobs.enqueue('invoice', inv.id, obj=inv)
        jobs.process_due()
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (BackgroundJob.FAILED, 1))
        self.assertIn('-5002: Tax code IGST@12 not defined', job.last_error)

    def test_it_gives_up_into_needs_attention_and_comes_back_on_new_work(self):
        self.refuse('/Invoices', 'Item has no HSN')
        inv = self.bill()
        job = sap_jobs.enqueue('invoice', inv.id, obj=inv)
        for _ in range(sap_jobs.MAX_ATTEMPTS):
            make_due([job.pk])
            jobs.process_due()
        job.refresh_from_db()
        self.assertEqual(job.status, BackgroundJob.DEAD)
        # Queuing the same document again revives it instead of being swallowed.
        sap_jobs.enqueue('invoice', inv.id, obj=inv)
        job.refresh_from_db()
        self.assertEqual((job.status, job.attempts), (BackgroundJob.QUEUED, 0))

    def test_a_payment_behind_a_dead_invoice_gives_up_too(self):
        inv = self.bill()
        dead = sap_jobs.enqueue('invoice', inv.id, obj=inv)
        BackgroundJob.objects.filter(pk=dead.pk).update(status=BackgroundJob.DEAD)
        pay = sap_jobs.enqueue('incoming_payment', inv.id, obj=inv)
        jobs.process_due()
        pay.refresh_from_db()
        self.assertEqual(pay.attempts, 1)
        self.assertIn('retry the invoice first', pay.last_error)


class RerunTests(TestCase):
    def test_new_work_while_a_job_runs_runs_it_once_more(self):
        """A payment added while the bill's payment job runs must not be missed."""
        seen = []

        @jobs.job('test.phase2.rerun')
        def handler(payload, row):
            seen.append(row.pk)
            if len(seen) == 1:
                # New work for the same key arrives mid-run.
                jobs.enqueue('test.phase2.rerun', {}, dedupe_key='rerun:1')

        jobs.enqueue('test.phase2.rerun', {}, dedupe_key='rerun:1')
        drain()
        self.assertEqual(len(seen), 2)
        self.assertEqual(BackgroundJob.objects.get(kind='test.phase2.rerun').status, BackgroundJob.DONE)


# ─── What is posted (X7) ──────────────────────────────────────────────────

class DocumentTests(Phase2Base):
    def invoice_payload(self, inv):
        self.assertTrue(sap_utils.create_sap_invoice(inv))
        (post,) = self.sap.posts('/Invoices')
        return post[2]

    def test_twelve_percent_is_posted_as_twelve_percent(self):
        """The bill charged 12%; SAP used to be told 5%."""
        line = self.invoice_payload(self.bill(tax='12'))['DocumentLines'][0]
        self.assertEqual(line['TaxCode'], 'CG+SG@12')

    def test_no_line_carries_a_forced_hsn(self):
        """HSNEntry 1 stamped one arbitrary HSN on every item of a GST invoice."""
        payload = self.invoice_payload(self.bill())
        self.assertTrue(all('HSNEntry' not in l for l in payload['DocumentLines']))

    def test_inter_state_b2b_is_igst(self):
        b2b = Client.objects.create(first_name='Acme', phone='9990000002', center=self.center,
                                    gst_number='29ABCDE1234F1Z5')
        payload = self.invoice_payload(self.bill(client=b2b))
        self.assertEqual(payload['CardCode'], f'C{b2b.id:06d}')
        self.assertEqual(payload['DocumentLines'][0]['TaxCode'], 'IGST@18')

    def test_credit_memo_lines_point_at_the_invoice_lines(self):
        inv = self.bill(status='cancelled', sap_doc_entry=500)
        item_code = f'S{self.service.id:06d}'
        self.sap.entities['Invoices(500)'] = {'DocumentStatus': 'bost_Open', 'DocumentLines': [
            {'ItemCode': item_code, 'LineNum': 0, 'TreeType': 'iSalesTree', 'AccountCode': '4130100018'},
            {'ItemCode': 'P1', 'LineNum': 1, 'TreeType': 'iIngredient', 'AccountCode': '9999'},
        ]}
        self.assertTrue(sap_utils.create_sap_credit_note(inv))
        (post,) = self.sap.posts('CreditNotes')
        line = post[2]['DocumentLines'][0]
        self.assertEqual((line['BaseType'], line['BaseEntry'], line['BaseLine']), (13, 500, 0))
        self.assertNotIn('UnitPrice', line)   # copied from the invoice line
        self.assertEqual(line['AccountCode'], '4130100018')
        self.assertEqual(len(post[2]['DocumentLines']), 1)  # ingredients are never credited

    def test_a_paid_invoice_s_credit_memo_is_unlinked_but_credits_the_sale_account(self):
        """Live SAP: a paid (closed) invoice cannot be a base document, and
        without an account SAP said 'G/L account is missing' - no credit memo
        had ever reached SAP."""
        inv = self.bill(status='cancelled', sap_doc_entry=500)
        self.sap.entities['Invoices(500)'] = {'DocumentStatus': 'bost_Close', 'DocumentLines': [
            {'ItemCode': f'S{self.service.id:06d}', 'LineNum': 0, 'AccountCode': '4130100018'}]}
        self.assertTrue(sap_utils.create_sap_credit_note(inv))
        (post,) = self.sap.posts('CreditNotes')
        line = post[2]['DocumentLines'][0]
        self.assertNotIn('BaseEntry', line)
        self.assertEqual((line['AccountCode'], line['TaxCode']), ('4130100018', 'CG+SG@18'))

    def test_a_returns_account_in_settings_is_used(self):
        AppSetting.objects.create(key='sap.account.sales_returns', scope='global', value='4130900001')
        app_settings.invalidate()
        inv = self.bill(status='cancelled', sap_doc_entry=500)
        self.sap.entities['Invoices(500)'] = {'DocumentStatus': 'bost_Close', 'DocumentLines': [
            {'ItemCode': f'S{self.service.id:06d}', 'LineNum': 0, 'AccountCode': '4130100018'}]}
        self.assertTrue(sap_utils.create_sap_credit_note(inv))
        (post,) = self.sap.posts('CreditNotes')
        self.assertEqual(post[2]['DocumentLines'][0]['AccountCode'], '4130900001')

    def test_a_credit_memo_line_sap_does_not_have_stays_unlinked(self):
        inv = self.bill(status='cancelled', sap_doc_entry=500)
        self.sap.entities['Invoices(500)'] = {'DocumentLines': []}
        self.assertTrue(sap_utils.create_sap_credit_note(inv))
        (post,) = self.sap.posts('CreditNotes')
        line = post[2]['DocumentLines'][0]
        self.assertNotIn('BaseEntry', line)
        self.assertEqual(line['TaxCode'], 'CG+SG@18')


class CardCodeTests(Phase2Base):
    def test_a_walk_in_bill_and_its_payment_go_to_the_same_partner(self):
        """The invoice went to WALKIN and its payment to B2C."""
        inv = self.bill(client=None)
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        sap_jobs.enqueue('invoice', inv.id, obj=inv)
        drain()
        (invoice_post,) = self.sap.posts('/Invoices')
        (payment_post,) = self.sap.posts('IncomingPayments')
        self.assertEqual(invoice_post[2]['CardCode'], 'B2C')
        self.assertEqual(payment_post[2]['CardCode'], 'B2C')

    def test_the_b2c_partner_comes_from_settings(self):
        AppSetting.objects.create(key='sap.b2c_card_code', scope='global', value='RETAIL01')
        app_settings.invalidate()
        self.assertEqual(sap_utils._resolve_sap_card_code(self.client_b2c), 'RETAIL01')
        self.assertEqual(sap_utils._resolve_sap_card_code(None), 'RETAIL01')

    def test_a_legacy_sap_code_is_kept(self):
        legacy = Client.objects.create(first_name='Old', phone='CUST00042', center=self.center)
        self.assertEqual(sap_utils._resolve_sap_card_code(legacy), 'CUST00042')


class MappingTests(Phase2Base):
    def test_an_untouched_system_posts_today_s_accounts(self):
        self.assertEqual(sap_utils._sap('sap.account.cash'), '1110100007')
        self.assertEqual(sap_utils._sap('sap.account.upi'), '1110100008')       # parking
        self.assertEqual(sap_utils._sap('sap.account.upi_bank'), '1110100009')  # bank
        self.assertEqual(sap_utils._sap('sap.account.advance_liability'), '2320100001')
        with self.assertRaises(KeyError):  # no fallback warehouse exists any more
            sap_utils._sap('sap.default_warehouse')

    def test_a_changed_account_is_what_gets_posted(self):
        AppSetting.objects.create(key='sap.account.cash', scope='global', value='1110299999')
        app_settings.invalidate()
        inv = self.bill(sap_doc_entry=500)
        SapPosting.objects.create(key=f'invoice:{inv.id}', status=SapPosting.DONE, doc_entry='500')
        Payment.objects.create(invoice=inv, amount=Decimal('1000'), payment_method='Cash')
        self.assertTrue(sap_utils.create_sap_incoming_payment(inv))
        (post,) = self.sap.posts('IncomingPayments')
        self.assertEqual(post[2]['CashAccount'], '1110299999')

    def test_a_malformed_account_is_refused(self):
        with self.assertRaises(app_settings.SettingError):
            app_settings.coerce(app_settings.BY_KEY['sap.account.cash'], '11102 0001; DROP')


class PagingTests(TestCase):
    """The Service Layer pages lists at 20 rows. Reading only page one missed
    business places after the 20th and re-created warehouse locations."""

    @override_settings(SAP_ENABLED=True)
    def test_every_page_is_read(self):
        pages = {
            'BusinessPlaces?$select=BPLID,BPLName': {
                'value': [{'BPLID': i, 'BPLName': f'B{i}'} for i in range(20)],
                'odata.nextLink': 'BusinessPlaces?$select=BPLID,BPLName&$skip=20'},
            'BusinessPlaces?$select=BPLID,BPLName&$skip=20': {
                'value': [{'BPLID': 20, 'BPLName': 'Guwahati'}]},
        }

        def send(method, url, timeout=15, **kw):
            return FakeResponse(200, pages[url.removeprefix('https://sap.test/')])

        with mock.patch('pos_backend.sap_utils._send_sap_request', send), \
                mock.patch('pos_backend.sap_utils.SAP_URL_BASE', 'https://sap.test'):
            rows = sap_utils._get_all('BusinessPlaces?$select=BPLID,BPLName')
        self.assertEqual(len(rows), 21)
        self.assertEqual(rows[-1]['BPLName'], 'Guwahati')


class SapErrorTextTests(TestCase):
    def test_reads_the_service_layer_error(self):
        body = '{"error": {"code": -5002, "message": {"lang": "en-us", "value": "No HSN"}}}'
        self.assertEqual(sap_utils._sap_error_text(body), '-5002: No HSN')

    def test_passes_anything_else_through(self):
        self.assertEqual(sap_utils._sap_error_text('<html>502</html>'), '<html>502</html>')


# ─── The legacy rows (X38) ────────────────────────────────────────────────

class LegacyQueueMigrationTests(Phase2Base):
    def migrate(self):
        module = importlib.import_module('pos_backend.migrations.0005_sap_outbox')
        module.forwards(django_apps, None)

    def test_unknown_rows_are_mapped_to_their_card_journal_and_held(self):
        inv = self.invoice(sap_doc_entry=None)
        ct = ContentType.objects.get(app_label='marketing', model='valuecard')
        item = InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=1, description='Card',
                                          total_price=Decimal('20000'), tax_amount=Decimal('1000'),
                                          tax_percentage=Decimal('5'))
        SapSyncQueue.objects.create(object_type='unknown', object_id=inv.id)
        SapSyncQueue.objects.create(object_type='invoice', object_id=inv.id)
        self.migrate()

        held = BackgroundJob.objects.get(dedupe_key=f'sap:marketing_je:{item.id}')
        self.assertEqual(held.status, BackgroundJob.DEAD)
        self.assertIn('Check SAP for it', held.last_error)
        # When released, SAP is asked before anything is posted.
        self.assertEqual(SapPosting.objects.get(key=f'je:sale:{item.id}').attempts, 1)
        self.assertEqual(BackgroundJob.objects.get(dedupe_key=f'sap:invoice:{inv.id}').status,
                         BackgroundJob.QUEUED)

    def test_a_released_legacy_journal_already_in_sap_is_not_posted_again(self):
        inv = self.invoice(sap_doc_entry=None)
        ct = ContentType.objects.get(app_label='marketing', model='valuecard')
        item = InvoiceItem.objects.create(invoice=inv, content_type=ct, object_id=1, description='Card',
                                          total_price=Decimal('20000'), tax_amount=Decimal('1000'),
                                          tax_percentage=Decimal('5'))
        SapSyncQueue.objects.create(object_type='unknown', object_id=inv.id)
        self.migrate()
        # SAP already holds a journal for that sale (the old reply was lost).
        self.sap.existing['JournalEntries'] = [{'JdtNum': 4242, 'JournalEntryLines': [
            {'Debit': 21000.0}, {'Debit': 0}]}]
        job = BackgroundJob.objects.get(dedupe_key=f'sap:marketing_je:{item.id}')
        jobs.revive(job.pk)
        drain()
        self.assertEqual(self.sap.posts('JournalEntries'), [])
        self.assertEqual(SapPosting.objects.get(key=f'je:sale:{item.id}').doc_entry, '4242')


# ─── Admin > SAP Sync ─────────────────────────────────────────────────────

class MonitorApiTests(Phase2Base):
    def setUp(self):
        super().setUp()
        self.other = Center.objects.create(center_name='Gurgaon', sap_warehouse_code='WH-GGN')
        self.n = 0
        here = self.bill()
        there = self.bill(center=self.other)
        self.here = sap_jobs.enqueue('invoice', here.id, obj=here)
        self.there = sap_jobs.enqueue('invoice', there.id, obj=there)
        BackgroundJob.objects.filter(pk__in=[self.here.pk, self.there.pk]).update(
            status=BackgroundJob.DEAD, attempts=10, last_error='SapPostingFailed: HTTP 400 - -5002: No HSN')

    def api(self, sub=None, actions=(), all_centers=False):
        self.n += 1
        perms = {'all_centers': all_centers}
        if sub:
            perms['admin'] = {'sap_sync': {'read': True, 'actions': {a: True for a in actions}}}
        role = Role.objects.create(name=f'R{self.n}', permissions=perms)
        user = CustomUser.objects.create_user(email=f'm{self.n}@t.com', password='StrongPass!234',
                                              full_name=f'M{self.n}', role=role, center=self.center)
        user.centers.add(self.center)
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_needs_the_sap_sync_permission(self):
        self.assertEqual(self.api().get('/sap-sync/api/').status_code, 403)
        self.assertEqual(self.api('sap_sync').get('/sap-sync/api/').status_code, 200)

    def test_a_branch_user_sees_only_their_branch_and_sap_s_message(self):
        data = self.api('sap_sync').get('/sap-sync/api/').json()
        self.assertEqual([r['id'] for r in data['results']], [self.here.pk])
        self.assertEqual(data['results'][0]['error'], 'HTTP 400 - -5002: No HSN')
        self.assertEqual(data['summary']['attention'], 1)

    def test_an_all_branch_user_sees_every_branch(self):
        data = self.api('sap_sync', all_centers=True).get('/sap-sync/api/').json()
        self.assertEqual({r['id'] for r in data['results']}, {self.here.pk, self.there.pk})

    def test_retry_needs_its_tick_and_brings_the_posting_back(self):
        url = f'/sap-sync/api/{self.here.pk}/retry/'
        self.assertEqual(self.api('sap_sync').post(url).status_code, 403)
        self.assertEqual(self.api('sap_sync', ['retry']).post(url).status_code, 200)
        self.here.refresh_from_db()
        self.assertEqual((self.here.status, self.here.attempts), (BackgroundJob.QUEUED, 0))

    def test_another_branch_s_posting_cannot_be_retried(self):
        response = self.api('sap_sync', ['retry']).post(f'/sap-sync/api/{self.there.pk}/retry/')
        self.assertEqual(response.status_code, 403)

    def test_resolve_records_what_was_done_and_stops_retrying(self):
        api = self.api('sap_sync', ['resolve'])
        url = f'/sap-sync/api/{self.here.pk}/resolve/'
        self.assertEqual(api.post(url, {}, format='json').status_code, 400)  # a note is required
        self.assertEqual(api.post(url, {'note': 'Posted by hand as 1234'}, format='json').status_code, 200)
        self.here.refresh_from_db()
        self.assertEqual(self.here.status, BackgroundJob.DONE)
        self.assertIn('Posted by hand as 1234', self.here.payload['resolved_note'])
        from audit_logs.models import SystemLog
        self.assertTrue(SystemLog.objects.filter(module='SAP', action='RESOLVE').exists())
