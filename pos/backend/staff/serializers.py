"""
Staff serializers.

C10 - StaffMemberSerializer used `fields = '__all__'`, so
GET /staff/api/members/ returned every staff member's `app_password` hash,
`aadhar_number`, `salary` and commission percentages to any authenticated user,
with only IsAuthenticated in front of it. `app_password` was also writable, so
any logged-in user could reset another staff member's app PIN - and because the
login path kept a plaintext-comparison fallback, setting it to a plain string
worked as a credential.

Fields are now explicit. Credentials are never serialised in either direction;
compensation and identity documents are only included for users who are allowed
to see them.
"""

from rest_framework import serializers

from pos_backend.money import D
from pos_backend.permissions import IsOwner, has_module_permission
from pos_backend.serializer_guards import RangeChecks
from .models import (
    Designation, PayrollRecord, ServiceLog, StaffConsumptionLog, StaffMember,
    StaffToolTracker, StaffTransfer,
)

# Never leaves the server, in either direction.
CREDENTIAL_FIELDS = ('app_password',)

# Visible only to an owner or a user with the staff management permission.
SENSITIVE_FIELDS = ('salary', 'commission_percentage', 'product_commission_percentage',
                    'aadhar_number', 'allocated_points')


# Pay fields whose changes are written to the activity log (X15).
AUDITED_FIELDS = ('salary', 'commission_percentage', 'product_commission_percentage')


def mask_aadhaar(value):
    """'1234 5678 9012' -> 'XXXXXXXX9012'. Blank stays blank; an already masked value is kept."""
    digits = ''.join(ch for ch in str(value or '') if ch.isdigit())
    if not digits:
        return value if not value else ''
    return 'X' * 8 + digits[-4:]


class DesignationSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('salary',)
    percent_fields = ('commission_percentage', 'product_commission_percentage')

    class Meta:
        model = Designation
        fields = '__all__'

    def validate_name(self, value):
        value = ' '.join(str(value or '').split())
        if not value:
            raise serializers.ValidationError('A designation needs a name.')
        clash = Designation.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('That designation already exists.')
        return value


