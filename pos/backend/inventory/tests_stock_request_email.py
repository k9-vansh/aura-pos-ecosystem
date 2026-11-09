"""The approver can decide a stock request from the link in their e-mail.

The link is the same single-use link the till approvals use (/approvals/api/link/<token>/),
but the decision is made by the stock request's own code, so the POS button and
the e-mail can never disagree. Who may decide is a permission tick, never a role.
"""

import re
from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import CustomUser
from approvals.models import ApprovalRequest, ApprovalToken
from approvals import service as approvals
from inventory.models import Product, StockRequest
from notifications.models import DeliveryLog, Notification
from pos_backend import app_settings
from salon_admin.models import Center, Role

D = Decimal
URL = '/inventory/api/stock_requests/'


def tick(**subs):
    return {'inventory': {k: {'read': True, 'create': True, 'update': True} for k in subs}}


class StockRequestEmailTests(TestCase):
    def setUp(self):
        app_settings.invalidate()
        self.addCleanup(app_settings.invalidate)
        self.branch = Center.objects.create(center_name='Saket', display_name='Saket', gst_number='07AAAAA0000A1Z5')
        self.other = Center.objects.create(center_name='Noida', display_name='Noida', gst_number='09CCCCC0000C1Z5')

        def user(email, perms, centers):
            u = CustomUser.objects.create_user(
                email=email, password='pw', full_name=email.split('@')[0],
                role=Role.objects.create(name=email, permissions=perms), center=centers[0])
            u.centers.add(*centers)
            api = APIClient()
            api.force_authenticate(u)
            return u, api

        self.clerk, self.clerk_api = user('clerk@t.com', tick(stock_requests=1), [self.branch])
        self.manager, self.manager_api = user('mgr@t.com', tick(approve_stock_requests=1), [self.branch, self.other])
        self.second, self.second_api = user('mgr2@t.com', tick(approve_stock_requests=1), [self.branch])
        self.elsewhere, _ = user('far@t.com', tick(approve_stock_requests=1), [self.other])
        self.shampoo = Product.objects.create(name='Shampoo', product_code='SH', category='Retail', price=D('100'),
                                              mrp=D('200'), current_stock=0, center=self.branch)
        self.anonymous = APIClient()

    def ask(self):
        res = self.clerk_api.post(URL, {'requesting_center': self.branch.id, 'stock_type': 'Retail',
                                        'items': [{'product': self.shampoo.id, 'quantity': '4'}]}, format='json')
        self.assertEqual(res.status_code, 201, res.data)
        return StockRequest.objects.get(pk=res.data['id'])

    def links(self):
        """{approver email: raw link secret}, read out of the queued e-mails."""
        out = {}
        for log in DeliveryLog.objects.filter(channel='email', event='stock.request_raised'):
            found = re.search(r'/approve/([A-Za-z0-9_-]+)', log.body)
            if found:
                out[log.recipient] = found.group(1)
        return out

    def test_asking_mails_each_approver_their_own_link(self):
        self.ask()
        links = self.links()
        self.assertEqual(set(links), {'mgr@t.com', 'mgr2@t.com'})              # the tick at that branch, nobody else
        self.assertEqual(len(set(links.values())), 2)                          # a link each
        body = DeliveryLog.objects.get(channel='email', recipient='mgr@t.com').body
        self.assertIn('Stock request REQ-', body)
        self.assertIn('Shampoo', body)

    def test_the_link_page_describes_the_request_without_deciding_it(self):
        req = self.ask()
        info = self.anonymous.get(f"/approvals/api/link/{self.links()['mgr@t.com']}/")
        self.assertEqual(info.status_code, 200, info.data)
        self.assertIn('Shampoo', info.data['summary'])
        self.assertEqual(info.data['status'], 'pending')
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Pending')

    def test_approving_from_the_link_approves_the_stock_request(self):
        req = self.ask()
        res = self.anonymous.post(f"/approvals/api/link/{self.links()['mgr@t.com']}/", {'action': 'approve'},
                                  format='json')
        self.assertEqual(res.status_code, 200, res.data)
        req.refresh_from_db()
        self.assertEqual((req.status, req.decided_by_id), ('Approved', self.manager.pk))
        self.assertTrue(Notification.objects.filter(event='approval.decided', user=self.clerk).exists())
        self.assertEqual(ApprovalRequest.objects.get(kind='stock_request').decided_via, 'link')

    def test_rejecting_from_the_link_rejects_it(self):
        req = self.ask()
        self.anonymous.post(f"/approvals/api/link/{self.links()['mgr@t.com']}/", {'action': 'reject'}, format='json')
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Rejected')

    def test_a_link_works_once_and_the_first_decision_wins(self):
        req = self.ask()
        links = self.links()
        first = f"/approvals/api/link/{links['mgr@t.com']}/"
        self.assertEqual(self.anonymous.post(first, {'action': 'approve'}, format='json').status_code, 200)
        self.assertEqual(self.anonymous.post(first, {'action': 'reject'}, format='json').status_code, 400)
        other = f"/approvals/api/link/{links['mgr2@t.com']}/"                    # the second manager's link is closed too
        self.assertEqual(self.anonymous.post(other, {'action': 'reject'}, format='json').status_code, 400)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Approved')

    def test_deciding_in_the_pos_closes_the_email_links(self):
        req = self.ask()
        link = f"/approvals/api/link/{self.links()['mgr2@t.com']}/"
        self.assertEqual(self.manager_api.post(f'{URL}{req.pk}/approve/', {}, format='json').status_code, 200)
        self.assertEqual(self.anonymous.post(link, {'action': 'reject'}, format='json').status_code, 400)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Approved')

    def test_a_link_outlives_the_till_approval_timeouts(self):
        req = self.ask()
        shadow = ApprovalRequest.objects.get(kind='stock_request')
        ApprovalRequest.objects.filter(pk=shadow.pk).update(created_at=timezone.now() - timedelta(days=2))
        approvals.expire_stale()
        self.assertEqual(ApprovalRequest.objects.get(pk=shadow.pk).status, 'pending')   # not lapsed after 2 days
        res = self.anonymous.post(f"/approvals/api/link/{self.links()['mgr@t.com']}/", {'action': 'approve'},
                                  format='json')
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Approved')

    def test_a_link_expires_after_the_days_in_settings(self):
        self.ask()
        ApprovalToken.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        res = self.anonymous.post(f"/approvals/api/link/{self.links()['mgr@t.com']}/", {'action': 'approve'},
                                  format='json')
        self.assertEqual(res.status_code, 400)

    def test_losing_the_tick_stops_the_link_working(self):
        req = self.ask()
        link = f"/approvals/api/link/{self.links()['mgr@t.com']}/"
        Role.objects.filter(pk=self.manager.role_id).update(permissions={})
        res = self.anonymous.post(link, {'action': 'approve'}, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Pending')

    def test_cancelling_the_request_closes_its_links(self):
        req = self.ask()
        link = f"/approvals/api/link/{self.links()['mgr@t.com']}/"
        self.assertEqual(self.clerk_api.post(f'{URL}{req.pk}/cancel/', {}, format='json').status_code, 200)
        self.assertEqual(self.anonymous.post(link, {'action': 'approve'}, format='json').status_code, 400)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Cancelled')

    def test_the_approvers_bell_shows_it_and_decides_it(self):
        req = self.ask()
        inbox = self.manager_api.get('/approvals/api/inbox/')
        self.assertEqual(inbox.status_code, 200)
        pending = [p for p in inbox.data['pending'] if p['kind'] == 'stock_request']
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]['can_decide'])
        self.assertEqual(pending[0]['kind_label'], 'Stock request')
        done = self.manager_api.post(f"/approvals/api/requests/{pending[0]['id']}/decide/", {'action': 'approve'},
                                     format='json')
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(StockRequest.objects.get(pk=req.pk).status, 'Approved')

    def test_an_approver_of_another_branch_does_not_see_it(self):
        self.ask()
        far = APIClient()
        far.force_authenticate(self.elsewhere)
        self.assertEqual([p for p in far.get('/approvals/api/inbox/').data['pending']
                          if p['kind'] == 'stock_request'], [])
