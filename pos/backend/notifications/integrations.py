"""The outside services the POS talks to, set from Admin > Integrations.

Owner decision F14/D5/D6: WhatsApp and the company mailbox are set (and changed)
in a panel, not in server files. WhatsApp ships with empty credentials, so
testing sends nothing and costs nothing until the owner fills it in.

Three services:

  graph     Microsoft 365 mailbox through Graph (an app registration by IT)
  smtp      the company mailbox through SMTP (Outlook / Office 365)
  whatsapp  WhatsApp Business Cloud API

E-mail goes through Graph if it is switched on, else SMTP if it is switched on,
else the server's own e-mail settings (the old behaviour, unchanged).

Every value is kept encrypted in one column. Secrets are never sent back to the
browser - the panel gets a mask and a "set" flag, and an empty box on save
means "keep what is stored".
"""

import base64
import hashlib
import json
import re

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

MASK = '********'

SERVICES = {
    'graph': {
        'label': 'Microsoft 365 mailbox (Graph)',
        'help': 'Sends e-mail as a company mailbox. Your IT creates an app registration with '
                'the Mail.Send permission and gives you the tenant, client id and secret.',
        'channel': 'email',
        'fields': [
            {'name': 'tenant_id', 'label': 'Tenant ID', 'secret': False},
            {'name': 'client_id', 'label': 'Application (client) ID', 'secret': False},
            {'name': 'client_secret', 'label': 'Client secret', 'secret': True},
            {'name': 'sender', 'label': 'Send as (mailbox address)', 'secret': False,
             'help': 'e.g. company@yourcompany.com'},
        ],
        'required': ['tenant_id', 'client_id', 'client_secret', 'sender'],
    },
    'smtp': {
        'label': 'Company mailbox (SMTP)',
        'help': 'Sends e-mail through your mail server, e.g. smtp.office365.com on port 587.',
        'channel': 'email',
        'fields': [
            {'name': 'host', 'label': 'Server', 'secret': False},
            {'name': 'port', 'label': 'Port', 'secret': False},
            {'name': 'use_tls', 'label': 'Use TLS (yes / no)', 'secret': False},
            {'name': 'username', 'label': 'User name', 'secret': False},
            {'name': 'password', 'label': 'Password', 'secret': True},
            {'name': 'from_email', 'label': 'From address', 'secret': False},
        ],
        'required': ['host', 'port', 'username', 'password', 'from_email'],
    },
    'whatsapp': {
        'label': 'WhatsApp Business (Cloud API)',
        'help': 'Leave empty until your Meta WhatsApp Business account is ready. While it is '
                'empty nothing is sent and nothing is charged.',
        'channel': 'whatsapp',
        'fields': [
            {'name': 'phone_number_id', 'label': 'Phone number ID', 'secret': False},
            {'name': 'access_token', 'label': 'Access token', 'secret': True},
            {'name': 'api_version', 'label': 'API version', 'secret': False,
             'help': 'e.g. v20.0'},
            {'name': 'template_language', 'label': 'Template language', 'secret': False,
             'help': 'e.g. en'},
            {'name': 'template_approval', 'label': 'Template: approval request', 'secret': False,
             'help': 'An approved template with one text variable. Empty = plain text (works '
                     'only when the person messaged you in the last 24 hours).'},
            {'name': 'template_reminder', 'label': 'Template: client reminder', 'secret': False},
            {'name': 'template_general', 'label': 'Template: other messages', 'secret': False},
        ],
        'required': ['phone_number_id', 'access_token'],
    },
}


# ─── Storage ────────────────────────────────────────────────────────────────

def _fernet():
    seed = getattr(settings, 'INTEGRATION_KEY', '') or settings.SECRET_KEY
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(seed.encode()).digest()))


def _encode(values):
    return _fernet().encrypt(json.dumps(values).encode()).decode()


def _decode(blob):
    if not blob:
        return {}
    try:
        return json.loads(_fernet().decrypt(blob.encode()).decode())
    except (InvalidToken, ValueError):
        # The server key changed since this was saved: treat it as not set.
        return {}


def load(key):
    """(IntegrationConfig row or None, values dict)."""
    from notifications.models import IntegrationConfig

    row = IntegrationConfig.objects.filter(key=key).first()
    return row, (_decode(row.blob) if row else {})


def is_ready(key):
    """Switched on and every required value present."""
    row, values = load(key)
    if row is None or not row.enabled:
        return False
    return all(str(values.get(name, '')).strip() for name in SERVICES[key]['required'])


