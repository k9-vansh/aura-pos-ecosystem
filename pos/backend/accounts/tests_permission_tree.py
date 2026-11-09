"""
The roles and permissions tree.

The owner reported this module as "not working correctly" with submodules
missing. It was:

  * the Logs module could not be granted to ANYONE. It was absent from the
    checkbox tree in roles.ts and hardcoded into `OWNER_ONLY_ROUTES` in
    module-access.ts, so the only way to see Logs was to be an owner;
  * the four things inside Marketing > Campaign - coupons/promotions, value
    cards, memberships, packages - shared ONE checkbox called `campaigns`, so
    you could not let a manager run memberships without also handing them the
    coupon engine;
  * the staff `profile` tab had no entry at all;
  * the tree existed as three hand-maintained copies (roles.ts,
    module-access.ts, permissions.py) that had drifted apart;
  * and only 13 of ~85 endpoints declared a `permission_module`, so for
    everything else the role was enforced only by an Angular route guard reading
    localStorage. A user whose role denied Marketing could still POST to
    /marketing/api/memberships/ directly.

There is now one definition - pos_backend/permission_tree.json - and these tests
keep it honest.
"""

import json
import os
import re

from django.test import TestCase
from rest_framework.test import APIClient

from accounts.models import CustomUser
from pos_backend.permission_tree import (
    FRONTEND_TREE_PATH, TREE_PATH, blank_permissions, candidate_submodule_keys,
    load_tree, module_keys, submodule_map, unknown_keys,
)
from pos_backend.permissions import has_module_permission
from pos_backend.testing import full_access_permissions
from salon_admin.models import Center, Role

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND_APP = os.path.join(REPO_ROOT, 'frontend', 'src', 'app')


class TreeIsOneDefinitionTests(TestCase):
    def test_the_frontend_copy_is_identical(self):
        """Three hand-maintained copies is how Logs went missing."""
        with open(TREE_PATH, encoding='utf-8') as handle:
            backend = json.load(handle)
        self.assertTrue(
            os.path.exists(FRONTEND_TREE_PATH),
            f'{FRONTEND_TREE_PATH} is missing; copy permission_tree.json there.')
        with open(FRONTEND_TREE_PATH, encoding='utf-8') as handle:
            frontend = json.load(handle)

        self.assertEqual(
            backend, frontend,
            'backend/pos_backend/permission_tree.json and '
            'frontend/src/app/services/permission-tree.json have diverged. '
            'Copy the backend file over the frontend one.')

    def test_roles_ts_no_longer_hardcodes_a_tree(self):
        """The checkbox list must come from the shared file, not a literal."""
        path = os.path.join(FRONTEND_APP, 'roles', 'roles.ts')
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('permission-tree', source,
                      'roles.ts should import the shared tree')
        # The old literal had this shape for every submodule.
        self.assertNotIn("read: false, create: false, update: false, delete: false\n    },",
                         source,
                         'roles.ts still contains a hand-written permission literal')

    def test_module_access_no_longer_hardcodes_a_tree(self):
        path = os.path.join(FRONTEND_APP, 'services', 'module-access.ts')
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('permission-tree', source)
        self.assertNotIn("const ROUTE_MODULES: Record<string, string> = {", source,
                         'module-access.ts still declares its own route map')


class TreeCoversTheAppTests(TestCase):
    def test_every_top_level_route_has_a_module_or_admin_submodule(self):
        """A route with no tree entry cannot be granted or denied."""
        routes_path = os.path.join(FRONTEND_APP, 'app.routes.ts')
        with open(routes_path, encoding='utf-8') as handle:
            source = handle.read()

        paths = set(re.findall(r"\{\s*path:\s*'([a-z][a-z0-9-]*)'", source))
        # Not part of the permission tree.
        paths -= {'login', 'admin', ''}

        tree = load_tree()
        covered = set()
        for module in tree['modules']:
            if module.get('route'):
                covered.add(module['route'])
            for sub in module['submodules']:
                if sub.get('route'):
                    covered.add(sub['route'])

        missing = sorted(paths - covered)
        self.assertEqual(
            missing, [],
            f'these routes exist but cannot be granted by any role: {missing}')

    def test_the_logs_module_is_grantable(self):
        """It was hardcoded owner-only and absent from the tree."""
        self.assertIn('logs', module_keys())
        self.assertIn('activity', submodule_map()['logs'])

    def test_the_marketing_campaign_items_are_separately_grantable(self):
        """One `campaigns` checkbox stood for four different things."""
        marketing = submodule_map()['marketing']
        for key in ('promotions', 'cards', 'memberships', 'packages'):
            self.assertIn(key, marketing, f'marketing.{key} is not grantable')

    def test_the_staff_profile_tab_is_grantable(self):
        self.assertIn('profile', submodule_map()['staff'])

    def test_the_tree_covers_the_ten_modules_the_owner_described(self):
        for key in ('overview', 'dashboard', 'logs', 'admin', 'billing',
                    'appointments', 'staff', 'inventory', 'marketing', 'finance'):
            self.assertIn(key, module_keys())


