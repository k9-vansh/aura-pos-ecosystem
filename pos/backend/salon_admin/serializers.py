from rest_framework import serializers
from .models import Center, Role

class CenterSerializer(serializers.ModelSerializer):
    mtd_revenue = serializers.DecimalField(max_digits=15, decimal_places=2, read_only=True, required=False)
    class Meta:
        model = Center
        fields = '__all__'
        # The SAP sync result is the sync's to record, not a form's (R20).
        read_only_fields = ('sap_sync_status', 'sap_sync_message')

    def validate_center_name(self, value):
        """Two centres with one name cannot be told apart in a drop-down, a report or SAP."""
        value = (value or '').strip()
        clash = Center.objects.filter(center_name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if value and clash.exists():
            raise serializers.ValidationError('A centre with this name already exists.')
        return value

    def validate_sap_branch_name(self, value):
        """The branch must be a business place SAP really has. Every bill, transfer and goods receipt of the
        centre is posted to it, and SAP refuses the whole document for a name it does not know ("Ahmedabad"
        for the business place SAP calls "Gujarat" stopped every posting of four centres). Checked only when
        SAP can be asked and only when the name is being changed: a SAP that cannot be read never blocks a save."""
        value = (value or '').strip()
        if not value or value == 'No Branch':
            return value
        if self.instance is not None and (self.instance.sap_branch_name or '').strip() == value:
            return value
        from inventory.places import _business_place_states
        places = _business_place_states()
        if places and value not in places:
            close = [name for name in places if name.lower() == value.lower()]
            hint = f' Did you mean "{close[0]}"?' if close else ''
            raise serializers.ValidationError(
                f'SAP has no business place called "{value}".{hint} SAP has: {", ".join(sorted(places))}.')
        return value

    def validate_pin_code(self, value):
        value = (value or '').strip()
        if value and (len(value) != 6 or not value.isdigit() or value[0] == '0'):
            raise serializers.ValidationError('A PIN code is 6 digits and does not start with 0.')
        return value

    def validate_gst_number(self, value):
        from clients.identity import gstin_problem
        value = (value or '').strip().upper() or None
        problem = gstin_problem(value)
        if problem:
            raise serializers.ValidationError(problem)
        return value

    def validate_monthly_target(self, value):
        if value is not None and value < 0:
            raise serializers.ValidationError('A monthly target cannot be negative.')
        return value

    def _phone(self, value):
        import re
        value = (value or '').strip()
        if not value:
            return value or None
        digits = re.sub(r'[\s+\-().]', '', value)
        if not digits.isdigit() or not 8 <= len(digits) <= 15:
            raise serializers.ValidationError('Enter a phone number with 8 to 15 digits.')
        return value

    validate_phone = validate_owner_phone = validate_owner_phone_2 = validate_owner_phone_3 = \
        validate_accountant_phone_1 = validate_accountant_phone_2 = _phone

    def to_internal_value(self, data):
        # "dl01 " is DL01; an empty box means no code (one is assigned on save).
        if hasattr(data, 'get') and 'branch_code' in data:
            data = data.copy() if hasattr(data, 'copy') else dict(data)
            data['branch_code'] = (str(data.get('branch_code') or '').strip().upper()) or None
        return super().to_internal_value(data)

class RoleSerializer(serializers.ModelSerializer):
    users_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Role
        fields = ['id', 'name', 'description', 'permissions', 'users_count']

    def validate_permissions(self, value):
        """Reject module or submodule keys the app does not define.

        `permissions` is a JSONField, so a typo used to be saved happily and then
        grant nothing - which looks exactly like a broken permission system, and
        is very hard to spot on a screen of 42 toggles. The valid keys come from
        pos_backend/permission_tree.json, the same file the Roles screen renders
        from and the same one the server enforces against.

        Legacy keys are still accepted: a role saved before Marketing > Campaign
        was split into four holds `campaigns`.
        """
        from pos_backend.permission_tree import unknown_keys, non_boolean_grants, with_implied_view

        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'Permissions must be an object of modules.')

        not_bool = non_boolean_grants(value)
        if not_bool:
            raise serializers.ValidationError(
                'Each permission must be true or false (not text): '
                + ', '.join(not_bool[:10]))

        problems = unknown_keys(value)
        if problems:
            raise serializers.ValidationError({
                'unknown_keys': problems[:25],
                'detail': ('These module or submodule names are not part of this '
                           'application. A role saved with them would grant '
                           'nothing.'),
            })
        return with_implied_view(value)

    def validate_name(self, value):
        value = ' '.join(str(value or '').split())
        if not value:
            raise serializers.ValidationError('A role needs a name.')
        clash = Role.objects.filter(name__iexact=value)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError('A role with this name already exists.')
        return value
