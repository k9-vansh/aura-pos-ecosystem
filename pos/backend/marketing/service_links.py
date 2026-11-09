"""Campaigns refer to services by id, with the name kept for display.

The Marketing form stores a campaign's service as the service's NAME. Renaming
the service then silently broke every campaign that named it ("not on this
bill"), and two services sharing a name were indistinguishable. The ids are
resolved from the names when a campaign is saved and travel inside `config`
beside the names:

    specific_service      "Hair wash"            (display, kept current)
    specific_service_ids  [12]                   (what billing matches on)
    trigger_services      ["Hair wash", "Spa"]
    trigger_service_ids   [[12], [31]]

A campaign saved before this change has no ids; billing then falls back to the
name, exactly as before.
"""


def _ids_for(name, ServiceMaster):
    name = str(name or '').strip()
    if not name:
        return []
    return list(ServiceMaster.objects.filter(name__iexact=name).order_by('id')
                .values_list('id', flat=True))


def add_service_ids(config, ServiceMaster=None):
    """A copy of `config` with the service ids worked out from the names."""
    if ServiceMaster is None:
        from services.models import ServiceMaster
    config = dict(config or {})
    config.pop('specific_service_ids', None)
    config.pop('trigger_service_ids', None)
    if config.get('specific_service'):
        ids = _ids_for(config['specific_service'], ServiceMaster)
        if ids:
            config['specific_service_ids'] = ids
    triggers = [s for s in (config.get('trigger_services') or [])]
    if any(triggers):
        resolved = [_ids_for(s, ServiceMaster) for s in triggers]
        if resolved and all(resolved):
            config['trigger_service_ids'] = resolved
    return config


def with_current_names(config):
    """A copy of `config` whose service names are the services' names today."""
    from services.models import ServiceMaster
    config = dict(config or {})
    ids = config.get('specific_service_ids')
    if ids:
        name = ServiceMaster.objects.filter(pk__in=ids).order_by('id').values_list('name', flat=True).first()
        if name:
            config['specific_service'] = name
    groups = config.get('trigger_service_ids')
    triggers = config.get('trigger_services')
    if groups and isinstance(triggers, list) and len(groups) == len(triggers):
        names = []
        for group, old in zip(groups, triggers):
            name = ServiceMaster.objects.filter(pk__in=group).order_by('id').values_list('name', flat=True).first()
            names.append(name or old)
        config['trigger_services'] = names
    return config
