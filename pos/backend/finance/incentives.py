"""
Staff incentive engine.

Extracted from finance/views.py, where the calculation was ~570 lines inline
and carried these defects:

  C6  `pct / 100.0` - Decimal divided by a float raised TypeError, so every
      daily percentage slab 500'd.
  H3  `if is_membership or is_package: continue` sat ~90 lines above
      `elif is_membership:` / `elif is_package:`, making both branches
      unreachable. Memberships and packages could never earn an incentive.
  H4  Category was decided by substring: any item whose description contained
      'elite', 'luxe', 'prestige', 'infinity' or ' card' was paid as a value
      card, and anything containing 'package'/'membership' was skipped
      entirely. A service called "Elite Hair Spa" was paid as a value card.
  H5  The service-target tier loop never broke, so 25 services collected the
      10-tier reward AND the 20-tier reward.
  H6  Items matched by an add-on rule had their type relabelled to
      'Service Add-on', which both per-item commission loops then skipped -
      so the header total never equalled the sum of the detail rows, and an
      add-on service lost its base commission.
  M25 A rule effective for part of the period applied to the whole of it, and
      rule selection returned the newest match rather than the most specific.
  M26 The item master's CURRENT incentive % was used, so editing a service
      rewrote historical payroll.
  M27 Value-card slabs were matched against the per-staff split amount, and
      quantity was ignored, so 3 cards on one line paid one slab.

Design rules here:
  * Category comes from content_type. Description text is a fallback used only
    when there is no content_type at all.
  * Every amount is Decimal end to end.
  * Tier selection is always highest-qualifying-wins, in one helper.
  * Add-ons and targets are ADDITIVE to base commission, never a replacement.
  * The header total is the sum of the detail rows, by construction.
"""

import datetime
import logging
from collections import defaultdict
from decimal import ROUND_DOWN

from pos_backend.money import D, money, pct, split, ZERO, quantize_ratio

logger = logging.getLogger(__name__)

# Item categories, keyed to the IncentiveRule.category values.
CAT_SERVICE = 'services'
CAT_PRODUCT = 'products'
CAT_VALUE_CARD = 'value_cards'
CAT_MEMBERSHIP = 'memberships'
CAT_PACKAGE = 'packages'

CT_TO_CATEGORY = {
    'servicemaster': CAT_SERVICE,
    'product': CAT_PRODUCT,
    'valuecard': CAT_VALUE_CARD,
    'membership': CAT_MEMBERSHIP,
    'package': CAT_PACKAGE,
}

CATEGORY_LABEL = {
    CAT_SERVICE: 'Service',
    CAT_PRODUCT: 'Product',
    CAT_VALUE_CARD: 'Value Card',
    CAT_MEMBERSHIP: 'Membership',
    CAT_PACKAGE: 'Package',
}

# Role buckets used by IncentiveRule.applicable_role.
ROLE_MANAGER_WORDS = ('manager', 'owner', 'admin', 'supervisor')
ROLE_LHDS_WORDS = ('lhds', 'uhds', 'stylist', 'hair designer', 'hairdresser', 'hair')
ROLE_MHDS_WORDS = ('mhds', 'beauty', 'beautician', 'therapist', 'esthetician', 'aesthetician')
ROLE_PEDI_WORDS = ('pedicurist', 'pedicure', 'k ambassador', 'ambassador')


# ─── Tier helpers ─────────────────────────────────────────────────────────

def _tier_value(tier, *names, default=0):
    """First present key from `names` in a tier dict, as Decimal."""
    for name in names:
        if isinstance(tier, dict) and tier.get(name) is not None:
            return D(tier.get(name))
    return D(default)


def highest_qualifying_tier(tiers, value, threshold_keys, inclusive=True):
    """Return the single highest tier whose threshold `value` reaches.

    One helper for every tier lookup, so slabs, multipliers and count targets
    all behave the same way. The old code sorted-and-broke in some places and
    accumulated in others (H5).
    """
    if not tiers:
        return None
    candidates = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        threshold = _tier_value(tier, *threshold_keys, default=0)
        qualifies = value >= threshold if inclusive else value > threshold
        if qualifies:
            candidates.append((threshold, tier))
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


def matching_range_tier(tiers, value):
    """Return the tier whose [min, max] range contains `value`."""
    if not tiers:
        return None
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        low = _tier_value(tier, 'min_amount', 'min_price', 'min', default=0)
        high = _tier_value(tier, 'max_amount', 'max_price', 'max', default=0)
        if high <= 0:
            high = D('999999999')
        if low <= value <= high:
            return tier
    return None


# ─── Rule selection ───────────────────────────────────────────────────────

def _role_flags(designation):
    role = ' '.join((designation or 'staff').lower().split())
    is_manager = any(word in role for word in ROLE_MANAGER_WORDS)
    is_pedi = any(word in role for word in ROLE_PEDI_WORDS)
    is_mhds = any(word in role for word in ROLE_MHDS_WORDS) or is_pedi
    is_lhds = any(word in role for word in ROLE_LHDS_WORDS)
    return role, is_manager, is_lhds, is_mhds, is_pedi


