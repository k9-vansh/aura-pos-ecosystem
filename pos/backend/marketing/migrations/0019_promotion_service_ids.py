from django.db import migrations


def link_campaigns_to_service_ids(apps, schema_editor):
    """Work out which services each existing campaign names, from its names."""
    Promotion = apps.get_model('marketing', 'Promotion')
    ServiceMaster = apps.get_model('services', 'ServiceMaster')

    def ids_for(name):
        name = str(name or '').strip()
        if not name:
            return []
        return list(ServiceMaster.objects.filter(name__iexact=name).order_by('id')
                    .values_list('id', flat=True))

    for promo in Promotion.objects.all():
        config = dict(promo.config) if isinstance(promo.config, dict) else {}
        changed = False
        ids = ids_for(config.get('specific_service'))
        if ids and config.get('specific_service_ids') != ids:
            config['specific_service_ids'] = ids
            changed = True
        triggers = config.get('trigger_services') or []
        if any(triggers):
            groups = [ids_for(s) for s in triggers]
            if all(groups) and config.get('trigger_service_ids') != groups:
                config['trigger_service_ids'] = groups
                changed = True
        if changed:
            promo.config = config
            promo.save(update_fields=['config'])


class Migration(migrations.Migration):

    dependencies = [
        ('marketing', '0018_package_gst_percent'),
        ('services', '0009_servicebom'),
    ]

    operations = [
        migrations.RunPython(link_campaigns_to_service_ids, migrations.RunPython.noop),
    ]
