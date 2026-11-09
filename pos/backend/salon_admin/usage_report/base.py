"""What every Usage Report section shares: the filters, the centres, the dates.

The report is read-only and works from the same rows Finance and the Overview use:
a bill counts when it is Paid or Partial, and a promotion, coupon or cashback only counts
while the bill it sits on stands (cancelling a bill reverses them).

Filters (query string), all optional:
  start_date, end_date   the period (default: this month so far); the previous period for
                         comparisons is worked out as the Overview does;
  center_id / the centre picker   the branches (several may be ticked);
  perk      comma list of: Discount, FlatPrice, Trigger, Cashback, Coupon, Membership,
            Package, ValueCard - which kinds of offer a section is about;
  q         part of an offer's / brand's name;
  customer  new | repeat | member - only bills of that kind of customer;
  status    running | ended | upcoming | off - the offers' own state.
"""
import hashlib
import time
from datetime import date as date_type, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.cache import cache
from django.db.models import Min

from billing.models import Invoice
from clients.models import ClientMembership
from marketing.models import Promotion
from salon_admin.analytics_endpoints import _bills, _centres, _month_label, _months_back, _month_keys, _period
from salon_admin.dashboard_endpoints import _apply_security

ZERO = Decimal('0')
OFFER_TYPES = ('Discount', 'FlatPrice', 'Trigger', 'Cashback')
PERK_KEYS = OFFER_TYPES + ('Coupon', 'Membership', 'Package', 'ValueCard')
STATUSES = ('running', 'ended', 'upcoming', 'off')
CUSTOMERS = ('new', 'repeat', 'member')
LIVE = Invoice.REVENUE_STATUSES
NEW_LIST_LIMIT = 20000      # more new customers than this are matched with a subquery, not a list of ids
#                             (kept under SQLite's 32,766 query-variable limit, which the dev database has)
SLOW_SECONDS = 1            # a section that took this long is kept for a while (see `cached`)


def dec(x):
    return Decimal(str(x or 0))


def f2(x):
    return round(float(x or 0), 2)


def pct(part, whole):
    whole = float(whole or 0)
    return round(float(part or 0) / whole * 100, 1) if whole else 0.0


def change(cur, prev):
    """Percent change from prev to cur, or None when there is nothing to compare with."""
    cur, prev = float(cur or 0), float(prev or 0)
    return round((cur - prev) / prev * 100, 1) if prev else None


def num(x):
    """1000.00 -> '1000', 12.5 -> '12.5': a figure in a sentence."""
    d = dec(x).normalize()
    return format(d, 'f')


def mask(phone):
    phone = str(phone or '')
    return ('X' * max(len(phone) - 4, 0) + phone[-4:]) if phone else ''


def person(client):
    """{'id','name','phone'} for a Client row (phone masked: this report goes to a team)."""
    if client is None:
        return {'id': None, 'name': 'Walk-in', 'phone': ''}
    return {'id': client.id, 'name': f'{client.first_name or ""} {client.last_name or ""}'.strip() or 'Customer',
            'phone': mask(client.phone)}


def cached(ctx, name, build):
    """build(ctx), but a slow answer is kept for USAGE_REPORT_CACHE_SECONDS (default 2 minutes).

    On a big database the Customers figures take several seconds, and the Overview needs the same
    ones, so opening the Overview and then the Customers tab would work them out twice. Only slow
    answers are kept, so a small database is always read live; the key holds the user (their centres
    decide what they see), the day and every filter; `fresh=1` (the Refresh button, the live
    refresh) skips the kept answer. Settings: 0 turns it off (the tests do)."""
    seconds = getattr(settings, 'USAGE_REPORT_CACHE_SECONDS', 120)
    if seconds <= 0:
        return build(ctx)
    get = ctx.request.GET
    asked = '&'.join(f'{k}={get.get(k)}' for k in sorted(get.keys()) if k != 'fresh')
    key = 'usage-report:' + hashlib.md5(f'{ctx.request.user.pk}|{name}|{ctx.today}|{asked}'.encode()).hexdigest()
    if not get.get('fresh'):
        kept = cache.get(key)
        if kept is not None:
            return kept
    began = time.monotonic()
    answer = build(ctx)
    if time.monotonic() - began >= SLOW_SECONDS:
        cache.set(key, answer, seconds)
    return answer


