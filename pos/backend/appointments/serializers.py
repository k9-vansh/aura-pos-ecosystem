from rest_framework import serializers
from django.db import transaction
from .models import Appointment, AppointmentService

class AppointmentServiceSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    end_time = serializers.TimeField(read_only=True, format='%H:%M')

    class Meta:
        model = AppointmentService
        fields = '__all__'
        read_only_fields = ('appointment',)
        # A catalogue line fills these in (clean_service_data checks them).
        extra_kwargs = {'service_name': {'required': False}, 'duration': {'required': False}}

    def get_staff_name(self, obj):
        if obj.staff:
            return f"{obj.staff.first_name} {obj.staff.last_name or ''}".strip()
        return "Any Staff"


# Keys the client may send that are not model fields.
_SERVICE_READONLY_FIELDS = {'id', 'appointment', 'staff_name', 'end_time'}

# The only writable fields on a nested service line. Anything else is dropped
# rather than splatted into objects.create(), where an unexpected key raised
# TypeError and returned a 500.
_SERVICE_WRITABLE_FIELDS = {'service', 'service_id', 'service_name', 'time', 'duration', 'price',
                            'staff', 'staff_id'}


def _extract_service_staff_id(staff_val):
    """Normalise the staff value coming from the frontend.
    The frontend can send either an integer ID or the full staff object dict."""
    if staff_val is None:
        return None
    if isinstance(staff_val, dict):
        return staff_val.get('id')
    try:
        return int(staff_val)
    except (TypeError, ValueError):
        return None


def clean_service_data(service_data: dict, index: int = 0, default_duration: int = 30) -> dict:
    """Validate and coerce one nested service line.

    The old code passed `self.initial_data` straight into
    AppointmentService.objects.create(**data): completely unvalidated client
    input, where any unexpected key was a TypeError 500 and a non-numeric
    duration blew up at save time.
    """
    from decimal import Decimal, InvalidOperation

    if not isinstance(service_data, dict):
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: expected an object.'}
        )

    cleaned = {
        k: v for k, v in service_data.items()
        if k in _SERVICE_WRITABLE_FIELDS and k not in _SERVICE_READONLY_FIELDS
    }

    # The catalogue service (B5.5): fills the name and duration when the
    # line does not give them.
    catalogue = None
    raw_service = cleaned.pop('service', None) or cleaned.pop('service_id', None)
    if isinstance(raw_service, dict):
        raw_service = raw_service.get('id')
    if raw_service not in (None, ''):
        from services.models import ServiceMaster
        try:
            catalogue = ServiceMaster.objects.filter(pk=int(raw_service)).first()
        except (TypeError, ValueError):
            catalogue = None
        if catalogue is None:
            raise serializers.ValidationError(
                {'services': f'Service {index + 1}: not found in the service catalogue.'}
            )
        cleaned['service_id'] = catalogue.pk
        if not (cleaned.get('service_name') or '').strip():
            cleaned['service_name'] = catalogue.name
        if cleaned.get('duration') in (None, '') and catalogue.duration_mins:
            cleaned['duration'] = catalogue.duration_mins
    else:
        cleaned['service_id'] = None

    name = (cleaned.get('service_name') or '').strip()
    if not name:
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: service_name is required.'}
        )
    cleaned['service_name'] = name[:100]

    parsed_time = parse_time_value(cleaned.get('time'))
    if not parsed_time:
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: a valid time (HH:MM) is required.'}
        )
    cleaned['time'] = parsed_time

    raw_duration = cleaned.get('duration')
    try:
        duration = int(raw_duration) if raw_duration not in (None, '') else default_duration
    except (TypeError, ValueError):
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: duration must be a whole number of minutes.'}
        )
    if duration <= 0 or duration > 24 * 60:
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: duration must be between 1 and 1440 minutes.'}
        )
    cleaned['duration'] = duration

    raw_price = cleaned.get('price')
    try:
        price = Decimal(str(raw_price)) if raw_price not in (None, '') else Decimal('0.00')
    except (InvalidOperation, TypeError, ValueError):
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: price is not a valid amount.'}
        )
    if price < 0:
        raise serializers.ValidationError(
            {'services': f'Service {index + 1}: price cannot be negative.'}
        )
    cleaned['price'] = price

    return cleaned


