"""Phase 5: approvals and notifications.

  * a switched-off policy changes nothing (the system behaves as before);
  * a discount / price change above the free limit needs an approver's yes, and
    the server checks the approved figures against the bill (F10-F13, D11);
  * the first decision wins; links are single-use and expire (D11);
  * an approval is spent by one bill only;
  * the bell, e-mail and WhatsApp (skipped while WhatsApp is empty).
"""

import datetime
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from approvals import service
from approvals.models import ApprovalApprover, ApprovalPolicy, ApprovalRequest, ApprovalToken
from billing.models import Invoice
from clients.models import Client
from notifications import integrations
from notifications.models import DeliveryLog, Notification
from pos_backend import app_settings, jobs
from pos_backend.models import AppSetting
from pos_backend.testing import bill_and_pay, full_access_permissions
from salon_admin.models import Center, Role
from services.models import ServiceMaster
from staff.models import StaffMember


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
        self.other = Center.objects.create(center_name='Noida', display_name='Noida')
        owner = Role.objects.create(name='Owner', permissions=full_access_permissions())
        cashier_role = Role.objects.create(name='Cashier', permissions={
            'billing': {'new_invoice': {'read': True, 'create': True, 'update': True}}})
        self.cashier = CustomUser.objects.create_user(
            email='cash@test.com', password='pw', full_name='Cashier One', role=cashier_role,
            center=self.center, phone='9810000001')
        self.cashier.centers.add(self.center)
        self.approver = CustomUser.objects.create_user(
            email='boss@test.com', password='pw', full_name='Manager', role=owner,
            center=self.center, phone='9810000002')
        self.admin = CustomUser.objects.create_superuser(
            email='root@test.com', password='pw', full_name='Root')
        self.api = APIClient()
        self.api.force_authenticate(self.cashier)
        self.boss = APIClient()
        self.boss.force_authenticate(self.approver)
        self.client_obj = Client.objects.create(first_name='Asha', phone='9990000001',
                                                center=self.center)
        self.svc = ServiceMaster.objects.create(name='Haircut', default_price=Decimal('1000'),
                                                tax_percentage=Decimal('5'))
        self.staff = StaffMember.objects.create(center=self.center, first_name='Ravi',
                                                designation='Stylist')

    def policy(self, kind, enabled=True, approvers=None):
        p, _ = ApprovalPolicy.objects.get_or_create(kind=kind)
        p.enabled = enabled
        p.save()
        for user, center in (approvers if approvers is not None else [(self.approver, None)]):
            ApprovalApprover.objects.get_or_create(policy=p, user=user, center=center)
        return p

    def line(self, price='1000.00', discount='0.00'):
        return {'content_type': 'services.servicemaster', 'object_id': self.svc.id,
                'description': 'Haircut', 'unit_price': price, 'discount': discount,
                'quantity': 1, 'staff_members': [self.staff.id]}

    def payload(self, *items, **extra):
        out = {'client': self.client_obj.id, 'center': self.center.id, 'status': 'paid',
               'discount': '0.00', 'items': list(items) or [self.line()]}
        out.update(extra)
        return out

    def bill(self, payload, user_api=None):
        return bill_and_pay(user_api or self.api, payload)


class PolicyOffTests(Base):
    def test_discounts_and_price_changes_work_as_before_when_no_policy_is_on(self):
        res = self.bill(self.payload(self.line(discount='500.00')))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(ApprovalRequest.objects.count(), 0)

    def test_a_disabled_policy_changes_nothing(self):
        self.policy('discount', enabled=False)
        self.assertEqual(self.bill(self.payload(self.line(discount='500.00'))).status_code, 201)

    def test_quote_lists_nothing_when_off(self):
        res = self.api.post('/billing/invoices/quote/', self.payload(self.line(discount='100.00')),
                            format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data['approvals_required'], [])


