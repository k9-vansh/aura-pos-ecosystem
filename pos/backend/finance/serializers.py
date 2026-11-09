from rest_framework import serializers
from pos_backend.serializer_guards import RangeChecks
from .models import PettyCashCategory, PettyCashEntry, DailyClosing, IncentiveConfig, IncentiveTier, Shift, IncentiveRule

class PettyCashCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = PettyCashCategory
        fields = ('id', 'name', 'is_active', 'sap_account')
        read_only_fields = fields


class PettyCashEntrySerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.full_name', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True, default='')
    # Where the picture can be opened (relative, like the stock transfer bill); '' when the entry has none.
    bill_image_url = serializers.SerializerMethodField()

    BILL_TYPES = ('.jpg', '.jpeg', '.png', '.webp')
    BILL_MAX_BYTES = 8 * 1024 * 1024

    class Meta:
        model = PettyCashEntry
        fields = '__all__'
        read_only_fields = ('sap_sync_status', 'sap_sync_message')
        extra_kwargs = {'bill_image': {'write_only': True}}

    def get_bill_image_url(self, obj):
        return obj.bill_image.url if obj.bill_image else ''

    def validate_bill_image(self, value):
        if value is None:
            return value
        if not (getattr(value, 'name', '') or '').lower().endswith(self.BILL_TYPES):
            raise serializers.ValidationError('The bill must be a picture (jpg, png or webp).')
        if value.size > self.BILL_MAX_BYTES:
            raise serializers.ValidationError('The picture is too big; keep it under 8 MB.')
        return value

    def validate_category(self, value):
        # Every expense is posted to SAP against its category's account, so a
        # category without one (a hand-made leftover) cannot be used.
        if value is not None and (not value.sap_account or not value.is_active):
            raise serializers.ValidationError('Pick one of the categories loaded from SAP.')
        return value

    def validate(self, attrs):
        if self.instance is None and attrs.get('category') is None:
            raise serializers.ValidationError({'category': 'Pick a category.'})
        if self.instance is None and not attrs.get('bill_image'):
            raise serializers.ValidationError({'bill_image': 'Attach a picture of the bill or slip.'})
        return attrs

    def validate_amount(self, value):
        # An expense is money paid out: zero or negative would add cash to the drawer.
        if value <= 0:
            raise serializers.ValidationError('Enter an amount greater than zero.')
        return value

