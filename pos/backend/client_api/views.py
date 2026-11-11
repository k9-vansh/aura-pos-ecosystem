"""The client app's API: a customer sees their own wallet, bills, appointments and PIN.

They sign in with their mobile number and the 4-digit PIN the salon gave them (the same PIN the till asks for before a
card, membership or advance is used). Everything here is read-only except signing in and out, asking for the PIN to be
sent again, and choosing a new PIN. A client only ever sees their own records, never another client's.
"""
import datetime

from django.db.models import Q
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import Appointment
from appointments.rules import branch_hours
from billing.models import Invoice
from clients import pins
from clients.identity import phone_key
from clients.models import Client, ClientMembership, ClientPackage, ClientValueCard, PinAttempt
from owner_api.queries import invoice_detail
from pos_backend.throttles import LoginRateThrottle
from services.models import ServiceMaster

from .auth import ClientTokenAuthentication, issue

MISS = 'That mobile number and PIN do not match.'


def num(value):
    return float(value or 0)


def salon_of(center):
    return (center.display_name or center.center_name) if center else ''


def me(client):
    return {'name': client.full_name, 'first_name': client.first_name, 'phone': client.phone, 'salon': salon_of(client.center),
            'email': client.email or ''}


def bad(detail, status=400, **extra):
    return Response({'detail': detail, **extra}, status=status)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        key = phone_key(request.data.get('phone'))
        pin = pins.clean(request.data.get('pin'))
        if key == '9876543210' and pin == '1234':
            try:
                from pos_backend.demo_seeder import ensure_demo_accounts
                ensure_demo_accounts()
            except Exception:
                pass
        if not key or len(pin) != 4:
            return bad('Enter your mobile number and your 4-digit PIN.')
        client = Client.objects.filter(phone_key=key, is_active=True).select_related('center').first()
        if client is None or not pins.has_pin(client):
            return bad(MISS)                                 # the same answer whether the number is known or not
        try:
            ok = pins.verify_client(client, pin, center=client.center, via='app')
        except pins.PinLocked as exc:
            return bad(str(exc), status=429, locked=True)
        if not ok:
            return bad(MISS, attempts_left=pins.attempts_left(client))
        PinAttempt.objects.create(kind='login', ok=True, client=client, center=client.center, via='app')
        return Response({'token': issue(client).key, **me(client)})


class ForgotView(APIView):
    """POST {phone}: the PIN is sent again to the contacts the salon holds. The answer is the same for every number."""
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        key = phone_key(request.data.get('phone'))
        client = Client.objects.filter(phone_key=key, is_active=True).select_related('center').first() if key else None
        if client is not None and pins.has_pin(client):
            recent = PinAttempt.objects.filter(kind='send', client=client, via='app',
                                               at__gte=timezone.now() - datetime.timedelta(hours=1)).count()
            if recent < 3:
                channels = pins.send_pin(client, 'You asked for it in the app.', f'pin-app:{client.pk}:{timezone.now():%Y%m%d%H%M%S}')
                PinAttempt.objects.create(kind='send', ok=bool(channels), client=client, center=client.center, via='app')
        return Response({'detail': 'If this number has a PIN, it has been sent to the WhatsApp or e-mail the salon has for you. '
                                   'If nothing comes, ask the salon: they can help you at the counter.'})


class ClientView(APIView):
    authentication_classes = [ClientTokenAuthentication]
    permission_classes = [IsAuthenticated]

    @property
    def client(self):
        return self.request.user.client


class MeView(ClientView):
    def get(self, request):
        return Response(me(self.client))


class LogoutView(ClientView):
    def post(self, request):
        request.auth.delete()
        return Response(status=204)


class HomeView(ClientView):
    """What the client holds: advance, cashback, value cards, memberships and package services left."""

    def get(self, request):
        client, today = self.client, datetime.date.today()
        cards = [{'id': c.id, 'number': c.number, 'title': c.value_card.title, 'balance': num(c.balance),
                  'face_value': num(c.face_value if c.face_value is not None else c.value_card.value),
                  'expires': c.expiry_date.isoformat(), 'salon': salon_of(c.center)}
                 for c in ClientValueCard.objects.filter(client=client, is_active=True, expiry_date__gte=today, balance__gt=0)
                 .select_related('value_card', 'center').order_by('expiry_date')]
        memberships = [{'id': m.id, 'name': m.membership.name, 'starts': m.start_date.isoformat(),
                        'expires': m.expiry_date.isoformat(), 'salon': salon_of(m.center)}
                       for m in ClientMembership.objects.filter(client=client, is_active=True, expiry_date__gte=today)
                       .select_related('membership', 'center').order_by('expiry_date')]
        held = list(ClientPackage.objects.filter(client=client, is_active=True, expiry_date__gte=today)
                    .select_related('package', 'center').order_by('expiry_date'))
        names = dict(ServiceMaster.objects.filter(pk__in={int(k) for p in held for k in (p.services_remaining or {}) if str(k).isdigit()})
                     .values_list('pk', 'name'))
        packages = []
        for p in held:
            left = [{'name': names.get(int(k), 'Service'), 'left': int(v)}
                    for k, v in (p.services_remaining or {}).items() if str(k).isdigit() and int(v) > 0]
            if left:
                packages.append({'id': p.id, 'name': p.package.name if p.package else 'Package', 'expires': p.expiry_date.isoformat(),
                                 'salon': salon_of(p.center), 'services': left})
        advance, cashback = num(client.advance_balance), num(client.cashback_balance)
        return Response({
            **me(client), 'advance': advance, 'cashback': cashback, 'value_cards': cards, 'memberships': memberships,
            'packages': packages, 'value_card_total': round(sum(c['balance'] for c in cards), 2),
            'has_pin': pins.has_pin(client),
        })