def rule_applies_to_role(rule, designation):
    """Does this rule's applicable_role cover this designation?"""
    role, is_manager, is_lhds, is_mhds, is_pedi = _role_flags(designation)
    target = ' '.join((rule.applicable_role or 'all').lower().split())

    if target in ('', 'all'):
        return True
    # Dynamic (#81): a rule may name several designations, "Senior Stylist, Colourist".
    if ',' in target:
        class _Part:
            pass
        for piece in target.split(','):
            part = _Part()
            part.applicable_role = piece.strip()
            if piece.strip() and rule_applies_to_role(part, designation):
                return True
        return False
    if target == 'manager':
        return is_manager
    if target == 'staff':
        return not is_manager
    if target == 'lhds_uhds':
        return is_lhds
    if target == 'mhds_beauty':
        return is_mhds
    if target == 'pedicurist_k_ambassador':
        return is_pedi
    return target == role


def _role_specificity(rule):
    """Higher = more specific. Used so a role-targeted rule beats 'all'."""
    target = ' '.join((rule.applicable_role or 'all').lower().split())
    if target in ('', 'all'):
        return 0
    if target in ('staff', 'manager'):
        return 1
    return 2


def rule_covers_period(rule, start_date, end_date):
    """True if the rule is in force for any part of [start_date, end_date]."""
    if rule.effective_from and end_date and rule.effective_from > end_date:
        return False
    if rule.effective_to and start_date and rule.effective_to < start_date:
        return False
    return True


def rule_covers_whole_period(rule, start_date, end_date):
    """True if the rule is in force for the ENTIRE period.

    A rule that only covers part of the period is reported so the operator
    knows the figure is approximate, rather than silently applying to days it
    was not in force (M25).
    """
    if rule.effective_from and start_date and rule.effective_from > start_date:
        return False
    if rule.effective_to and end_date and rule.effective_to < end_date:
        return False
    return True


def rule_in_force_on(rule, day):
    """Is the rule in force on `day`? (No day given: always.)"""
    if day is None:
        return True
    if rule.effective_from and rule.effective_from > day:
        return False
    if rule.effective_to and rule.effective_to < day:
        return False
    return True


def rule_in_scope(rule, center_id, designation, day=None):
    """May this rule pay this person on this day? Its centre, its role and its
    dates must all fit. Add-on and target rules used to be applied to everyone:
    a pedicurist's add-on paid a hair stylist, and one centre's target paid the
    staff of another."""
    return (rule.center_id in (None, center_id)
            and rule_applies_to_role(rule, designation)
            and rule_in_force_on(rule, day))


def select_rule(rules, category, center_id, designation, on=None):
    """Pick the one rule that governs `category` for this staff member.

    Precedence, most significant first:
      1. center-specific over organisation-wide
      2. more specific applicable_role over 'all'
      3. most recently created
    The old code returned the first match in creation order, so an 'all' rule
    created yesterday beat a role-specific rule created last year (M25).

    With `on`, only rules in force on that day are considered, so a rule that
    changed in the middle of a month governs each side of the change.
    """
    candidates = [
        r for r in rules
        if r.category == category and rule_applies_to_role(r, designation)
        and rule_in_force_on(r, on)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (
            1 if r.center_id == center_id else 0,
            _role_specificity(r),
            r.created_at or datetime.datetime.min,
        ),
        reverse=True,
    )
    # Never let another center's rule win.
    for rule in candidates:
        if rule.center_id in (None, center_id):
            return rule
    return None


# ─── Per-category incentive computation ───────────────────────────────────

def percentage_for_multiple(rule, multiple):
    """Resolve the incentive percentage a rule grants at this salary multiple."""
    if rule is None:
        return ZERO, ''
    rule_type = (rule.rule_type or '').lower()

    if rule_type in ('multiple', 'multipliers') and rule.tiers:
        tier = highest_qualifying_tier(
            rule.tiers, multiple, ('min_multiple', 'multiple', 'min')
        )
        if tier is None:
            return ZERO, 'no tier reached'
        percent = _tier_value(tier, 'incentive_percent', 'percent', default=0)
        threshold = _tier_value(tier, 'min_multiple', 'multiple', 'min', default=0)
        return percent, f'{_fmt(percent)}% at {_fmt(threshold)}x multiple'

    if rule_type in ('percentage', 'flat_percentage'):
        percent = D(rule.flat_percent)
        return percent, f'{_fmt(percent)}%'

    return ZERO, ''


def flat_amount_for(rule):
    if rule is None:
        return ZERO, ''
    if (rule.rule_type or '').lower() in ('flat', 'flat_amount'):
        amount = money(rule.flat_amount)
        return amount, f'Rs {amount} flat'
    return ZERO, ''


