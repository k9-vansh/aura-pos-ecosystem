"""
The module/submodule tree a role can be granted, loaded from one JSON file.

Why this exists: the tree was written out by hand in three places that had
drifted apart - the Roles checkboxes, the frontend nav guard, and the backend
enforcement. The Logs module was absent from the checkboxes entirely (it was
hardcoded owner-only), and the four things inside Marketing > Campaign were a
single checkbox.

`permission_tree.json` is now the only definition. The frontend keeps a byte
copy and a test asserts the two match.
"""

import functools
import json
import os

TREE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'permission_tree.json')

#: Where the frontend's copy lives, relative to the repository root.
FRONTEND_TREE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'frontend', 'src', 'app', 'services', 'permission-tree.json',
)


@functools.lru_cache(maxsize=1)
def load_tree():
    """The parsed tree. Cached: it is a static file read on every permission check."""
    with open(TREE_PATH, encoding='utf-8') as handle:
        return json.load(handle)


@functools.lru_cache(maxsize=1)
def module_keys():
    """Every valid module key, in display order."""
    return tuple(m['key'] for m in load_tree()['modules'])


@functools.lru_cache(maxsize=1)
def submodule_map():
    """{module_key: (submodule_key, ...)} in display order."""
    return {
        m['key']: tuple(s['key'] for s in m['submodules'])
        for m in load_tree()['modules']
    }


@functools.lru_cache(maxsize=1)
def offered_verbs():
    """{(module_key, submodule_key): (verb, ...)} - what the tree lets a role hold.

    A report tick offers `read` only. A role file that also carries `create` on it
    (a crafted or hand-edited one) must not turn that into a working write, so the
    permission check refuses a verb the tree does not offer."""
    return {
        (m['key'], s['key']): tuple(s.get('verbs', ('read', 'create', 'update', 'delete')))
        for m in load_tree()['modules']
        for s in m['submodules']
    }


@functools.lru_cache(maxsize=1)
def action_map():
    """{(module_key, submodule_key): (action_key, ...)} for submodules with actions."""
    return {
        (m['key'], s['key']): tuple(a['key'] for a in s.get('actions', ()))
        for m in load_tree()['modules']
        for s in m['submodules']
        if s.get('actions')
    }


@functools.lru_cache(maxsize=1)
def module_aliases():
    """{written_key: canonical_key} for module keys older roles used."""
    aliases = dict(load_tree().get('legacy_aliases', {}).get('modules', {}))
    # Aliases resolve in both directions: a lookup for the canonical key must
    # also find a role that stored the legacy one. A one-directional map is what
    # silently denied `appointment` when the stored key was `appointments`.
    reverse = {}
    for written, canonical in aliases.items():
        reverse.setdefault(canonical, written)
    return aliases, reverse


@functools.lru_cache(maxsize=1)
def submodule_aliases():
    """{module_key: {legacy_submodule: (canonical_submodules, ...)}}."""
    raw = load_tree().get('legacy_aliases', {}).get('submodules', {})
    return {
        module: {legacy: tuple(targets) for legacy, targets in mapping.items()}
        for module, mapping in raw.items()
    }


@functools.lru_cache(maxsize=1)
def always_readable_modules():
    """Modules everyone may open - the landing page."""
    return frozenset(
        m['key'] for m in load_tree()['modules'] if m.get('always_readable')
    )


def candidate_module_keys(module_key):
    """The keys to look for in a stored permissions dict for `module_key`."""
    forward, reverse = module_aliases()
    keys = [module_key]
    if module_key in forward:
        keys.append(forward[module_key])
    if module_key in reverse:
        keys.append(reverse[module_key])
    return tuple(dict.fromkeys(keys))


def candidate_submodule_keys(module_key, submodule_key):
    """The stored submodule keys that would grant `submodule_key`.

    A role saved before Marketing > Campaign was split into four holds
    `campaigns`; that must still grant all four.
    """
    keys = [submodule_key]
    for legacy, targets in submodule_aliases().get(module_key, {}).items():
        if submodule_key in targets:
            keys.append(legacy)
    return tuple(dict.fromkeys(keys))


def blank_permissions():
    """A fully-denied permissions dict covering the whole tree."""
    permissions = {'all_centers': False}
    actions = action_map()
    for module, submodules in submodule_map().items():
        permissions[module] = {}
        for sub in submodules:
            entry = {'read': False, 'create': False, 'update': False, 'delete': False}
            if (module, sub) in actions:
                entry['actions'] = {a: False for a in actions[(module, sub)]}
            permissions[module][sub] = entry
    return permissions


