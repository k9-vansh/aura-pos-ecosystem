"""The permission contract: every guarded route is gated by exactly the tick the tree says.

Walks every URL and calls the permission class itself (no data needed), for each
(method, action):
  * the tick it names exists in the tree (no typos that silently grant nothing);
  * the verb it needs is held by a role that has ONLY that verb, and refused to a
    role that has every other verb (so Add never opens Edit, and so on);
  * a verb the tree does not offer for the tick (a report tick is read-only) is
    refused even if a stored role file carries it;
  * an action tick (cancel a bill, lock payroll ...) opens its action, and full
    Add/Edit/Delete without the tick does not.
"""

from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from accounts.models import CustomUser
from pos_backend.permission_tree import load_tree
from pos_backend.permissions import HasModulePermission, METHOD_TO_VERB
from pos_backend.tests_rbac_phase1 import grant, with_actions
from salon_admin.models import Center, Role

LETTER = {'read': 'r', 'create': 'c', 'update': 'u', 'delete': 'd'}
factory = APIRequestFactory()


def walk(patterns, prefix=''):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from walk(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield prefix + str(p.pattern), p.callback


class PermissionContract(TestCase):
    def test_every_route_is_gated_by_the_tick_the_tree_names(self):
        center = Center.objects.create(center_name='M')
        tree = load_tree()
        valid = {(m['key'], s['key']) for m in tree['modules'] for s in m['submodules']}
        verbs_of = {(m['key'], s['key']): s.get('verbs', ['read', 'create', 'update', 'delete'])
                    for m in tree['modules'] for s in m['submodules']}
        actions_of = {(m['key'], s['key']): [a['key'] for a in s.get('actions', [])]
                      for m in tree['modules'] for s in m['submodules']}
        n = [0]

        def user(perms):
            n[0] += 1
            role = Role.objects.create(name=f'r{n[0]}', permissions=perms)
            u = CustomUser.objects.create_user(email=f'a{n[0]}@t.com', password='StrongPass!234', full_name='A',
                                               role=role, center=center)
            u.centers.add(center)
            return u

        def allowed(cls, method, action, u, route):
            view = cls()
            view.action = action
            view.kwargs = {}
            dj = factory.generic(method, '/x/')
            dj.user = u
            req = Request(dj)
            req._user = u
            req._authenticator = None
            view.request = req
            return HasModulePermission().has_permission(req, view)

        problems, rows = [], []
        seen = set()
        for route, cb in walk(get_resolver().url_patterns):
            cls = getattr(cb, 'cls', None)
            if cls is None or '(?P<format>' in route:
                continue
            if not any(isinstance(pc, type) and issubclass(pc, HasModulePermission)
                       for pc in getattr(cls, 'permission_classes', [])):
                continue
            module = getattr(cls, 'permission_module', None)
            if not module:
                continue
            acts = dict(getattr(cb, 'actions', None) or {})
            if acts:
                pairs = [(m.upper(), a) for m, a in acts.items()]
            else:
                pairs = [(m.upper(), None) for m in getattr(cls, 'http_method_names', [])
                         if m not in ('options', 'head', 'trace')]
            ticks = getattr(cls, 'permission_action_ticks', None) or {}
            verbs_map = getattr(cls, 'permission_action_verbs', None) or {}
            subs_map = getattr(cls, 'permission_action_submodules', None) or {}
            for method, action in pairs:
                key = (cls.__name__, method, action)
                if key in seen:
                    continue
                seen.add(key)
                sub = subs_map.get(action, getattr(cls, 'permission_submodule', None))
                verb = verbs_map.get(action) or METHOD_TO_VERB.get(method, 'read')
                label = f'{method} {cls.__name__}.{action or "-"} ({route})'
                if action in ticks:
                    tsub, tact = ticks[action]
                    if (module, tsub) not in valid or tact not in actions_of[(module, tsub)]:
                        problems.append(f'TICK NOT IN TREE {module}.{tsub}.{tact} for {label}')
                        continue
                    no_tick = allowed(cls, method, action, user(grant(**{module: {tsub: 'rcud'}})), route)
                    has_tick = allowed(cls, method, action,
                                       user(with_actions(grant(**{module: {tsub: 'r'}}), module, tsub, tact)), route)
                    rows.append(('action', f'{module}.{tsub}.{tact}', label, (not no_tick) and has_tick))
                    if no_tick:
                        problems.append(f'OPEN WITHOUT ACTION TICK {label} needs {module}.{tsub}.{tact}')
                    if not has_tick:
                        problems.append(f'ACTION TICK DOES NOT OPEN {label} {module}.{tsub}.{tact}')
                    continue
                if (module, sub) not in valid:
                    problems.append(f'UNKNOWN TICK {module}.{sub} for {label}')
                    continue
                if verb not in LETTER:
                    continue
                if verb not in verbs_of[(module, sub)]:
                    ok = allowed(cls, method, action, user(grant(**{module: {sub: 'rcud'}})), route)
                    rows.append(('notaverb', f'{module}.{sub}.{verb}', label, ok))
                    if ok:
                        problems.append(f'VERB NOT OFFERED BUT WORKS {label} {module}.{sub}.{verb}')
                    continue
                others = ''.join(l for v, l in LETTER.items() if v != verb)
                with_verb = allowed(cls, method, action, user(grant(**{module: {sub: LETTER[verb]}})), route)
                without = allowed(cls, method, action, user(grant(**{module: {sub: others}})), route) \
                    if verb != 'read' else None
                rows.append(('verb', f'{module}.{sub}.{verb}', label, with_verb and not without))
                if not with_verb:
                    problems.append(f'VERB TICK DOES NOT OPEN {label} {module}.{sub}.{verb}')
                if without:
                    problems.append(f'OTHER VERBS OPEN IT {label} needs only {module}.{sub}.{verb}')
        self.assertGreater(len(rows), 400, 'the walk found too few routes to mean anything')
        self.assertEqual(problems, [])