def describe(key):
    """What the panel shows: values with secrets masked."""
    spec = SERVICES[key]
    row, values = load(key)
    fields = []
    for field in spec['fields']:
        raw = str(values.get(field['name'], '') or '')
        shown = (MASK if raw else '') if field['secret'] else raw
        fields.append({**field, 'value': shown, 'is_set': bool(raw)})
    return {
        'key': key, 'label': spec['label'], 'help': spec['help'], 'channel': spec['channel'],
        'enabled': bool(row and row.enabled),
        'ready': is_ready(key),
        'fields': fields,
        'last_test_at': row.last_test_at if row else None,
        'last_test_ok': row.last_test_ok if row else None,
        'last_test_message': row.last_test_message if row else '',
        'updated_at': row.updated_at if row else None,
    }


def save(key, enabled, values, user):
    """Store `values` (name -> text). A secret sent as the mask or empty keeps its stored value."""
    from notifications.models import IntegrationConfig

    spec = SERVICES[key]
    row, current = load(key)
    merged = dict(current)
    for field in spec['fields']:
        name = field['name']
        if name not in values:
            continue
        text = str(values[name] if values[name] is not None else '').strip()
        if field['secret'] and text in ('', MASK):
            continue
        merged[name] = text
    if enabled:
        missing = [f['label'] for f in spec['fields']
                   if f['name'] in spec['required'] and not str(merged.get(f['name'], '')).strip()]
        if missing:
            raise ValueError('Fill in before switching on: ' + ', '.join(missing) + '.')
    row = row or IntegrationConfig(key=key)
    row.enabled = bool(enabled)
    row.blob = _encode(merged)
    row.updated_by = user
    row.save()
    return row


# ─── Sending ────────────────────────────────────────────────────────────────

class NotConfigured(Exception):
    """The channel has no usable credentials - skip, do not retry."""


class SendFailed(Exception):
    """The provider refused or could not be reached - retry."""


def _http():
    import requests
    return requests


def _graph_token(values):
    resp = _http().post(
        f"https://login.microsoftonline.com/{values['tenant_id']}/oauth2/v2.0/token",
        data={'client_id': values['client_id'], 'client_secret': values['client_secret'],
              'scope': 'https://graph.microsoft.com/.default', 'grant_type': 'client_credentials'},
        timeout=20)
    if resp.status_code != 200:
        raise SendFailed(f'Microsoft sign-in failed ({resp.status_code}): {resp.text[:200]}')
    return resp.json()['access_token']


LOGO_CID = 'geetanjali-logo'


def _logo_bytes():
    """The salon's logo as sent inside the e-mail (a few KB, read once)."""
    import os
    path = os.path.join(os.path.dirname(__file__), 'mail_assets', 'geetanjali-logo.png')
    try:
        with open(path, 'rb') as fh:
            return fh.read()
    except OSError:
        return b''


def _mail_with_logo_class():
    from django.core.mail import EmailMultiAlternatives

    class MailWithLogo(EmailMultiAlternatives):
        """text + HTML, where the HTML and the logo it points at (`cid:geetanjali-logo`) form one `multipart/related`
        block: the logo shows inside the message instead of arriving as a separate attachment."""

        def __init__(self, *args, logo=b'', **kwargs):
            super().__init__(*args, **kwargs)
            self._logo = logo

        def message(self, *args, **kwargs):
            msg = super().message(*args, **kwargs)
            parts = msg.get_payload() if msg.is_multipart() else []
            if self._logo and msg.get_content_type() == 'multipart/alternative' and parts:
                from email.mime.image import MIMEImage
                from email.mime.multipart import MIMEMultipart
                image = MIMEImage(self._logo, _subtype='png')
                image.add_header('Content-ID', f'<{LOGO_CID}>')
                image.add_header('Content-Disposition', 'inline', filename='geetanjali-logo.png')
                related = MIMEMultipart('related')
                related.attach(parts[-1])
                related.attach(image)
                parts[-1] = related
            return msg

    return MailWithLogo


def _MailWithLogo(*args, **kwargs):
    return _mail_with_logo_class()(*args, **kwargs)