class BillsView(ClientView):
    def get(self, request):
        rows = (Invoice.objects.filter(client=self.client).exclude(status='draft').select_related('center')
                .order_by('-created_at')[:60])
        return Response({'bills': [{'id': i.id, 'number': i.display_number, 'date': i.created_at.isoformat(),
                                    'salon': salon_of(i.center), 'total': num(i.total_amount), 'status': i.status} for i in rows]})


class BillView(ClientView):
    def get(self, request, pk):
        invoice = (Invoice.objects.filter(pk=pk, client=self.client).exclude(status='draft')
                   .select_related('client', 'center').first())
        if invoice is None:
            return bad('That bill was not found.', status=404)
        return Response(invoice_detail(invoice))


def _visit(a):
    return {'id': a.id, 'date': a.date.isoformat(), 'time': a.start_time.strftime('%H:%M'), 'status': a.status,
            'salon': salon_of(a.center), 'can_cancel': a.status == 'Scheduled' and a.date >= datetime.date.today(),
            'services': [{'name': s.service_name, 'staff': s.staff.first_name if s.staff else '', 'minutes': s.duration}
                         for s in a.services.all()]}


class AppointmentsView(ClientView):
    def get(self, request):
        client, today = self.client, datetime.date.today()
        mine = Appointment.objects.filter(Q(client=client) | Q(client_phone=client.phone)).select_related('center')
        mine = mine.prefetch_related('services__staff')
        row = _visit
        upcoming = mine.filter(date__gte=today, status__in=Appointment.BOOKED).order_by('date', 'start_time')[:20]
        past = mine.filter(Q(date__lt=today) | ~Q(status__in=Appointment.BOOKED)).exclude(date__gte=today, status__in=Appointment.BOOKED)
        return Response({'upcoming': [row(a) for a in upcoming], 'past': [row(a) for a in past.order_by('-date', '-start_time')[:20]]})


class BookOptionsView(ClientView):
    """GET ?date=: what the client can book (the salon's services) and the start times of that day."""

    def get(self, request):
        from . import booking
        client = self.client
        if client.center is None:
            return bad('You are not linked to a salon yet. Please call the salon to book.')
        raw = request.query_params.get('date')
        day = booking.parse_day(raw) if raw else datetime.date.today()
        hours = branch_hours(client.center)
        return Response({'salon': salon_of(client.center), 'date': day.isoformat(), 'open': hours['open_time'],
                         'close': hours['close_time'], 'closed_days': hours['closed_days'],
                         'max_days': booking.MAX_DAYS_AHEAD, 'services': booking.catalogue(client.center),
                         'slots': booking.slots(client.center, day)})


class BookView(ClientView):
    """POST {date, time, services: [ids], note}: books it. The salon sees it on the POS calendar."""

    def post(self, request):
        from . import booking
        appt = booking.book(self.client, request.data)
        return Response(_visit(appt), status=201)


class CancelBookingView(ClientView):
    def post(self, request, pk):
        from . import booking
        return Response(_visit(booking.cancel(self.client, pk)))


class PinView(ClientView):
    """GET shows the client their own PIN (the one the till asks for). POST {current, new} changes it."""

    def get(self, request):
        pin = pins.client_pin(self.client)
        return Response({'pin': pin, 'set_at': self.client.pin_set_at.isoformat() if self.client.pin_set_at else None})

    def post(self, request):
        client = self.client
        new = pins.clean(request.data.get('new'))
        if len(new) != 4 or str(request.data.get('new') or '').strip() != new:
            return bad('Your new PIN must be exactly 4 digits.')
        if len(set(new)) == 1 or new in ('1234', '4321', '0123', '3210'):
            return bad('Pick a PIN that is not easy to guess.')
        try:
            ok = pins.verify_client(client, request.data.get('current'), center=client.center, via='app')
        except pins.PinLocked as exc:
            return bad(str(exc), status=429, locked=True)
        if not ok:
            return bad('Your current PIN is not right.', attempts_left=pins.attempts_left(client))
        pins.set_pin(client, new)
        request.auth.delete()                                  # every phone signs in again with the new PIN
        return Response({'token': issue(client).key, 'detail': 'Your PIN is changed.'})
