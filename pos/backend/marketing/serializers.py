from decimal import Decimal, InvalidOperation

from rest_framework import serializers
from pos_backend.serializer_guards import RangeChecks
from .models import WhatsAppMessage, Promotion, ValueCard, Membership, Package

class WhatsAppMessageSerializer(serializers.ModelSerializer):
    center_name = serializers.CharField(source='center.display_name', read_only=True)

    class Meta:
        model = WhatsAppMessage
        fields = '__all__'
        # The log is written by the sender and by delivery updates, never by a
        # browser. Making the whole row read-only is what stops a log line being
        # forged ("Delivered") through the API.
        read_only_fields = [
            'id', 'campaign', 'center', 'date', 'time', 'client_name',
            'client_phone', 'message', 'status', 'created_at']


# ── Promotion terms ───────────────────────────────────────────────────────────

MAX_VALIDITY_DAYS = 36500          # a hundred years; beyond that a date sum overflows
MAX_USES_PER_CLIENT = 100000


def _number(config, key, errors, *, label, minimum=None, maximum=None,
            required=False, exclusive_min=False):
    """A config value as a Decimal, or None when it was left empty.

    Records a message in `errors` instead of raising, so one save can report
    every problem at once. A value the checkout would silently read as 0 ("abc")
    is refused here, where the operator can still fix it."""
    raw = config.get(key)
    if raw in (None, ''):
        if required:
            errors[key] = f'Enter {label}.'
        return None
    if isinstance(raw, bool):
        errors[key] = f'{label[0].upper()}{label[1:]} must be a number.'
        return None
    try:
        value = Decimal(str(raw).strip())
        if not value.is_finite():
            raise InvalidOperation
    except (InvalidOperation, ValueError, TypeError):
        errors[key] = f'{label[0].upper()}{label[1:]} must be a number.'
        return None
    if minimum is not None and (value <= minimum if exclusive_min else value < minimum):
        errors[key] = (f'{label[0].upper()}{label[1:]} must be more than {minimum}.' if exclusive_min
                       else f'{label[0].upper()}{label[1:]} cannot be less than {minimum}.')
    elif maximum is not None and value > maximum:
        errors[key] = f'{label[0].upper()}{label[1:]} cannot be more than {maximum}.'
    return value


def _service_exists(name):
    from services.models import ServiceMaster
    return ServiceMaster.objects.filter(name__iexact=str(name).strip()).exists()


