"""Give a PIN to every existing client who has none (clients made before PINs existed).

    python manage.py issue_client_pins            # make the PINs, tell nobody
    python manage.py issue_client_pins --send     # ...and message each of them their PIN

New clients get their PIN when they are created; this is only for the ones already in the database. Run it without
--send first: a message to every client at once is a decision for the business, not a side effect.
"""
from django.core.management.base import BaseCommand

from clients import pins
from clients.models import Client


class Command(BaseCommand):
    help = 'Give a PIN to every active client who has none.'

    def add_arguments(self, parser):
        parser.add_argument('--send', action='store_true', help='Also message them the PIN (WhatsApp / e-mail)')

    def handle(self, *args, **opts):
        made = sent = 0
        for client in Client.objects.filter(is_active=True, pin_sealed='').exclude(phone='').iterator():
            pins.set_pin(client)
            made += 1
            if opts['send'] and pins.send_pin(client, 'It was just made for you.', f'pin-backfill:{client.pk}'):
                sent += 1
        self.stdout.write(self.style.SUCCESS(f'Made {made} PIN(s)' + (f', messaged {sent} client(s).' if opts['send'] else '. Nobody was messaged.')))
