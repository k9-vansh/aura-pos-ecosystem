"""
WhatsApp campaign tests.

The finding that matters most is not a crash: `send_campaign` writes every row
with `status='Sent (Mock)'` - nothing is transmitted anywhere - and then replied
"Campaign sent to N clients". An operator had every reason to believe their
customers had been messaged.

Also pinned:
  * `center_id` went straight into `filter(center_id=...)`, so junk was a 500;
  * the reported total was counted before the loop that skips customers with no
    phone number, so it overstated the reach;
  * DND was matched as `iexact='ON DND'`, one spelling only, so a customer
    recorded as 'DND' was messaged anyway;
  * blacklisted and deactivated customers were not excluded at all;
  * a customer with no branch had their message filed under
    `Center.objects.first()`.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from clients.models import Client
from marketing.models import WhatsAppMessage
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role


class CampaignTestBase(TestCase):
    URL = '/marketing/api/whatsapp/send_campaign/'

    def setUp(self):
        self.center = Center.objects.create(center_name='Main Center',
                                            display_name='Main')
        self.other = Center.objects.create(center_name='Second Center')

        self.owner_role = Role.objects.create(name='Owner',
                                              permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center)

        self.reception_role = Role.objects.create(name='Receptionist',
                                                  permissions={})
        self.receptionist = CustomUser.objects.create_user(
            email='r@test.com', password='StrongPass!234', full_name='R',
            role=self.reception_role, center=self.center)

        self.client = APIClient()
        self.client.force_authenticate(self.owner)

    def customer(self, name='Asha', phone='9876500001', **extra):
        return Client.objects.create(
            center=extra.pop('center', self.center),
            first_name=name, phone=phone, **extra)

    def send(self, **payload):
        body = {'message': 'Half price blow-dry this Friday'}
        body.update(payload)
        return self.client.post(self.URL, body, format='json')


class HonestReportingTests(CampaignTestBase):
    def test_the_response_says_nothing_was_actually_sent(self):
        """Every row is written 'Sent (Mock)'; the reply said "Campaign sent"."""
        self.customer()
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(response.data['gateway_configured'])
        self.assertEqual(response.data['delivered'], 0)
        self.assertIn('nothing was transmitted', response.data['message'])

    def test_the_count_excludes_customers_with_no_phone(self):
        """The total was counted before the phone check."""
        self.customer('Asha', '9876500001')
        Client.objects.create(center=self.center, first_name='NoPhone', phone='')
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(WhatsAppMessage.objects.count(), 1)

    def test_a_campaign_with_no_eligible_customers_is_a_404(self):
        response = self.send()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(WhatsAppMessage.objects.count(), 0)


class ConsentTests(CampaignTestBase):
    def test_the_standard_dnd_marker_excludes_a_customer(self):
        self.customer('OnDnd', '9876500002', dnd_status='ON DND')
        response = self.send()
        self.assertEqual(response.status_code, 404, response.data)

    def test_a_shorthand_dnd_marker_also_excludes(self):
        """`iexact='ON DND'` matched one spelling only."""
        self.customer('OnDnd', '9876500002', dnd_status='DND')
        response = self.send()
        self.assertEqual(response.status_code, 404, response.data)

    def test_the_default_value_does_not_exclude_anyone(self):
        """'NOT ON DND' contains 'ON DND', so a contains-match would exclude all."""
        self.customer('Asha', '9876500001', dnd_status='NOT ON DND')
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)

    def test_a_blacklisted_customer_is_not_messaged(self):
        self.customer('Banned', '9876500003', is_blacklisted=True)
        response = self.send()
        self.assertEqual(response.status_code, 404, response.data)

    def test_an_inactive_customer_is_not_messaged(self):
        self.customer('Gone', '9876500004', is_active=False)
        response = self.send()
        self.assertEqual(response.status_code, 404, response.data)

    def test_an_eligible_customer_alongside_an_excluded_one_still_gets_it(self):
        self.customer('Asha', '9876500001')
        self.customer('Banned', '9876500003', is_blacklisted=True)
        self.customer('OnDnd', '9876500002', dnd_status='ON DND')
        response = self.send()
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(
            WhatsAppMessage.objects.get().client_phone, '9876500001')


class BranchTests(CampaignTestBase):
    def test_a_junk_center_id_is_a_400_not_a_500(self):
        self.customer()
        for value in ('abc', 'null', 'undefined'):
            response = self.send(center_id=value)
            self.assertEqual(response.status_code, 400, value)

    def test_all_targets_every_accessible_branch(self):
        self.customer('Asha', '9876500001', center=self.center)
        self.customer('Bela', '9876500002', center=self.other)
        response = self.send(center_id='all')
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 2)

    def test_a_named_branch_targets_only_that_branch(self):
        self.customer('Asha', '9876500001', center=self.center)
        self.customer('Bela', '9876500002', center=self.other)
        response = self.send(center_id=self.center.id)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(WhatsAppMessage.objects.get().client_phone, '9876500001')

    def test_a_customer_with_no_branch_is_not_filed_under_another_one(self):
        """`Center.objects.first()` credited an arbitrary branch."""
        Client.objects.create(first_name='Floating', phone='9876500005',
                              center=None)
        response = self.send()
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['without_branch'], 1)

        message = WhatsAppMessage.objects.get()
        self.assertIsNone(message.center)
        self.assertEqual(WhatsAppMessage.objects.filter(center=self.center).count(), 0)


class GuardTests(CampaignTestBase):
    def test_a_receptionist_cannot_send_a_campaign(self):
        self.customer()
        client = APIClient()
        client.force_authenticate(self.receptionist)
        response = client.post(self.URL, {'message': 'hi'}, format='json')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(WhatsAppMessage.objects.count(), 0)

    def test_an_anonymous_caller_cannot_send_a_campaign(self):
        response = APIClient().post(self.URL, {'message': 'hi'}, format='json')
        self.assertIn(response.status_code, (401, 403))

    def test_an_empty_message_is_refused(self):
        self.customer()
        for value in ('', '   ', None):
            response = self.client.post(
                self.URL, {'message': value} if value is not None else {},
                format='json')
            self.assertEqual(response.status_code, 400, value)

    def test_an_oversized_message_is_refused(self):
        from marketing.views import MAX_CAMPAIGN_MESSAGE_CHARS

        self.customer()
        response = self.send(message='x' * (MAX_CAMPAIGN_MESSAGE_CHARS + 1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(WhatsAppMessage.objects.count(), 0)
