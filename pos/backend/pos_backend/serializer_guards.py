"""Fields only the server writes (finding R20, mass assignment).

Serializers declared with `fields = '__all__'` accepted every column from the
browser, including the SAP sync result - a client could mark a bill, product
or transfer as "synced to SAP" and the posting would never be retried, or
plant a fake SAP document number.

`ServerFieldsReadOnly` makes those columns read-only wherever they appear,
so no serializer can forget one.
"""

SERVER_ONLY_FIELDS = frozenset({
    'sap_sync_status', 'sap_sync_message', 'sap_doc_entry', 'sap_po_doc_entry',
})


class RangeChecks:
    """Refuse values that make no sense: a negative price, a discount or
    commission over 100%. Mix in BEFORE serializers.ModelSerializer and list the
    fields; `validate` of the serializer itself still runs after.

    Promotions, cards, memberships, packages, incentives and petty cash all
    accepted -100, 250% and 300%.
    """

    non_negative_fields = ()
    percent_fields = ()

    def validate(self, attrs):
        errors = {}
        for name in self.non_negative_fields:
            value = attrs.get(name)
            if value is not None and value < 0:
                errors[name] = 'This cannot be negative.'
        for name in self.percent_fields:
            value = attrs.get(name)
            if value is not None and not (0 <= value <= 100):
                errors[name] = 'This is a percentage: enter a number from 0 to 100.'
        if errors:
            from rest_framework import serializers
            raise serializers.ValidationError(errors)
        return super().validate(attrs)


class ServerFieldsReadOnly:
    """Mix in BEFORE serializers.ModelSerializer."""

    server_only_fields = SERVER_ONLY_FIELDS

    def get_fields(self):
        fields = super().get_fields()
        for name in self.server_only_fields & set(fields):
            fields[name].read_only = True
            fields[name].required = False
        return fields
