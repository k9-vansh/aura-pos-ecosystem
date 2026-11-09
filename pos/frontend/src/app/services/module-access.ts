import { Injectable } from '@angular/core';
import { getStoredUser } from './session';

/**
 * The single source of truth for what a user may see in the UI.
 *
 * There used to be two implementations that read DIFFERENT keys out of the
 * same permissions object and therefore disagreed:
 *
 *   auth.guard.ts        checked `Object.values(module).some(sub => sub.read)`
 *   ModuleAccessService  checked `permissions.billing?.access`
 *
 * The real shape, set by the Roles screen, is
 *   { all_centers: boolean,
 *     "<module>": { "<submodule>": { read, create, update, delete } } }
 *
 * so `.access` never matched anything and this service returned false for
 * every module for every non-owner - while the route guard let them in.
 * `finance` was not even in the old switch, so one method denied it and the
 * other allowed it.
 *
 * This is presentation only. The backend enforces the same permissions through
 * HasModulePermission; editing localStorage changes what the menu shows and
 * nothing else.
 */

export type { PermissionVerb } from './permission-tree';
import {
  ALWAYS_READABLE,
  ADMIN_SUBMODULE_ROUTES,
  ROUTE_MODULES,
  candidateModuleKeys,
  candidateSubmoduleKeys,
  type PermissionVerb,
} from './permission-tree';

/**
 * `ROUTE_MODULES`, `ADMIN_SUBMODULES` and the alias groups used to be written out
 * here by hand, a fourth copy of the tree. They now come from
 * permission-tree.json, which the backend reads too.
 *
 * That hand-written copy is why the Logs module could not be granted: it was
 * absent from the checkbox tree and listed here as `OWNER_ONLY_ROUTES`, so the
 * only way to see Logs was to be an owner. Logs is now a normal module with an
 * `activity` submodule, and an owner still passes every check.
 */

@Injectable({ providedIn: 'root' })
export class ModuleAccessService {
  /**
   * The superuser - the one built-in full-access identity (decision #6).
   * A role NAMED "owner" no longer grants anything by its name; every other
   * user gets exactly what their role's permissions say.
   */
  isOwner(user: any = null): boolean {
    const current = user ?? getStoredUser();
    return current?.is_superuser === true;
  }

  /** True if the role grants the action tick `action` on module > submodule. */
  hasAction(moduleKey: string, submoduleKey: string, action: string, user: any = null): boolean {
    if (this.isOwner(user)) {
      return true;
    }
    const sub = this.getPermissions(user)?.[moduleKey]?.[submoduleKey];
    return sub?.actions?.[action] === true;
  }

  getPermissions(user: any = null): Record<string, any> {
    const current = user ?? getStoredUser();
    const perms = current?.permissions;
    return perms && typeof perms === 'object' ? perms : {};
  }

  hasAllCenters(user: any = null): boolean {
    return this.isOwner(user) || this.getPermissions(user)['all_centers'] === true;
  }

  /** True if any submodule of `moduleKey` grants `verb`. */
  canAccessModule(moduleKey: string, user: any = null, verb: PermissionVerb = 'read'): boolean {
    if (this.isOwner(user)) {
      return true;
    }
    const permissions = this.getPermissions(user);
    for (const key of candidateModuleKeys(moduleKey)) {
      const module = permissions[key];
      if (!module || typeof module !== 'object') {
        continue;
      }
      const granted = Object.values(module).some(
        (sub: any) => sub && typeof sub === 'object' && sub[verb] === true,
      );
      if (granted) {
        return true;
      }
    }
    return false;
  }

  /** True if a specific submodule grants `verb`. */
  can(moduleKey: string, submoduleKey: string, verb: PermissionVerb = 'read', user: any = null): boolean {
    if (this.isOwner(user)) {
      return true;
    }
    const permissions = this.getPermissions(user);
    for (const key of candidateModuleKeys(moduleKey)) {
      const module = permissions[key];
      if (!module || typeof module !== 'object') {
        continue;
      }
      // A role saved before Marketing > Campaign was split into four holds
      // `campaigns`; that must still grant all four.
      for (const subKey of candidateSubmoduleKeys(moduleKey, submoduleKey)) {
        const sub = module[subKey];
        if (sub && typeof sub === 'object' && sub[verb] === true) {
          return true;
        }
      }
    }
    return false;
  }

  /**
   * True if the user may open `routePath`.
   *
   * Fails CLOSED for anything unrecognised. The old `canAccessRoute` ended in
   * `default: return true` while `canAccessModule` ended in
   * `default: return false`, so the two halves of the same decision
   * contradicted each other.
   */
  canAccessRoute(routePath: string | null | undefined, user: any = null): boolean {
    const normalised = (routePath || '').replace(/^\/+|\/+$/g, '');
    if (!normalised || normalised === 'admin') {
      return true;
    }

    const path = normalised.replace(/^admin\//, '');

    // `admin/<sub>` pages are checked as submodules of `admin`. Checked before
    // ROUTE_MODULES because a module and an admin submodule can share a name
    // (`services`, `clients`).
    if (path in ADMIN_SUBMODULE_ROUTES) {
      return this.can('admin', ADMIN_SUBMODULE_ROUTES[path], 'read', user);
    }

    if (path in ROUTE_MODULES) {
      const moduleKey = ROUTE_MODULES[path];
      // The landing page is reachable by anyone who can reach anything.
      if (ALWAYS_READABLE.has(moduleKey)) {
        return true;
      }
      return this.canAccessModule(moduleKey, user, 'read');
    }

    // Unrecognised route: fail closed.
    return this.isOwner(user);
  }

  /** The first route this user can actually open, for post-login redirects. */
  firstAccessibleRoute(user: any = null): string {
    if (this.isOwner(user)) {
      return '/admin/home';
    }
    for (const [path, moduleKey] of Object.entries(ROUTE_MODULES)) {
      if (ALWAYS_READABLE.has(moduleKey)) {
        continue;
      }
      if (this.canAccessModule(moduleKey, user, 'read')) {
        return `/admin/${path}`;
      }
    }
    for (const [path, submodule] of Object.entries(ADMIN_SUBMODULE_ROUTES)) {
      if (this.can('admin', submodule, 'read', user)) {
        return `/admin/${path}`;
      }
    }
    return '/admin/home';
  }

  /** Route module keys, for menu rendering. */
  get routeModules(): Record<string, string> {
    return { ...ROUTE_MODULES };
  }
}