class DailyClosingSerializer(RangeChecks, serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    center_name = serializers.SerializerMethodField()
    # The counted cash, the deposit and the other payment totals cannot be below zero.
    non_negative_fields = ('cash_in_hand', 'cash_deposit', 'credit_card', 'upi', 'paytm', 'bharat_pe',
                           'cheque_netbanking', 'google_pay', 'phone_pe', 'nearbuy', 'gift_card', 'other')

    def validate_date(self, value):
        import datetime
        if value > datetime.date.today():
            raise serializers.ValidationError('A closing cannot be for a date that has not come yet.')
        return value

    class Meta:
        model = DailyClosing
        fields = '__all__'
        read_only_fields = ('user', 'created_at', 'updated_at')
        # The (center, date) uniqueness is enforced by the view, which
        # distinguishes "you may not overwrite someone else's closing" (403 /
        # 409) from "an owner is amending it" (200 + an audit log entry). The
        # default validator would reject both cases as a flat 400 before any of
        # that logic ran.
        validators = []

    def get_user_name(self, obj):
        return getattr(obj.user, 'full_name', '') or ''

    def get_center_name(self, obj):
        if not obj.center:
            return ''
        return obj.center.display_name or obj.center.center_name or ''


class IncentiveTierSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('min_multiple',)
    percent_fields = ('incentive_percent',)

    class Meta:
        model = IncentiveTier
        fields = ('id', 'min_multiple', 'incentive_percent')


class IncentiveConfigSerializer(RangeChecks, serializers.ModelSerializer):
    percent_fields = ('custom_percent',)
    tiers = IncentiveTierSerializer(many=True, required=False)

    class Meta:
        model = IncentiveConfig
        fields = ('id', 'center', 'name', 'category', 'use_multiple', 'use_custom_percent', 'custom_percent', 'tiers')

    def create(self, validated_data):
        tiers_data = validated_data.pop('tiers', [])
        config = IncentiveConfig.objects.create(**validated_data)
        for tier in tiers_data:
            IncentiveTier.objects.create(config=config, **tier)
        return config

    def update(self, instance, validated_data):
        tiers_data = validated_data.pop('tiers', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        if tiers_data is not None:
            instance.tiers.all().delete()
            for tier in tiers_data:
                IncentiveTier.objects.create(config=instance, **tier)
        return instance

class ShiftSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('starting_float',)
    opened_by_name = serializers.CharField(source='opened_by.full_name', read_only=True)
    closed_by_name = serializers.CharField(source='closed_by.full_name', read_only=True)

    class Meta:
        model = Shift
        fields = '__all__'
        # Opening takes the branch and the float; Close shift records the rest.
        # These were writable through a plain edit, so a shift could be marked
        # closed, or its variance rewritten, with no close at all (R20).
        read_only_fields = ('opened_at', 'opened_by', 'closed_by', 'closed_at', 'status',
                            'expected_cash', 'actual_cash', 'variance')


class IncentiveRuleSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('flat_amount',)
    percent_fields = ('flat_percent',)
    center_name = serializers.SerializerMethodField()
    slabs = serializers.JSONField(required=False, write_only=False)

    def validate(self, attrs):
        start = attrs.get('effective_from', getattr(self.instance, 'effective_from', None))
        end = attrs.get('effective_to', getattr(self.instance, 'effective_to', None))
        if start and end and end < start:
            raise serializers.ValidationError({'effective_to': 'The end date cannot be before the start date.'})
        if 'tiers' in attrs or 'category' in attrs or 'rule_type' in attrs:
            category = attrs.get('category', getattr(self.instance, 'category', ''))
            rule_type = attrs.get('rule_type', getattr(self.instance, 'rule_type', ''))
            tiers = attrs['tiers'] if 'tiers' in attrs else getattr(self.instance, 'tiers', [])
            problem = self._tier_problem(category, rule_type, tiers)
            if problem:
                raise serializers.ValidationError({'tiers': problem})
        return super().validate(attrs)

    @staticmethod
    def _tier_problem(category, rule_type, tiers):
        """Why these tiers could never pay what the person setting them meant, or ''.

        Tiers are free JSON, so a negative bonus, a percentage over 100, a slab
        with no keyword or a target of zero used to be stored and then either
        paid nonsense or silently paid nothing."""
        from pos_backend.money import D

        if tiers in (None, ''):
            return ''
        if not isinstance(tiers, list):
            return 'Tiers must be a list.'

        def num(tier, *names):
            for name in names:
                if tier.get(name) not in (None, ''):
                    value = D(tier.get(name), default=None)
                    return value
            return D(0)

        seen = set()
        for number, tier in enumerate(tiers, start=1):
            where = f'Row {number}: '
            if not isinstance(tier, dict):
                return where + 'is not a valid row.'
            if category in ('daily_business', 'overall'):
                low = num(tier, 'min_amount', 'min_sales', 'min')
                if low is None or low < 0:
                    return where + 'the sales level cannot be negative.'
                if low in seen:
                    return where + f'another row already starts at Rs {low}.'
                seen.add(low)
                kind = str(tier.get('bonus_type') or 'flat').lower()
                if kind not in ('flat', 'percentage', 'percent'):
                    return where + 'the bonus must be a flat amount or a percentage.'
                amount, percent = num(tier, 'bonus_amount', 'amount'), num(tier, 'bonus_percent', 'percent')
                if amount is None or amount < 0 or percent is None or not 0 <= percent <= 100:
                    return where + 'the bonus cannot be negative and a percentage cannot be over 100.'
            elif category == 'service_addon':
                if not str(tier.get('match_keyword') or tier.get('service_name') or '').strip():
                    return where + 'name the service this add-on is for.'
                amount, percent = num(tier, 'incentive_amount', 'bonus_amount', 'amount'), num(tier, 'incentive_percent', 'percent')
                if amount is None or amount < 0 or percent is None or not 0 <= percent <= 100:
                    return where + 'the add-on cannot be negative and a percentage cannot be over 100.'
            elif category == 'service_target':
                if not str(tier.get('match_keyword') or tier.get('service_name') or '').strip():
                    return where + 'name the service this target counts.'
                count, reward = num(tier, 'target_count', 'count'), num(tier, 'reward_amount', 'incentive_amount', 'amount')
                if count is None or count < 1:
                    return where + 'the target must be at least 1.'
                if reward is None or reward < 0:
                    return where + 'the reward cannot be negative.'
            elif category == 'value_cards' and rule_type in ('slab', 'slabs'):
                low, high = num(tier, 'min_amount', 'min_price', 'min'), num(tier, 'max_amount', 'max_price', 'max')
                reward = num(tier, 'incentive_amount', 'amount', 'reward_amount')
                if low is None or high is None or reward is None or low < 0 or high < 0 or reward < 0:
                    return where + 'amounts cannot be negative.'
                if high and high < low:
                    return where + 'the highest card value is below the lowest.'
            elif rule_type in ('multiple', 'multipliers'):
                low, percent = num(tier, 'min_multiple', 'multiple', 'min'), num(tier, 'incentive_percent', 'percent')
                if low is None or low < 0:
                    return where + 'the multiple cannot be negative.'
                if low in seen:
                    return where + f'another row already starts at {low}x.'
                seen.add(low)
                if percent is None or not 0 <= percent <= 100:
                    return where + 'the incentive must be a percentage from 0 to 100.'
        return ''

    class Meta:
        model = IncentiveRule
        fields = '__all__'

    def get_center_name(self, obj):
        if obj.center:
            return obj.center.display_name or obj.center.center_name
        return 'All Centers (Organization-wide)'

    def to_internal_value(self, data):
        data = data.copy()
        rule_type = data.get('rule_type', '')
        # Normalize rule_type to model choices
        if rule_type in ['slabs', 'slab']:
            data['rule_type'] = 'slab'
            # `slabs` is the screen's own row shape. It fills `tiers` only when no
            # tiers were sent: the screen sends BOTH for daily slabs, add-ons and
            # targets (tiers already mapped to the engine's shape), and letting
            # `slabs` overwrite them stored rows the engine could not read, so the
            # rule paid nothing.
            if not data.get('tiers') and data.get('slabs'):
                data['tiers'] = data['slabs']
        elif rule_type in ['multiple', 'multipliers']:
            data['rule_type'] = 'multiple'
        elif rule_type in ['flat_percentage', 'percentage']:
            data['rule_type'] = 'percentage'
        elif rule_type in ['flat_amount', 'flat']:
            data['rule_type'] = 'flat'
        return super().to_internal_value(data)

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        if instance.rule_type in ['slab', 'slabs']:
            ret['slabs'] = instance.tiers or []
        else:
            ret['slabs'] = []
        return ret

    def create(self, validated_data):
        validated_data.pop('slabs', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('slabs', None)
        return super().update(instance, validated_data)