def normalise_permissions(stored):
    """The stored role permissions mapped onto the current tree.

    Resolves legacy keys (e.g. `marketing.campaigns`, `appointment`) and the
    oldest flat shape, and fills every module/submodule/action the role does
    not mention with False. Sent at login so the screens check one shape only
    (finding R12: screens that read the raw dict missed renamed keys).
    """
    result = blank_permissions()
    if not isinstance(stored, dict):
        return result
    result['all_centers'] = stored.get('all_centers') is True
    verbs = ('read', 'create', 'update', 'delete')
    actions = action_map()

    for module, submodules in submodule_map().items():
        for stored_key in candidate_module_keys(module):
            stored_module = stored.get(stored_key)
            if not isinstance(stored_module, dict):
                continue
            # Oldest shape: verbs directly on the module, no submodules.
            if any(isinstance(stored_module.get(v), bool) for v in verbs) or \
                    isinstance(stored_module.get('access'), bool):
                granted = stored_module.get('read') is True or stored_module.get('access') is True
                for sub in submodules:
                    result[module][sub]['read'] = result[module][sub]['read'] or granted
                continue
            for sub in submodules:
                for sub_key in candidate_submodule_keys(module, sub):
                    stored_sub = stored_module.get(sub_key)
                    if not isinstance(stored_sub, dict):
                        continue
                    for verb in verbs:
                        if stored_sub.get(verb) is True:
                            result[module][sub][verb] = True
                    stored_actions = stored_sub.get('actions')
                    if isinstance(stored_actions, dict):
                        for action in actions.get((module, sub), ()):
                            if stored_actions.get(action) is True:
                                result[module][sub]['actions'][action] = True
    return result


def granted_paths(stored):
    """Every grant a role holds, as a set of paths ('all_centers',
    'billing.invoices.read', 'billing.invoices.actions.cancel', ...)."""
    normal = normalise_permissions(stored)
    paths = set()
    if normal.get('all_centers'):
        paths.add('all_centers')
    for module, submodules in normal.items():
        if module == 'all_centers':
            continue
        for sub, entry in submodules.items():
            for verb in ('read', 'create', 'update', 'delete'):
                if entry.get(verb):
                    paths.add(f'{module}.{sub}.{verb}')
            for action, on in (entry.get('actions') or {}).items():
                if on:
                    paths.add(f'{module}.{sub}.actions.{action}')
    return paths


def exceeding_grants(holder_permissions, wanted_permissions):
    """Grants in `wanted_permissions` that the holder does not have himself.

    Whoever hands out a role may not hand out more than they hold: a user
    administrator could otherwise give themselves (or anyone) the HQ Admin role.
    """
    return sorted(granted_paths(wanted_permissions) - granted_paths(holder_permissions))


def with_implied_view(permissions):
    """A copy of `permissions` in which anyone who can Add, Edit or Delete, or holds
    an action tick, can also View.

    A role stored with `create` but no `read` could post to a screen whose list it
    could not load. Applied when a role is saved, so the stored data is consistent
    whichever client saved it.
    """
    if not isinstance(permissions, dict):
        return permissions
    result = {}
    for module, value in permissions.items():
        if not isinstance(value, dict) or module == 'all_centers':
            result[module] = value
            continue
        fixed = {}
        for sub, grants in value.items():
            if isinstance(grants, dict):
                grants = dict(grants)
                actions = grants.get('actions')
                acts_on = isinstance(actions, dict) and any(v is True for v in actions.values())
                if grants.get('create') is True or grants.get('update') is True \
                        or grants.get('delete') is True or acts_on:
                    grants['read'] = True
            fixed[sub] = grants
        result[module] = fixed
    return result


def non_boolean_grants(permissions):
    """Paths whose value is not a real true/false.

    The checks read a verb with plain truthiness, so a stored "false" (a
    non-empty string) used to count as granted.
    """
    problems = []
    if not isinstance(permissions, dict):
        return problems
    for module, value in permissions.items():
        if module == 'all_centers':
            if not isinstance(value, bool):
                problems.append('all_centers')
            continue
        if not isinstance(value, dict):
            continue
        for sub, grants in value.items():
            if not isinstance(grants, dict):
                continue
            for key, on in grants.items():
                if key == 'actions':
                    if isinstance(on, dict):
                        for action, flag in on.items():
                            if not isinstance(flag, bool):
                                problems.append(f'{module}.{sub}.actions.{action}')
                elif not isinstance(on, bool):
                    problems.append(f'{module}.{sub}.{key}')
    return problems


def unknown_keys(permissions):
    """Keys in `permissions` that the tree does not define.

    Used to reject a role payload that invents modules - a typo used to be saved
    happily and then grant nothing, which looks exactly like a broken
    permission system.
    """
    if not isinstance(permissions, dict):
        return ['<not an object>']

    valid_modules = set(module_keys())
    forward, _ = module_aliases()
    valid_modules |= set(forward)

    problems = []
    for module, value in permissions.items():
        if module == 'all_centers':
            continue
        canonical = forward.get(module, module)
        if canonical not in set(module_keys()):
            problems.append(module)
            continue
        if not isinstance(value, dict):
            problems.append(f'{module} (not an object)')
            continue

        allowed = set(submodule_map().get(canonical, ()))
        allowed |= set(submodule_aliases().get(canonical, {}))
        for sub, grants in value.items():
            if sub not in allowed:
                problems.append(f'{module}.{sub}')
                continue
            actions = grants.get('actions') if isinstance(grants, dict) else None
            if actions is None:
                continue
            if not isinstance(actions, dict):
                problems.append(f'{module}.{sub}.actions (not an object)')
                continue
            valid = set(action_map().get((canonical, sub), ()))
            for action in actions:
                if action not in valid:
                    problems.append(f'{module}.{sub}.actions.{action}')
    return problems
