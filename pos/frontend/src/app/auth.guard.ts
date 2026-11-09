import { inject } from '@angular/core';
import { ActivatedRouteSnapshot, CanActivateFn, Router } from '@angular/router';
import { ModuleAccessService } from './services/module-access';
import { clearSession, getStoredToken, getStoredUser } from './services/session';

/**
 * Route guards.
 *
 * Both guards now delegate every permission decision to ModuleAccessService,
 * so the menu and the router can no longer disagree. This file used to carry a
 * second, independent implementation that read a different shape out of the
 * same permissions object.
 *
 * These guards are UX, not security: the backend enforces the same permissions
 * via HasModulePermission, so editing localStorage only changes what the menu
 * offers.
 */

export const authGuard: CanActivateFn = () => {
  const router = inject(Router);

  const token = getStoredToken();
  const user = getStoredUser();

  if (token && user) {
    return true;
  }

  // Partial or corrupt session state: clear it so we do not loop.
  clearSession();
  router.navigate(['/login']);
  return false;
};

export const roleGuard: CanActivateFn = (route: ActivatedRouteSnapshot) => {
  const router = inject(Router);
  const access = inject(ModuleAccessService);

  const user = getStoredUser();
  if (!user) {
    clearSession();
    router.navigate(['/login']);
    return false;
  }

  const path = route.routeConfig?.path ?? '';

  if (access.canAccessRoute(path, user)) {
    return true;
  }

  // Send them somewhere they can actually use rather than bouncing to a page
  // they also cannot open.
  const fallback = access.firstAccessibleRoute(user);
  const requested = `/admin/${path}`.replace(/\/+$/, '');

  if (fallback === requested) {
    // Nothing is accessible at all - the role grants no read anywhere.
    clearSession();
    router.navigate(['/login'], { queryParams: { reason: 'no-access' } });
    return false;
  }

  router.navigate([fallback]);
  return false;
};