def send_email(to, subject, body, only=None, html=None):
    """Send one e-mail by the first ready provider (or just `only`, for a test).
    `body` is the plain text; with `html` the message carries both and the mail program shows the designed one.
    A designed message that points at `cid:geetanjali-logo` carries the logo inside it (an inline attachment),
    so it shows without the reader having to "load images". Returns the provider used."""
    if only and not is_ready(only):
        raise NotConfigured('Switch it on and fill every field first.')
    logo = _logo_bytes() if (html and f'cid:{LOGO_CID}' in html) else b''
    if only in (None, 'graph') and is_ready('graph'):
        import base64
        _, values = load('graph')
        token = _graph_token(values)
        message = {'subject': subject,
                   'body': ({'contentType': 'HTML', 'content': html} if html else {'contentType': 'Text', 'content': body}),
                   'toRecipients': [{'emailAddress': {'address': to}}]}
        if logo:
            message['attachments'] = [{'@odata.type': '#microsoft.graph.fileAttachment', 'name': 'geetanjali-logo.png',
                                       'contentType': 'image/png', 'contentBytes': base64.b64encode(logo).decode(),
                                       'isInline': True, 'contentId': LOGO_CID}]
        resp = _http().post(
            f"https://graph.microsoft.com/v1.0/users/{values['sender']}/sendMail",
            headers={'Authorization': f'Bearer {token}'},
            json={'message': message, 'saveToSentItems': True},
            timeout=20)
        if resp.status_code not in (200, 202):
            raise SendFailed(f'Microsoft Graph refused the message ({resp.status_code}): {resp.text[:200]}')
        return 'graph'

    from django.core.mail import EmailMultiAlternatives, get_connection

    if only in (None, 'smtp') and is_ready('smtp'):
        _, values = load('smtp')
        connection = get_connection(
            backend='django.core.mail.backends.smtp.EmailBackend',
            host=values['host'], port=int(values['port']),
            username=values['username'], password=values['password'],
            use_tls=str(values.get('use_tls', 'yes')).strip().lower() not in ('no', 'false', '0'),
            timeout=20, fail_silently=False)
        sender = values['from_email']
        provider = 'smtp'
    else:
        connection = get_connection(fail_silently=False)
        sender = getattr(settings, 'DEFAULT_FROM_EMAIL', None)
        provider = 'server'
    try:
        message = (_MailWithLogo(subject, body, sender, [to], connection=connection, logo=logo) if (html and logo)
                   else EmailMultiAlternatives(subject, body, sender, [to], connection=connection))
        if html:
            message.attach_alternative(html, 'text/html')
        message.send()
    except Exception as exc:                       # noqa: BLE001 - any transport error is a retry
        raise SendFailed(f'E-mail could not be sent: {exc}')
    return provider


def whatsapp_number(phone):
    """E.164 digits for an Indian mobile number: '98765 43210' -> '919876543210'."""
    digits = re.sub(r'\D', '', str(phone or ''))
    if len(digits) == 10:
        return '91' + digits
    if len(digits) == 11 and digits.startswith('0'):
        return '91' + digits[1:]
    return digits


def send_whatsapp(phone, body, template='', params=None):
    """Send one WhatsApp message. Raises NotConfigured while the credentials are empty."""
    if not is_ready('whatsapp'):
        raise NotConfigured('WhatsApp is not set up yet (Admin > Integrations).')
    _, values = load('whatsapp')
    number = whatsapp_number(phone)
    if len(number) < 11:
        raise NotConfigured('The phone number is not a valid mobile number.')
    version = (values.get('api_version') or 'v20.0').strip()
    if template:
        payload = {
            'messaging_product': 'whatsapp', 'to': number, 'type': 'template',
            'template': {
                'name': template,
                'language': {'code': (values.get('template_language') or 'en').strip()},
                'components': [{'type': 'body', 'parameters': [
                    {'type': 'text', 'text': str(p)[:1000]} for p in (params or [body])]}],
            },
        }
    else:
        payload = {'messaging_product': 'whatsapp', 'to': number, 'type': 'text',
                   'text': {'preview_url': True, 'body': body[:4000]}}
    resp = _http().post(
        f"https://graph.facebook.com/{version}/{values['phone_number_id']}/messages",
        headers={'Authorization': f"Bearer {values['access_token']}"}, json=payload, timeout=20)
    if resp.status_code >= 400:
        raise SendFailed(f'WhatsApp refused the message ({resp.status_code}): {resp.text[:200]}')
    try:
        return resp.json()['messages'][0]['id']
    except (KeyError, IndexError, ValueError):
        return 'sent'


def template_for(kind):
    """The WhatsApp template name configured for `kind` ('approval', 'reminder', 'general')."""
    _, values = load('whatsapp')
    return (values.get(f'template_{kind}') or '').strip()
