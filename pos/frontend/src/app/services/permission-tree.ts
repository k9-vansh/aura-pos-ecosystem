/**
 * The module/submodule tree a role can be granted.
 *
 * Everything here is derived from `permission-tree.json`, which is a copy of
 * `backend/pos_backend/permission_tree.json`. A backend test asserts the two
 * files are identical, so the checkbox list, the nav guard and the server-side
 * enforcement cannot disagree again.
 *
 * They did disagree. The tree was written out by hand in three places:
 *
 *   roles.ts            defaultPermissions  - the checkboxes an admin ticks
 *   module-access.ts    ROUTE_MODULES etc.  - what the menu shows
 *   permissions.py      has_module_permission - what the server allows
 *
 * The Logs module was in none of the checkboxes (it was hardcoded into
 * `OWNER_ONLY_ROUTES`), so it could not be granted to anyone. And the four
 * things inside Marketing > Campaign - coupons, cards, memberships, packages -
 * shared a single checkbox, so you could not let someone run memberships
 * without also handing them the coupon engine.
 */

import treeJson from './permission-tree.json';

export type PermissionVerb = 'read' | 'create' | 'update' | 'delete';

export const VERBS: readonly PermissionVerb[] = ['read', 'create', 'update', 'delete'] as const;

/** A grantable tick beyond read/create/update/delete, e.g. "Cancel a bill". */
export interface ActionDef {
  key: string;
  label: string;
}

export interface SubmoduleDef {
  key: string;
  label: string;
  /** One line on what holding it lets a person do. */
  help?: string;
  /** Which verbs mean something here (a report is read-only). Absent = all four. */
  verbs?: PermissionVerb[];
  /** Sub-route under /admin, where this submodule is a page of its own. */
  route?: string;
  /** Action ticks, stored as `permissions[module][sub].actions[key]`. */
  actions?: ActionDef[];
}

export interface ModuleDef {
  key: string;
  label: string;
  /** One line on what the module is for. */
  help?: string;
  /** Route under /admin, where the module is a page. */
  route?: string;
  /** Reachable by anyone who can reach anything - the landing page. */
  always_readable?: boolean;
  submodules: SubmoduleDef[];
}

interface TreeShape {
  modules: ModuleDef[];
  legacy_aliases?: {
    modules?: Record<string, string>;
    submodules?: Record<string, Record<string, string[]>>;
  };
}

const TREE = treeJson as unknown as TreeShape;

/** Every module, in display order. */
export const PERMISSION_MODULES: readonly ModuleDef[] = TREE.modules;

/** Module keys everyone may open. */
export const ALWAYS_READABLE = new Set(
  TREE.modules.filter((m) => m.always_readable).map((m) => m.key),
);

/** `route path -> module key`, for top-level module pages. */
export const ROUTE_MODULES: Record<string, string> = TREE.modules.reduce(
  (map, module) => {
    if (module.route) {
      map[module.route] = module.key;
    }
    return map;
  },
  {} as Record<string, string>,
);

/**
 * `admin sub-route -> submodule key`.
 *
 * Built from the tree rather than hand-listed, which is how
 * `manager-discounts` -> `manager_discounts` used to be easy to get wrong.
 */
export const ADMIN_SUBMODULE_ROUTES: Record<string, string> = (() => {
  const admin = TREE.modules.find((m) => m.key === 'admin');
  const map: Record<string, string> = {};
  for (const sub of admin?.submodules ?? []) {
    if (sub.route) {
      map[sub.route] = sub.key;
    }
  }
  return map;
})();

/**
 * Module keys that mean the same thing, resolved in BOTH directions.
 *
 * A one-directional map silently denied `appointment` when the stored key was
 * `appointments`.
 */
export const MODULE_ALIASES: Record<string, string[]> = (() => {
  const groups: string[][] = [];
  for (const [written, canonical] of Object.entries(TREE.legacy_aliases?.modules ?? {})) {
    groups.push([canonical, written]);
  }
  const map: Record<string, string[]> = {};
  for (const group of groups) {
    for (const key of group) {
      map[key] = group;
    }
  }
  return map;
})();

/**
 * `module -> canonical submodule -> the stored keys that would grant it`.
 *
 * A role saved before Campaign was split into four holds `campaigns`; that must
 * still grant promotions, cards, memberships and packages.
 */
