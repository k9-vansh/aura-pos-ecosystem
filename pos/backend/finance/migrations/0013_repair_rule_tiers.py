from django.db import migrations

# Saving a Daily Business Slab, Service Add-on or Service Target rule from the
# Manage screen stored the screen's own row shape in `tiers` ({type,
# incentive_amount}, {keyword, name}...) instead of the shape the incentive engine
# reads ({bonus_type, bonus_amount}, {match_keyword, service_name}...). The engine
# found no bonus and no keyword in them, so such a rule paid nothing. Rewrite the
# rows that are still in the screen's shape; rows already in the engine's shape
# are left alone.


def _daily(tier):
    if 'bonus_type' in tier or 'bonus_amount' in tier or 'bonus_percent' in tier:
        return tier
    percent = str(tier.get('type', '')).lower() in ('percent', 'percentage')
    amount = tier.get('incentive_amount', 0) or 0
    return {'min_amount': tier.get('min_amount', 0),
            'bonus_type': 'percentage' if percent else 'flat',
            'bonus_amount': 0 if percent else amount,
            'bonus_percent': amount if percent else 0}


def _addon(tier):
    if 'match_keyword' in tier or 'service_name' in tier:
        return tier
    return {'service_name': tier.get('name', ''), 'match_keyword': tier.get('keyword', ''),
            'worth_amount': tier.get('worth', 0) or 0,
            'incentive_amount': tier.get('incentive_amount', 0) or 0}


def _target(tier):
    if 'match_keyword' in tier or 'target_count' in tier or 'service_name' in tier:
        return tier
    return {'service_name': tier.get('name', ''), 'match_keyword': tier.get('keyword', ''),
            'target_count': tier.get('min_amount', 1) or 1,
            'reward_amount': tier.get('incentive_amount', 0) or 0}


FIX = {'daily_business': _daily, 'service_addon': _addon, 'service_target': _target}


def repair(apps, schema_editor):
    Rule = apps.get_model('finance', 'IncentiveRule')
    for rule in Rule.objects.filter(category__in=FIX.keys()):
        tiers = rule.tiers
        if not isinstance(tiers, list):
            continue
        fixed = [FIX[rule.category](t) if isinstance(t, dict) else t for t in tiers]
        if fixed != tiers:
            rule.tiers = fixed
            rule.save(update_fields=['tiers'])


class Migration(migrations.Migration):

    dependencies = [
        ('finance', '0012_pettycashcategory_shift_till_pettycashentry_category'),
    ]

    operations = [
        migrations.RunPython(repair, migrations.RunPython.noop),
    ]