class LegacyRoleTests(TestCase):
    """A role saved before the tree changed must keep working."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main')

    def user_with(self, permissions):
        role = Role.objects.create(name=f'R{id(permissions)}', permissions=permissions)
        return CustomUser.objects.create_user(
            email=f'u{id(permissions)}@test.com', password='StrongPass!234',
            full_name='U', role=role, center=self.center)

    def test_an_old_campaigns_grant_still_covers_all_four(self):
        user = self.user_with({
            'marketing': {'campaigns': {'read': True, 'create': True,
                                        'update': False, 'delete': False}},
        })
        for key in ('promotions', 'cards', 'memberships', 'packages'):
            self.assertTrue(
                has_module_permission(user, 'marketing', 'read', key),
                f'legacy campaigns grant lost marketing.{key}')
            self.assertTrue(has_module_permission(user, 'marketing', 'create', key))
        # And it does not invent verbs it never had.
        self.assertFalse(has_module_permission(user, 'marketing', 'delete', 'cards'))

    def test_the_singular_appointment_key_still_matches(self):
        user = self.user_with({
            'appointment': {'calendar': {'read': True}},
        })
        self.assertTrue(has_module_permission(user, 'appointments', 'read', 'calendar'))

    def test_candidate_keys_include_the_legacy_name(self):
        self.assertIn('campaigns', candidate_submodule_keys('marketing', 'memberships'))
        self.assertIn('memberships', candidate_submodule_keys('marketing', 'memberships'))

    def test_an_empty_role_grants_nothing(self):
        user = self.user_with({})
        for module in module_keys():
            for sub in submodule_map()[module]:
                self.assertFalse(
                    has_module_permission(user, module, 'read', sub),
                    f'an empty role granted {module}.{sub}')

    def test_a_blank_tree_grants_nothing(self):
        user = self.user_with(blank_permissions())
        self.assertFalse(has_module_permission(user, 'finance', 'read', 'incentives'))
        self.assertFalse(has_module_permission(user, 'logs', 'read', 'activity'))


class RoleValidationTests(TestCase):
    """A typo used to be saved happily and then grant nothing."""

    def setUp(self):
        self.center = Center.objects.create(center_name='Main')
        self.owner_role = Role.objects.create(
            name='Owner', permissions=full_access_permissions())
        self.owner = CustomUser.objects.create_user(
            email='owner@test.com', password='StrongPass!234', full_name='Owner',
            role=self.owner_role, center=self.center)
        self.api = APIClient()
        self.api.force_authenticate(self.owner)

    def test_an_unknown_module_is_rejected(self):
        response = self.api.post('/salon_admin/api/roles/', {
            'name': 'Typo Role',
            'permissions': {'financee': {'incentives': {'read': True}}},
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('financee', str(response.data))

    def test_an_unknown_submodule_is_rejected(self):
        response = self.api.post('/salon_admin/api/roles/', {
            'name': 'Typo Role 2',
            'permissions': {'finance': {'incentivez': {'read': True}}},
        }, format='json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('incentivez', str(response.data))

    def test_a_valid_tree_is_accepted(self):
        response = self.api.post('/salon_admin/api/roles/', {
            'name': 'Good Role',
            'permissions': blank_permissions(),
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_legacy_campaigns_key_is_still_accepted(self):
        response = self.api.post('/salon_admin/api/roles/', {
            'name': 'Legacy Role',
            'permissions': {'marketing': {'campaigns': {'read': True}}},
        }, format='json')
        self.assertEqual(response.status_code, 201, response.data)

    def test_unknown_keys_helper(self):
        self.assertEqual(unknown_keys(blank_permissions()), [])
        self.assertIn('nope', unknown_keys({'nope': {}}))
        self.assertIn('finance.nope', unknown_keys({'finance': {'nope': {}}}))
        # all_centers is a flag, not a module.
        self.assertEqual(unknown_keys({'all_centers': True}), [])



class EnforcementCoverageTests(TestCase):
    """Permissions must be enforced by the server, not only by the menu."""

    def test_most_viewsets_declare_a_module(self):
        """It was 13 of ~85. This asserts the wiring did not silently regress."""
        declared = 0
        for app in ('accounts', 'appointments', 'audit_logs', 'billing', 'clients',
                    'finance', 'inventory', 'marketing', 'salon_admin', 'services',
                    'staff'):
            path = os.path.join(os.path.dirname(REPO_ROOT), 'backend', app, 'views.py')
            if not os.path.exists(path):
                path = os.path.join(REPO_ROOT, 'backend', app, 'views.py')
            if not os.path.exists(path):
                continue
            with open(path, encoding='utf-8') as handle:
                declared += handle.read().count('permission_module = ')

        self.assertGreaterEqual(
            declared, 30,
            f'only {declared} endpoints declare a permission module; the '
            f'server-side wiring has regressed')

    def test_every_declared_module_exists_in_the_tree(self):
        """A viewset declaring a module the tree does not define can never be
        granted, so it would deny everyone but the owner forever."""
        import importlib
        import inspect

        valid_modules = set(module_keys())
        problems = []

        for app in ('accounts', 'appointments', 'audit_logs', 'billing', 'clients',
                    'finance', 'inventory', 'marketing', 'salon_admin', 'services',
                    'staff'):
            module = importlib.import_module(f'{app}.views')
            for name, obj in inspect.getmembers(module, inspect.isclass):
                declared = getattr(obj, 'permission_module', None)
                if not declared:
                    continue
                if declared not in valid_modules:
                    problems.append(f'{app}.{name} -> {declared}')
                    continue
                sub = getattr(obj, 'permission_submodule', None)
                if sub and sub not in submodule_map()[declared]:
                    allowed_legacy = set()
                    from pos_backend.permission_tree import submodule_aliases
                    allowed_legacy = set(submodule_aliases().get(declared, {}))
                    if sub not in allowed_legacy:
                        problems.append(f'{app}.{name} -> {declared}.{sub}')

        self.assertEqual(problems, [], f'undefined permission targets: {problems}')