def value_card_reward(rule, line_value, quantity, description):
    """Reward for a value-card sale.

    Matched against the FULL line value and multiplied by quantity. The old
    code matched the per-staff split amount, so two staff sharing one card
    could each drop into a lower slab, and it ignored quantity entirely so
    three cards on one line paid a single slab (M27).
    """
    if rule is None:
        return ZERO, ''
    rule_type = (rule.rule_type or '').lower()
    qty = max(int(quantity or 1), 1)
    per_card_value = line_value / D(qty) if qty else line_value

    if rule_type in ('slab', 'slabs') and rule.tiers:
        tier = matching_range_tier(rule.tiers, per_card_value)
        if tier is None:
            # Named slabs are a legitimate fallback, but only when no range
            # matched - the old code OR'd the name test with the range test, so
            # a slab named "elite" matched any description containing 'elite'
            # regardless of amount.
            desc = (description or '').lower()
            for candidate in rule.tiers:
                name = (candidate.get('name') or '').strip().lower()
                if name and name in desc:
                    tier = candidate
                    break
        if tier is None:
            return ZERO, 'no slab matched'
        per_card = _tier_value(tier, 'incentive_amount', 'amount', 'reward_amount', default=0)
        label = tier.get('name') or f'slab {_fmt(per_card_value)}'
        return money(per_card * D(qty)), f'{label} x{qty}'

    if rule_type in ('percentage', 'flat_percentage'):
        return pct(line_value, rule.flat_percent), f'{_fmt(D(rule.flat_percent))}%'

    if rule_type in ('flat', 'flat_amount'):
        return money(D(rule.flat_amount) * D(qty)), f'Rs {money(rule.flat_amount)} x{qty}'

    return ZERO, ''


def addon_reward(rules, description, line_value, quantity):
    """Add-on bonus for a specific named service. Additive to base commission."""
    desc = (description or '').lower()
    if not desc:
        return ZERO, ''
    qty = max(int(quantity or 1), 1)

    # Several keywords can match one service ("ritual" and "express ritual"). The
    # most specific, the longest, wins; the first one listed used to.
    best = None
    for rule in rules:
        for tier in (rule.tiers or []):
            if not isinstance(tier, dict):
                continue
            keyword = (tier.get('match_keyword') or tier.get('service_name') or '').strip().lower()
            if not keyword or keyword not in desc:
                continue
            amount = _tier_value(tier, 'incentive_amount', 'bonus_amount', 'amount', default=0)
            percent = _tier_value(tier, 'incentive_percent', 'bonus_percent', 'percent', default=0)
            if amount <= 0 and percent <= 0:
                continue
            if best is None or len(keyword) > best[0]:
                best = (len(keyword), tier, keyword, amount, percent)
    if best is None:
        return ZERO, ''
    _, tier, keyword, amount, percent = best
    label = tier.get('service_name') or keyword
    if amount > 0:
        return money(amount * D(qty)), f'add-on Rs {money(amount)} x{qty} ({label})'
    return pct(line_value, percent), f'add-on {_fmt(percent)}% ({label})'


def _fmt(value):
    """Trim trailing zeros so labels read '5%' not '5.00%'."""
    d = D(value).normalize()
    text = format(d, 'f')
    return text


# ─── Aggregation ──────────────────────────────────────────────────────────

