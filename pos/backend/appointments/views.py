from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from django.db import transaction
from .models import Appointment, AppointmentService
from .serializers import AppointmentSerializer
import datetime
from pos_backend.permissions import IsOwner, HasModulePermission
from pos_backend.scoping import coerce_center_id, safe_date_param
from pos_backend.scoping import narrow_to_selection

class AppointmentViewSet(viewsets.ModelViewSet):
    serializer_class = AppointmentSerializer
    permission_classes = [permissions.IsAuthenticated, HasModulePermission]
    permission_module = 'appointments'
    permission_submodule = 'calendar'
    def get_queryset(self):
        user = self.request.user
        # `invoices` is prefetched because the serializer reads it for
        # invoice_id and invoice_status - two extra queries per row otherwise.
        queryset = Appointment.objects.select_related(
            'client', 'center'
        ).prefetch_related(
            'services', 'services__staff', 'invoices'
        ).order_by('-date', '-start_time')
        role = getattr(user, 'role', None)
        is_owner = IsOwner.check_is_owner(user)
        perms = getattr(role, 'permissions', {}) or {}

        if not is_owner and not perms.get('all_centers', False):
            if user.centers.exists():
                queryset = queryset.filter(center__in=user.centers.all())
            elif hasattr(user, 'center') and user.center:
                queryset = queryset.filter(center=user.center)

            else:
                # Fail closed: a user assigned to no center sees nothing.
                queryset = queryset.none()
        center_id = coerce_center_id(self.request.query_params.get('center_id'))
        if center_id:
            queryset = queryset.filter(center_id=center_id)
        queryset = narrow_to_selection(queryset, 'center')

        # `safe_date_param`, not the raw string. `?date=not-a-date` reached the
        # ORM and raised Django's ValidationError - an unhandled 500 on the
        # appointments list, from one bad query parameter. Every other module was
        # fixed for this; appointments was missed because it had no tests.
        date_str = safe_date_param(self.request.query_params.get('date'))
        if date_str:
            queryset = queryset.filter(date=date_str)

        # A date range, which the calendar view needs and could not ask for.
        start_date = safe_date_param(self.request.query_params.get('start_date'))
        if start_date:
            queryset = queryset.filter(date__gte=start_date)

        end_date = safe_date_param(self.request.query_params.get('end_date'))
        if end_date:
            queryset = queryset.filter(date__lte=end_date)

        client_phone = self.request.query_params.get('client_phone')
        if client_phone:
            queryset = queryset.filter(client_phone=client_phone.strip())

        status_filter = self.request.query_params.get('status')
        if status_filter:
            queryset = queryset.filter(status__iexact=status_filter.strip())

        return queryset

    @action(detail=False, methods=['get'])
    def hours(self, request):
        """The branch's booking hours, slot size and default duration."""
        from salon_admin.models import Center
        from .rules import branch_hours

        center = None
        center_id = coerce_center_id(request.query_params.get('center_id'))
        if center_id:
            center = Center.objects.filter(pk=center_id).first()
        return Response(branch_hours(center))

    def _check_double_booking(self, lines, appt_date, exclude_appt_id=None):
        """Is any stylist double-booked by these lines on this date? (#21)

        Returns (is_conflict, message). Lines are prepared (see prepare_lines):
        a parsed time, whole minutes and a staff_id. Unallocated lines are
        never a clash.
        """
        from staff.models import StaffMember

        staffed = sorted((l for l in lines if l.get('staff_id') and l.get('time')),
                         key=lambda l: l['staff_id'])

        def span(line):
            start = line['time'].hour * 60 + line['time'].minute
            return start, start + max(int(line.get('duration') or 30), 1)

        # Two lines of this one booking for the same stylist.
        seen = {}
        for line in staffed:
            start, end = span(line)
            for prev_start, prev_end in seen.get(line['staff_id'], []):
                if start < prev_end and end > prev_start:
                    return True, ('This booking has two overlapping services for the same '
                                  'staff member. Give them different times or different staff.')
            seen.setdefault(line['staff_id'], []).append((start, end))

        for line in staffed:
            staff_id = line['staff_id']
            # Lock the stylist (in id order, so two bookings can't deadlock)
            # to serialise bookings for them.
            staff_obj = StaffMember.objects.select_for_update().filter(id=staff_id).first()
            staff_name = (f"{staff_obj.first_name} {staff_obj.last_name or ''}".strip()
                          if staff_obj else f'Staff #{staff_id}')
            existing = AppointmentService.objects.filter(
                staff_id=staff_id, appointment__date=appt_date,
                appointment__status__in=Appointment.BOOKED,
            )
            if exclude_appt_id:
                existing = existing.exclude(appointment_id=exclude_appt_id)
            new_start, new_end = span(line)
            for other in existing:
                other_start = other.time.hour * 60 + other.time.minute
                other_end = other_start + max(int(other.duration or 30), 1)
                if new_start < other_end and new_end > other_start:
                    return True, (
                        f"{staff_name} already has an appointment at "
                        f"{other.time.strftime('%I:%M %p')} on {appt_date}. "
                        f"Please choose a different time slot."
                    )
        return False, None

    def _require(self, action_key, message):
        from rest_framework.exceptions import PermissionDenied
        from pos_backend.permissions import has_action_permission
        if not has_action_permission(self.request.user, 'appointments', 'calendar', action_key):
            raise PermissionDenied(message)

    @staticmethod
    def _blacklisted(phone):
        from clients.identity import phone_key
        from clients.models import Client
        key = phone_key(phone)
        return bool(key) and Client.objects.filter(phone_key=key, is_blacklisted=True).exists()

    def _check_booking(self, center, appt_date, lines, exclude_appt_id=None):
        """Branch hours (#20) and the stylist clash guard (#21)."""
        from rest_framework.exceptions import ValidationError
        from .rules import hours_problem

        problem = hours_problem(center, appt_date, lines)
        if problem:
            raise ValidationError({'services': problem})
        is_conflict, error_msg = self._check_double_booking(lines, appt_date, exclude_appt_id)
        if is_conflict:
            raise ValidationError(error_msg)

    @transaction.atomic
    def perform_create(self, serializer):
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from pos_backend.scoping import can_use_center
        from .rules import CREATE_STATUSES
        from .serializers import prepare_lines

        center = serializer.validated_data.get('center')
        if center is not None and not can_use_center(self.request.user, center.pk):
            raise PermissionDenied("You cannot create appointments for this center.")

        status = serializer.validated_data.get('status', 'Scheduled')
        if status not in CREATE_STATUSES:
            raise ValidationError({'status': 'A new appointment is Scheduled or Checked-in.'})
        if CREATE_STATUSES[status]:
            self._require(CREATE_STATUSES[status], 'Checking a client in needs the "check_in" permission.')

        if self._blacklisted(serializer.validated_data.get('client_phone')):
            raise ValidationError("Client is blacklisted and cannot book appointments.")

        lines = prepare_lines(serializer.initial_data.get('services', []), center)
        self._check_booking(center, serializer.validated_data.get('date'), lines)

        serializer.prepared_lines = lines
        appt = serializer.save()
        self._link_client(appt)

    @transaction.atomic
    def perform_update(self, serializer):
        """Check the appointment as it will END UP, whatever the request changed.

        One validator for update, assign and restore (B5.5): a date move, a
        restore or a new stylist is re-checked for clashes (#21, #22) and
        branch hours (#20); status moves and stylist changes need their ticks.
        """
        from rest_framework.exceptions import PermissionDenied, ValidationError
        from pos_backend.scoping import can_use_center
        from .rules import changed_staff, status_action
        from .serializers import prepare_lines

        instance = Appointment.objects.select_for_update().get(pk=serializer.instance.pk)
        data = serializer.validated_data
        center = data.get('center', instance.center)
        if center is not None and not can_use_center(self.request.user, center.pk):
            raise PermissionDenied("You cannot move appointments to this center.")

        new_status = data.get('status', instance.status)
        allowed, tick, message = status_action(instance.status, new_status)
        if not allowed:
            raise ValidationError({'status': message})
        if tick:
            self._require(tick, f'Changing an appointment to {new_status} needs the "{tick}" permission.')

        client_phone = data.get('client_phone')
        if client_phone and client_phone != instance.client_phone and self._blacklisted(client_phone):
            raise ValidationError("Client is blacklisted and cannot book appointments.")

        sent = serializer.initial_data.get('services')
        if sent is not None:
            lines = prepare_lines(sent, center)
            if changed_staff(instance, lines):
                self._require('assign_staff', 'Changing the stylist needs the "assign_staff" permission.')
        else:
            if center != instance.center and instance.services.filter(
                    staff__isnull=False).exclude(staff__center=center).exists():
                raise ValidationError({'center': 'The booked stylists do not work at that branch.'})
            lines = [{'service_id': s.service_id, 'service_name': s.service_name, 'time': s.time,
                      'duration': s.duration, 'staff_id': s.staff_id}
                     for s in instance.services.all() if s.time]

        appt_date = data.get('date', instance.date)
        moved = (sent is not None or appt_date != instance.date
                 or center != instance.center or new_status != instance.status)
        if new_status in Appointment.BOOKED and moved:
            self._check_booking(center, appt_date, lines, exclude_appt_id=instance.id)

        serializer.prepared_lines = lines if sent is not None else None
        appt = serializer.save()
        self._link_client(appt)

    def _link_client(self, appt):
        if not appt.client and appt.client_phone:
            from clients.identity import phone_key
            from clients.models import Client
            key = phone_key(appt.client_phone)
            client = Client.objects.filter(phone_key=key).first() if key else None
            if client:
                appt.client = client
                appt.save(update_fields=['client'])
