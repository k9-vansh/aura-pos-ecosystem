"""The designed e-mail: every kind renders, nothing a person typed can inject markup, the items table and the
button are there, and the mail goes out with both a plain and a designed part."""
from decimal import Decimal
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings

from accounts.models import CustomUser
from inventory.models import Product, StockRequest, StockRequestItem
from notifications import email_layout
from notifications.background_jobs import deliver
from notifications.events import EVENTS
from notifications.models import DeliveryLog
from notifications.service import notify
from salon_admin.models import Center, Role

D = Decimal


class EmailLayoutTests(TestCase):
    def setUp(self):
        self.center = Center.objects.create(center_name='Saket', display_name='Saket')
        role = Role.objects.create(name='Approver', permissions={})
        self.user = CustomUser.objects.create_user(email='kavya@t.com', password='pw', full_name='Kavya Nair',
                                                   role=role, center=self.center)
        self.user.centers.add(self.center)
        self.asker = CustomUser.objects.create_user(email='aarav@t.com', password='pw', full_name='Aarav Mehta',
                                                    role=role, center=self.center)

    def request_with_items(self):
        req = StockRequest.objects.create(requesting_center=self.center, stock_type='Retail', created_by=self.asker,
                                          notes='For the weekend rush')
        serum = Product.objects.create(name='Serum 30ml', product_code='SER1', category='Retail', price=D('1850'),
                                       gst_percent=D('18'), center=self.center)
        mask = Product.objects.create(name='Mask <b>pack</b>', product_code='MSK1', category='Retail',
                                      price=D('250.50'), gst_percent=D('5'), center=self.center)
        StockRequestItem.objects.create(stock_request=req, product=serum, quantity=D('4'))
        StockRequestItem.objects.create(stock_request=req, product=mask, quantity=D('10'))
        return req

    def test_every_event_renders_a_complete_page(self):
        for event in EVENTS:
            out = email_layout.render(event, 'Subject here', 'First line.\nLabel: a value\nSecond line.',
                                      user=self.user, center=self.center)
            self.assertTrue(out['html'].lstrip().startswith('<!doctype html>'), event)
            self.assertIn('Subject here', out['html'])
            self.assertIn('Kavya', out['html'])
            self.assertIn(email_layout.brand(), out['html'])
            self.assertNotIn('<script', out['html'])
            self.assertIn('First line.', out['text'])

    def test_what_a_person_typed_cannot_inject_markup(self):
        evil = '<script>alert(1)</script><img src=x onerror=alert(2)>'
        self.user.full_name = evil
        out = email_layout.render('approval.requested', evil, f'{evil} asked.\nReason: {evil}\n'
                                  'Approve here: http://localhost:4200/approve/abc"onmouseover="x', user=self.user)
        self.assertNotIn('<script', out['html'])
        self.assertNotIn('<img src=x', out['html'])          # nothing a person typed became a tag (the logo is the only image)
        self.assertEqual(out['html'].count('<img'), 1)
        self.assertNotIn('onmouseover="x', out['html'].replace('&quot;', ''))      # the quote in the link is escaped

    def test_a_link_in_the_text_becomes_the_button_and_keeps_its_note(self):
        out = email_layout.render('stock.request_raised', 'Stock request from Saket',
                                  'Aarav asked for stock.\nApprove or reject from this link (works once, for 3 days): '
                                  'http://localhost:4200/approve/TOKEN123', user=self.user)
        self.assertIn('href="http://localhost:4200/approve/TOKEN123"', out['html'])
        self.assertIn('Review and decide', out['html'])
        self.assertIn('works once, for 3 days', out['html'])
        self.assertIn('http://localhost:4200/approve/TOKEN123', out['text'])

    def test_a_stock_request_mail_carries_the_items_and_the_total(self):
        req = self.request_with_items()
        out = email_layout.render('stock.request_raised', 'Stock request from Saket', 'Aarav asked for stock.',
                                  user=self.user, url='/inventory', data={'stock_request_id': req.pk})
        html = out['html']
        for expected in ('REQ-%d' % req.pk, 'Serum 30ml', 'SER1', 'Aarav Mehta', 'For the weekend rush'):
            self.assertIn(expected, html)
        self.assertIn('Mask &lt;b&gt;pack&lt;/b&gt;', html)              # escaped, not bold
        # 4 x 1850 x 1.18 = 8,732.00 ; 10 x 250.50 x 1.05 = 2,630.25 ; total 11,362.25
        self.assertIn('8,732.00', html)
        self.assertIn('11,362.25', html)
        self.assertIn('Review and decide', html)

    def test_money_is_written_the_indian_way(self):
        self.assertEqual(email_layout.inr(1234567.5), '12,34,567.50')
        self.assertEqual(email_layout.inr(999), '999.00')
        self.assertEqual(email_layout.inr(-1500), '-1,500.00')
        self.assertEqual(email_layout.inr(100000), '1,00,000.00')

    def test_a_client_mail_greets_the_client_and_has_no_staff_footer(self):
        client = mock.Mock(first_name='Priya', full_name='Priya Sharma')
        out = email_layout.render('client.welcome', 'Welcome to Saket', 'Thank you for visiting.', client=client,
                                  center=self.center)
        self.assertIn('Hi Priya,', out['html'])
        self.assertNotIn('You can approve', out['html'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_a_notification_is_sent_with_a_plain_part_and_a_designed_part(self):
        req = self.request_with_items()
        notify('stock.request_raised', title='Stock request from Saket', body='Aarav asked for stock.',
               url='/inventory', center=self.center, users=[self.user], data={'stock_request_id': req.pk},
               email_body=lambda u: 'Aarav asked for stock.\nApprove from this link: http://localhost:4200/approve/T')
        log = DeliveryLog.objects.get(event='stock.request_raised', channel='email')
        self.assertIn('Serum 30ml', log.html)
        deliver({'log_id': log.pk}, None)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertIn('Approve from this link', message.body)                       # the plain part stays readable
        self.assertEqual(message.alternatives[0][1], 'text/html')
        self.assertIn('Serum 30ml', message.alternatives[0][0])
        self.assertEqual(DeliveryLog.objects.get(pk=log.pk).status, 'sent')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
    def test_if_the_design_fails_the_plain_mail_still_goes(self):
        with mock.patch('notifications.email_layout.render', side_effect=RuntimeError('boom')):
            notify('stock.request_raised', title='T', body='Plain words.', center=self.center, users=[self.user])
        log = DeliveryLog.objects.get(event='stock.request_raised', channel='email')
        self.assertEqual(log.html, '')
        deliver({'log_id': log.pk}, None)
        self.assertEqual((len(mail.outbox), mail.outbox[0].alternatives, 'Plain words.' in mail.outbox[0].body),
                         (1, [], True))

    def test_the_graph_provider_receives_html_when_there_is_a_designed_part(self):
        from notifications import integrations
        posted = {}

        def fake_post(url, **kwargs):
            if 'oauth2' in url:                                   # the sign-in call
                return mock.Mock(status_code=200, text='', json=lambda: {'access_token': 't'})
            posted.update(kwargs.get('json') or {})
            return mock.Mock(status_code=202, text='')

        with mock.patch.object(integrations, 'is_ready', side_effect=lambda key: key == 'graph'), \
                mock.patch.object(integrations, 'load', return_value=(None, {
                    'tenant_id': 'x', 'client_id': 'y', 'client_secret': 'z', 'sender': 'pos@example.com'})), \
                mock.patch.object(integrations, '_http', return_value=mock.Mock(post=fake_post)):
            integrations.send_email('a@b.com', 'S', 'plain', html='<p>designed</p>')
        self.assertEqual(posted['message']['body'], {'contentType': 'HTML', 'content': '<p>designed</p>'})