def promo_status(promo, today):
    if not promo.is_active:
        return 'off'
    if promo.start_date and today < promo.start_date:
        return 'upcoming'
    if promo.end_date and today > promo.end_date:
        return 'ended'
    return 'running'


def brand_of(promo):
    cfg = promo.config if isinstance(promo.config, dict) else {}
    return str(cfg.get('brand') or '').strip()


def terms_of(promo):
    """A promotion's rule in a few words, for a table row."""
    cfg = promo.config if isinstance(promo.config, dict) else {}
    kind = promo.promo_type
    if kind == 'Discount':
        how = promo.discount_type or cfg.get('discount_type') or 'Percentage'
        value = promo.discount_value if promo.discount_value is not None else cfg.get('discount_value')
        text = f'{num(value)}% off' if how == 'Percentage' else f'Rs {num(value)} off'
        target = cfg.get('specific_service') if cfg.get('target') == 'Specific Service' else 'the bill'
        text += f' {target or "the bill"}'
        if dec(cfg.get('min_bill_amount')) > 0:
            text += f', min bill Rs {num(cfg.get("min_bill_amount"))}'
        return text
    if kind == 'FlatPrice':
        service = cfg.get('specific_service')
        return (f'{service} at Rs {num(cfg.get("flat_price"))}' if service
                else f'Rs {num(cfg.get("flat_price"))} off the services')
    if kind == 'Trigger':
        pair = [s for s in (cfg.get('trigger_services') or []) if s]
        return f'{" + ".join(pair)}: {num(cfg.get("trigger_discount"))}% off'
    if kind == 'Cashback':
        text = f'{num(cfg.get("cashback_discount"))}% cashback'
        if dec(cfg.get('cashback_max')) > 0:
            text += f' (up to Rs {num(cfg.get("cashback_max"))})'
        if dec(cfg.get('cashback_min_bill')) > 0:
            text += f', min bill Rs {num(cfg.get("cashback_min_bill"))}'
        return text
    if kind == 'Coupon':
        value = (f'{num(cfg.get("coupon_value"))}%' + (f' up to Rs {num(cfg.get("coupon_max_discount"))}'
                                                         if dec(cfg.get('coupon_max_discount')) > 0 else '')
                 if cfg.get('coupon_value_type') == 'Percentage' else f'Rs {num(cfg.get("coupon_value"))}')
        text = f'{brand_of(promo) or "Brand"}: {value} each'
        if dec(cfg.get('coupon_min_bill')) > 0:
            text += f', min bill Rs {num(cfg.get("coupon_min_bill"))}'
        return text
    return promo.get_promo_type_display()


