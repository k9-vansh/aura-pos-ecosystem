from rest_framework import serializers

from .models import SystemLog


class SystemLogSerializer(serializers.ModelSerializer):
    outcome = serializers.SerializerMethodField()
    location_label = serializers.SerializerMethodField()
    ip_scope = serializers.SerializerMethodField()

    class Meta:
        model = SystemLog
        fields = [
            'id', 'timestamp',
            # Who
            'user_id_snapshot', 'user_name', 'user_email', 'user_role',
            # Where
            'center_id', 'center_name',
            # What
            'action', 'module', 'entity_type', 'entity_id', 'human_description',
            'status_code', 'outcome',
            # Device
            'device_type', 'browser', 'os_info',
            # Geo
            'ip_address', 'ip_scope', 'geo_city', 'geo_region', 'geo_country', 'geo_country_code',
            'location_label', 'geo_lat', 'geo_lng', 'geo_accuracy',
            # Technical
            'path', 'description', 'device_info',
        ]

    def get_outcome(self, obj):
        """'success', 'failed', or 'blocked' (a throttled sign-in). Rows written
        before this was recorded have no status and count as success."""
        code = obj.status_code
        if code is None or code < 400:
            return 'success'
        return 'blocked' if code == 429 else 'failed'

    def get_ip_scope(self, obj):
        from .middleware import _ip_scope
        return _ip_scope(obj.ip_address) if obj.ip_address else ''

    def get_location_label(self, obj):
        """Branch and place in one line: 'Delhi Main Warehouse - this computer'."""
        place = obj.geo_city or ''
        if obj.geo_region and obj.geo_city and obj.geo_city not in ('This computer', 'Local network'):
            place = f'{obj.geo_city}, {obj.geo_region}'
        if obj.geo_country and obj.geo_country != 'Local' and obj.geo_country not in place:
            place = f'{place}, {obj.geo_country}' if place else obj.geo_country
        parts = [p for p in (obj.center_name, place) if p]
        return ' - '.join(parts)