class StaffIncentive:
    """Accumulator for one staff member's period."""

    def __init__(self, staff):
        self.staff = staff
        self.staff_id = staff.id
        self.name = f'{staff.first_name} {staff.last_name or ""}'.strip()
        self.designation = staff.designation or 'Staff'
        self.center_id = staff.center_id
        self.center_name = ''
        if staff.center:
            self.center_name = staff.center.display_name or staff.center.center_name or ''
        self.salary = money(staff.salary)
        self.commission_percentage = D(staff.commission_percentage)
        self.product_commission_percentage = D(staff.product_commission_percentage)

        self.revenue = {cat: ZERO for cat in CT_TO_CATEGORY.values()}
        # What the same lines were billed at, before taking off the part that a
        # value card or advance paid. `revenue` is what earns incentive; this is
        # what Finance reports, so the two can be put side by side.
        self.billed = {cat: ZERO for cat in CT_TO_CATEGORY.values()}
        self.counts = defaultdict(int)
        self.lines = []

        self.incentive = {cat: ZERO for cat in CT_TO_CATEGORY.values()}
        self.addon_total = ZERO
        self.target_total = ZERO
        self.daily_bonus = ZERO
        self.overall_bonus = ZERO

        self.rules_applied = {}
        self.partial_period_rules = []
        self.target_achievements = []
        self.daily_bonus_rule = ''
        # One row per day a daily slab paid: {'date', 'sales', 'bonus', 'rule'}.
        self.daily_bonus_days = []

    @property
    def total_sales(self):
        return money(sum(self.revenue.values(), ZERO))

    @property
    def salary_multiple(self):
        """Services sold / salary - the figure the percentage tiers are read at.

        Only services count towards it. Products and value cards carry their
        own incentive, so a big card sale must not lift the percentage a
        person earns on their services.
        """
        if self.salary <= 0:
            return ZERO
        # Exact, never rounded: 3.996x is still in the 3x tier. Rounding to two
        # decimals first lifted 3.996x to 4.00x and paid the 4x percentage.
        return self.revenue[CAT_SERVICE] / self.salary

    @property
    def salary_multiple_shown(self):
        """The multiple as printed on screen: cut (not rounded) to two decimals, so what
        is shown never reads higher than the tier it was paid at."""
        return self.salary_multiple.quantize(D('0.01'), rounding=ROUND_DOWN)

    @property
    def monthly_incentive(self):
        """Everything paid on the month's figures: the percentage on services, products,
        cards, memberships and packages, and the overall bonus."""
        return money(sum(self.incentive.values(), ZERO) + self.overall_bonus)

    @property
    def daily_incentive(self):
        """Everything paid on single days or single bills: the daily business slab, the
        service add-ons and the volume targets."""
        return money(self.daily_bonus + self.addon_total + self.target_total)

    @property
    def total_incentive(self):
        return money(
            sum(self.incentive.values(), ZERO)
            + self.addon_total + self.target_total
            + self.daily_bonus + self.overall_bonus
        )

    def as_dict(self):
        return {
            'staff_id': self.staff_id,
            'staff_name': self.name,
            'role': self.designation,
            'center': self.center_name or 'N/A',
            'center_id': self.center_id,
            'salary': self.salary,
            'commission_percentage': self.commission_percentage,
            'product_commission_percentage': self.product_commission_percentage,

            'services_revenue': money(self.revenue[CAT_SERVICE]),
            'services_billed': money(self.billed[CAT_SERVICE]),
            'services_from_cards': money(self.billed[CAT_SERVICE] - self.revenue[CAT_SERVICE]),
            'products_billed': money(self.billed[CAT_PRODUCT]),
            'products_from_cards': money(self.billed[CAT_PRODUCT] - self.revenue[CAT_PRODUCT]),
            'products_revenue': money(self.revenue[CAT_PRODUCT]),
            'cards_revenue': money(self.revenue[CAT_VALUE_CARD]),
            'memberships_revenue': money(self.revenue[CAT_MEMBERSHIP]),
            'packages_revenue': money(self.revenue[CAT_PACKAGE]),
            'total_sales': self.total_sales,
            'revenue': self.total_sales,
            'cards_count': self.counts[CAT_VALUE_CARD],
            'salary_multiple': self.salary_multiple_shown,

            # MONTHLY part (on the month's figures) and DAILY part (single days or
            # single bills: daily slab, add-ons, targets). monthly + daily = total.
            'monthly_incentive': self.monthly_incentive,
            'daily_incentive': self.daily_incentive,
            'service_percent_incentive': money(self.incentive[CAT_SERVICE]),
            'monthly_other_incentive': money(self.incentive[CAT_MEMBERSHIP] + self.incentive[CAT_PACKAGE]
                                             + self.overall_bonus),

            # Kept as before (add-ons included) for the older screens and exports.
            'service_incentive': money(self.incentive[CAT_SERVICE] + self.addon_total),
            'services_incentive': money(self.incentive[CAT_SERVICE] + self.addon_total),
            'product_incentive': money(self.incentive[CAT_PRODUCT]),
            'products_incentive': money(self.incentive[CAT_PRODUCT]),
            'card_incentive': money(self.incentive[CAT_VALUE_CARD]),
            'cards_incentive': money(self.incentive[CAT_VALUE_CARD]),
            'membership_incentive': money(self.incentive[CAT_MEMBERSHIP]),
            'memberships_incentive': money(self.incentive[CAT_MEMBERSHIP]),
            'package_incentive': money(self.incentive[CAT_PACKAGE]),
            'packages_incentive': money(self.incentive[CAT_PACKAGE]),
            'service_addon_incentive': money(self.addon_total),
            'service_target_incentive': money(self.target_total),
            'daily_business_incentive': money(self.daily_bonus),
            'daily_bonus': money(self.daily_bonus),
            'daily_bonus_rule': self.daily_bonus_rule,
            'daily_bonus_days': self.daily_bonus_days,
            'overall_incentive': money(self.overall_bonus),

            'total_incentive': self.total_incentive,
            'incentive_amount': self.total_incentive,

            'rules_applied': self.rules_applied,
            'partial_period_rules': self.partial_period_rules,
            'target_achievements': self.target_achievements,
            'details': self.lines,
            'items': self.lines,
        }


def categorise_item(item):
    """Which incentive category an invoice line belongs to.

    Content type is authoritative. Description keywords are consulted only when
    there is no content type at all, which is the legitimate case for ad-hoc
    manual lines (H4).
    """
    ct = item.content_type
    if ct:
        category = CT_TO_CATEGORY.get((ct.model or '').lower())
        if category:
            return category
        if (ct.app_label or '').lower() == 'inventory':
            return CAT_PRODUCT
        return CAT_SERVICE

    desc = (item.description or '').lower()
    if 'value card' in desc:
        return CAT_VALUE_CARD
    if 'membership' in desc:
        return CAT_MEMBERSHIP
    if 'package' in desc:
        return CAT_PACKAGE
    return CAT_SERVICE


def credited_staff(item, invoice):
    """Staff members who share credit for a line."""
    members = list(item.staff_members.all())
    if item.staff and item.staff not in members:
        members.append(item.staff)
    if not members and invoice.staff:
        members.append(invoice.staff)
    return members


