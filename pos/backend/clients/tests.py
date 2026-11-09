"""Phase 4: clients.

Pins the confirmed rules:
  * #11 global clients - found from any branch, from the start of the name/GSTIN or the start or end of the phone
    (any part when one centre is asked for);
  * F2 one client per phone across the business, the clash names the owner;
  * F3 name and phone required for new clients; Q1 legacy phone-less B2B
    records stay editable;
  * GSTIN validated (format, state code, check character);
  * the merge tool moves history, perks and wallets;
  * #12 blacklisting needs its own permission;
  * #3 the client app and its PIN are gone, and the PIN never leaks;
  * the welcome e-mail is a background job, switchable.
"""

import datetime
from decimal import Decimal

from django.contrib.auth.hashers import make_password
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import AdvancePayment, Invoice
from clients.identity import gstin_problem, normalise_phone
from clients.models import Client
from pos_backend import app_settings
from pos_backend.models import AppSetting, BackgroundJob
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

URL = '/clients/api/clients/'
VALID_GSTIN = '27AAACR5055K1Z7'


class ClientBase(TestCase):
    def setUp(self):
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.delhi = Center.objects.create(center_name='Delhi')
        self.noida = Center.objects.create(center_name='Noida')
        self.n = 0
        self.api = self.as_user(full_access_permissions())

    def as_user(self, perms, center=None):
        self.n += 1
        role = Role.objects.create(name=f'R{self.n}', permissions=perms)
        user = CustomUser.objects.create_user(email=f'u{self.n}@t.com', password='StrongPass!234',
                                              full_name=f'U{self.n}', role=role, center=center or self.delhi)
        user.centers.add(center or self.delhi)
        api = APIClient()
        api.force_authenticate(user)
        return api


class IdentityRulesTests(TestCase):
    def test_phones_are_normalised(self):
        for raw in ('+91 98765-43210', '09876543210', '98765 43210'):
            self.assertEqual(normalise_phone(raw), '9876543210')
        self.assertEqual(normalise_phone('cust00042'), 'CUST00042')     # legacy SAP code

    def test_gstin_rules(self):
        self.assertIsNone(gstin_problem(VALID_GSTIN))
        self.assertIsNone(gstin_problem(''))
        self.assertIn('check character', gstin_problem('27AAACR5055K1Z8'))
        self.assertIn('state code', gstin_problem('99AAACR5055K1Z7'))


