"""The login e-mails: what a new client and a new team member receive, and that the salon's logo is inside them."""
from decimal import Decimal
from unittest import mock

from django.contrib.auth.hashers import check_password
from django.core import mail
from django.test import TestCase, override_settings

from clients import pins
from clients.models import Client
from notifications import email_layout, integrations
from salon_admin.models import Center
from staff.models import StaffMember
from staff_api import welcome
from staff_api.background_jobs import login_email


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
class LoginEmailTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Geetanjali Saket')

    def staff(self, **extra):
        return StaffMember.objects.create(center=self.center, first_name='Ravi', designation='Stylist', phone='9811100001',
                                          email='ravi@example.com', salary=Decimal('1'), **extra)

    def test_the_designed_mail_carries_the_logo_and_the_logo_travels_inside_it(self):
        html = email_layout.render('client.welcome', 'Welcome', 'Hello there.\n\nYour PIN: 4821')['html']
        self.assertIn('src="cid:geetanjali-logo"', html)
        self.assertIn('alt="Geetanjali Salon"', html)                          # readable even if images are blocked
        integrations.send_email('a@example.com', 'Welcome', 'Hello there.', html=html)
        raw = mail.outbox[0].message()
        kinds = [p.get_content_type() for p in raw.walk()]
        self.assertEqual(kinds, ['multipart/alternative', 'text/plain', 'multipart/related', 'text/html', 'image/png'])
        logo = next(p for p in raw.walk() if p.get_content_type() == 'image/png')
        self.assertEqual(logo.get('Content-ID'), '<geetanjali-logo>')
        self.assertGreater(len(logo.get_payload(decode=True)), 1000)

    def test_a_plain_mail_without_the_logo_reference_carries_no_image(self):
        integrations.send_email('a@example.com', 'Plain', 'Just text.', html='<p>No logo</p>')
        self.assertNotIn('image/png', [p.get_content_type() for p in mail.outbox[0].message().walk()])

    def test_the_logo_goes_with_a_graph_mail_too(self):
        html = email_layout.render('client.welcome', 'Welcome', 'Hi.')['html']
        sent = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            sent['json'] = json
            return mock.Mock(status_code=202, text='')
        with mock.patch.object(integrations, 'is_ready', side_effect=lambda k: k == 'graph'), \
                mock.patch.object(integrations, 'load', return_value=(None, {'sender': 's@x.in'})), \
                mock.patch.object(integrations, '_graph_token', return_value='t'), \
                mock.patch.object(integrations, '_http') as http:
            http.return_value.post.side_effect = fake_post
            integrations.send_email('a@example.com', 'Welcome', 'Hi.', html=html)
        attachment = sent['json']['message']['attachments'][0]
        self.assertEqual((attachment['contentId'], attachment['isInline'], attachment['contentType']), ('geetanjali-logo', True, 'image/png'))

    def test_a_new_client_gets_one_welcome_with_their_pin_the_phone_and_the_app_link(self):
        from notifications.models import DeliveryLog
        client = Client.objects.create(first_name='Asha', phone='9990000001', email='asha@example.com', center=self.center)
        with mock.patch.object(pins, 'app_url', return_value='https://app.example.com/'):
            pins.on_create(client)
            pins.on_create(client)                                              # a second call sends nothing more
        rows = DeliveryLog.objects.filter(event='client.welcome', client=client)
        self.assertEqual(sorted(rows.values_list('channel', flat=True)), ['email', 'whatsapp'])
        email = rows.get(channel='email')
        self.assertIn(f'Your PIN: {pins.client_pin(client)}', email.body)
        self.assertIn('Mobile number: 9990000001', email.body)
        self.assertIn('https://app.example.com/', email.body)
        self.assertIn('cid:geetanjali-logo', email.html)
        self.assertIn(pins.client_pin(client), email.html)
        self.assertIn('Open my app', email.html)

    def test_no_link_is_written_when_the_app_address_is_not_known_yet(self):
        from notifications.models import DeliveryLog
        client = Client.objects.create(first_name='Asha', phone='9990000001', email='asha@example.com', center=self.center)
        with mock.patch.object(pins, 'app_url', return_value=''):
            pins.on_create(client)
        self.assertNotIn('http', DeliveryLog.objects.get(event='client.welcome', channel='email').body)

    def test_a_new_team_member_gets_a_pin_that_works_and_an_e_mail_that_carries_it_sealed(self):
        person = self.staff()
        pin = welcome.give_pin(person)
        self.assertRegex(pin, r'^\d{6}$')
        person.refresh_from_db()
        self.assertTrue(check_password(pin, person.app_password))
        with mock.patch('pos_backend.jobs.enqueue') as enqueue:
            self.assertTrue(welcome.announce(person, pin, 'added'))
        payload = enqueue.call_args.args[1]
        self.assertNotIn(pin, str(payload))                                      # the job table never holds the PIN
        self.assertEqual(pins.unseal(payload['sealed_pin']), pin)

    def test_the_login_e_mail_says_who_what_and_where_and_is_sent_once_the_job_runs(self):
        person = self.staff()
        pin = welcome.give_pin(person)
        with mock.patch.object(pins, 'app_url', return_value='https://staff.example.com/'):
            login_email({'staff_id': person.pk, 'sealed_pin': pins.seal(pin), 'why': 'added'}, None)
        message = mail.outbox[0]
        self.assertEqual(message.to, ['ravi@example.com'])
        self.assertIn('Welcome to the Geetanjali Saket team', message.subject)
        self.assertIn(f'Your PIN: {pin}', message.body)
        self.assertIn('Mobile number: 9811100001', message.body)
        self.assertIn('https://staff.example.com/', message.body)
        html = next(p for p in message.message().walk() if p.get_content_type() == 'text/html').get_payload(decode=True).decode()
        self.assertIn('cid:geetanjali-logo', html)
        self.assertIn('Open the staff app', html)
        self.assertIn('Hi Ravi,', html)

    def test_a_changed_pin_says_the_old_one_stopped_working(self):
        person = self.staff()
        login_email({'staff_id': person.pk, 'sealed_pin': pins.seal('654321'), 'why': 'changed'}, None)
        self.assertIn('Your new staff app PIN', mail.outbox[0].subject)
        self.assertIn('old PIN no longer works', mail.outbox[0].body)

    def test_nothing_is_sent_to_a_person_without_an_e_mail_address(self):
        person = self.staff(**{})
        StaffMember.objects.filter(pk=person.pk).update(email=None)
        person.refresh_from_db()
        with mock.patch('pos_backend.jobs.enqueue') as enqueue:
            self.assertFalse(welcome.announce(person, '123456', 'added'))
        enqueue.assert_not_called()
        login_email({'staff_id': person.pk, 'sealed_pin': pins.seal('123456'), 'why': 'added'}, None)
        self.assertEqual(mail.outbox, [])

    def test_the_pin_shows_in_the_terminal_only_while_whatsapp_is_not_linked_and_only_in_development(self):
        import io
        from contextlib import redirect_stdout
        person = self.staff()
        out = io.StringIO()
        with override_settings(DEBUG=True), redirect_stdout(out), mock.patch('pos_backend.jobs.enqueue'):
            welcome.announce(person, '246810', 'added')
        self.assertIn('246810', out.getvalue())
        quiet = io.StringIO()
        with override_settings(DEBUG=False), redirect_stdout(quiet), mock.patch('pos_backend.jobs.enqueue'):
            welcome.announce(person, '246810', 'added')
        self.assertEqual(quiet.getvalue(), '')
