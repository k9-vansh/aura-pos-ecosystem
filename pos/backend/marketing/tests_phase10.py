"""Phase 10: marketing - multi-service packages with GST, automated reminders,
consent before any promotional message, campaigns through the WhatsApp channel."""

import datetime
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client, ClientMembership, ClientValueCard
from marketing import background_jobs as reminders
from marketing.models import Membership, Package, ValueCard, WhatsAppMessage
from notifications import integrations
from notifications.models import DeliveryLog
from pos_backend import app_settings, jobs
from pos_backend.models import AppSetting
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster

TODAY = datetime.date.today()


def run_jobs():
    for _ in range(5):
        if not jobs.process_due():
            break


class Base(TestCase):
    def setUp(self):
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.center = Center.objects.create(center_name='Saket Pvt', display_name='Saket',
                                            gst_number='07AAAAA0000A1Z5')
        role = Role.objects.create(name='Marketing', permissions=full_access_permissions())
        self.user = CustomUser.objects.create_user(email='m@test.com', password='pw',
                                                   full_name='Mkt', role=role, center=self.center)
        self.user.centers.add(self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.user)
        self.asha = Client.objects.create(first_name='Asha', phone='9990000001', center=self.center,
                                          email='asha@example.com')


class PackageTests(Base):
    def setUp(self):
        super().setUp()
        self.a = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('500'),
                                              tax_percentage=Decimal('5'))
        self.b = ServiceMaster.objects.create(name='Spa', default_price=Decimal('1500'),
                                              tax_percentage=Decimal('18'))

    def make(self, **extra):
        body = {'level': 'Organisation', 'name': 'Bridal', 'price': '3000.00', 'validity_days': 90,
                'services_json': [{'id': self.a.id, 'pkgQty': 3}, {'id': self.b.id, 'pkgQty': 2}]}
        body.update(extra)
        return self.api.post('/marketing/api/packages/', body, format='json')

    def test_a_package_holds_several_services_named_from_the_catalogue(self):
        res = self.make()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual([(s['name'], s['pkgQty']) for s in res.data['services_json']],
                         [('Haircut', 3), ('Spa', 2)])

    def test_bad_service_lists_are_refused(self):
        for services in ([{'id': self.a.id, 'pkgQty': 0}], [{'id': self.a.id, 'pkgQty': 1.5}],
                         [{'id': 99999, 'pkgQty': 1}],
                         [{'id': self.a.id, 'pkgQty': 1}, {'id': self.a.id, 'pkgQty': 1}]):
            self.assertEqual(self.make(services_json=services).status_code, 400, services)

    def test_a_package_can_carry_its_own_gst(self):
        from billing import pricing
        own = self.make(name='Own', gst_percent='12.00')
        plain = self.make(name='Plain')
        self.assertEqual(pricing.resolve('marketing.package', own.data['id']).tax_percentage, Decimal('12'))
        # no rate of its own: the business-wide setting, exactly as before
        self.assertEqual(pricing.resolve('marketing.package', plain.data['id']).tax_percentage, Decimal('0'))
        AppSetting.objects.create(key='billing.package_gst_percent', scope='global', value='5')
        app_settings.invalidate()
        self.assertEqual(pricing.resolve('marketing.package', plain.data['id']).tax_percentage, Decimal('5'))
        self.assertEqual(pricing.resolve('marketing.package', own.data['id']).tax_percentage, Decimal('12'))