class Ctx:
    """One request's filters, worked out once."""

    def __init__(self, request):
        self.request = request
        self.start, self.end, self.prev_start, self.prev_end = _period(request)
        self.today = date_type.today()
        _, self.ids = _centres(request)                     # None: every centre
        get = request.GET
        raw = [p.strip() for p in (get.get('perk') or '').split(',') if p.strip()]
        self.perks = {p for p in raw if p in PERK_KEYS} or None
        self.q = (get.get('q') or '').strip()[:60].lower()
        self.customer = get.get('customer') if get.get('customer') in CUSTOMERS else 'all'
        self.status = get.get('status') if get.get('status') in STATUSES else ''
        self._new = {}

    # ── what the filters say ────────────────────────────────────────────
    def wants(self, *perks):
        """Is the section about any of these kinds of offer, given the Perk filter?"""
        return self.perks is None or bool(self.perks & set(perks))

    def filters_text(self):
        bits = []
        if self.perks:
            bits.append('kind: ' + ', '.join(sorted(self.perks)))
        if self.q:
            bits.append(f'name has "{self.q}"')
        if self.customer != 'all':
            bits.append(f'{self.customer} customers')
        if self.status:
            bits.append(f'offers {self.status}')
        return bits

    def frame(self):
        """The header every section carries: the period it covers and the filters in force."""
        return {'start': str(self.start), 'end': str(self.end), 'prev_start': str(self.prev_start),
                'prev_end': str(self.prev_end), 'centres': self.ids, 'filters': self.filters_text(),
                'today': str(self.today)}

    # ── rows in scope ───────────────────────────────────────────────────
    def bills(self, start=None, end=None):
        return _bills(self.request, start or self.start, end or self.end)

    def in_centres(self, qs, field='center_id'):
        return qs if self.ids is None else qs.filter(**{f'{field}__in': self.ids})

    def centre_or_client_centre(self, qs, field='center_id', client_field='client__center_id'):
        """A ledger row's own centre, falling back to the customer's when it has none."""
        if self.ids is None:
            return qs
        from django.db.models import Q
        return qs.filter(Q(**{f'{field}__in': self.ids}) | Q(**{f'{field}__isnull': True, f'{client_field}__in': self.ids}))

    def promotions(self):
        """Offers this person may see: organisation-wide ones and those of their centres."""
        qs = Promotion.objects.all().select_related('center')
        if self.ids is not None:
            from django.db.models import Q
            qs = qs.filter(Q(level='Organisation') | Q(center_id__in=self.ids))
        return qs

    def keep_promotion(self, promo):
        if self.q and self.q not in promo.name.lower() and self.q not in brand_of(promo).lower():
            return False
        return not self.status or promo_status(promo, self.today) == self.status

    # ── customers ───────────────────────────────────────────────────────
    def _first_bills(self):
        return (_apply_security(self.request, Invoice.objects.filter(status__in=LIVE, client__isnull=False))
                .values('client').annotate(first=Min('created_at')))

    def new_ids(self, start=None, end=None):
        """The customers whose first bill (in the centres in scope) falls in the period, as a set of ids.

        Finding them reads every bill once, so it is done once per request and kept."""
        key = (start or self.start, end or self.end)
        if key not in self._new:
            sub = self._first_bills().filter(first__gte=key[0], first__lt=key[1] + timedelta(days=1)).values('client')
            self._new[key] = (set(sub.values_list('client', flat=True)), sub)
        return self._new[key][0]

    def new_clients(self, start=None, end=None):
        """The same customers, to put in a filter: a short list of ids (a very long one stays a
        subquery, to keep the SQL short)."""
        ids = self.new_ids(start, end)
        return list(ids) if len(ids) <= NEW_LIST_LIMIT else self._new[(start or self.start, end or self.end)][1]

    @staticmethod
    def count_of(found):
        """How many customers a new_clients() result holds."""
        return len(found) if isinstance(found, list) else found.count()

    def repeat_clients(self):
        return _apply_security(self.request, Invoice.objects.filter(
            status__in=LIVE, client__isnull=False, created_at__lt=self.start)).values('client')

    def member_clients(self):
        return ClientMembership.objects.filter(
            is_active=True, start_date__lte=self.end, expiry_date__gte=self.end).values('client')

    def by_customer(self, qs, field='client_id'):
        if self.customer == 'new':
            return qs.filter(**{f'{field}__in': self.new_clients()})
        if self.customer == 'repeat':
            return qs.filter(**{f'{field}__in': self.repeat_clients()})
        if self.customer == 'member':
            return qs.filter(**{f'{field}__in': self.member_clients()})
        return qs

    # ── time ────────────────────────────────────────────────────────────
    def months(self, n=12):
        """The last n months ending with the period's end: [(year, month), ...]."""
        return _month_keys(_months_back(self.end, n), self.end)

    @staticmethod
    def month_label(key):
        return _month_label(*key)

    def first_of_months(self, n=12):
        return _months_back(self.end, n)


def span(start, end):
    """A datetime range [start, end] (both dates, end included) for a DateTimeField."""
    return {'gte': start, 'lt': end + timedelta(days=1)}