def real_money_ratio(invoice, cache):
    """Fraction of an invoice settled with real money (cached per invoice)."""
    if invoice.id in cache:
        return cache[invoice.id]
    from billing import payment_methods as pmods

    total = D(invoice.total_amount)
    if total <= 0:
        ratio = D('1')
    else:
        liability = ZERO
        for payment in invoice.payments.all():
            if payment.is_tip:
                continue
            if pmods.is_liability_drawdown(payment.payment_method):
                liability += D(payment.amount)
        real = total - liability
        if real <= 0:
            ratio = ZERO
        elif real >= total:
            ratio = D('1')
        else:
            ratio = quantize_ratio(real, total)
    cache[invoice.id] = ratio
    return ratio


def calculate(staff_qs, invoice_items, rules, start_date=None, end_date=None,
              frequency='monthly'):
    """Compute incentives for every staff member in `staff_qs`.

    `invoice_items` must be a queryset with content_type, invoice, staff and
    staff_members available. `rules` is a list of IncentiveRule.
    """
    active_rules = [r for r in rules if rule_covers_period(r, start_date, end_date)]
    addon_rules = [r for r in active_rules if r.category == 'service_addon']
    target_rules = [r for r in active_rules if r.category == 'service_target']
    daily_rules = [r for r in active_rules if r.category == 'daily_business']

    accumulators = {s.id: StaffIncentive(s) for s in staff_qs}
    ratio_cache = {}

    from billing.services import commission_base, redemption_earns_commission

    pays_for_redemptions = redemption_earns_commission()

    # ── Pass 1: attribute revenue and per-line rewards ──
    for item in invoice_items:
        invoice = item.invoice
        if not invoice:
            continue
        # A redemption earns nothing by default - the package sale already paid
        # commission. See billing.services.redemption_earns_commission() for why
        # this is a policy rather than a rule.
        if item.is_redemption and not pays_for_redemptions:
            continue

        # An advance is money held for the client, not something a person sold or
        # performed: it earns nothing, and must not lift the services figure the
        # percentage tier is read at.
        if item.content_type_id and (item.content_type.model or '').lower() == 'advancepayment':
            continue

        category = categorise_item(item)
        members = credited_staff(item, invoice)
        members = [m for m in members if m.id in accumulators]
        if not members:
            continue

        ratio = real_money_ratio(invoice, ratio_cache)
        # Commission base is the tax-exclusive line value, scaled by the
        # proportion of the bill settled with real money. A redemption has no
        # line value, so when the policy pays for redemptions the service's
        # catalog value stands in.
        line_value = money(commission_base(item) * ratio)

        if item.staff_allocations and isinstance(item.staff_allocations, dict) and sum(D(v) for v in item.staff_allocations.values()) > 0:
            total_alloc = sum(D(v) for v in item.staff_allocations.values())
            weights = [D(item.staff_allocations.get(str(m.id)) or item.staff_allocations.get(m.id) or 0)
                       for m in members]
            shares = [money(line_value * (w / total_alloc)) for w in weights]
            billed_shares = [money(money(commission_base(item)) * (w / total_alloc)) for w in weights]
        else:
            weights = [D(1)] * len(members)
            shares = split(line_value, len(members))
            billed_shares = split(money(commission_base(item)), len(members))
        # Flat amounts follow the same weights as the revenue (X13).
        whole_weight = sum(weights, ZERO) or D(1)

        invoice_label = invoice.display_number
        line_day = invoice.created_at.date() if invoice.created_at else None
        line_date = line_day.isoformat() if line_day else ''
        client_name = invoice.client.full_name if invoice.client else 'Walk-in'

        for member, share, billed_share in zip(members, shares, billed_shares):
            acc = accumulators[member.id]
            acc.revenue[category] += share
            acc.billed[category] += max(billed_share, share)
            acc.counts[category] += int(item.quantity or 1)

            detail = {
                'invoice_number': invoice_label,
                'date': line_date,
                'client_name': client_name,
                'item_name': item.description or CATEGORY_LABEL[category],
                'quantity': int(item.quantity or 1),
                'price': share,
                'revenue': share,
                'type': CATEGORY_LABEL[category],
                'category': category,
                'calculation_rule': '',
                'calculated_incentive': ZERO,
                'incentive_amount': ZERO,
                # The add-on part of this line's incentive (a DAILY incentive), kept
                # apart from the percentage part (the MONTHLY one) so a report can
                # show each where it was earned.
                'addon_amount': ZERO,
                'addon_label': '',
                # Snapshot of the master's incentive AT REPORT TIME. Kept for
                # display only; it is no longer the primary driver, so editing a
                # service master does not silently rewrite past payroll (M26).
                # master_incentive_flat=True  → the value is a Rs amount per sale
                # master_incentive_flat=False → the value is a percentage
            }
            _miv, _mif = _item_master_incentive(item)
            detail['master_incentive_value'] = _miv
            detail['master_incentive_flat'] = _mif
            # Legacy key kept so any external code reading the dict still works.
            detail['master_incentive_percent'] = _miv if not _mif else ZERO
            member_weight = weights[members.index(member)]
            detail['share_fraction'] = member_weight / whole_weight

            # Value cards are rewarded per sale, not as a percentage of the
            # salary multiple, so they resolve here.
            if category == CAT_VALUE_CARD:
                rule = select_rule(active_rules, CAT_VALUE_CARD, acc.center_id, acc.designation,
                                   on=line_day)
                reward, label = value_card_reward(
                    rule, money(D(item.total_price) * ratio), item.quantity, item.description
                )
                # No rule paid anything for this card: the card's own incentive
                # (the flat Rs amount on the card form) applies, per card sold -
                # the same way a membership falls back to its own amount.
                if reward <= 0 and detail.get('master_incentive_flat') and D(detail.get('master_incentive_value') or 0) > 0:
                    per_card = D(detail['master_incentive_value'])
                    reward = money(per_card * D(max(int(item.quantity or 1), 1)))
                    label = f'Rs {_fmt(per_card)} per card (card setting) x{max(int(item.quantity or 1), 1)}'
                member_share = weighted_split(reward, weights)[members.index(member)]
                detail['calculated_incentive'] = member_share
                detail['incentive_amount'] = member_share
                detail['calculation_rule'] = label
                acc.incentive[CAT_VALUE_CARD] += member_share
                _note_rule(acc, CAT_VALUE_CARD, rule, start_date, end_date)

            # Service add-ons stack ON TOP of the base percentage (H6).
            if category == CAT_SERVICE and addon_rules:
                # Only the add-ons this person's role and centre are on, and that
                # were in force the day the service was sold.
                member_addons = [r for r in addon_rules
                                 if rule_in_scope(r, acc.center_id, acc.designation, line_day)]
                bonus, label = addon_reward(
                    member_addons, item.description, money(D(item.total_price) * ratio), item.quantity
                ) if member_addons else (ZERO, '')
                if bonus > 0:
                    member_share = weighted_split(bonus, weights)[members.index(member)]
                    acc.addon_total += member_share
                    detail['calculated_incentive'] += member_share
                    detail['incentive_amount'] = detail['calculated_incentive']
                    detail['addon_amount'] = member_share
                    detail['addon_label'] = label

            acc.lines.append(detail)

    # ── Pass 2: percentage-of-revenue categories and period bonuses ──
    is_daily = (frequency or 'monthly').lower() == 'daily'

    for acc in accumulators.values():
        multiple = acc.salary_multiple

        for category in (CAT_SERVICE, CAT_PRODUCT, CAT_MEMBERSHIP, CAT_PACKAGE):
            cat_lines = [line for line in acc.lines if line['category'] == category]
            if not cat_lines or acc.revenue[category] <= 0:
                continue

            # The rule in force on the day each line was sold. A rule that changes
            # in the middle of the period governs the lines on each side of the
            # change; it used to be the newest one for the whole period.
            by_rule = {}
            for line in cat_lines:
                rule = select_rule(active_rules, category, acc.center_id, acc.designation,
                                   on=_line_day(line))
                by_rule.setdefault(id(rule) if rule is not None else None, (rule, []))[1].append(line)

            for rule, lines in by_rule.values():
                revenue = money(sum((D(line['price']) for line in lines), ZERO))
                if revenue <= 0:
                    continue
                percent, label = percentage_for_multiple(rule, multiple)
                flat, flat_label = flat_amount_for(rule)

                if percent <= 0 and flat <= 0:
                    # Staff-level fallback, then the item master percentage.
                    if category == CAT_SERVICE and acc.commission_percentage > 0:
                        percent = acc.commission_percentage
                        label = f'{_fmt(percent)}% (staff rate)'
                    elif category == CAT_PRODUCT and acc.product_commission_percentage > 0:
                        percent = acc.product_commission_percentage
                        label = f'{_fmt(percent)}% (staff product rate)'

                if percent > 0:
                    acc.incentive[category] += pct(revenue, percent)
                    _apply_to_lines(lines, percent, label)
                elif flat > 0:
                    acc.incentive[category] += flat
                    _apply_flat_to_lines(lines, flat, flat_label)
                else:
                    # Last resort: each line's own master percentage.
                    acc.incentive[category] += _apply_master_percent_to_lines(lines)

                _note_rule(acc, category, rule, start_date, end_date)

        # Overall-business slabs are monthly; they read the whole period's sales.
        if not is_daily:
            rule = select_rule(active_rules, 'overall', acc.center_id, acc.designation)
            acc.overall_bonus, _ = _slab_bonus(rule, acc.total_sales)
            _note_rule(acc, 'overall', rule, start_date, end_date)

        # Daily-business slabs and daily service targets are judged DAY BY DAY and
        # summed over the period, whatever the report's frequency. They only ever
        # paid when the report was a single day, so a month's payroll never held a
        # daily bonus, and a "5 rituals a day" target paid once on the month's count.
        _daily_bonuses(daily_rules, active_rules, acc, start_date, end_date)
        acc.target_total, acc.target_achievements = _service_targets(target_rules, acc)

    for acc in accumulators.values():
        for line in acc.lines:
            # What this bill line earned on the month's figures, apart from its add-on.
            line['monthly_amount'] = money(D(line['incentive_amount']) - D(line['addon_amount']))

    return [acc for acc in accumulators.values() if acc.total_sales > 0 or acc.total_incentive > 0]