class StaffMemberSerializer(serializers.ModelSerializer):
    center_name = serializers.CharField(source='center.display_name', read_only=True)
    has_overdue_tools = serializers.BooleanField(source='has_overdue_tools_annotated', read_only=True, default=False)
    full_name = serializers.SerializerMethodField()
    # Write-only: set an app PIN without ever reading one back.
    new_app_pin = serializers.CharField(write_only=True, required=False, allow_blank=True,
                                        min_length=4, max_length=32)

    class Meta:
        model = StaffMember
        fields = (
            'id', 'center', 'center_name', 'first_name', 'last_name', 'full_name',
            'gender', 'designation', 'designation_fk', 'joining_date', 'staff_code',
            'email', 'phone', 'address', 'city', 'state', 'pin_code', 'image',
            'is_active', 'created_at',
            # Sensitive; stripped in to_representation unless permitted.
            'salary', 'commission_percentage', 'product_commission_percentage',
            'aadhar_number', 'allocated_points',
            # Derived
            'has_overdue_tools',
            # Write-only
            'new_app_pin',
        )
        read_only_fields = ('created_at',)

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name or ''}".strip()

    def get_has_overdue_tools(self, obj):
        # Fallback if the view didn't annotate
        from datetime import date
        return obj.tool_trackers.filter(
            status='Taken', expected_return_date__lt=date.today()
        ).exists()

    # ─── Field-level access control ───────────────────────────────────────

    def _may_see_sensitive(self):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if IsOwner.check_is_owner(user):
            return True
        # Salary and commission belong to Staff > Staff Profile, which the
        # Roles screen offered but nothing enforced (finding R9). Management is
        # still honoured so roles set up before this change keep their access.
        return (has_module_permission(user, 'staff', 'read', 'profile')
                or has_module_permission(user, 'staff', 'read', 'management'))

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if not self._may_see_sensitive():
            for field in SENSITIVE_FIELDS:
                data.pop(field, None)
        return data

    def _may_change_sensitive(self):
        """Seeing salary is Read on Staff Profile; changing it is Edit."""
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not user or not getattr(user, 'is_authenticated', False):
            return False
        if IsOwner.check_is_owner(user):
            return True
        return (has_module_permission(user, 'staff', 'update', 'profile')
                or has_module_permission(user, 'staff', 'update', 'management'))

    def validate_phone(self, value):
        """A mobile number, kept the way clients keep it (10 digits, +91 / leading 0 dropped)."""
        from clients.identity import normalise_phone, phone_problem
        value = normalise_phone(value)
        if not value:
            return None
        problem = phone_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate_pin_code(self, value):
        value = (value or '').strip()
        if value and (len(value) != 6 or not value.isdigit() or value[0] == '0'):
            raise serializers.ValidationError('A PIN code is 6 digits and does not start with 0.')
        return value or None

    def _check_not_a_duplicate(self, attrs):
        """One phone belongs to one staff member, and a staff code is used once per centre:
        the same person typed twice used to become two staff members (two payrolls)."""
        me = self.instance.pk if self.instance else None
        phone = attrs.get('phone')
        if phone and (self.instance is None or phone != self.instance.phone):
            if StaffMember.objects.filter(phone=phone).exclude(pk=me).exists():
                raise serializers.ValidationError({'phone': 'Another staff member already has this phone number.'})
        code = (attrs.get('staff_code') or '').strip()
        center = attrs.get('center') or (self.instance.center if self.instance else None)
        if code and (self.instance is None or code.lower() != (self.instance.staff_code or '').lower()):
            if StaffMember.objects.filter(center=center, staff_code__iexact=code).exclude(pk=me).exists():
                raise serializers.ValidationError({'staff_code': 'This staff code is already used at this centre.'})

    def validate(self, attrs):
        self._check_not_a_duplicate(attrs)
        if not self._may_change_sensitive():
            for field in SENSITIVE_FIELDS:
                if field in attrs:
                    raise serializers.ValidationError({
                        field: 'You do not have permission to change this field.'
                    })
        for field in ('salary', 'commission_percentage', 'product_commission_percentage'):
            if field in attrs and D(attrs[field]) < 0:
                raise serializers.ValidationError({field: 'Cannot be negative.'})
        for field in ('commission_percentage', 'product_commission_percentage'):
            if field in attrs and D(attrs[field]) > 100:
                raise serializers.ValidationError({field: 'Cannot exceed 100%.'})
        # The designation is picked from the master list. Typed free, a typo
        # silently made a new "designation" that no pay rule or report knew.
        # (With no designations set up yet, free text is still accepted.)
        chosen = attrs.get('designation_fk')
        if chosen is not None:
            attrs['designation'] = chosen.name
        elif attrs.get('designation') and Designation.objects.exists():
            match = Designation.objects.filter(name__iexact=str(attrs['designation']).strip()).first()
            if match is None:
                raise serializers.ValidationError({
                    'designation': f'"{attrs["designation"]}" is not in the designation list. '
                                   f'Add it under Staff > Management first, or pick an existing one.'})
            attrs['designation'] = match.name
            attrs['designation_fk'] = match
        return attrs

    def _apply_pin(self, instance, raw_pin, why='changed'):
        """Hash and store a new app PIN. Needs Staff > Directory > "set app PIN"."""
        from django.contrib.auth.hashers import make_password
        from pos_backend.permissions import has_action_permission

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if not has_action_permission(user, 'staff', 'directory', 'set_app_pin'):
            raise serializers.ValidationError({
                'new_app_pin': 'Setting a staff app PIN needs the "set app PIN" permission.'
            })
        if not raw_pin.isdigit():
            raise serializers.ValidationError({
                'new_app_pin': 'The app PIN must be digits only.'
            })
        instance.app_password = make_password(raw_pin)
        instance.save(update_fields=['app_password'])
        self._announce(instance, raw_pin, why)

    @staticmethod
    def _announce(instance, pin, why):
        """Tell the person their staff-app login (logo e-mail; the PIN shows in the terminal while WhatsApp is not
        linked). A message that cannot be sent never undoes saving the person."""
        try:
            from staff_api import welcome
            welcome.announce(instance, pin, why)
        except Exception:                                  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning('Could not announce the staff app login', exc_info=True)

    def create(self, validated_data):
        raw_pin = validated_data.pop('new_app_pin', None)
        instance = super().create(validated_data)
        if raw_pin:
            self._apply_pin(instance, raw_pin, 'added')
        elif instance.phone:
            # Everyone added to the team gets a login of their own, so nobody waits for a manager to type one in.
            from staff_api import welcome
            self._announce(instance, welcome.give_pin(instance), 'added')
        return instance

    def update(self, instance, validated_data):
        raw_pin = validated_data.pop('new_app_pin', None)
        before = {f: getattr(instance, f) for f in AUDITED_FIELDS}
        instance = super().update(instance, validated_data)
        self._audit_sensitive_changes(instance, before)
        if raw_pin:
            self._apply_pin(instance, raw_pin)
        return instance

    def _audit_sensitive_changes(self, instance, before):
        """Salary and commission changes leave a trace (X15): who, what, from and to."""
        changes = [f'{f}: {before[f]} -> {getattr(instance, f)}' for f in AUDITED_FIELDS
                   if str(before[f]) != str(getattr(instance, f))]
        if not changes:
            return
        from audit_logs.models import SystemLog

        request = self.context.get('request')
        user = getattr(request, 'user', None)
        who = user if getattr(user, 'is_authenticated', False) else None
        summary = f'Changed {instance} - ' + '; '.join(changes)
        try:
            SystemLog.objects.create(
                module='STAFF', action='UPDATE', entity_type='Staff Pay', entity_id=str(instance.pk),
                path='/staff/api/staff/', user=who,
                user_name=getattr(who, 'full_name', '') or '', user_email=getattr(who, 'email', '') or '',
                center_id=instance.center_id,
                center_name=(instance.center.display_name or instance.center.center_name) if instance.center else '',
                human_description=summary[:1000], description=summary[:2000])
        except Exception:                                            # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception('Could not log a staff pay change')

    def validate_aadhar_number(self, value):
        """Only the last four digits are kept (X16, Q9): a full ID number in the
        database is a liability under the DPDP Act and nothing in the POS needs it."""
        return mask_aadhaar(value)


