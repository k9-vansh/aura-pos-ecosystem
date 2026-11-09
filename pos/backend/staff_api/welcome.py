"""The e-mail a team member gets with their login for the staff app: the salon's logo, their mobile number, their PIN and
the address of the app. It is sent when they are added to the team, and again whenever a manager sets a new PIN for
them. The PIN travels sealed in the background job (never as plain text in the job table) and is only opened to be
written into the e-mail."""
import os

from django.conf import settings
from django.contrib.auth.hashers import make_password

from clients import pins

PIN_DIGITS = 6


def make_pin():
    import secrets
    return ''.join(str(secrets.randbelow(10)) for _ in range(PIN_DIGITS))


def give_pin(staff, pin=None):
    """Store a PIN for this person (a new random one when none is given) and return it."""
    pin = pin or make_pin()
    staff.app_password = make_password(pin)
    staff.save(update_fields=['app_password'])
    return pin


def announce(staff, pin, why):
    """Queue the login e-mail (when they have an e-mail address) and, on a development machine while WhatsApp is not
    linked, show the PIN in the terminal. `why` is 'added' or 'changed'."""
    if staff.phone and (settings.DEBUG or str(os.environ.get('PIN_TO_TERMINAL', '')).lower() in ('1', 'true', 'yes')):
        from notifications import integrations
        if not integrations.is_ready('whatsapp'):
            print(f'[PIN] Staff app login of {staff.first_name} ({staff.phone}): PIN {pin}', flush=True)
    if not staff.email:
        return False
    from pos_backend.jobs import enqueue
    enqueue('staff_api.login_email', {'staff_id': staff.pk, 'sealed_pin': pins.seal(pin), 'why': why},
            dedupe_key=f'staff-login:{staff.pk}:{why}:{(staff.app_password or "")[-12:]}')
    return True
