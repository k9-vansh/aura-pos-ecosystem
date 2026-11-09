"""Marketing audit of 2026-10-29: one test per defect that was reproduced.

  * promotion rules were never checked (cashback 500%, none, negative flat price...);
  * a branch user could PATCH their item to `level=Organisation` or onto another branch;
  * a branch user could switch an offer off and never switch it back on;
  * an expired offer could not be edited, switched or deleted (404);
  * the usage report dropped finished / switched-off offers, and handed other
    branches' sales to a caller who asked for a branch that is not theirs;
  * the WhatsApp log could be forged and deleted through the API;
  * a campaign could be sent twice by a double click, ran inside the web request,
    and the screen said "sent" when nothing had been;
  * cards, memberships and packages were accepted with no price.
"""

import datetime
from decimal import Decimal
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from billing.models import Invoice, InvoiceItem
from clients.models import Client
from marketing import campaigns
from marketing.models import (
    Membership, Package, Promotion, PromotionUsage, ValueCard, WhatsAppCampaign, WhatsAppMessage)
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster

SUBS = ['promotions', 'cards', 'memberships', 'packages', 'whatsapp', 'usage']
ALL = {'read': True, 'create': True, 'update': True, 'delete': True}


def day(offset=0):
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


class Base(TestCase):
    def setUp(self):
        self.a = Center.objects.create(center_name='A', display_name='A')
        self.b = Center.objects.create(center_name='B', display_name='B')
        owner_role = Role.objects.create(name='Boss', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='boss@t.com', password='StrongPass!234', full_name='Boss', role=owner_role, center=self.a)
        role = Role.objects.create(name='Mgr', permissions={'marketing': {s: dict(ALL) for s in SUBS}})
        self.mgr = CustomUser.objects.create_user(
            email='m@t.com', password='StrongPass!234', full_name='M', role=role, center=self.a)
        self.loner = CustomUser.objects.create_user(
            email='n@t.com', password='StrongPass!234', full_name='N', role=role)
        self.svc = ServiceMaster.objects.create(
            name='Cut', default_price=Decimal('500'), tax_percentage=Decimal('18'))
        self.svc2 = ServiceMaster.objects.create(
            name='Spa', default_price=Decimal('900'), tax_percentage=Decimal('18'))
        self.boss_api = self.login(self.owner)
        self.api = self.login(self.mgr)

    @staticmethod
    def login(user):
        api = APIClient()
        api.force_authenticate(user)
        return api

    def promo(self, **extra):
        body = dict(name='P', start_date=day(), end_date=day(5), level='Organisation',
                    promo_type='Discount', discount_type='Percentage', discount_value='10', config={})
        body.update(extra)
        return self.boss_api.post('/marketing/api/promotions/', body, format='json')