class DiscountApprovalTests(Base):
    def setUp(self):
        super().setUp()
        self.policy('discount')

    def test_a_discount_above_the_free_limit_is_refused_without_approval(self):
        res = self.bill(self.payload(self.line(discount='100.00')))
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data['code'], 'approval_required')
        self.assertEqual(res.data['required'][0]['kind'], 'discount')
        self.assertEqual(Invoice.objects.exclude(status='draft').count(), 0)

    def test_a_discount_within_the_free_limit_needs_nothing(self):
        AppSetting.objects.create(key='approvals.discount_free_percent', scope='global', value='15')
        app_settings.invalidate()
        res = self.bill(self.payload(self.line(discount='100.00')))        # 10%
        self.assertEqual(res.status_code, 201, res.data)

    def test_quote_says_what_is_needed(self):
        res = self.api.post('/billing/invoices/quote/', self.payload(self.line(discount='100.00')),
                            format='json')
        need = res.data['approvals_required'][0]
        self.assertEqual((need['kind'], need['amount'], need['percent']), ('discount', '100.00', '10.00'))

    def test_full_flow_ask_approve_bill(self):
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        self.assertEqual(asked.status_code, 201, asked.data)
        req_id = asked.data['requests'][0]['id']

        # the approver is told in the bell, by e-mail, and (WhatsApp is empty) skipped there
        self.assertTrue(Notification.objects.filter(user=self.approver, event='approval.requested').exists())
        run_jobs()
        self.assertTrue(any('/approve/' in m.body for m in mail.outbox))
        self.assertEqual(DeliveryLog.objects.get(channel='whatsapp').status, DeliveryLog.SKIPPED)

        # still pending: the bill is refused
        held = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(held.status_code, 409)

        decided = self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'},
                                 format='json')
        self.assertEqual(decided.status_code, 200, decided.data)
        self.assertTrue(Notification.objects.filter(user=self.cashier, event='approval.decided').exists())

        done = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(done.status_code, 201, done.data)
        req = ApprovalRequest.objects.get(pk=req_id)
        self.assertEqual(req.status, 'used')
        self.assertEqual(req.used_invoice_id, done.data['id'])

    def _approved(self, discount='100.00'):
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount=discount)), format='json')
        req_id = asked.data['requests'][0]['id']
        self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'}, format='json')
        return req_id

    def test_an_approval_covers_only_what_was_approved(self):
        req_id = self._approved('100.00')
        bigger = self.bill(self.payload(self.line(discount='300.00'), approval_ids=[req_id]))
        self.assertEqual(bigger.status_code, 409)
        smaller = self.bill(self.payload(self.line(discount='50.00'), approval_ids=[req_id]))
        self.assertEqual(smaller.status_code, 201, smaller.data)

    def test_an_approval_is_spent_by_one_bill_only(self):
        req_id = self._approved('100.00')
        first = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(first.status_code, 201)
        second = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(second.status_code, 409)

    def test_another_cashier_cannot_borrow_an_approval(self):
        req_id = self._approved('100.00')
        other = CustomUser.objects.create_user(
            email='c2@test.com', password='pw', full_name='Two', role=self.cashier.role,
            center=self.center)
        other.centers.add(self.center)
        api = APIClient()
        api.force_authenticate(other)
        res = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]), api)
        self.assertEqual(res.status_code, 409)

    def test_an_approval_lapses(self):
        req_id = self._approved('100.00')
        ApprovalRequest.objects.filter(pk=req_id).update(
            decided_at=timezone.now() - datetime.timedelta(minutes=61))
        res = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(res.status_code, 409)

    def test_a_held_bill_needs_no_approval_but_completing_it_does(self):
        held = self.api.post('/billing/invoices/', self.payload(self.line(discount='100.00'),
                                                                  status='draft'), format='json')
        self.assertEqual(held.status_code, 201, held.data)
        pay = self.api.post(f"/billing/invoices/{held.data['id']}/pay/",
                            {'payment_method': 'Cash', 'amount': held.data['total_amount']},
                            format='json')
        self.assertEqual(pay.status_code, 409, pay.data)

    def test_a_rejected_request_does_not_cover_the_bill(self):
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'reject'}, format='json')
        res = self.bill(self.payload(self.line(discount='100.00'), approval_ids=[req_id]))
        self.assertEqual(res.status_code, 409)

    def test_the_first_decision_wins(self):
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        second = CustomUser.objects.create_user(email='b2@test.com', password='pw',
                                                full_name='Second', role=self.approver.role,
                                                center=self.center)
        ApprovalApprover.objects.create(policy=ApprovalPolicy.objects.get(kind='discount'),
                                        user=second, center=None)
        api2 = APIClient()
        api2.force_authenticate(second)
        self.assertEqual(self.boss.post(f'/approvals/api/requests/{req_id}/decide/',
                                        {'action': 'approve'}, format='json').status_code, 200)
        late = api2.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'reject'}, format='json')
        self.assertEqual(late.status_code, 400)
        self.assertEqual(ApprovalRequest.objects.get(pk=req_id).status, 'approved')

    def test_only_an_approver_for_the_branch_can_decide(self):
        ApprovalApprover.objects.all().delete()
        ApprovalApprover.objects.create(policy=ApprovalPolicy.objects.get(kind='discount'),
                                        user=self.approver, center=self.other)
        # the only mapped approver covers Noida; Saket falls back to superusers
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        no = self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'}, format='json')
        self.assertEqual(no.status_code, 403)
        root = APIClient()
        root.force_authenticate(self.admin)
        self.assertEqual(root.post(f'/approvals/api/requests/{req_id}/decide/',
                                   {'action': 'approve'}, format='json').status_code, 200)

    def test_asking_twice_returns_the_same_request(self):
        a = self.api.post('/billing/invoices/request_approval/',
                          self.payload(self.line(discount='100.00')), format='json')
        b = self.api.post('/billing/invoices/request_approval/',
                          self.payload(self.line(discount='100.00')), format='json')
        self.assertEqual(a.data['requests'][0]['id'], b.data['requests'][0]['id'])

    def test_a_cashier_cannot_decide_their_own_request(self):
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        res = self.api.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 403)


