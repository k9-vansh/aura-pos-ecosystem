"""PIN endpoints of the POS: the till checks a client's PIN (or the branch's master PIN), asks for a client's PIN to be
sent again, and people who hold the "Master PINs" tick read each branch's master PIN of the day."""
import datetime

from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from pos_backend.permissions import HasModulePermission, has_module_permission
from pos_backend.scoping import can_use_center, scope_to_centers
from salon_admin.models import Center

from . import pins
from .models import Client


class TillPinView(APIView):
    """Base of the till's PIN calls: whoever raises bills (Billing > New Invoice) may ask."""
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'billing'
    permission_submodule = 'new_invoice'

    @staticmethod
    def parties(request):
        client = Client.objects.filter(pk=request.data.get('client'), is_active=True).first()
        center = Center.objects.filter(pk=request.data.get('center')).first()
        if client is None or center is None:
            raise ValidationError({'detail': 'Choose the client and the branch first.'})
        if not can_use_center(request.user, center.pk):
            raise PermissionDenied('You cannot bill at that branch.')
        return client, center


class PinVerifyView(TillPinView):
    """POST {client, center, pin} or {client, center, pin, master: true}: a pass for the payment, or a refusal."""

    def post(self, request):
        client, center = self.parties(request)
        pin = request.data.get('pin')
        try:
            if request.data.get('master'):
                if pins.verify_master(center, pin, user=request.user, client=client):
                    return Response({'ok': True, 'via': 'master', 'token': pins.issue_token(client, center, request.user, 'master')})
                return Response({'detail': 'That is not the master PIN of this branch for today.'}, status=400)
            if not pins.has_pin(client):
                return Response({'detail': 'This client has no PIN yet. Use "Send PIN", or the master PIN.', 'no_pin': True}, status=400)
            if pins.verify_client(client, pin, center=center, user=request.user):
                return Response({'ok': True, 'via': 'client', 'token': pins.issue_token(client, center, request.user, 'client')})
            return Response({'detail': 'That PIN is not right.', 'attempts_left': pins.attempts_left(client)}, status=400)
        except pins.PinLocked as exc:
            return Response({'detail': str(exc), 'locked': True}, status=429)


class PinNeededView(TillPinView):
    """POST {client, center}: does spending this client's card, advance or wallet here need their PIN? The till asks
    as soon as one of them is chosen, so the PIN is asked then and not when the bill is finished."""

    def post(self, request):
        from billing import pin_gate
        client, center = self.parties(request)
        return Response({'needed': pin_gate.holds_back(client, center)})


class PinSendView(TillPinView):
    """POST {client, center}: the client gets a NEW PIN on WhatsApp or e-mail (the old one stops working).

    A PIN is never shown to the person at the till, so it can only be the client who knows it."""

    def post(self, request):
        from django.utils import timezone
        from .models import PinAttempt
        client, center = self.parties(request)
        recent = PinAttempt.objects.filter(kind='send', client=client, at__gte=timezone.now() - datetime.timedelta(hours=1)).count()
        if recent >= 3:
            return Response({'detail': 'A PIN was sent three times in the last hour. Try again later.'}, status=429)
        from notifications.service import client_channels
        if not client_channels(client):
            raise ValidationError({'detail': 'This client cannot be messaged (no WhatsApp consent or e-mail, or on DND). '
                                             'Ask for the master PIN instead.'})
        pins.set_pin(client)
        channels = pins.send_pin(client, 'It was just changed.', f'pin-send:{client.pk}:{timezone.now():%Y%m%d%H%M%S}')
        PinAttempt.objects.create(kind='send', ok=bool(channels), client=client, center=center, user_id=request.user.pk, via='till')
        return Response({'sent_to': channels})


class MasterPinsView(APIView):
    """GET: each branch's master PIN of today, for people whose role ticks Admin > Master PINs."""
    permission_classes = [IsAuthenticated, HasModulePermission]
    permission_module = 'admin'
    permission_submodule = 'master_pins'
    permission_strict_read = True

    def get(self, request):
        today = datetime.date.today()
        centers = scope_to_centers(Center.objects.all(), request.user, field='id').order_by('display_name', 'center_name')
        return Response({'date': today.isoformat(), 'pins': [
            {'center_id': c.pk, 'center': c.display_name or c.center_name, 'pin': pins.master_pin(c, today)} for c in centers]})