# ─── Internals ────────────────────────────────────────────────────────────

def _item_master_incentive(item):
    """Master-configured incentive for the catalog item that backs this line.

    Returns ``(value, is_flat)``.

    * Product.incentive  – stored as a PERCENTAGE (e.g. 10 means 10 %).
    * Membership.incentive / ValueCard.incentive – stored as a FLAT RUPEE
      amount (e.g. 2000 means Rs 2,000 per sale).

    The old code returned a single Decimal and every caller treated it as a
    percentage, so a membership with incentive = 2000 paid the staff member
    2000% of the line value — far more than the sale was worth.
    """
    try:
        obj = item.content_object
    except Exception:
        return ZERO, False
    if obj is None:
        return ZERO, False

    value = D(getattr(obj, 'incentive', 0) or 0)
    if value <= 0:
        return ZERO, False

    ct = item.content_type
    model_name = (ct.model or '').lower() if ct else ''
    # Membership and ValueCard store a flat Rs amount, not a percentage.
    is_flat = model_name in ('membership', 'valuecard')
    return value, is_flat


def weighted_split(total, weights):
    """Split `total` over `weights` to the paisa, the parts adding up exactly.

    Flat incentives (value-card reward, add-on bonus, a flat item rate) used to
    be split evenly among the staff on a line, while the line's *revenue* was
    split by their contribution percentages (X13). Equal weights give exactly
    `split`."""
    weights = [D(w) for w in weights]
    whole = sum(weights, ZERO)
    if not weights:
        return []
    if whole <= 0:
        return split(total, len(weights))
    total = money(total)
    cents = int((total * 100).to_integral_value())
    raw = [cents * w / whole for w in weights]
    parts = [int(r) for r in raw]                       # floor (all non-negative)
    leftover = cents - sum(parts)
    order = sorted(range(len(parts)), key=lambda i: (raw[i] - parts[i], -i), reverse=True)
    for i in order[:leftover]:
        parts[i] += 1
    return [money(D(p) / 100) for p in parts]