class PromotionRulesTests(Base):
    def test_cashback_needs_a_sensible_percentage(self):
        for config in ({}, {'cashback_discount': 500}, {'cashback_discount': 0}, {'cashback_discount': 'abc'}):
            r = self.promo(promo_type='Cashback', discount_value=None, config=config)
            self.assertEqual(r.status_code, 400, config)
            self.assertIn('cashback_discount', r.data['config'])
        r = self.promo(promo_type='Cashback', discount_value=None, config={'cashback_discount': 5})
        self.assertEqual(r.status_code, 201, r.data)

    def test_cashback_other_numbers_are_checked(self):
        for extra in ({'cashback_max': -1}, {'cashback_min_bill': 'x'}, {'cashback_validity_days': 1.5},
                      {'cashback_specific_date': 'not-a-date'}):
            r = self.promo(promo_type='Cashback', discount_value=None, config={'cashback_discount': 5, **extra})
            self.assertEqual(r.status_code, 400, extra)

    def test_trigger_needs_two_real_services_and_a_percentage(self):
        good = {'trigger_services': ['Cut', 'Spa'], 'trigger_discount': 10}
        self.assertEqual(self.promo(promo_type='Trigger', discount_value=None, config=good).status_code, 201)
        for bad in ({'trigger_services': ['Cut', '']}, {'trigger_services': ['Cut', 'Nope']},
                    {'trigger_discount': 0}, {'trigger_discount': 900}):
            r = self.promo(promo_type='Trigger', discount_value=None, config={**good, **bad})
            self.assertEqual(r.status_code, 400, bad)

    def test_flat_price_rules(self):
        self.assertEqual(self.promo(promo_type='FlatPrice', discount_value=None,
                                    config={'flat_price': 99}).status_code, 201)
        for config in ({}, {'flat_price': -5, 'specific_service': 'Cut'},
                       {'flat_price': 0}, {'flat_price': 50, 'specific_service': 'Nope'}):
            r = self.promo(promo_type='FlatPrice', discount_value=None, config=config)
            self.assertEqual(r.status_code, 400, config)
        # A service priced at 0 is a real offer ("this one is free").
        self.assertEqual(self.promo(promo_type='FlatPrice', discount_value=None,
                                    config={'flat_price': 0, 'specific_service': 'Cut'}).status_code, 201)

    def test_a_discount_for_a_missing_service_is_refused(self):
        config = {'target': 'Specific Service', 'specific_service': 'Nope'}
        self.assertEqual(self.promo(config=config).status_code, 400)
        config['specific_service'] = 'cut'
        self.assertEqual(self.promo(config=config).status_code, 201)

    def test_limits_and_shape(self):
        self.assertEqual(self.promo(max_usage_per_client=0).status_code, 400)
        self.assertEqual(self.promo(max_usage_per_client=1).status_code, 201)
        self.assertEqual(self.promo(config='junk').status_code, 400)
        self.assertEqual(self.promo(config={'min_bill_amount': 'abc'}).status_code, 400)
        self.assertEqual(self.promo(config={'min_bill_amount': -5}).status_code, 400)

    def test_editing_other_fields_does_not_recheck_old_terms(self):
        promo = Promotion.objects.create(
            name='Old', promo_type='Cashback', level='Organisation', config={},
            start_date=datetime.date.today(), end_date=datetime.date.today() + datetime.timedelta(days=3))
        r = self.boss_api.patch(f'/marketing/api/promotions/{promo.id}/', {'is_active': False}, format='json')
        self.assertEqual(r.status_code, 200, r.data)


class BranchBoundaryTests(Base):
    def make_pkg(self, api=None):
        r = (api or self.api).post('/marketing/api/packages/', {
            'name': 'Mine', 'price': '100', 'validity_days': 10, 'level': 'Center',
            'services_json': [{'id': self.svc.id, 'pkgQty': 1}]}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        return r.data['id']

    def test_a_branch_user_cannot_make_their_item_organisation_wide(self):
        pid = self.make_pkg()
        r = self.api.patch(f'/marketing/api/packages/{pid}/', {'level': 'Organisation'}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(Package.objects.get(pk=pid).level, 'Center')

    def test_a_branch_user_cannot_move_their_item_to_another_branch(self):
        pid = self.make_pkg()
        r = self.api.patch(f'/marketing/api/packages/{pid}/', {'center': self.b.id}, format='json')
        self.assertEqual(r.status_code, 403, r.data)
        self.assertEqual(Package.objects.get(pk=pid).center_id, self.a.id)

    def test_a_branch_user_cannot_create_at_another_branch(self):
        r = self.api.post('/marketing/api/packages/', {
            'name': 'Sneaky', 'price': '100', 'validity_days': 10, 'level': 'Center', 'center': self.b.id,
            'services_json': [{'id': self.svc.id, 'pkgQty': 1}]}, format='json')
        self.assertEqual(r.status_code, 403, r.data)

    def test_a_branch_user_still_edits_their_own_item(self):
        pid = self.make_pkg()
        r = self.api.patch(f'/marketing/api/packages/{pid}/',
                           {'description': 'new', 'level': 'Center', 'center': self.a.id}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_an_owner_must_name_a_branch_for_a_branch_item(self):
        r = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'X', 'pre_tax_price': '100', 'gst_percent': '18', 'expiry_days': 30,
            'level': 'Center'}, format='json')
        self.assertEqual(r.status_code, 400, r.data)

    def test_organisation_items_have_no_branch(self):
        r = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'X', 'pre_tax_price': '100', 'gst_percent': '18', 'expiry_days': 30,
            'level': 'Organisation', 'center': self.b.id}, format='json')
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIsNone(Membership.objects.get().center)

    def test_a_branch_user_can_switch_their_item_back_on(self):
        pid = self.make_pkg()
        url = f'/marketing/api/packages/{pid}/toggle-status/'
        self.assertEqual(self.api.patch(url, {}, format='json').data['is_active'], False)
        self.assertEqual(self.api.patch(url, {}, format='json').data['is_active'], True)

    def test_a_branch_user_sees_their_switched_off_items_when_asking(self):
        pid = self.make_pkg()
        self.api.patch(f'/marketing/api/packages/{pid}/toggle-status/', {}, format='json')
        self.assertEqual(len(self.api.get('/marketing/api/packages/').data), 0)
        self.assertEqual(len(self.api.get('/marketing/api/packages/?show_inactive=true').data), 1)

    def test_a_branch_user_cannot_switch_an_organisation_item(self):
        pkg = Package.objects.create(name='Org', price=Decimal('10'), validity_days=5, level='Organisation')
        r = self.api.patch(f'/marketing/api/packages/{pkg.id}/toggle-status/', {}, format='json')
        self.assertEqual(r.status_code, 403)

    def test_a_branch_user_cannot_touch_another_branchs_item(self):
        pkg = Package.objects.create(name='B only', price=Decimal('10'), validity_days=5,
                                     level='Center', center=self.b)
        self.assertEqual(self.api.patch(f'/marketing/api/packages/{pkg.id}/',
                                        {'description': 'x'}, format='json').status_code, 404)