class GlobalClientTests(ClientBase):
    def test_a_client_of_another_branch_is_found_from_this_one(self):
        """#11: global clients."""
        Client.objects.create(first_name='Asha', last_name='Rao', phone='9990000001', center=self.noida)
        branch_user = self.as_user({'billing': {'new_invoice': {'read': True, 'create': True}}})
        rows = branch_user.get(URL, {'q': 'rao'}).data
        self.assertEqual([r['first_name'] for r in rows], ['Asha'])

    def test_search_matches_the_start_of_name_gstin_or_the_start_or_end_of_phone(self):
        """Was "any part"; at 1+ crore customers that scanned the table (27-36 s), see clients/search.py and
        tests_search.py. One centre (center_id) still searches any part."""
        Client.objects.create(first_name='Meenakshi', last_name='Iyer', phone='9876543210',
                              center=self.delhi, gst_number=VALID_GSTIN)
        Client.objects.create(first_name='Ravi', phone='9123456789', center=self.delhi)
        for q in ('meena', '9876', '3210', 'iyer 987', '27AAAC'):
            with self.subTest(q):
                self.assertEqual([r['first_name'] for r in self.api.get(URL, {'q': q}).data],
                                 ['Meenakshi'])
        for q in ('aksh', '6543', 'AACR5055'):
            with self.subTest(q):
                self.assertEqual(self.api.get(URL, {'q': q}).data, [])
                self.assertEqual([r['first_name'] for r in self.api.get(URL, {'q': q, 'center_id': self.delhi.id}).data],
                                 ['Meenakshi'])

    def test_a_phone_used_at_another_branch_is_refused_and_named(self):
        """F2: a phone is unique across the business."""
        Client.objects.create(first_name='Asha', phone='9990000001', center=self.noida)
        response = self.api.post(URL, {'first_name': 'Other', 'phone': '+91 99900 00001',
                                       'center': self.delhi.id}, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('already used by Asha', str(response.data))

    def test_name_and_phone_are_required_for_a_new_client(self):
        self.assertEqual(self.api.post(URL, {'first_name': 'NoPhone', 'center': self.delhi.id},
                                       format='json').status_code, 400)

    def test_a_legacy_client_without_a_phone_stays_editable(self):
        """Q1: the 34,334 B2B records keep a blank phone."""
        legacy = Client.objects.create(first_name='Acme Traders', phone='', center=self.delhi)
        Client.objects.create(first_name='Beta Traders', phone='', center=self.delhi)  # blanks may repeat
        response = self.api.patch(f'{URL}{legacy.id}/', {'gst_number': VALID_GSTIN.lower()}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        legacy.refresh_from_db()
        self.assertEqual(legacy.gst_number, VALID_GSTIN)

    def test_an_invalid_gstin_is_refused(self):
        response = self.api.post(URL, {'first_name': 'Acme', 'phone': '9990000002',
                                       'gst_number': '27AAACR5055K1Z8', 'center': self.delhi.id},
                                 format='json')
        self.assertEqual(response.status_code, 400)

    def test_registering_an_archived_number_brings_the_client_back(self):
        old = Client.objects.create(first_name='Old', phone='9990000003', center=self.delhi,
                                    is_active=False)
        response = self.api.post(URL, {'first_name': 'Returning', 'phone': '9990000003',
                                       'center': self.delhi.id}, format='json')
        self.assertEqual(response.status_code, 201, response.data)
        old.refresh_from_db()
        self.assertTrue(old.is_active)
        self.assertEqual(old.first_name, 'Returning')

    def test_a_home_branch_must_be_one_the_user_works_at(self):
        branch_user = self.as_user({'admin': {'clients': {'read': True, 'create': True}}})
        response = branch_user.post(URL, {'first_name': 'X', 'phone': '9990000004',
                                          'center': self.noida.id}, format='json')
        self.assertEqual(response.status_code, 403)


class MergeTests(ClientBase):
    def test_merge_moves_everything_and_archives_the_duplicate(self):
        keep = Client.objects.create(first_name='Asha', phone='9990000010', center=self.delhi)
        dup = Client.objects.create(first_name='Asha R', phone='9990000011', center=self.delhi,
                                    email='asha@example.com')
        AdvancePayment.objects.create(client=dup, amount=Decimal('500'), notes='top up')
        bill = Invoice.objects.create(client=dup, center=self.delhi, status='paid',
                                      total_amount=Decimal('100'), paid_amount=Decimal('100'))
        response = self.api.post(f'{URL}{keep.id}/merge/', {'from_id': dup.id}, format='json')
        self.assertEqual(response.status_code, 200, response.data)
        bill.refresh_from_db()
        dup.refresh_from_db()
        keep.refresh_from_db()
        self.assertEqual(bill.client_id, keep.id)
        self.assertEqual(keep.advance_balance, Decimal('500.00'))
        self.assertEqual(keep.email, 'asha@example.com')       # filled where blank
        self.assertFalse(dup.is_active)
        self.assertIn(f'Merged into client #{keep.id}', dup.notes)

    def test_merge_needs_its_tick(self):
        keep = Client.objects.create(first_name='A', phone='9990000012', center=self.delhi)
        dup = Client.objects.create(first_name='B', phone='9990000013', center=self.delhi)
        clerk = self.as_user({'admin': {'clients': {'read': True, 'update': True}}})
        self.assertEqual(clerk.post(f'{URL}{keep.id}/merge/', {'from_id': dup.id},
                                    format='json').status_code, 403)

    def test_duplicates_lists_records_that_share_a_number(self):
        owner = Client.objects.create(first_name='First', phone='9990000014', center=self.delhi)
        extra = Client.objects.create(first_name='Second', phone='9990000099', center=self.delhi)
        # As the migration leaves an older duplicate: same phone, no key.
        Client.objects.filter(pk=extra.pk).update(phone='9990000014', phone_key=None)
        rows = self.api.get(f'{URL}duplicates/').data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['keeps_number']['id'], owner.id)
        self.assertEqual([d['id'] for d in rows[0]['duplicates']], [extra.id])


class BlacklistTests(ClientBase):
    def test_blacklisting_needs_its_tick(self):
        client = Client.objects.create(first_name='A', phone='9990000020', center=self.delhi)
        editor = self.as_user({'admin': {'clients': {'read': True, 'update': True}}})
        refused = editor.patch(f'{URL}{client.id}/', {'is_blacklisted': True}, format='json')
        self.assertEqual(refused.status_code, 400)
        allowed = self.api.patch(f'{URL}{client.id}/', {'is_blacklisted': True,
                                                        'blacklist_reason': 'abuse'}, format='json')
        self.assertEqual(allowed.status_code, 200, allowed.data)
        # Ordinary edits by the same editor still work.
        self.assertEqual(editor.patch(f'{URL}{client.id}/', {'notes': 'x'}, format='json').status_code, 200)


class AppGoneTests(ClientBase):
    def test_the_client_app_endpoints_are_gone(self):
        for path in ('/clients/api/app/login/', '/clients/api/app/data/', '/staff/api/app/login/'):
            with self.subTest(path):
                self.assertEqual(APIClient().post(path, {}, format='json').status_code, 404)

    def test_the_pin_hash_is_never_returned(self):
        client = Client.objects.create(first_name='A', phone='9990000030', center=self.delhi,
                                       app_pin=make_password('4729'))
        self.assertNotIn('app_pin', self.api.get(f'{URL}{client.id}/').data)
        self.assertNotIn('app_pin', self.api.get(URL).data[0])

    def test_creating_a_client_no_longer_makes_a_pin(self):
        response = self.api.post(URL, {'first_name': 'New', 'phone': '9990000031',
                                       'center': self.delhi.id}, format='json')
        self.assertFalse(Client.objects.get(pk=response.data['id']).app_pin)


class WelcomeEmailTests(ClientBase):
    def test_a_new_client_with_email_gets_a_welcome_job(self):
        response = self.api.post(URL, {'first_name': 'New', 'phone': '9990000040', 'email': 'n@x.com',
                                       'center': self.delhi.id}, format='json')
        self.assertTrue(BackgroundJob.objects.filter(kind='clients.welcome_email',
                                                     payload__client_id=response.data['id']).exists())

    def test_the_welcome_can_be_switched_off(self):
        AppSetting.objects.create(key='clients.welcome_email', scope='global', value=False)
        app_settings.invalidate()
        self.api.post(URL, {'first_name': 'New', 'phone': '9990000041', 'email': 'n@x.com',
                            'center': self.delhi.id}, format='json')
        self.assertFalse(BackgroundJob.objects.filter(kind='clients.welcome_email').exists())

    def test_the_welcome_is_signed_by_the_branch_not_chilli_potato(self):
        from django.core import mail
        from clients.background_jobs import welcome_email
        self.delhi.display_name = 'Geetanjali Saket'
        self.delhi.save()
        client = Client.objects.create(first_name='Asha', phone='9990000042', email='a@x.com',
                                       center=self.delhi)
        welcome_email({'client_id': client.id}, None)
        # The welcome now carries the client's PIN and the app link, and is queued like every client message.
        from notifications.models import DeliveryLog
        row = DeliveryLog.objects.get(event='client.welcome', client=client, channel='email')
        self.assertEqual(row.subject, 'Welcome to Geetanjali Saket: your PIN and app login')
        self.assertNotIn('Chilli', row.body)
        self.assertIn('Geetanjali Saket', row.body)
        self.assertIn('Your PIN: ', row.body)
        self.assertIn('cid:geetanjali-logo', row.html)
        self.assertEqual(mail.outbox, [])                      # nothing leaves until the delivery job runs

    def test_with_the_welcome_switched_off_the_pin_still_goes_by_whatsapp_but_not_by_e_mail(self):
        from clients import pins
        from notifications.models import DeliveryLog
        AppSetting.objects.create(key='clients.welcome_email', scope='global', value=False)
        app_settings.invalidate()
        client = Client.objects.create(first_name='Asha', phone='9990000043', email='a@x.com', center=self.delhi)
        pins.on_create(client)
        self.assertEqual(list(DeliveryLog.objects.filter(client=client).values_list('channel', flat=True)), ['whatsapp'])
