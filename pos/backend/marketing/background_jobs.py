"""Automated client reminders (decision #58) - background jobs owned by marketing.

  * expiry: a value card, membership, package or cashback credit that expires
    within `reminders.expiry_days` (30 by default) - one message each, once;
  * appointment: `reminders.appointment_hours` before a booked appointment
    (off by default).

Every message goes through notifications.service.send_to_client, which checks
the client's WhatsApp consent and DND, finds their address, signs nothing it
should not and never sends the same reminder twice (a dedupe key per item and
channel). While WhatsApp has no credentials the message is logged as skipped.
"""

import datetime
import logging

from django.utils import timezone

from pos_backend import app_settings
from pos_backend.jobs import job, periodic

logger = logging.getLogger(__name__)


@job('marketing.send_campaign')
def send_campaign(payload, job_row):
    """Prepare a large WhatsApp broadcast outside the web request."""
    from marketing.campaigns import run_campaign

    return run_campaign(payload['campaign_id'], payload.get('center_ids'))


def _branch(center, client):
    center = center or getattr(client, 'center', None)
    return (center.display_name or center.center_name) if center else 'us'


def _send(client, center, key, what, expires, extra=''):
    from notifications.service import send_to_client

    branch = _branch(center, client)
    first = client.first_name or 'there'
    when = f'{expires:%d %b %Y}'
    body = (f'Hello {first}, your {what} at {branch} expires on {when}.{(" " + extra) if extra else ""} '
            f'Visit us before then to make the most of it. - {branch}')
    return send_to_client('client.expiry_reminder', client, subject=f'Your {what} expires on {when}',
                          body=body, key=key, template_kind='reminder')


@periodic('marketing.expiry_reminders', every=6 * 3600)
def expiry_reminders(today=None):
    """Queue this period's expiry reminders. Returns how many messages were queued."""
    from billing.models import CashbackTransaction
    from clients.models import ClientMembership, ClientPackage, ClientValueCard

    days = int(app_settings.get('reminders.expiry_days'))
    if days <= 0:
        return 0
    today = today or datetime.date.today()
    horizon = today + datetime.timedelta(days=days)
    queued = 0

    def fresh(item_created):
        # Not on the day it was sold: nobody needs "expires in 20 days" at the till.
        return item_created is None or item_created.date() < today

    cards = (ClientValueCard.objects.select_related('client', 'client__center', 'value_card', 'center')
             .filter(is_active=True, balance__gt=0, forfeited_on__isnull=True,
                     expiry_date__gte=today, expiry_date__lte=horizon))
    def attempt(what, send):
        """One customer's reminder. A row that cannot be read or sent is logged and
        skipped: it used to raise out of the whole job, so a single odd package
        balance stopped every other customer's reminder, every run, for ever."""
        try:
            return len(send())
        except Exception:                                      # noqa: BLE001 - logged, the rest go on
            logger.exception('Expiry reminder skipped for %s', what)
            return 0

    for card in cards:
        if fresh(card.created_at):
            queued += attempt(f'value card {card.pk}', lambda card=card: _send(
                card.client, card.center, f'exp:card:{card.pk}', f'{card.value_card.title} value card',
                card.expiry_date, f'Rs {card.balance} is still on it.'))

    memberships = (ClientMembership.objects.select_related('client', 'client__center', 'membership', 'center')
                   .filter(is_active=True, expiry_date__gte=today, expiry_date__lte=horizon))
    for m in memberships:
        if fresh(m.created_at):
            queued += attempt(f'membership {m.pk}', lambda m=m: _send(
                m.client, m.center, f'exp:membership:{m.pk}', f'{m.membership.name} membership', m.expiry_date))

    packages = (ClientPackage.objects.select_related('client', 'client__center', 'package', 'center')
                .filter(is_active=True, expiry_date__gte=today, expiry_date__lte=horizon))
    for p in packages:
        left = _units_left(p.services_remaining)
        if left > 0 and fresh(p.created_at):
            name = p.package.name if p.package else 'package'
            queued += attempt(f'package {p.pk}', lambda p=p, name=name, left=left: _send(
                p.client, p.center, f'exp:package:{p.pk}', f'{name} package', p.expiry_date,
                f'{left} service(s) are still unused.'))

    credits = (CashbackTransaction.objects.select_related('client', 'client__center', 'center')
               .filter(kind=CashbackTransaction.KIND_EARNED, remaining__gt=0,
                       expires_on__gte=today, expires_on__lte=horizon))
    for c in credits:
        if fresh(c.created_at):
            queued += attempt(f'cashback {c.pk}', lambda c=c: _send(
                c.client, c.center, f'exp:cashback:{c.pk}', 'cashback', c.expires_on,
                f'Rs {c.remaining} is still to spend.'))
    return queued


def _units_left(remaining):
    """Sessions still unused on a package; anything unreadable counts as none."""
    if not isinstance(remaining, dict):
        return 0
    total = 0
    for value in remaining.values():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and number not in (float('inf'), float('-inf')) and number > 0:
            total += int(number)
    return total


@periodic('marketing.appointment_reminders', every=900)
def appointment_reminders(now=None):
    """Message clients shortly before a booked appointment, where the branch has it on."""
    from appointments.models import Appointment
    from notifications.service import send_to_client
    from salon_admin.models import Center

    now = now or datetime.datetime.now()
    queued = 0
    for center in Center.objects.all():
        hours = int(app_settings.get('reminders.appointment_hours', center=center))
        if hours <= 0:
            continue
        window_end = now + datetime.timedelta(hours=hours)
        rows = (Appointment.objects.select_related('client', 'client__center', 'center')
                .filter(center=center, status__in=Appointment.BOOKED, client__isnull=False,
                        date__gte=now.date(), date__lte=window_end.date()))
        for appt in rows:
            try:
                starts = datetime.datetime.combine(appt.date, appt.start_time)
                if not (now <= starts <= window_end):
                    continue
                branch = _branch(center, appt.client)
                body = (f'Hello {appt.client.first_name or "there"}, a reminder of your appointment at '
                        f'{branch} on {starts:%d %b} at {starts:%I:%M %p}. - {branch}')
                queued += len(send_to_client(
                    'client.appointment_reminder', appt.client, subject='Your appointment',
                    body=body, key=f'appt:{appt.pk}:{appt.date}:{appt.start_time:%H%M}',
                    template_kind='reminder'))
            except Exception:                                  # noqa: BLE001 - one odd row must not stop the rest
                logger.exception('Appointment reminder skipped for appointment %s', appt.pk)
    return queued