class ExpiredOfferTests(Base):
    def expired(self, **extra):
        return Promotion.objects.create(
            name='Old', start_date=datetime.date.today() - datetime.timedelta(days=20),
            end_date=datetime.date.today() - datetime.timedelta(days=5), promo_type='Discount',
            discount_type='Percentage', discount_value=10, level='Organisation', **extra)

    def test_an_expired_offer_can_be_extended(self):
        promo = self.expired()
        r = self.boss_api.patch(f'/marketing/api/promotions/{promo.id}/', {'end_date': day(30)}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(self.boss_api.get('/marketing/api/promotions/').data[0]['id'], promo.id)

    def test_an_expired_offer_can_be_switched_and_deleted(self):
        promo = self.expired()
        self.assertEqual(self.boss_api.patch(f'/marketing/api/promotions/{promo.id}/toggle-status/',
                                             {}, format='json').status_code, 200)
        self.assertEqual(self.boss_api.delete(f'/marketing/api/promotions/{promo.id}/').status_code, 204)

    def test_the_list_still_hides_finished_offers_unless_asked(self):
        self.expired()
        self.assertEqual(self.boss_api.get('/marketing/api/promotions/').data, [])
        self.assertEqual(len(self.boss_api.get('/marketing/api/promotions/?show_expired=true').data), 1)

    def test_an_offer_that_was_used_is_not_deleted(self):
        promo = self.expired()
        PromotionUsage.objects.create(promotion=promo, center=self.a, bill_amount_before=100,
                                      bill_amount_after=90, revenue_generated=90)
        r = self.boss_api.delete(f'/marketing/api/promotions/{promo.id}/')
        self.assertEqual(r.status_code, 409, r.data)
        self.assertTrue(Promotion.objects.filter(pk=promo.id).exists())


class UsageReportTests(Base):
    URL = '/marketing/api/promotions/usage_report/'

    def sell_at(self, perk, center, price=100):
        client = Client.objects.create(first_name='X', phone=f'99{perk.pk:08d}', center=center)
        inv = Invoice.objects.create(client=client, center=center, status=Invoice.STATUS_PAID,
                                     subtotal=Decimal(price), total_amount=Decimal(price))
        InvoiceItem.objects.create(
            invoice=inv, content_type=ContentType.objects.get_for_model(perk), object_id=perk.id,
            description='x', unit_price=price, quantity=1, total_price=price)

    def rows(self, api, **params):
        r = api.get(self.URL, params)
        self.assertEqual(r.status_code, 200, r.data)
        return {x['id']: x for x in r.data}

    def test_asking_for_a_branch_that_is_not_yours_returns_no_sales(self):
        pkg = Package.objects.create(name='Org', price=Decimal('100'), validity_days=5, level='Organisation')
        self.sell_at(pkg, self.b)
        mine = self.rows(self.api)[f'package_{pkg.id}']
        self.assertEqual((mine['sold'], mine['revenue']), (0, 0))
        other = self.rows(self.api, center_id=self.b.id)[f'package_{pkg.id}']
        self.assertEqual((other['sold'], other['revenue']), (0, 0))
        # ...while the owner sees it.
        self.assertEqual(self.rows(self.boss_api)[f'package_{pkg.id}']['sold'], 1)

    def test_a_user_with_no_branch_sees_no_sales(self):
        pkg = Package.objects.create(name='Org', price=Decimal('100'), validity_days=5, level='Organisation')
        self.sell_at(pkg, self.b)
        row = self.rows(self.login(self.loner))[f'package_{pkg.id}']
        self.assertEqual((row['sold'], row['revenue']), (0, 0))

    def test_promotion_usage_is_branch_scoped_too(self):
        promo = Promotion.objects.create(
            name='Org', start_date=datetime.date.today(), end_date=datetime.date.today(),
            promo_type='Discount', discount_value=10, level='Organisation')
        PromotionUsage.objects.create(promotion=promo, center=self.b, bill_amount_before=100,
                                      bill_amount_after=90, revenue_generated=90)
        self.assertEqual(self.rows(self.api, center_id=self.b.id)[f'promo_{promo.id}']['used'], 0)
        self.assertEqual(self.rows(self.boss_api)[f'promo_{promo.id}']['used'], 1)

    def test_finished_and_switched_off_offers_still_report(self):
        old = Promotion.objects.create(
            name='LastMonth', start_date=datetime.date.today() - datetime.timedelta(days=40),
            end_date=datetime.date.today() - datetime.timedelta(days=5), promo_type='Discount',
            discount_value=10, level='Organisation')
        off = Promotion.objects.create(
            name='Off', start_date=datetime.date.today() - datetime.timedelta(days=40),
            end_date=datetime.date.today() + datetime.timedelta(days=30), promo_type='Discount',
            discount_value=10, level='Organisation', is_active=False)
        for promo in (old, off):
            PromotionUsage.objects.create(promotion=promo, center=self.a, bill_amount_before=100,
                                          bill_amount_after=90, revenue_generated=90)
        for api in (self.api, self.boss_api):
            rows = self.rows(api, start_date=day(-40), end_date=day(0))
            self.assertEqual(rows[f'promo_{old.id}']['used'], 1)
            self.assertEqual(rows[f'promo_{old.id}']['status'], 'Expired')
            self.assertEqual(rows[f'promo_{off.id}']['used'], 1)
            self.assertEqual(rows[f'promo_{off.id}']['status'], 'Inactive')


class WhatsAppLogTests(Base):
    URL = '/marketing/api/whatsapp/'

    def test_the_log_cannot_be_written_through_the_api(self):
        for api in (self.api, self.boss_api):
            r = api.post(self.URL, {'center': self.a.id, 'date': day(), 'time': '10:00:00', 'client_name': 'Fake',
                                    'client_phone': '1', 'message': 'hi', 'status': 'Delivered'}, format='json')
            self.assertEqual(r.status_code, 405)
        row = WhatsAppMessage.objects.create(center=self.a, date=datetime.date.today(),
                                             time=datetime.time(10, 0), client_name='Real',
                                             client_phone='9', message='hi', status='Queued')
        for api in (self.api, self.boss_api):
            self.assertEqual(api.patch(f'{self.URL}{row.id}/', {'status': 'Delivered'}, format='json').status_code, 405)
            self.assertEqual(api.put(f'{self.URL}{row.id}/', {}, format='json').status_code, 405)
            self.assertEqual(api.delete(f'{self.URL}{row.id}/').status_code, 405)
            self.assertEqual(api.patch(f'{self.URL}{row.id}/toggle-status/', {}, format='json').status_code, 405)
        row.refresh_from_db()
        self.assertEqual(row.status, 'Queued')

    def test_the_log_can_still_be_read(self):
        WhatsAppMessage.objects.create(center=self.a, date=datetime.date.today(), time=datetime.time(10, 0),
                                       client_name='Real', client_phone='9', message='hi', status='Queued')
        self.assertEqual(len(self.boss_api.get(self.URL).data), 1)


class CampaignTests(Base):
    SEND = '/marketing/api/whatsapp/send_campaign/'
    PREVIEW = '/marketing/api/whatsapp/campaign_preview/'

    def customer(self, name='Asha', phone='9876500001', center=None, **extra):
        return Client.objects.create(first_name=name, phone=phone, center=center or self.a, **extra)

    def test_preview_counts_without_sending(self):
        self.customer()
        self.customer('Bina', '9876500002', whatsapp_consent=False)
        self.customer('Cara', '9876500003', dnd_status=' on dnd ')
        self.customer('Dev', '9876500004', is_blacklisted=True)
        r = self.boss_api.post(self.PREVIEW, {'center_id': 'all', 'message': 'Hi'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual((r.data['eligible'], r.data['skipped_no_consent'], r.data['skipped_dnd']), (1, 1, 1))
        self.assertFalse(r.data['background'])
        self.assertEqual(WhatsAppMessage.objects.count(), 0)

    def test_dnd_with_stray_spaces_or_case_is_still_dnd(self):
        self.customer(dnd_status='  On Dnd ')
        self.customer('Bina', '9876500002', dnd_status='NOT ON DND')
        r = self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertEqual(r.data['count'], 1)
        self.assertEqual(WhatsAppMessage.objects.get().client_name, 'Bina')

    def test_the_same_message_twice_needs_a_deliberate_yes(self):
        self.customer()
        first = self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertEqual(first.status_code, 200, first.data)
        again = self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertEqual(again.status_code, 409)
        self.assertTrue(again.data['duplicate'])
        self.assertEqual(WhatsAppMessage.objects.count(), 1)
        pre = self.boss_api.post(self.PREVIEW, {'message': 'Hi'}, format='json')
        self.assertTrue(pre.data['duplicate'])
        forced = self.boss_api.post(self.SEND, {'message': 'Hi', 'confirm_repeat': True}, format='json')
        self.assertEqual(forced.status_code, 200)
        self.assertEqual(WhatsAppMessage.objects.count(), 2)

    def test_a_different_message_or_audience_is_not_a_duplicate(self):
        self.customer()
        self.customer('Bina', '9876500002', center=self.b)
        self.assertEqual(self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json').status_code, 200)
        self.assertEqual(self.boss_api.post(self.SEND, {'message': 'Hello'}, format='json').status_code, 200)
        self.assertEqual(self.boss_api.post(self.SEND, {'message': 'Hi', 'center_id': self.b.id},
                                            format='json').status_code, 200)

    def test_name_is_filled_in_per_customer(self):
        self.customer('Asha')
        self.customer('', '9876500002')
        self.boss_api.post(self.SEND, {'message': 'Hi {name}, 20% off'}, format='json')
        self.assertEqual(sorted(WhatsAppMessage.objects.values_list('message', flat=True)),
                         ['Hi Asha, 20% off', 'Hi there, 20% off'])

    def test_a_big_audience_is_prepared_by_a_worker_and_never_twice(self):
        for i in range(3):
            self.customer(f'C{i}', f'98765000{i:02d}')
        with mock.patch.object(campaigns, 'INLINE_LIMIT', 1):
            r = self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data['background'])
        self.assertEqual(r.data['count'], 3)
        self.assertIn('background', r.data['message'])
        self.assertEqual(WhatsAppMessage.objects.count(), 0)          # nothing in the request
        from pos_backend.models import BackgroundJob
        job = BackgroundJob.objects.get(kind='marketing.send_campaign')

        from marketing.background_jobs import send_campaign
        send_campaign(job.payload, job)
        self.assertEqual(WhatsAppMessage.objects.count(), 3)
        send_campaign(job.payload, job)                               # a retry after a crash
        self.assertEqual(WhatsAppMessage.objects.count(), 3)
        self.assertEqual(WhatsAppCampaign.objects.get().status, 'Done')

    def test_the_reply_says_when_nothing_was_transmitted(self):
        self.customer()
        r = self.boss_api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertFalse(r.data['gateway_configured'])
        self.assertIn('nothing was transmitted', r.data['message'])

    def test_a_branch_user_only_reaches_their_branch(self):
        self.customer()
        self.customer('Bina', '9876500002', center=self.b)
        r = self.api.post(self.SEND, {'message': 'Hi'}, format='json')
        self.assertEqual(r.data['count'], 1)
        self.assertEqual(self.api.post(self.SEND, {'message': 'Hey', 'center_id': self.b.id},
                                       format='json').status_code, 404)

    def test_a_user_with_no_branch_cannot_send(self):
        self.assertEqual(self.login(self.loner).post(self.SEND, {'message': 'Hi'}, format='json').status_code, 403)


class PerkPriceTests(Base):
    def test_a_membership_needs_a_price(self):
        r = self.boss_api.post('/marketing/api/memberships/', {'name': 'M', 'expiry_days': 30,
                                                                 'level': 'Organisation'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('pre_tax_price', r.data)

    def test_only_a_tax_inclusive_price_is_enough(self):
        r = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'M', 'post_tax_price': '1180', 'gst_percent': '18', 'expiry_days': 30,
            'level': 'Organisation'}, format='json')
        self.assertEqual(r.status_code, 201, r.data)

    def test_price_tax_and_total_must_agree(self):
        r = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'M', 'pre_tax_price': '1000', 'post_tax_price': '5000', 'gst_percent': '18',
            'expiry_days': 30, 'level': 'Organisation'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('post_tax_price', r.data)
        ok = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'M', 'pre_tax_price': '1000', 'post_tax_price': '1180', 'gst_percent': '18',
            'expiry_days': 30, 'level': 'Organisation'}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_a_card_needs_price_and_value(self):
        url = '/marketing/api/cards/'
        base = {'title': 'C', 'expiry_days': 30, 'level': 'Organisation'}
        self.assertEqual(self.boss_api.post(url, base, format='json').status_code, 400)
        self.assertEqual(self.boss_api.post(url, {**base, 'pre_tax_price': '1000'}, format='json').status_code, 400)
        self.assertEqual(self.boss_api.post(url, {**base, 'value': '1000'}, format='json').status_code, 400)
        ok = self.boss_api.post(url, {**base, 'pre_tax_price': '1000', 'post_tax_price': '1180',
                                      'gst_percent': '18', 'value': '1200'}, format='json')
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_the_accountants_sheet_card_can_be_created(self):
        """Face Rs 20,000 sold at Rs 20,000 + 5% GST (the customer pays Rs 21,000): scenario 01."""
        url = '/marketing/api/cards/'
        sheet = {'title': 'Sheet', 'expiry_days': 365, 'level': 'Organisation', 'pre_tax_price': '20000',
                 'post_tax_price': '21000', 'gst_percent': '5', 'value': '20000'}
        self.assertEqual(self.boss_api.post(url, sheet, format='json').status_code, 201)
        # ...but a card worth less than its price before tax still loses the customer money.
        r = self.boss_api.post(url, {**sheet, 'title': 'Bad', 'value': '19000'}, format='json')
        self.assertEqual(r.status_code, 400)
        self.assertIn('value', r.data)

    def test_a_package_needs_a_price_and_a_service(self):
        url = '/marketing/api/packages/'
        base = {'name': 'P', 'validity_days': 30, 'level': 'Organisation'}
        svc = [{'id': self.svc.id, 'pkgQty': 2}]
        self.assertEqual(self.boss_api.post(url, {**base, 'price': '0', 'services_json': svc}, format='json').status_code, 400)
        self.assertEqual(self.boss_api.post(url, {**base, 'price': '500'}, format='json').status_code, 400)
        self.assertEqual(self.boss_api.post(url, {**base, 'price': '500', 'services_json': []}, format='json').status_code, 400)
        self.assertEqual(self.boss_api.post(url, {**base, 'price': '500', 'services_json': svc}, format='json').status_code, 201)

    def test_an_old_single_service_package_can_still_be_edited(self):
        pkg = Package.objects.create(name='Old', price=Decimal('100'), validity_days=5, service_name='Cut',
                                     level='Organisation')
        r = self.boss_api.patch(f'/marketing/api/packages/{pkg.id}/', {'description': 'x'}, format='json')
        self.assertEqual(r.status_code, 200, r.data)

    def test_a_sold_perk_is_not_deleted_but_an_unsold_one_is(self):
        sold = Membership.objects.create(name='Sold', pre_tax_price=Decimal('100'), expiry_days=10,
                                         level='Organisation')
        fresh = Membership.objects.create(name='Fresh', pre_tax_price=Decimal('100'), expiry_days=10,
                                          level='Organisation')
        client = Client.objects.create(first_name='X', phone='9990000001', center=self.a)
        inv = Invoice.objects.create(client=client, center=self.a, status=Invoice.STATUS_PAID,
                                     subtotal=Decimal('100'), total_amount=Decimal('100'))
        InvoiceItem.objects.create(invoice=inv, content_type=ContentType.objects.get_for_model(sold),
                                   object_id=sold.id, description='x', unit_price=100, quantity=1, total_price=100)
        self.assertEqual(self.boss_api.delete(f'/marketing/api/memberships/{sold.id}/').status_code, 409)
        self.assertEqual(self.boss_api.delete(f'/marketing/api/memberships/{fresh.id}/').status_code, 204)

    def test_a_far_future_validity_is_refused(self):
        r = self.boss_api.post('/marketing/api/memberships/', {
            'name': 'M', 'pre_tax_price': '100', 'gst_percent': '18', 'expiry_days': 999999,
            'level': 'Organisation'}, format='json')
        self.assertEqual(r.status_code, 400)
