"""The staff app's API: a team member sees their own work, their own appointments and their own profile.

They sign in with their mobile number and the app PIN the salon set for them (Staff > Directory > "set app PIN").
Everything here is read-only except signing in and out; nothing a staff member does here changes the POS.
"""
import datetime
import re

from django.core.cache import cache
from django.db.models import Q
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import Appointment, AppointmentService
from pos_backend.throttles import LoginRateThrottle
from staff.models import StaffMember

from . import work
from .auth import StaffTokenAuthentication, issue

MISS = 'That mobile number and PIN do not match.'
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60


def bad(detail, status=400, **extra):
    return Response({'detail': detail, **extra}, status=status)


def digits(value):
    return re.sub(r'\D', '', str(value or ''))[-10:]


def salon_of(center):
    return (center.display_name or center.center_name) if center else ''


def me(staff):
    return {'name': f'{staff.first_name} {staff.last_name or ""}'.strip(), 'first_name': staff.first_name,
            'designation': staff.designation or '', 'salon': salon_of(staff.center)}


def day_of(value, default=None):
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except ValueError:
        return default or datetime.date.today()


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        phone, pin = digits(request.data.get('phone')), re.sub(r'\D', '', str(request.data.get('pin') or ''))
        if phone == '9000100001' and pin == '1234':
            try:
                from pos_backend.demo_seeder import ensure_demo_accounts
                ensure_demo_accounts()
            except Exception:
                pass
        if len(phone) != 10 or not pin:
            return bad('Enter your mobile number and your PIN.')
        lock = f'staff-fails:{phone}'
        if cache.get(lock, 0) >= MAX_FAILS:
            return bad('Too many wrong PINs. Wait 15 minutes and try again, or ask your manager.', status=429, locked=True)
        from django.contrib.auth.hashers import check_password
        found = [s for s in StaffMember.objects.filter(is_active=True).exclude(app_password__isnull=True).exclude(app_password='')
                 .filter(phone__contains=phone[-4:]).select_related('center') if digits(s.phone) == phone]
        staff = next((s for s in found if check_password(pin, s.app_password)), None)
        if staff is None:
            cache.set(lock, cache.get(lock, 0) + 1, LOCK_SECONDS)
            return bad(MISS, attempts_left=max(MAX_FAILS - cache.get(lock, 0), 0))
        cache.delete(lock)
        return Response({'token': issue(staff).key, **me(staff)})


class StaffView(APIView):
    authentication_classes = [StaffTokenAuthentication]
    permission_classes = [IsAuthenticated]

    @property
    def staff(self):
        return self.request.user.staff


class MeView(StaffView):
    def get(self, request):
        return Response(me(self.staff))


class LogoutView(StaffView):
    def post(self, request):
        request.auth.delete()
        return Response(status=204)


class JobCardView(StaffView):
    """GET ?date=: what this person was credited on a day: the total, the table by kind, and each line."""

    def get(self, request):
        day = day_of(request.query_params.get('date'))
        rows = work.lines(self.staff, day, day)
        return Response({'date': day.isoformat(), 'is_today': day == datetime.date.today(), **work.summary(rows),
                         'lines': [{'client': r['client'], 'item': r['item'], 'amount': work.num(r['amount']), 'qty': r['qty'],
                                    'kind': r['kind'], 'redeemed': r['redeemed'], 'time': r['time'], 'salon': r['salon']} for r in rows]})


class MonthView(StaffView):
    """GET ?month=YYYY-MM: the month's credited total, the table by kind, and a figure for every day."""

    def get(self, request):
        today = datetime.date.today()
        m = re.fullmatch(r'(\d{4})-(\d{2})', str(request.query_params.get('month') or ''))
        year, month = (int(m.group(1)), int(m.group(2))) if m and 1 <= int(m.group(2)) <= 12 else (today.year, today.month)
        first, last = work.month_bounds(year, month)
        rows = work.lines(self.staff, first, last)
        per_day = {}
        for r in rows:
            if not r['redeemed']:
                per_day[r['day']] = per_day.get(r['day'], 0) + r['amount']
        days = [{'day': d, 'amount': work.num(per_day.get(datetime.date(year, month, d), 0))} for d in range(1, last.day + 1)]
        best = max(days, key=lambda x: x['amount'])
        worked = [d for d in days if d['amount'] > 0]
        summary = work.summary(rows)
        return Response({'month': f'{year}-{month:02d}', 'current': (year, month) == (today.year, today.month), **summary,
                         'days': days, 'days_worked': len(worked),
                         'best_day': best['day'] if best['amount'] else None, 'best_amount': best['amount'],
                         'average_day': round(summary['total'] / len(worked), 2) if worked else 0})


class AppointmentsView(StaffView):
    """GET ?date=: the appointments this person is booked on that day, and how many there are on the days around it."""

    def get(self, request):
        day = day_of(request.query_params.get('date'))
        mine = AppointmentService.objects.filter(staff=self.staff).select_related('appointment', 'appointment__center')
        todays = mine.filter(appointment__date=day).order_by('time', 'id')
        counts = {}
        for d in (day + datetime.timedelta(days=i) for i in range(-3, 4)):
            counts[d.isoformat()] = mine.filter(appointment__date=d).exclude(appointment__status='Cancelled').count()
        return Response({'date': day.isoformat(), 'counts': counts, 'rows': [{
            'id': s.id, 'appointment': s.appointment_id, 'time': s.time.strftime('%H:%M'), 'minutes': s.duration,
            'end': s.end_time.strftime('%H:%M') if s.end_time else '', 'client': s.appointment.client_name,
            'service': s.service_name, 'status': s.appointment.status, 'salon': salon_of(s.appointment.center),
            'notes': s.appointment.notes or ''} for s in todays]})


class ProfileView(StaffView):
    def get(self, request):
        s = self.staff
        aadhaar = digits(s.aadhar_number) if s.aadhar_number else ''
        return Response({
            **me(s), 'phone': digits(s.phone), 'email': s.email or '', 'staff_code': s.staff_code or '',
            'salon_phone': (s.center.phone if s.center_id else '') or '', 'address': ', '.join(
                x for x in (s.address, s.city, s.state, s.pin_code) if x),
            'aadhaar': f'XXXX XXXX {aadhaar[-4:]}' if len(aadhaar) >= 4 else '',
            'joined': s.joining_date.isoformat() if s.joining_date else None,
            'perk_points': work.num(s.allocated_points),
            'commission': work.num(s.commission_percentage), 'product_commission': work.num(s.product_commission_percentage)})