def parse_time_value(value):
    """Accept 'HH:MM', 'HH:MM:SS' or a time object. Returns time or None."""
    import datetime as _dt

    if value is None or value == '':
        return None
    if hasattr(value, 'hour'):
        return value
    for fmt in ('%H:%M', '%H:%M:%S'):
        try:
            return _dt.datetime.strptime(str(value), fmt).time()
        except (TypeError, ValueError):
            continue
    return None


# Backwards-compatible alias.
_clean_service_data = clean_service_data

class AppointmentSerializer(serializers.ModelSerializer):
    services = AppointmentServiceSerializer(many=True, required=False)
    invoice_id = serializers.SerializerMethodField()
    invoice_status = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = '__all__'

    def _live_invoice(self, obj):
        """The appointment's non-reversed invoice, if any.

        `obj.invoices` is prefetched by the viewset. Without that prefetch this
        cost two extra queries per appointment - about 100 on a 50-appointment
        day view.
        """
        for invoice in obj.invoices.all():
            if invoice.status not in ('cancelled', 'refunded'):
                return invoice
        return None

    def get_invoice_id(self, obj):
        invoice = self._live_invoice(obj)
        return invoice.id if invoice else None

    def get_invoice_status(self, obj):
        invoice = self._live_invoice(obj)
        return invoice.status if invoice else None

    def validate_client_phone(self, value):
        from clients.identity import normalise_phone, phone_problem
        value = normalise_phone(value)
        unchanged = self.instance is not None and value == (self.instance.client_phone or '')
        if not unchanged:
            problem = phone_problem(value) if value else 'Enter the client\'s phone number.'
            if problem:
                raise serializers.ValidationError(problem)
        return value

    def validate_date(self, value):
        import datetime
        # A new booking, or one being moved, cannot be put in the past.
        moved = self.instance is None or value != self.instance.date
        if moved and value < datetime.date.today():
            raise serializers.ValidationError('An appointment cannot be booked for a date that has passed.')
        return value

    def _write_services(self, appointment, lines):
        """Create the prepared service lines (see prepare_lines)."""
        for line in lines:
            AppointmentService.objects.create(appointment=appointment, **line)

    @transaction.atomic
    def create(self, validated_data):
        validated_data.pop('services', None)
        lines = getattr(self, 'prepared_lines', None)
        if lines is None:
            lines = prepare_lines(self.initial_data.get('services', []), validated_data.get('center'))
        appointment = Appointment.objects.create(**validated_data)
        self._write_services(appointment, lines)
        return appointment

    @transaction.atomic
    def update(self, instance, validated_data):
        validated_data.pop('services', None)
        lines = getattr(self, 'prepared_lines', None)
        if lines is None and self.initial_data.get('services') is not None:
            lines = prepare_lines(self.initial_data.get('services'),
                                  validated_data.get('center', instance.center))

        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()

        if lines is not None:
            instance.services.all().delete()
            self._write_services(instance, lines)

        return instance


def prepare_lines(services_data, center):
    """Validate the nested service lines of a request for `center`.

    Returns dicts ready for AppointmentService.objects.create(): a parsed
    time, a whole-minute duration, the catalogue service and the stylist,
    who must work at the appointment's branch.
    """
    from staff.models import StaffMember
    from appointments.rules import branch_hours

    default_duration = branch_hours(center)['default_duration'] if center else 45
    lines = []
    for index, raw in enumerate(services_data or []):
        line = clean_service_data(dict(raw) if isinstance(raw, dict) else raw, index,
                                  default_duration=default_duration)
        staff_id = _extract_service_staff_id(line.pop('staff', None) or line.pop('staff_id', None))
        line.pop('staff', None)
        line.pop('staff_id', None)
        if staff_id:
            staff_member = StaffMember.objects.filter(id=staff_id).first()
            if not staff_member:
                raise serializers.ValidationError(
                    {'services': f'Service {index + 1}: staff member not found.'})
            if center is not None and staff_member.center_id != center.pk:
                raise serializers.ValidationError(
                    {'services': f'Service {index + 1}: {staff_member} does not work at this center.'})
        line['staff_id'] = staff_id
        lines.append(line)
    return lines
