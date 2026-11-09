from rest_framework import serializers

from pos_backend.serializer_guards import ServerFieldsReadOnly
from .models import Client, ClientMembership, ClientPackage, ClientValueCard
from salon_admin.models import Center
from marketing.serializers import MembershipSerializer, PackageSerializer, ValueCardSerializer
from django.db.models import Sum


class _SoldAt:
    """`sold_at`: the branch that sold the perk - the only one it is used at
    (#32, #36) - so the till can offer only what works at its branch."""

    def get_sold_at(self, obj):
        from billing.engine import perk_branch_id
        return perk_branch_id(obj)


class ClientMembershipSerializer(_SoldAt, serializers.ModelSerializer):
    membership_detail = MembershipSerializer(source='membership', read_only=True)
    sold_at = serializers.SerializerMethodField()

    class Meta:
        model = ClientMembership
        fields = '__all__'


class ClientPackageSerializer(_SoldAt, serializers.ModelSerializer):
    package_detail = PackageSerializer(source='package', read_only=True)
    sold_at = serializers.SerializerMethodField()

    class Meta:
        model = ClientPackage
        fields = '__all__'


class ClientValueCardSerializer(_SoldAt, serializers.ModelSerializer):
    value_card_detail = ValueCardSerializer(source='value_card', read_only=True)
    sold_at = serializers.SerializerMethodField()
    number = serializers.CharField(read_only=True)
    terms = serializers.SerializerMethodField()

    class Meta:
        model = ClientValueCard
        fields = '__all__'

    def get_terms(self, obj):
        face, pre, rate = obj.terms()
        return {'face_value': str(face), 'pre_tax_price': str(pre), 'gst_percent': str(rate)}


class ClientSerializer(ServerFieldsReadOnly, serializers.ModelSerializer):
    center = serializers.PrimaryKeyRelatedField(queryset=Center.objects.all(), allow_null=True, required=False)
    center_detail = serializers.SerializerMethodField()
    active_memberships = serializers.SerializerMethodField()
    active_packages = serializers.SerializerMethodField()
    active_value_cards = serializers.SerializerMethodField()
    # advance_balance uses the model property — it is computed per-client on read.
    advance_balance = serializers.SerializerMethodField()
    cashback_balance = serializers.SerializerMethodField()

    def get_advance_balance(self, obj):
        if hasattr(obj, 'advance_balance_annotated'):
            return obj.advance_balance_annotated
        return obj.advance_balance

    def get_cashback_balance(self, obj):
        if hasattr(obj, 'cashback_balance_annotated'):
            return obj.cashback_balance_annotated
        return getattr(obj, 'cashback_balance', 0)

    class Meta:
        model = Client
        # The old PIN column is unused; the sealed PIN is never sent to the till (it only checks a PIN, see
        # clients/pins.py), so staff cannot read a client's PIN.
        exclude = ('app_pin', 'pin_sealed')
        read_only_fields = ('phone_key',)

    def validate_pin_code(self, value):
        value = (value or '').strip()
        if value and (len(value) != 6 or not value.isdigit() or value[0] == '0'):
            raise serializers.ValidationError('A PIN code is 6 digits and does not start with 0.')
        return value

    def validate_phone(self, value):
        from clients.identity import normalise_phone, phone_problem
        value = normalise_phone(value)
        # A phone that is being set or changed must be a real number. Legacy
        # codes (CUST...) already on a client are kept as they are.
        unchanged = self.instance is not None and value == (self.instance.phone or '')
        if value and not unchanged:
            problem = phone_problem(value)
            if problem:
                raise serializers.ValidationError(problem)
        return value

    def validate_birthday(self, value):
        import datetime
        if value is not None and (value > datetime.date.today() or value.year < 1900):
            raise serializers.ValidationError('Enter a real date of birth (not in the future, not before 1900).')
        return value

    def validate_gst_number(self, value):
        from clients.identity import gstin_problem
        value = (value or '').strip().upper() or None
        problem = gstin_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate(self, attrs):
        from clients.identity import phone_key

        # Name and phone are required for a NEW client (F3). The legacy B2B
        # records without a phone (Q1) may still be edited without one.
        if self.instance is None:
            if not str(attrs.get('first_name') or '').strip():
                raise serializers.ValidationError({'first_name': 'Name is required.'})
            if not attrs.get('phone'):
                raise serializers.ValidationError({'phone': 'Phone number is required.'})
        elif 'phone' in attrs and not attrs['phone'] and self.instance.phone:
            raise serializers.ValidationError({'phone': 'A phone number cannot be removed.'})

        # One client per phone across the business (F2), named so the
        # operator can find the existing record.
        key = phone_key(attrs.get('phone')) if 'phone' in attrs else None
        if key:
            clash = Client.objects.filter(phone_key=key)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            other = clash.order_by('-is_active', 'id').first()
            if other is not None and not other.is_active and self.instance is None:
                # Registering an archived client's number brings that client
                # back (the view reactivates it) rather than refusing.
                self.reactivate = other
                other = None
            if other is not None:
                raise serializers.ValidationError(
                    {'phone': f'This number is already used by {other.full_name or "another client"}'
                              f' (client #{other.pk}).'})

        # Blacklisting is its own permission (#12).
        request = self.context.get('request')
        if request is not None:
            # Only an actual change counts: the till sends the whole client
            # back on every save, blacklist fields included.
            current = bool(getattr(self.instance, 'is_blacklisted', False))
            current_reason = (getattr(self.instance, 'blacklist_reason', None) or '').strip()
            changed = (bool(attrs.get('is_blacklisted', current)) != current
                       or (attrs.get('blacklist_reason', current_reason) or '').strip() != current_reason)
            if changed:
                from pos_backend.permissions import has_action_permission
                if not has_action_permission(request.user, 'admin', 'clients', 'blacklist'):
                    raise serializers.ValidationError(
                        {'is_blacklisted': 'Blacklisting a client needs the "blacklist" permission.'})
        return attrs

    def to_internal_value(self, data):
        # Convert frontend empty strings to Python None to prevent DRF validation crashes
        data = data.copy() if hasattr(data, 'copy') else data
        for field in ['birthday', 'email', 'gst_number', 'notes', 'blacklist_reason']:
            if field in data and data[field] == '':
                data[field] = None
        return super().to_internal_value(data)

    def get_center_detail(self, obj):
        if obj.center:
            return {
                'id': obj.center.id,
                'display_name': obj.center.display_name,
                'center_name': obj.center.display_name or obj.center.center_name,
                'address': obj.center.address,
            }
        return None

    def get_active_memberships(self, obj):
        from datetime import date
        today = date.today()
        active = [m for m in obj.memberships.all() if m.is_active and m.expiry_date >= today]
        return ClientMembershipSerializer(active, many=True).data

    def get_active_packages(self, obj):
        from datetime import date
        today = date.today()
        active = [p for p in obj.packages.all() if p.is_active and p.expiry_date >= today]
        return ClientPackageSerializer(active, many=True).data

    def get_active_value_cards(self, obj):
        from datetime import date
        today = date.today()
        active = [c for c in obj.value_cards.all() if c.is_active and c.expiry_date >= today and c.balance > 0]
        return ClientValueCardSerializer(active, many=True).data
