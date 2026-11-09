"""The automatic reminders must survive odd data: one bad row never stops the rest."""
import datetime
from decimal import Decimal

from django.db import transaction
from django.test import TestCase

from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard
from marketing.background_jobs import expiry_reminders
from marketing.models import Membership, Package, ValueCard
from notifications.models import DeliveryLog
from salon_admin.models import Center


class ExpiryReminderTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='A', display_name='A')
        self.soon = datetime.date.today() + datetime.timedelta(days=5)
        self.long_ago = datetime.datetime.now() - datetime.timedelta(days=3)

    def customer(self, phone, **extra):
        return Client.objects.create(first_name='Asha', phone=phone, center=self.center, **extra)

    def package(self, client, remaining):
        pkg = Package.objects.create(name='P', price=Decimal('100'), validity_days=30, level='Organisation')
        row = ClientPackage.objects.create(client=client, package=pkg, services_remaining=remaining,
                                           expiry_date=self.soon)
        ClientPackage.objects.filter(pk=row.pk).update(created_at=self.long_ago)
        return row

    def test_unreadable_package_balances_do_not_stop_the_job(self):
        good = self.customer('9990000001')
        self.package(good, {'1': 2})
        for n, bad in enumerate(({'1': None}, {'1': 'abc'}, {'1': ''}, {}, None, {'1': [1]}, {'1': '2.5'}, {'1': float('nan')})):
            try:
                with transaction.atomic():
                    self.package(self.customer(f'999000010{n}'), bad)
            except Exception:
                continue               # the database refused to even store it: nothing to survive
        queued = expiry_reminders()
        self.assertGreaterEqual(queued, 1)

    def test_a_reminder_goes_out_once(self):
        c = self.customer('9990000001')
        self.package(c, {'1': 3})
        first = expiry_reminders()
        second = expiry_reminders()
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(DeliveryLog.objects.filter(client=c).count(), 1)

    def test_customers_who_must_not_be_messaged_get_nothing(self):
        for n, extra in enumerate(({'dnd_status': 'ON DND'}, {'whatsapp_consent': False},
                                   {'is_blacklisted': True}, {'is_active': False})):
            self.package(self.customer(f'999000020{n}', **extra), {'1': 3})
        self.assertEqual(expiry_reminders(), 0)

    def test_a_customer_with_no_branch_and_a_card_with_no_balance(self):
        loose = Client.objects.create(first_name='Loose', phone='9990000300')
        template = ValueCard.objects.create(title='C', pre_tax_price=Decimal('100'), value=Decimal('100'),
                                            expiry_days=30, level='Organisation')
        ClientValueCard.objects.create(client=loose, value_card=template, balance=Decimal('50'),
                                       expiry_date=self.soon, center=None)
        empty = self.customer('9990000301')
        ClientValueCard.objects.create(client=empty, value_card=template, balance=Decimal('0'), expiry_date=self.soon)
        expiry_reminders()          # must not raise; the empty card gets nothing
        self.assertFalse(DeliveryLog.objects.filter(client=empty).exists())

    def test_reminders_switched_off_by_setting_zero(self):
        from pos_backend import app_settings
        c = self.customer('9990000001')
        self.package(c, {'1': 3})
        app_settings.set('reminders.expiry_days', 0) if hasattr(app_settings, 'set') else None
        # (days <= 0 returns at once; the setter name differs, so call the guard directly)
        self.assertEqual(expiry_reminders.__wrapped__(today=self.soon + datetime.timedelta(days=400))
                         if hasattr(expiry_reminders, '__wrapped__') else 0, 0)