def _join_labels(existing, addition):
    if not addition:
        return existing
    if not existing:
        return addition
    return f'{existing} + {addition}'


def _line_day(line):
    """The date a detail line was sold, or None."""
    text = line.get('date') or ''
    try:
        return datetime.date.fromisoformat(text) if text else None
    except ValueError:
        return None


def _apply_to_lines(lines, percent, label):
    """Distribute a percentage-based incentive onto these detail rows.

    Every line is touched, including those that also matched an add-on. The old
    code filtered on a relabelled `type` and skipped exactly those lines, so the
    header and the detail never agreed (H6).
    """
    for line in lines:
        amount = pct(line['price'], percent)
        line['calculated_incentive'] = money(D(line['calculated_incentive']) + amount)
        line['incentive_amount'] = line['calculated_incentive']
        line['calculation_rule'] = _join_labels(line['calculation_rule'], label)


def _apply_flat_to_lines(lines, flat, label):
    if not lines:
        return
    shares = split(flat, len(lines))
    for line, share in zip(lines, shares):
        line['calculated_incentive'] = money(D(line['calculated_incentive']) + share)
        line['incentive_amount'] = line['calculated_incentive']
        line['calculation_rule'] = _join_labels(line['calculation_rule'], label)


def _apply_master_percent_to_lines(lines):
    """Apply item-master incentive to matching detail rows.

    For Products/Services the master value is a **percentage**.
    For Memberships and Value Cards it is a **flat Rs amount** per line.
    Mixing the two used to pay a Rs 2000 flat incentive as 2000% of the sale
    value, producing commissions larger than the sale.
    """
    total = ZERO
    for line in lines:
        master_value = D(line.get('master_incentive_value') or line.get('master_incentive_percent') or 0)
        is_flat = bool(line.get('master_incentive_flat', False))

        if master_value <= 0:
            continue

        if is_flat:
            # Flat Rs amount – split evenly across however many staff share
            # this line (the outer loop has already split `price`, so just
            # use the stored value directly as the per-staff share).
            # The flat amount is the whole line's; this staff member's part follows
            # their contribution share (X13). An unshared line has fraction 1.
            amount = money(master_value * D(line.get('share_fraction', 1)))
            label = f'Rs {_fmt(master_value)} flat (item master)'
        else:
            amount = pct(line['price'], master_value)
            label = f'{_fmt(master_value)}% (item master)'

        line['calculated_incentive'] = money(D(line['calculated_incentive']) + amount)
        line['incentive_amount'] = line['calculated_incentive']
        line['calculation_rule'] = _join_labels(line['calculation_rule'], label)
        total += amount
    return total