export const SUBMODULE_ALIASES: Record<string, Record<string, string[]>> = (() => {
  const out: Record<string, Record<string, string[]>> = {};
  for (const [module, mapping] of Object.entries(TREE.legacy_aliases?.submodules ?? {})) {
    for (const [legacy, targets] of Object.entries(mapping)) {
      for (const target of targets) {
        out[module] ??= {};
        out[module][target] = [...(out[module][target] ?? []), legacy];
      }
    }
  }
  return out;
})();

/** The stored module keys to look under when checking `moduleKey`. */
export function candidateModuleKeys(moduleKey: string): string[] {
  return MODULE_ALIASES[moduleKey] ?? [moduleKey];
}

/** The stored submodule keys that would grant `submoduleKey`. */
export function candidateSubmoduleKeys(moduleKey: string, submoduleKey: string): string[] {
  const legacy = SUBMODULE_ALIASES[moduleKey]?.[submoduleKey] ?? [];
  return [submoduleKey, ...legacy];
}

/** A fully-denied permissions object covering the whole tree. */
export function blankPermissions(): Record<string, any> {
  const permissions: Record<string, any> = { all_centers: false };
  for (const module of TREE.modules) {
    permissions[module.key] = {};
    for (const sub of module.submodules) {
      permissions[module.key][sub.key] = blankSubmodule(sub);
    }
  }
  return permissions;
}

/** The verbs that mean something for a submodule (all four unless the tree narrows it). */
export function verbsOf(sub: SubmoduleDef): readonly PermissionVerb[] {
  return sub.verbs?.length ? sub.verbs : VERBS;
}

/**
 * Make a permissions object consistent: whoever can Add, Edit or Delete, or holds
 * an action tick, can View. The server does the same when a role is saved.
 */
export function withImpliedView(permissions: Record<string, any>): Record<string, any> {
  for (const module of TREE.modules) {
    for (const sub of module.submodules) {
      const entry = permissions[module.key]?.[sub.key];
      if (!entry) continue;
      const acts = entry['actions'] ? Object.values(entry['actions']).some((v) => v === true) : false;
      if (entry['create'] || entry['update'] || entry['delete'] || acts) entry['read'] = true;
    }
  }
  return permissions;
}

/** A denied submodule entry, with every action tick it offers set false. */
export function blankSubmodule(sub: SubmoduleDef): Record<string, any> {
  const entry: Record<string, any> = {
    read: false, create: false, update: false, delete: false,
  };
  if (sub.actions?.length) {
    entry['actions'] = Object.fromEntries(sub.actions.map((a) => [a.key, false]));
  }
  return entry;
}

/**
 * Bring a stored permissions object up to the current tree, without losing what
 * was already granted.
 *
 * Handles three generations of shape:
 *  - the oldest, `{ module: { read: true } }` with no submodules at all,
 *  - `{ marketing: { campaigns: {...} } }` before Campaign was split,
 *  - the current shape, possibly missing modules added since it was saved.
 */
export function normalisePermissions(stored: any): Record<string, any> {
  const result = blankPermissions();
  if (!stored || typeof stored !== 'object') {
    return result;
  }

  result['all_centers'] = stored['all_centers'] === true;

  for (const module of TREE.modules) {
    for (const storedKey of candidateModuleKeys(module.key)) {
      const storedModule = stored[storedKey];
      if (!storedModule || typeof storedModule !== 'object') {
        continue;
      }

      // Oldest shape: verbs directly on the module, no submodules. Spread the
      // module-level grant across every submodule.
      const isFlat = VERBS.some((verb) => typeof storedModule[verb] === 'boolean')
        || typeof storedModule['access'] === 'boolean';
      if (isFlat) {
        const granted = storedModule['read'] === true || storedModule['access'] === true;
        for (const sub of module.submodules) {
          result[module.key][sub.key] = { ...blankSubmodule(sub), read: granted };
        }
        continue;
      }

      for (const sub of module.submodules) {
        for (const subKey of candidateSubmoduleKeys(module.key, sub.key)) {
          const storedSub = storedModule[subKey];
          if (!storedSub || typeof storedSub !== 'object') {
            continue;
          }
          for (const verb of VERBS) {
            if (storedSub[verb] === true) {
              result[module.key][sub.key][verb] = true;
            }
          }
          // Only ticks the tree still offers survive; unknown ones are dropped,
          // matching the backend's normalise_permissions.
          for (const action of sub.actions ?? []) {
            if (storedSub['actions']?.[action.key] === true) {
              result[module.key][sub.key]['actions'][action.key] = true;
            }
          }
        }
      }
    }
  }

  return result;
}
