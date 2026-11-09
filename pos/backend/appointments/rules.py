"""Booking rules shared by create, update, assign and restore (B5.5).

  * branch hours (#20): every service starts at or after the branch opens and
    ends by closing time, and nothing is booked on a closed weekday;
  * statuses (D14): Scheduled -> Checked-in -> (billing) Completed, or
    Cancelled / No-show; each move is its own ticked action;
  * changing the stylist of a booked line needs the assign_staff tick.
"""

import datetime

from pos_backend import app_settings

WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')


def _hhmm(text, fallback):
    try:
        h, m = (int(x) for x in str(text).split(':'))
        return h * 60 + m
    except (TypeError, ValueError):
        return fallback


def branch_hours(center):
    """The branch's booking rules, as the calendar and the checks read them."""
    closed = app_settings.get('appointments.closed_days', center) or ''
    return {
        'open_time': app_settings.get('appointments.open_time', center),
        'close_time': app_settings.get('appointments.close_time', center),
        'closed_days': [d.strip() for d in closed.split(',') if d.strip()],
        'slot_minutes': app_settings.get('appointments.slot_minutes', center),
        'default_duration': app_settings.get('appointments.default_duration', center),
    }


def hours_problem(center, date, lines):
    """Why these lines can't be booked at this branch on this date, or None.

    `lines` are dicts with a parsed `time` and an int `duration`.
    """
    hours = branch_hours(center)
    if date and WEEKDAYS[date.weekday()] in hours['closed_days']:
        return f'The branch is closed on {date.strftime("%A")}s.'
    opens = _hhmm(hours['open_time'], 9 * 60)
    closes = _hhmm(hours['close_time'], 22 * 60)
    for line in lines:
        start_at = line.get('time')
        if not isinstance(start_at, datetime.time):
            continue
        start = start_at.hour * 60 + start_at.minute
        end = start + max(int(line.get('duration') or 0), 1)
        if start < opens or end > closes:
            name = line.get('service_name') or 'A service'
            return (f'{name} at {start_at.strftime("%H:%M")} is outside the branch hours '
                    f'({hours["open_time"]}-{hours["close_time"]}).')
    return None


# (from, to) -> the calendar action that allows it. None means anyone with
# calendar update may do it; a missing pair is not allowed at all.
TRANSITIONS = {
    ('Scheduled', 'Checked-in'): 'check_in',
    ('Checked-in', 'Scheduled'): 'check_in',      # undo a check-in made by mistake
    ('Scheduled', 'No-show'): 'no_show',
    ('Checked-in', 'No-show'): 'no_show',
    ('Scheduled', 'Cancelled'): 'cancel',
    ('Checked-in', 'Cancelled'): 'cancel',
    ('Cancelled', 'Scheduled'): 'restore',
    ('No-show', 'Scheduled'): 'restore',
}

# Allowed when the booking is first made.
CREATE_STATUSES = {'Scheduled': None, 'Checked-in': 'check_in'}


def status_action(old, new):
    """The tick a status change needs: (allowed, action_or_None, message)."""
    if old == new:
        return True, None, ''
    if new == 'Completed':
        return False, None, 'An appointment is completed by billing it, not by hand.'
    if old == 'Completed':
        return False, None, 'This appointment has been billed; cancel the bill instead.'
    if (old, new) not in TRANSITIONS:
        return False, None, f'An appointment cannot go from {old} to {new}.'
    return True, TRANSITIONS[(old, new)], ''


def changed_staff(instance, new_lines):
    """True when a line that already existed is given a different stylist.

    Lines are matched on (service, time); new lines can be booked with anyone.
    """
    def key(service_id, name, time):
        return (service_id or (name or '').strip().lower(), time)

    before = {}
    for line in instance.services.all():
        before.setdefault(key(line.service_id, line.service_name, line.time), []).append(line.staff_id)
    for line in new_lines:
        k = key(line.get('service_id'), line.get('service_name'), line.get('time'))
        if k in before and before[k]:
            if line.get('staff_id') not in before[k]:
                return True
            before[k].remove(line.get('staff_id'))
    return False