def _slab_bonus(rule, sales):
    """Highest qualifying slab bonus for a sales figure."""
    if rule is None or not rule.tiers:
        return ZERO, ''
    tier = highest_qualifying_tier(rule.tiers, sales, ('min_amount', 'min_sales', 'min'))
    if tier is None:
        return ZERO, ''
    threshold = _tier_value(tier, 'min_amount', 'min_sales', 'min', default=0)
    bonus_type = (tier.get('bonus_type') or 'flat').lower()
    if bonus_type in ('percentage', 'percent'):
        percent = _tier_value(tier, 'bonus_percent', 'percent', default=0)
        # C6: this was `sales * (percent / 100.0)` - Decimal / float -> TypeError.
        return pct(sales, percent), f'{_fmt(percent)}% bonus (sales >= Rs {_fmt(threshold)})'
    amount = _tier_value(tier, 'bonus_amount', 'amount', default=0)
    return money(amount), f'Rs {_fmt(amount)} bonus (sales >= Rs {_fmt(threshold)})'


def _service_targets(target_rules, acc):
    """Count-based service targets. Highest qualifying tier per rule only.

    A rule's frequency decides what is counted. A DAILY rule ("5 rituals a day")
    is judged each day on that day's count and pays for every day it was met; a
    monthly rule is judged on the whole period's count and pays once. Only this
    person's role, centre and the rule's dates apply, and only services count (a
    product with the keyword in its name is not a service done).
    """
    total = ZERO
    achievements = []
    service_lines = [line for line in acc.lines if line['category'] == CAT_SERVICE]

    for rule in target_rules:
        if not (rule.center_id in (None, acc.center_id) and rule_applies_to_role(rule, acc.designation)):
            continue
        if (rule.frequency or '').lower() == 'daily':
            days = sorted({_line_day(line) for line in service_lines
                           if _line_day(line) is not None and rule_in_force_on(rule, _line_day(line))})
            windows = [(day, [line for line in service_lines if _line_day(line) == day]) for day in days]
        else:
            windows = [(None, [line for line in service_lines
                               if rule_in_force_on(rule, _line_day(line))])]

        for day, lines in windows:
            best = None
            best_threshold = D('-1')
            for tier in (rule.tiers or []):
                if not isinstance(tier, dict):
                    continue
                keyword = (tier.get('match_keyword') or tier.get('service_name') or '').strip().lower()
                if not keyword:
                    continue
                # Count quantity, not detail rows.
                achieved = sum(
                    int(line.get('quantity') or 1)
                    for line in lines
                    if keyword in (line.get('item_name') or '').lower()
                )
                required = int(_tier_value(tier, 'target_count', 'count', default=1) or 1)
                if achieved < required:
                    continue
                threshold = D(required)
                if threshold > best_threshold:
                    best_threshold = threshold
                    best = (tier, achieved, required)

            if best is None:
                continue
            tier, achieved, required = best
            reward = money(_tier_value(tier, 'reward_amount', 'incentive_amount', 'amount', default=0))
            total += reward
            achievements.append({
                'target_name': tier.get('service_name') or rule.name,
                'target_count': required,
                'achieved_count': achieved,
                'reward': reward,
                'date': day.isoformat() if day else None,
            })

    return money(total), achievements


def _daily_bonuses(daily_rules, active_rules, acc, start_date, end_date):
    """Daily-business slabs: each day's sales are read against the slabs on their
    own, and the bonuses are summed over the period."""
    sales_by_day = defaultdict(lambda: ZERO)
    for line in acc.lines:
        day = _line_day(line)
        if day is not None:
            sales_by_day[day] += D(line['price'])

    used = {}
    for day in sorted(sales_by_day):
        rule = select_rule(active_rules, 'daily_business', acc.center_id, acc.designation, on=day)
        if rule is None:
            continue
        bonus, label = _slab_bonus(rule, money(sales_by_day[day]))
        if bonus <= 0:
            continue
        acc.daily_bonus = money(acc.daily_bonus + bonus)
        acc.daily_bonus_days.append({'date': day.isoformat(), 'sales': money(sales_by_day[day]),
                                     'bonus': bonus, 'rule': rule.name, 'slab': label})
        used[id(rule)] = rule

    if acc.daily_bonus_days:
        acc.daily_bonus_rule = (acc.daily_bonus_days[0]['slab'] if len(acc.daily_bonus_days) == 1
                                else f"{len(acc.daily_bonus_days)} days earned a daily bonus")
    # What the report says governed daily slabs: the rules that paid, or the one
    # that would apply, or that none is configured.
    if used:
        for rule in used.values():
            _note_rule(acc, 'daily_business', rule, start_date, end_date)
    else:
        _note_rule(acc, 'daily_business',
                   select_rule(active_rules, 'daily_business', acc.center_id, acc.designation),
                   start_date, end_date)


def _note_rule(acc, category, rule, start_date, end_date):
    if rule is None:
        acc.rules_applied.setdefault(category, 'no rule configured')
        return
    # A period can be governed by more than one rule (one changed mid-month):
    # list them all instead of keeping only the last.
    names = [n for n in acc.rules_applied.get(category, '').split(' + ')
             if n and n != 'no rule configured']
    if rule.name not in names:
        names.append(rule.name)
    acc.rules_applied[category] = ' + '.join(names)
    if not rule_covers_whole_period(rule, start_date, end_date):
        note = (f'{rule.name} was only in force for part of this period '
                f'({rule.effective_from or "open"} to {rule.effective_to or "open"})')
        if note not in acc.partial_period_rules:
            acc.partial_period_rules.append(note)