class PriceChangeTests(Base):
    def test_selling_below_list_price_needs_approval_and_matches_the_price(self):
        self.policy('price_change')
        res = self.bill(self.payload(self.line(price='800.00')))
        self.assertEqual(res.status_code, 409, res.data)
        self.assertEqual(res.data['required'][0]['kind'], 'price_change')

        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(price='800.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'}, format='json')
        wrong = self.bill(self.payload(self.line(price='700.00'), approval_ids=[req_id]))
        self.assertEqual(wrong.status_code, 409)
        ok = self.bill(self.payload(self.line(price='800.00'), approval_ids=[req_id]))
        self.assertEqual(ok.status_code, 201, ok.data)

    def test_the_list_price_needs_nothing(self):
        self.policy('price_change')
        self.assertEqual(self.bill(self.payload(self.line())).status_code, 201)


class LinkTests(Base):
    def setUp(self):
        super().setUp()
        self.policy('discount')
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        self.req_id = asked.data['requests'][0]['id']
        run_jobs()
        body = next(m.body for m in mail.outbox if self.approver.email in m.to)
        self.raw = body.split('/approve/')[1].split()[0]
        self.anon = APIClient()

    def test_opening_the_link_changes_nothing(self):
        res = self.anon.get(f'/approvals/api/link/{self.raw}/')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(ApprovalRequest.objects.get(pk=self.req_id).status, 'pending')

    def test_the_link_decides_once(self):
        ok = self.anon.post(f'/approvals/api/link/{self.raw}/', {'action': 'approve'}, format='json')
        self.assertEqual(ok.status_code, 200, ok.data)
        req = ApprovalRequest.objects.get(pk=self.req_id)
        self.assertEqual((req.status, req.decided_via, req.decided_by_id), ('approved', 'link', self.approver.pk))
        again = self.anon.post(f'/approvals/api/link/{self.raw}/', {'action': 'reject'}, format='json')
        self.assertEqual(again.status_code, 400)

    def test_an_expired_link_is_refused(self):
        ApprovalToken.objects.update(expires_at=timezone.now() - datetime.timedelta(minutes=1))
        res = self.anon.post(f'/approvals/api/link/{self.raw}/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ApprovalRequest.objects.get(pk=self.req_id).status, 'pending')

    def test_a_wrong_link_is_refused(self):
        self.assertEqual(self.anon.get('/approvals/api/link/nonsense/').status_code, 400)

    def test_the_link_is_closed_once_someone_else_decides(self):
        self.boss.post(f'/approvals/api/requests/{self.req_id}/decide/', {'action': 'reject'}, format='json')
        res = self.anon.post(f'/approvals/api/link/{self.raw}/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ApprovalRequest.objects.get(pk=self.req_id).status, 'rejected')


class PolicyScreenTests(Base):
    def test_admin_switches_a_policy_on_and_maps_approvers(self):
        root = APIClient()
        root.force_authenticate(self.admin)
        res = root.put('/approvals/api/policies/discount/', {
            'enabled': True,
            'approvers': [{'user_id': self.approver.pk, 'center_id': self.center.pk},
                          {'user_id': self.approver.pk, 'center_id': self.other.pk}]}, format='json')
        self.assertEqual(res.status_code, 200, res.data)
        listing = root.get('/approvals/api/policies/').data['policies']
        discount = next(p for p in listing if p['kind'] == 'discount')
        self.assertTrue(discount['enabled'])
        self.assertEqual(len(discount['approvers']), 2)
        self.assertEqual({p['kind'] for p in listing}, {'discount', 'price_change'})

    def test_a_cashier_cannot_open_the_policy_screen(self):
        self.assertEqual(self.api.get('/approvals/api/policies/').status_code, 403)
        self.assertEqual(self.api.put('/approvals/api/policies/discount/', {'enabled': True},
                                      format='json').status_code, 403)

    def test_inbox_lists_only_what_the_user_may_decide(self):
        self.policy('discount')
        self.api.post('/billing/invoices/request_approval/',
                      self.payload(self.line(discount='100.00')), format='json')
        self.assertEqual(self.boss.get('/approvals/api/inbox/').data['pending_count'], 1)
        self.assertEqual(self.api.get('/approvals/api/inbox/').data['pending_count'], 0)

    def test_stale_requests_lapse(self):
        self.policy('discount')
        asked = self.api.post('/billing/invoices/request_approval/',
                              self.payload(self.line(discount='100.00')), format='json')
        req_id = asked.data['requests'][0]['id']
        ApprovalRequest.objects.filter(pk=req_id).update(
            created_at=timezone.now() - datetime.timedelta(minutes=31))
        res = self.boss.post(f'/approvals/api/requests/{req_id}/decide/', {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertEqual(ApprovalRequest.objects.get(pk=req_id).status, 'expired')


class NotificationTests(Base):
    def test_bell_lists_and_marks_read(self):
        Notification.objects.create(user=self.cashier, event='x', title='Hello')
        Notification.objects.create(user=self.approver, event='x', title='Not mine')
        res = self.api.get('/notifications/api/bell/')
        self.assertEqual((res.data['unread'], len(res.data['results'])), (1, 1))
        self.api.post('/notifications/api/bell/read/', {'all': True}, format='json')
        self.assertEqual(self.api.get('/notifications/api/bell/').data['unread'], 0)
        self.assertEqual(Notification.objects.get(user=self.approver).read_at, None)

    def test_secrets_are_encrypted_and_never_returned(self):
        root = APIClient()
        root.force_authenticate(self.admin)
        res = root.put('/notifications/api/integrations/whatsapp/', {
            'enabled': True, 'values': {'phone_number_id': '123', 'access_token': 'TOPSECRET'}},
            format='json')
        self.assertEqual(res.status_code, 200, res.data)
        shown = next(f for f in res.data['fields'] if f['name'] == 'access_token')
        self.assertEqual(shown['value'], integrations.MASK)
        from notifications.models import IntegrationConfig
        self.assertNotIn('TOPSECRET', IntegrationConfig.objects.get(key='whatsapp').blob)
        # saving the mask back keeps the stored secret
        root.put('/notifications/api/integrations/whatsapp/', {
            'enabled': True, 'values': {'phone_number_id': '456', 'access_token': integrations.MASK}},
            format='json')
        self.assertEqual(integrations.load('whatsapp')[1]['access_token'], 'TOPSECRET')

    def test_cannot_switch_on_with_missing_fields(self):
        root = APIClient()
        root.force_authenticate(self.admin)
        res = root.put('/notifications/api/integrations/whatsapp/', {'enabled': True, 'values': {}},
                       format='json')
        self.assertEqual(res.status_code, 400)

    def test_whatsapp_is_skipped_while_empty_and_sent_when_set(self):
        from notifications.service import notify
        notify('approval.requested', title='T', body='B', center=self.center, users=[self.approver])
        run_jobs()
        self.assertEqual(DeliveryLog.objects.get(channel='whatsapp').status, DeliveryLog.SKIPPED)

        integrations.save('whatsapp', True, {'phone_number_id': '1', 'access_token': 'tok'}, self.admin)
        DeliveryLog.objects.all().delete()
        with mock.patch.object(integrations, '_http') as http:
            http.return_value.post.return_value = mock.Mock(
                status_code=200, json=lambda: {'messages': [{'id': 'wamid.1'}]})
            notify('approval.requested', title='T', body='B', center=self.center, users=[self.approver])
            run_jobs()
        log = DeliveryLog.objects.get(channel='whatsapp')
        self.assertEqual((log.status, log.provider_ref), (DeliveryLog.SENT, 'wamid.1'))
        sent = http.return_value.post.call_args.kwargs['json']
        self.assertEqual(sent['to'], '919810000002')

    def test_a_rule_can_turn_a_channel_off_and_add_a_role(self):
        from notifications.models import NotificationRule
        from notifications.service import notify
        NotificationRule.objects.create(event='stock.request_raised', bell=True, email=False,
                                        whatsapp=False, role_ids=[self.approver.role_id])
        told = notify('stock.request_raised', title='Stock', body='Saket needs stock', center=self.center)
        self.assertEqual([u.pk for u in told], [self.approver.pk])
        NotificationRule.objects.filter(event='stock.request_raised').update(enabled=False)
        self.assertEqual(notify('stock.request_raised', title='x', body='y', center=self.center), [])
