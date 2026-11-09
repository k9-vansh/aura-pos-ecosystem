from rest_framework import serializers

from pos_backend.serializer_guards import RangeChecks
from .models import ServiceMaster, CenterService

class CenterServiceSerializer(serializers.ModelSerializer):
    class Meta:
        model = CenterService
        fields = '__all__'

class ServiceMasterSerializer(RangeChecks, serializers.ModelSerializer):
    # A service with a negative price, GST over 100% or a negative length used to be saved.
    non_negative_fields = ('default_price', 'incentive')
    percent_fields = ('tax_percentage',)

    def validate(self, attrs):
        """One name per category: two "Haircut"s under Hair cannot be told apart on the till or in
        a report. Checked when a service is made or renamed (the SAP pull has its own path)."""
        name = (attrs.get('name', getattr(self.instance, 'name', '')) or '').strip()
        category = (attrs.get('category', getattr(self.instance, 'category', '')) or '').strip()
        # Only when the name or category really changes: the edit form sends every field back, and a
        # service pulled from SAP may already share its name with another (it must stay editable).
        renamed = self.instance is not None and (
            name.lower() != (self.instance.name or '').strip().lower()
            or category.lower() != (self.instance.category or '').strip().lower())
        if name and (self.instance is None or renamed):
            clash = ServiceMaster.objects.filter(name__iexact=name, category__iexact=category)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({'name': f'There is already a service called "{name}" in {category or "this category"}.'})
        return super().validate(attrs)

    def validate_duration_mins(self, value):
        # A service made here needs a real length. One loaded from SAP starts at 0 ("not set") and
        # must stay editable, so an edit may leave it there.
        if value == 0 and self.instance is not None and not self.instance.duration_mins:
            return value
        if value is not None and not 1 <= value <= 1440:
            raise serializers.ValidationError('A service takes between 1 minute and 24 hours (1440 minutes).')
        return value

    center_overrides = CenterServiceSerializer(many=True, read_only=True)
    center_override = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()
    center_incentive = serializers.SerializerMethodField()
    level = serializers.CharField(read_only=False, required=False)
    # Who owns the recipe, and how many lines it has (the recipe itself is edited through
    # /services/api/master/<id>/recipe/).
    recipe_line_count = serializers.SerializerMethodField()

    class Meta:
        model = ServiceMaster
        fields = (
            'id', 'service_code', 'name', 'brand', 'category', 'sub_category',
            'sac_code', 'hsn_code', 'default_price', 'tax_percentage', 'duration_mins',
            'level', 'centers', 'created_at', 'updated_at', 'center_overrides',
            'center_override', 'price', 'center_incentive', 'incentive',
            'recipe_source', 'recipe_line_count'
        )
        read_only_fields = ('recipe_source',)

    def get_recipe_line_count(self, obj):
        count = getattr(obj, 'recipe_line_count', None)
        if count is None:
            count = obj.bom_items.count()
        return count

    def _get_center_id(self):
        request = self.context.get('request')
        if not request:
            return None
        center_id = getattr(request, 'query_params', request.GET).get('center_id')
        if not center_id:
            return None
        try:
            return int(center_id)
        except (TypeError, ValueError):
            return None

    def _get_center_override_obj(self, obj):
        """Return the CenterService override for the requested center, or None.
        Uses the prefetch cache (center_overrides is prefetched on the queryset),
        so this does NOT hit the DB — it filters Python-side from the cache."""
        cid = self._get_center_id()
        if cid is None:
            return None
        # Use prefetch cache via .all() — no extra DB query
        for o in obj.center_overrides.all():
            if o.center_id == cid:
                return o
        return None

    def get_center_override(self, obj):
        override = self._get_center_override_obj(obj)
        if override is None:
            return None
        return CenterServiceSerializer(override).data

    def get_price(self, obj):
        override = self._get_center_override_obj(obj)
        if override and override.price is not None:
            return override.price
        return obj.default_price

    def get_center_incentive(self, obj):
        """Return the per-center incentive % if set, else fallback to ServiceMaster.incentive."""
        override = self._get_center_override_obj(obj)
        if override and override.incentive is not None:
            return float(override.incentive)
        return float(obj.incentive or 0)


