"""Booking an appointment from the client app.

The client picks services, a day and a start time. The booking lands in the same table the POS calendar reads, as a
normal "Scheduled" appointment with no stylist yet: the front desk assigns one, exactly as for a phone booking. It
follows the branch's own booking rules (opening hours, closed days) and the same blacklist rule as the POS.
"""
import datetime

from django.db import transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from appointments.models import Appointment, AppointmentService
from appointments.rules import branch_hours, hours_problem
from services.models import CenterService, ServiceMaster

MAX_UPCOMING = 5            # bookings a client may hold at once
MAX_DAYS_AHEAD = 60
MAX_SERVICES = 6
NOTE = 'Booked in the client app.'


def minutes(text, fallback):
    try:
        h, m = (int(x) for x in str(text).split(':'))
        return h * 60 + m
    except (TypeError, ValueError):
        return fallback


def catalogue(center):
    """[{id, name, category, price, minutes}] the branch sells: its own services and the organisation's, minus any it switched off."""
    off = set(CenterService.objects.filter(center=center, is_active=False).values_list('service_id', flat=True))
    prices = {o.service_id: o.price for o in CenterService.objects.filter(center=center, is_active=True, price__isnull=False)}
    default = branch_hours(center)['default_duration']
    rows = ServiceMaster.objects.filter(Q(centers=center) | Q(level='Organisation')).exclude(pk__in=off).distinct().order_by('category', 'name')
    return [{'id': s.id, 'name': s.name, 'category': s.category or 'Services',
             'price': float(prices.get(s.id) if prices.get(s.id) is not None else s.default_price),
             'minutes': s.duration_mins or default} for s in rows]


def slots(center, day):
    """The start times offered on `day`: every slot of the branch's day, none in the past, none on a closed weekday."""
    hours = branch_hours(center)
    if day < datetime.date.today() or day > datetime.date.today() + datetime.timedelta(days=MAX_DAYS_AHEAD):
        return []
    if ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')[day.weekday()] in hours['closed_days']:
        return []
    start, end, step = minutes(hours['open_time'], 540), minutes(hours['close_time'], 1320), max(int(hours['slot_minutes'] or 15), 5)
    now = datetime.datetime.now()
    floor = now.hour * 60 + now.minute + 30 if day == now.date() else -1          # at least half an hour's notice today
    return [f'{m // 60:02d}:{m % 60:02d}' for m in range(start, end, step) if m >= floor]


def parse_day(value):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        raise ValidationError({'date': 'Pick a day.'})


@transaction.atomic
def book(client, data):
    """Make the appointment, or raise a ValidationError that says in plain words why not."""
    center = client.center
    if center is None:
        raise ValidationError({'detail': 'You are not linked to a salon yet. Please call the salon to book.'})
    if client.is_blacklisted:
        raise ValidationError({'detail': 'Online booking is not available for this account. Please call the salon.'})
    day = parse_day(data.get('date'))
    if not slots(center, day):
        raise ValidationError({'date': 'The salon cannot take bookings on that day. Pick another day.'})
    try:
        hh, mm = (int(x) for x in str(data.get('time') or '').split(':'))
        start = datetime.time(hh, mm)
    except (TypeError, ValueError):
        raise ValidationError({'time': 'Pick a time.'})
    if f'{hh:02d}:{mm:02d}' not in slots(center, day):
        raise ValidationError({'time': 'That time is not available. Pick one from the list.'})

    ids = []
    for raw in data.get('services') or []:
        if str(raw).isdigit() and int(raw) not in ids:
            ids.append(int(raw))
    if not ids:
        raise ValidationError({'services': 'Pick at least one service.'})
    if len(ids) > MAX_SERVICES:
        raise ValidationError({'services': f'Pick at most {MAX_SERVICES} services in one booking.'})
    menu = {s['id']: s for s in catalogue(center)}
    if any(i not in menu for i in ids):
        raise ValidationError({'services': 'One of those services is not offered at your salon.'})

    upcoming = Appointment.objects.filter(Q(client=client) | Q(client_phone=client.phone), date__gte=datetime.date.today(),
                                          status__in=Appointment.BOOKED)
    if upcoming.count() >= MAX_UPCOMING:
        raise ValidationError({'detail': f'You already have {MAX_UPCOMING} bookings coming up. Cancel one or call the salon.'})
    if upcoming.filter(date=day, start_time=start).exists():
        raise ValidationError({'detail': 'You already have a booking at that time.'})

    lines, cursor = [], hh * 60 + mm
    for sid in ids:
        item = menu[sid]
        lines.append({'service_id': sid, 'service_name': item['name'], 'time': datetime.time(cursor // 60, cursor % 60),
                      'duration': item['minutes'], 'price': item['price']})
        cursor += item['minutes']
    problem = hours_problem(center, day, lines)
    if problem:
        raise ValidationError({'services': problem})

    notes = NOTE + (f' Note: {str(data.get("note")).strip()[:300]}' if str(data.get('note') or '').strip() else '')
    appt = Appointment.objects.create(center=center, client=client, client_phone=client.phone, client_name=client.full_name,
                                      date=day, start_time=start, notes=notes, status='Scheduled')
    for line in lines:
        AppointmentService.objects.create(appointment=appt, **line)
    return appt


@transaction.atomic
def cancel(client, pk):
    appt = Appointment.objects.select_for_update().filter(Q(client=client) | Q(client_phone=client.phone), pk=pk).first()
    if appt is None:
        raise ValidationError({'detail': 'That booking was not found.'})
    if appt.status != 'Scheduled' or appt.date < datetime.date.today():
        raise ValidationError({'detail': 'Only a booking that is still coming up can be cancelled here. Please call the salon.'})
    appt.status = 'Cancelled'
    appt.notes = ((appt.notes or '') + ' Cancelled by the client in the app.').strip()
    appt.save(update_fields=['status', 'notes', 'updated_at'])
    return appt