def validate_promotion_config(promo_type, config):
    """Check the rules a campaign carries in its `config`, per kind.

    Nothing here was checked before, so a cashback of 500% (Rs 5000 credited on
    a Rs 1000 bill), a cashback with no percentage (which then failed every
    checkout that used it), a negative flat price, or a campaign naming a
    service that does not exist all saved without complaint."""
    errors = {}
    hundred = Decimal('100')

    _number(config, 'min_bill_amount', errors, label='the minimum bill', minimum=Decimal('0'))

    if promo_type == 'Discount':
        target = config.get('target') or 'Overall Bill'
        if target not in ('Overall Bill', 'Specific Service'):
            errors['target'] = 'Choose Overall Bill or Specific Service.'
        elif target == 'Specific Service':
            name = str(config.get('specific_service') or '').strip()
            if not name:
                errors['specific_service'] = 'Choose the service this discount is for.'
            elif not _service_exists(name):
                errors['specific_service'] = f'There is no service called "{name}".'

    elif promo_type == 'FlatPrice':
        flat = _number(config, 'flat_price', errors, label='the flat price',
                       minimum=Decimal('0'), required=True)
        name = str(config.get('specific_service') or '').strip()
        if name and not _service_exists(name):
            errors['specific_service'] = f'There is no service called "{name}".'
        if not name and flat is not None and flat <= 0:
            errors['flat_price'] = ('With no service chosen, the flat price is an amount taken off '
                                    'the bill: enter more than 0.')

    elif promo_type == 'Trigger':
        names = [str(s or '').strip() for s in (config.get('trigger_services') or [])]
        if len(names) != 2 or not all(names):
            errors['trigger_services'] = 'Choose both services that trigger this deal.'
        else:
            missing = [n for n in names if not _service_exists(n)]
            if missing:
                errors['trigger_services'] = f'There is no service called "{missing[0]}".'
        _number(config, 'trigger_discount', errors, label='the deal discount',
                minimum=Decimal('0'), maximum=hundred, required=True, exclusive_min=True)

    elif promo_type == 'Cashback':
        _number(config, 'cashback_discount', errors, label='the cashback percentage',
                minimum=Decimal('0'), maximum=hundred, required=True, exclusive_min=True)
        _number(config, 'cashback_min_bill', errors, label='the cashback minimum bill',
                minimum=Decimal('0'))
        _number(config, 'cashback_max', errors, label='the cashback cap', minimum=Decimal('0'))
        days = _number(config, 'cashback_validity_days', errors, label='the cashback validity',
                       minimum=Decimal('0'), maximum=Decimal(MAX_VALIDITY_DAYS))
        if days is not None and days != days.to_integral_value():
            errors['cashback_validity_days'] = 'Cashback validity is a whole number of days.'
        raw_date = config.get('cashback_specific_date')
        if raw_date not in (None, ''):
            import datetime
            try:
                datetime.date.fromisoformat(str(raw_date)[:10])
            except ValueError:
                errors['cashback_specific_date'] = 'That is not a valid date.'

    elif promo_type == 'Coupon':
        brand = str(config.get('brand') or '').strip()
        if not brand:
            errors['brand'] = "Enter the brand's name (for example Blinkit)."
        elif len(brand) > 60:
            errors['brand'] = 'The brand name is at most 60 characters.'
        kind = config.get('coupon_value_type') or 'Flat'
        if kind not in ('Flat', 'Percentage'):
            errors['coupon_value_type'] = 'Choose Flat (Rs) or Percentage (%).'
        value = _number(config, 'coupon_value', errors, label='the coupon value',
                        minimum=Decimal('0'), required=True, exclusive_min=True)
        if value is not None and 'coupon_value' not in errors:
            if kind == 'Percentage' and value > hundred:
                errors['coupon_value'] = 'A percentage cannot be more than 100.'
            elif value > Decimal('1000000'):
                errors['coupon_value'] = 'That coupon value is too large.'
        _number(config, 'coupon_min_bill', errors, label='the coupon minimum bill', minimum=Decimal('0'))
        _number(config, 'coupon_max_discount', errors, label='the coupon cap', minimum=Decimal('0'))

    return errors


class PromotionSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('discount_value', 'max_usage_per_client')

    class Meta:
        model = Promotion
        fields = '__all__'

    def validate_max_usage_per_client(self, value):
        # 0 used to be accepted and then behaved as "unlimited", the opposite of
        # what anyone typing 0 means. Leave it empty for no limit.
        if value is not None and value < 1:
            raise serializers.ValidationError('Use 1 or more, or leave it empty for no limit.')
        if value is not None and value > MAX_USES_PER_CLIENT:
            raise serializers.ValidationError(f'Use {MAX_USES_PER_CLIENT:,} or fewer.')
        return value

    def validate_config(self, value):
        if value in (None, ''):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError('The campaign rules must be an object.')
        return value

    def validate(self, attrs):
        from django.utils import timezone
        # If this is a creation (no instance) or start_date is being changed
        if not self.instance and 'start_date' in attrs:
            if attrs['start_date'] < timezone.now().date():
                raise serializers.ValidationError({"start_date": "Start date cannot be in the past."})
        start = attrs.get('start_date', getattr(self.instance, 'start_date', None))
        end = attrs.get('end_date', getattr(self.instance, 'end_date', None))
        if start and end and end < start:
            raise serializers.ValidationError({"end_date": "The end date cannot be before the start date."})
        kind = attrs.get('discount_type', getattr(self.instance, 'discount_type', 'Percentage'))
        value = attrs.get('discount_value')
        if kind == 'Percentage' and value is not None and value > 100:
            raise serializers.ValidationError({"discount_value": "A percentage discount cannot be more than 100."})
        # A discount that discounts nothing used to be saved: no value, or zero, and every bill
        # then "applied" it for Rs 0. Checked when the promotion is made or its terms change.
        changing_terms = (self.instance is None
                          or any(k in attrs for k in ('promo_type', 'discount_value', 'discount_type', 'config')))
        promo_type = attrs.get('promo_type', getattr(self.instance, 'promo_type', 'Discount'))
        if (self.instance is not None and self.instance.promo_type == 'Coupon' and promo_type != 'Coupon'
                and self.instance.codes.exists()):
            raise serializers.ValidationError(
                {'promo_type': 'This promotion holds coupon codes, so it cannot be turned into another kind.'})
        if changing_terms and promo_type == 'Discount':
            config = attrs.get('config', getattr(self.instance, 'config', None)) or {}
            amount = attrs.get('discount_value', getattr(self.instance, 'discount_value', None))
            if amount is None and isinstance(config, dict):
                try:
                    amount = Decimal(str(config.get('discount_value')))
                except (InvalidOperation, TypeError, ValueError):
                    amount = None
            if amount is None or amount <= 0:
                raise serializers.ValidationError({"discount_value": "Enter how much the discount is (more than 0)."})
        if changing_terms:
            config = attrs.get('config', getattr(self.instance, 'config', None)) or {}
            problems = validate_promotion_config(promo_type, config if isinstance(config, dict) else {})
            if problems:
                raise serializers.ValidationError({'config': problems})
        # Remember WHICH services the campaign names, so renaming one later
        # does not break it.
        if isinstance(attrs.get('config'), dict):
            from .service_links import add_service_ids
            attrs['config'] = add_service_ids(attrs['config'])
        return super().validate(attrs)

    def to_representation(self, instance):
        data = super().to_representation(instance)
        if isinstance(data.get('config'), dict):
            from .service_links import with_current_names
            data['config'] = with_current_names(data['config'])
        if instance.promo_type == 'Coupon':
            from . import coupons
            data['codes_summary'] = coupons.counts(instance)
        return data


# ── Cards, memberships, packages ─────────────────────────────────────────────

def _check_days(value):
    if value is not None and value > MAX_VALIDITY_DAYS:
        raise serializers.ValidationError(f'That is too long: use {MAX_VALIDITY_DAYS:,} days or fewer.')
    return value


def _pick(serializer, attrs, name):
    """The value `name` will have after this save: the new one, else the stored one."""
    if name in attrs:
        return attrs[name]
    return getattr(serializer.instance, name, None)


def _check_price_pair(serializer, attrs, errors):
    """A card or membership needs a price, and its pre-tax and post-tax figures
    must agree with the stated GST rate.

    Billing prices these from the pre-tax figure and the rate, so a pair that
    disagrees leaves the form showing one price and the till charging another.
    Priceless rows (no price at all) were accepted and sold for nothing."""
    touched = serializer.instance is None or any(
        k in attrs for k in ('pre_tax_price', 'post_tax_price', 'gst_percent'))
    if not touched:
        return
    pre = _pick(serializer, attrs, 'pre_tax_price')
    post = _pick(serializer, attrs, 'post_tax_price')
    rate = _pick(serializer, attrs, 'gst_percent')
    if not (pre and pre > 0) and not (post and post > 0):
        errors['pre_tax_price'] = 'Enter the price (before or after tax).'
        return
    if pre and post and rate is not None:
        expected = (pre * (Decimal('1') + Decimal(rate) / Decimal('100'))).quantize(Decimal('0.01'))
        if abs(expected - post) > Decimal('0.05'):
            errors['post_tax_price'] = (
                f'Rs {pre} plus {rate}% GST is Rs {expected}, not Rs {post}. '
                f'Change one of the three so they agree.')


class ValueCardSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('pre_tax_price', 'post_tax_price', 'value', 'incentive')
    percent_fields = ('benefit_percent',)

    class Meta:
        model = ValueCard
        fields = '__all__'

    def validate_expiry_days(self, value):
        return _check_days(value)

    def validate(self, attrs):
        errors = {}
        _check_price_pair(self, attrs, errors)
        price = _pick(self, attrs, 'pre_tax_price')
        value = _pick(self, attrs, 'value')
        if self.instance is None and not (value and value > 0):
            errors['value'] = 'Enter what the card is worth.'
        # Only the price BEFORE tax is compared. The accountant's sheet sells a card
        # with face value Rs 20,000 at Rs 20,000 + 5% GST (customer pays Rs 21,000):
        # the GST is the government's, not part of the card's price.
        if price and value and value < price:
            errors['value'] = 'A card cannot be worth less than its price: the customer would lose money.'
        if errors:
            raise serializers.ValidationError(errors)
        return super().validate(attrs)


class MembershipSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('pre_tax_price', 'post_tax_price', 'value', 'incentive')
    percent_fields = ('discount_percent',)

    class Meta:
        model = Membership
        fields = '__all__'

    def validate_expiry_days(self, value):
        return _check_days(value)

    def validate(self, attrs):
        errors = {}
        _check_price_pair(self, attrs, errors)
        if errors:
            raise serializers.ValidationError(errors)
        return super().validate(attrs)


class PackageSerializer(RangeChecks, serializers.ModelSerializer):
    non_negative_fields = ('price',)

    class Meta:
        model = Package
        fields = '__all__'

    def validate_validity_days(self, value):
        return _check_days(value)

    def validate(self, attrs):
        errors = {}
        if self.instance is None or 'price' in attrs:
            price = _pick(self, attrs, 'price')
            if price is None or price <= 0:
                errors['price'] = 'Enter the package price (more than 0).'
        # A new package must say what it contains. Old single-service packages
        # (service_name only) stay editable as they are.
        if self.instance is None or 'services_json' in attrs:
            services = _pick(self, attrs, 'services_json')
            legacy = _pick(self, attrs, 'service_name')
            if not services and not (self.instance is not None and legacy):
                errors['services_json'] = 'Add at least one service to the package.'
        if errors:
            raise serializers.ValidationError(errors)
        return super().validate(attrs)

    def validate_services_json(self, value):
        """A package holds one or more services, each with a quantity (#55).

        The list is checked against the service catalogue and the names are
        written from it, so a package can never promise a service that does not
        exist, twice, or with a nonsense quantity."""
        from decimal import Decimal, InvalidOperation
        from services.models import ServiceMaster

        if value in (None, []):
            return value        # the package-level check decides whether that is allowed
        if not isinstance(value, list):
            raise serializers.ValidationError('Send the services as a list.')
        cleaned, seen = [], set()
        for row in value:
            if not isinstance(row, dict):
                raise serializers.ValidationError('Each service needs an id and a quantity.')
            try:
                svc_id = int(row.get('id'))
                qty = Decimal(str(row.get('pkgQty', 1)))
            except (TypeError, ValueError, InvalidOperation):
                raise serializers.ValidationError('Each service needs an id and a quantity.')
            if qty < 1 or qty != qty.to_integral_value():
                raise serializers.ValidationError('A service quantity must be a whole number, at least 1.')
            if svc_id in seen:
                raise serializers.ValidationError('A service can be listed only once.')
            seen.add(svc_id)
            svc = ServiceMaster.objects.filter(pk=svc_id).first()
            if svc is None:
                raise serializers.ValidationError(f'Service {svc_id} does not exist.')
            cleaned.append({**row, 'id': svc_id, 'name': svc.name, 'pkgQty': int(qty)})
        return cleaned