class ExpiryReminderTests(Base):
    def setUp(self):
        super().setUp()
        vc = ValueCard.objects.create(title='Gold', pre_tax_price=1000, post_tax_price=1180, value=1500,
                                      expiry_days=365)
        self.card = ClientValueCard.objects.create(
            client=self.asha, value_card=vc, balance=Decimal('900'), center=self.center,
            expiry_date=TODAY + datetime.timedelta(days=20))
        ClientValueCard.objects.filter(pk=self.card.pk).update(
            created_at=datetime.datetime.now() - datetime.timedelta(days=40))

    def test_a_card_expiring_within_30_days_is_reminded_once(self):
        self.assertEqual(reminders.expiry_reminders(), 2)       # WhatsApp + e-mail
        run_jobs()
        self.assertTrue(any('Gold value card' in m.body and 'Rs 900' in m.body for m in mail.outbox))
        self.assertTrue(DeliveryLog.objects.filter(channel='whatsapp', status='skipped').exists())
        self.assertEqual(reminders.expiry_reminders(), 0)       # not twice

    def test_far_off_and_same_day_items_are_left_alone(self):
        ClientValueCard.objects.filter(pk=self.card.pk).update(
            expiry_date=TODAY + datetime.timedelta(days=90))
        self.assertEqual(reminders.expiry_reminders(), 0)
        ClientValueCard.objects.filter(pk=self.card.pk).update(
            expiry_date=TODAY + datetime.timedelta(days=10), created_at=datetime.datetime.now())
        self.assertEqual(reminders.expiry_reminders(), 0)

    def test_the_reminder_can_be_switched_off(self):
        AppSetting.objects.create(key='reminders.expiry_days', scope='global', value='0')
        app_settings.invalidate()
        self.assertEqual(reminders.expiry_reminders(), 0)

    def test_consent_and_dnd_are_respected(self):
        Client.objects.filter(pk=self.asha.pk).update(whatsapp_consent=False)
        reminders.expiry_reminders()
        self.assertFalse(DeliveryLog.objects.filter(channel='whatsapp').exists())
        self.assertTrue(DeliveryLog.objects.filter(channel='email').exists())

        DeliveryLog.objects.all().delete()
        Client.objects.filter(pk=self.asha.pk).update(whatsapp_consent=True, dnd_status='ON DND')
        reminders.expiry_reminders()
        self.assertFalse(DeliveryLog.objects.filter(channel='whatsapp').exists())

    def test_a_membership_is_reminded_too(self):
        plan = Membership.objects.create(name='VIP', expiry_days=365)
        m = ClientMembership.objects.create(client=self.asha, membership=plan, center=self.center,
                                            expiry_date=TODAY + datetime.timedelta(days=5))
        ClientMembership.objects.filter(pk=m.pk).update(
            created_at=datetime.datetime.now() - datetime.timedelta(days=60))
        reminders.expiry_reminders()
        self.assertTrue(DeliveryLog.objects.filter(dedupe_key=f'exp:membership:{m.pk}:email').exists())


class CampaignTests(Base):
    URL = '/marketing/api/whatsapp/send_campaign/'

    def test_a_client_who_has_not_consented_is_never_messaged(self):
        Client.objects.create(first_name='Bina', phone='9990000002', center=self.center,
                              whatsapp_consent=False)
        res = self.api.post(self.URL, {'message': 'Offer'}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['count'], 1)                     # Asha only
        self.assertEqual(res.data['skipped_no_consent'], 1)
        self.assertEqual(list(WhatsAppMessage.objects.values_list('client_name', flat=True)), ['Asha'])

    def test_no_credentials_means_nothing_is_sent(self):
        res = self.api.post(self.URL, {'message': 'Offer'}, format='json')
        self.assertFalse(res.data['gateway_configured'])
        self.assertEqual(WhatsAppMessage.objects.get().status, 'Not sent - WhatsApp not set up')
        self.assertFalse(DeliveryLog.objects.exists())

    def test_with_credentials_it_is_sent_and_the_row_settles(self):
        integrations.save('whatsapp', True, {'phone_number_id': '1', 'access_token': 't'}, self.user)
        with mock.patch.object(integrations, '_http') as http:
            http.return_value.post.return_value = mock.Mock(
                status_code=200, json=lambda: {'messages': [{'id': 'wamid.9'}]})
            res = self.api.post(self.URL, {'message': 'Offer'}, format='json')
            self.assertTrue(res.data['gateway_configured'])
            self.assertEqual(WhatsAppMessage.objects.get().status, 'Queued')
            run_jobs()
        self.assertEqual(WhatsAppMessage.objects.get().status, 'Sent')

    def test_a_role_name_no_longer_matters_only_the_tick(self):
        nobody = Role.objects.create(name='owner', permissions={'billing': {'invoices': {'read': True}}})
        user = CustomUser.objects.create_user(email='x@test.com', password='pw', full_name='X',
                                              role=nobody, center=self.center)
        api = APIClient()
        api.force_authenticate(user)
        self.assertEqual(api.post(self.URL, {'message': 'Offer'}, format='json').status_code, 403)


class WelcomeMailTests(Base):
    def test_the_welcome_goes_through_the_panels_mailbox_choice(self):
        with mock.patch.object(integrations, 'send_email') as send:
            from clients.background_jobs import welcome_email
            welcome_email({'client_id': self.asha.pk}, None)
        to, subject, body = send.call_args.args
        self.assertEqual(to, 'asha@example.com')
        self.assertIn('Saket', subject)
        self.assertIn('Hello Asha', body)