class StaffAppProfileSerializer(serializers.ModelSerializer):
    """What the staff mobile app sees about the logged-in staff member.

    Deliberately narrow: no credential hash, no Aadhaar, no commission rates.
    """
    center_name = serializers.CharField(source='center.display_name', read_only=True)
    full_name = serializers.SerializerMethodField()

    class Meta:
        model = StaffMember
        fields = ('id', 'first_name', 'last_name', 'full_name', 'designation',
                  'staff_code', 'email', 'phone', 'image', 'center', 'center_name',
                  'joining_date', 'allocated_points')

    def get_full_name(self, obj):
        return f"{obj.first_name} {obj.last_name or ''}".strip()


class ServiceLogSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    center_name = serializers.CharField(source='center.display_name', read_only=True)
    invoice_number = serializers.CharField(source='invoice.display_number', read_only=True, allow_null=True)

    class Meta:
        model = ServiceLog
        fields = '__all__'
        # Commission rows are written by the billing flow, not by hand.
        read_only_fields = ('created_at', 'invoice')

    def get_staff_name(self, obj):
        name = obj.staff.first_name
        if obj.staff.last_name:
            name += f" {obj.staff.last_name}"
        return name

    def validate_price(self, value):
        if D(value) < 0:
            raise serializers.ValidationError('A price cannot be negative.')
        return value

    def validate(self, attrs):
        # The person must work at the branch the log is written for.
        staff = attrs.get('staff', getattr(self.instance, 'staff', None))
        center = attrs.get('center', getattr(self.instance, 'center', None))
        if (('staff' in attrs or 'center' in attrs)
                and staff is not None and center is not None and staff.center_id != center.id):
            raise serializers.ValidationError({'staff': 'This person does not work at that center.'})
        return attrs

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if instance.invoice and instance.invoice.client:
            data['client_name'] = instance.invoice.client.full_name
        return data


class StaffConsumptionLogSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    center_name = serializers.CharField(source='center.display_name', read_only=True)

    class Meta:
        model = StaffConsumptionLog
        fields = '__all__'

    def get_staff_name(self, obj):
        name = obj.staff.first_name
        if obj.staff.last_name:
            name += f" {obj.staff.last_name}"
        return name

    def validate_amount(self, value):
        if D(value) <= 0:
            raise serializers.ValidationError('Amount must be greater than zero.')
        return value


class StaffTransferSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    from_center_name = serializers.CharField(source='from_center.center_name', read_only=True)
    to_center_name = serializers.CharField(source='to_center.center_name', read_only=True)

    class Meta:
        model = StaffTransfer
        fields = '__all__'
        read_only_fields = ('applied',)

    def get_staff_name(self, obj):
        name = obj.staff.first_name
        if obj.staff.last_name:
            name += f" {obj.staff.last_name}"
        return name

    def validate(self, attrs):
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if start and end and end < start:
            raise serializers.ValidationError(
                {'end_date': 'The end date cannot be before the start date.'}
            )
        source = attrs.get('from_center', getattr(self.instance, 'from_center', None))
        target = attrs.get('to_center', getattr(self.instance, 'to_center', None))
        if source and target and source == target:
            raise serializers.ValidationError(
                {'to_center': 'The destination center must differ from the source.'}
            )
        # A new transfer moves the person, so it must start from where they
        # actually work; otherwise anyone could be pulled out of another branch.
        if self.instance is None:
            staff = attrs.get('staff')
            if staff is not None and source is not None and staff.center_id != source.id:
                raise serializers.ValidationError(
                    {'from_center': 'This person does not work at that center.'}
                )
        return attrs


class StaffToolTrackerSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    staff_center_name = serializers.CharField(source='staff.center.center_name', read_only=True)
    is_overdue = serializers.SerializerMethodField()

    class Meta:
        model = StaffToolTracker
        fields = '__all__'

    def get_staff_name(self, obj):
        name = obj.staff.first_name
        if obj.staff.last_name:
            name += f" {obj.staff.last_name}"
        return name

    def get_is_overdue(self, obj):
        from datetime import date
        return bool(
            obj.status == 'Taken' and obj.expected_return_date
            and obj.expected_return_date < date.today()
        )

    def validate(self, attrs):
        taken = attrs.get('date_taken', getattr(self.instance, 'date_taken', None))
        due = attrs.get('expected_return_date', getattr(self.instance, 'expected_return_date', None))
        back = attrs.get('actual_return_date', getattr(self.instance, 'actual_return_date', None))
        if taken and due and due < taken:
            raise serializers.ValidationError({'expected_return_date': 'The return date cannot be before the date taken.'})
        if taken and back and back < taken:
            raise serializers.ValidationError({'actual_return_date': 'A tool cannot be returned before it was taken.'})
        if 'amount' in attrs and attrs['amount'] is not None and not 1 <= attrs['amount'] <= 10000:
            raise serializers.ValidationError({'amount': 'The quantity must be between 1 and 10,000.'})
        return attrs


class PayrollRecordSerializer(serializers.ModelSerializer):
    staff_name = serializers.SerializerMethodField()
    center_name = serializers.CharField(source='center.display_name', read_only=True)
    computed_net_pay = serializers.SerializerMethodField()

    class Meta:
        model = PayrollRecord
        fields = '__all__'
        # Status changes go through the lock / mark_paid actions so they are
        # permission-checked and auditable.
        read_only_fields = ('created_at', 'updated_at', 'status')

    def get_staff_name(self, obj):
        name = obj.staff.first_name
        if obj.staff.last_name:
            name += f" {obj.staff.last_name}"
        return name

    def get_computed_net_pay(self, obj):
        from pos_backend.money import money
        return money(
            D(obj.base_salary) + D(obj.service_commission)
            + D(obj.product_commission) + D(obj.other_incentive)
            - D(obj.deductions)
        )

    def validate(self, attrs):
        """net_pay must equal its components.

        It used to be a free-form field that nothing cross-checked, so a
        payroll row could claim any figure at all.
        """
        from pos_backend.money import money

        def field(name):
            if name in attrs:
                return D(attrs[name])
            return D(getattr(self.instance, name, 0))

        expected = money(
            field('base_salary') + field('service_commission')
            + field('product_commission') + field('other_incentive')
            - field('deductions')
        )
        if expected < 0:
            raise serializers.ValidationError({
                'deductions': f'Deductions are more than the earnings (net pay would be Rs {expected}).'})
        if 'net_pay' in attrs:
            supplied = money(attrs['net_pay'])
            if supplied != expected:
                raise serializers.ValidationError({
                    'net_pay': f'Net pay Rs {supplied} does not match its components '
                               f'(base + commissions + incentive - deductions = Rs {expected}).'
                })
        else:
            attrs['net_pay'] = expected

        month = attrs.get('month', getattr(self.instance, 'month', None))
        if month is not None and not 1 <= int(month) <= 12:
            raise serializers.ValidationError({'month': 'Month must be between 1 and 12.'})

        for name in ('base_salary', 'service_commission', 'product_commission',
                     'other_incentive', 'deductions'):
            if name in attrs and D(attrs[name]) < 0:
                raise serializers.ValidationError({name: 'Cannot be negative.'})
        return attrs
