import { describe, expect, it } from 'vitest';
import { ModuleAccessService } from './module-access';

/**
 * H29 - there used to be two permission readers that looked at DIFFERENT keys
 * of the same object and therefore disagreed:
 *
 *   auth.guard.ts        `Object.values(module).some(sub => sub.read)`
 *   ModuleAccessService  `permissions.billing?.access`
 *
 * The real shape has no `access` key, so the service denied every module to
 * every non-owner while the route guard let them through. `finance` was not in
 * the old switch at all, so one method denied it and the other allowed it.
 */

const service = new ModuleAccessService();

// Decision #6: the superuser is the only built-in full-access identity. A role
// that is merely NAMED "Owner" gets exactly what its permissions grant.
const owner = { is_superuser: true, role: null, permissions: {} };
const namedOwner = { is_superuser: false, role: 'Owner', permissions: {} };
const superuser = { is_superuser: true, role: null, permissions: {} };

const receptionist = {
  is_superuser: false,
  role: 'Receptionist',
  permissions: {
    all_centers: false,
    billing: {
      invoices: { read: true, create: true, update: false, delete: false },
      new_invoice: { read: true, create: true, update: false, delete: false },
    },
    appointments: {
      calendar: { read: true, create: true, update: true, delete: false },
    },
  },
};

const accountant = {
  is_superuser: false,
  role: 'Accountant',
  permissions: {
    finance: {
      register_summary: { read: true, create: false, update: false, delete: false },
      incentives: { read: false, create: false, update: false, delete: false },
    },
    admin: {
      bills: { read: true, create: false, update: false, delete: false },
    },
  },
};

const nobody = { is_superuser: false, role: 'Nobody', permissions: {} };

describe('isOwner', () => {
  it('recognises only the superuser', () => {
    expect(service.isOwner(superuser)).toBe(true);
    expect(service.isOwner(receptionist)).toBe(false);
  });

  it('gives a role named "Owner" nothing from its name', () => {
    expect(service.isOwner(namedOwner)).toBe(false);
    expect(service.isOwner({ role: { name: 'owner' } })).toBe(false);
    expect(service.canAccessModule('billing', namedOwner)).toBe(false);
    expect(service.hasAllCenters(namedOwner)).toBe(false);
  });

  it('treats a missing user as not an owner', () => {
    expect(service.isOwner({})).toBe(false);
  });
});

describe('canAccessModule', () => {
  it('reads the real nested permission shape', () => {
    // The old implementation checked `.access`, which never exists.
    expect(service.canAccessModule('billing', receptionist)).toBe(true);
    expect(service.canAccessModule('staff', receptionist)).toBe(false);
  });

  it('handles finance, which the old switch omitted entirely', () => {
    expect(service.canAccessModule('finance', accountant)).toBe(true);
    expect(service.canAccessModule('finance', receptionist)).toBe(false);
  });

  it('supports the appointments / appointment key alias', () => {
    expect(service.canAccessModule('appointment', receptionist)).toBe(true);
    expect(service.canAccessModule('appointments', receptionist)).toBe(true);
  });

  it('checks the requested verb, not just presence', () => {
    expect(service.canAccessModule('billing', receptionist, 'create')).toBe(true);
    expect(service.canAccessModule('billing', receptionist, 'delete')).toBe(false);
  });

  it('grants an owner everything', () => {
    expect(service.canAccessModule('anything-at-all', owner)).toBe(true);
    expect(service.canAccessModule('finance', owner, 'delete')).toBe(true);
  });

  it('denies a role with no permissions', () => {
    expect(service.canAccessModule('billing', nobody)).toBe(false);
  });
});

describe('can (submodule level)', () => {
  it('distinguishes submodules', () => {
    expect(service.can('finance', 'register_summary', 'read', accountant)).toBe(true);
    expect(service.can('finance', 'incentives', 'read', accountant)).toBe(false);
  });

  it('grants an owner every submodule', () => {
    expect(service.can('finance', 'incentives', 'delete', owner)).toBe(true);
  });
});

describe('canAccessRoute', () => {
  it('agrees with canAccessModule for module routes', () => {
    for (const path of ['billing', 'staff', 'inventory', 'marketing', 'finance', 'dashboard']) {
      const viaRoute = service.canAccessRoute(path, receptionist);
      const viaModule = service.canAccessModule(
        service.routeModules[path], receptionist, 'read',
      );
      expect(viaRoute).toBe(viaModule);
    }
  });

  it('fails CLOSED for an unknown route', () => {
    // The old canAccessRoute ended in `default: return true` while
    // canAccessModule ended in `default: return false`.
    expect(service.canAccessRoute('some-new-screen', receptionist)).toBe(false);
    expect(service.canAccessRoute('some-new-screen', owner)).toBe(true);
  });

  it('restricts admin sub-routes by submodule', () => {
    expect(service.canAccessRoute('bills', accountant)).toBe(true);
    expect(service.canAccessRoute('users', accountant)).toBe(false);
    expect(service.canAccessRoute('roles', receptionist)).toBe(false);
  });

  it('opens the logs screen only to the superuser or a Logs grant', () => {
    expect(service.canAccessRoute('logs', owner)).toBe(true);
    expect(service.canAccessRoute('logs', accountant)).toBe(false);
    expect(service.canAccessRoute('logs', {
      permissions: { logs: { activity: { read: true } } },
    })).toBe(true);
  });

  it('guards the settings page by Admin > Settings', () => {
    expect(service.canAccessRoute('settings', accountant)).toBe(false);
    expect(service.canAccessRoute('settings', {
      permissions: { admin: { settings: { read: true } } },
    })).toBe(true);
  });

  it('always allows home and the admin shell', () => {
    expect(service.canAccessRoute('home', nobody)).toBe(true);
    expect(service.canAccessRoute('admin', nobody)).toBe(true);
    expect(service.canAccessRoute('', nobody)).toBe(true);
  });

  it('tolerates a leading /admin/ prefix and stray slashes', () => {
    expect(service.canAccessRoute('/admin/billing/', receptionist)).toBe(true);
    expect(service.canAccessRoute('admin/finance', accountant)).toBe(true);
  });
});

describe('firstAccessibleRoute', () => {
  it('sends an owner to home', () => {
    expect(service.firstAccessibleRoute(owner)).toBe('/admin/home');
  });

  it('sends a user to a module they can actually open', () => {
    const target = service.firstAccessibleRoute(receptionist);
    expect(target).toBe('/admin/billing');
    expect(service.canAccessRoute(target.replace('/admin/', ''), receptionist)).toBe(true);
  });

  it('falls back to an admin submodule when no module is granted', () => {
    const target = service.firstAccessibleRoute(accountant);
    expect(service.canAccessRoute(target.replace('/admin/', ''), accountant)).toBe(true);
  });

  it('never returns a route the guard would refuse', () => {
    for (const user of [owner, superuser, receptionist, accountant, nobody]) {
      const target = service.firstAccessibleRoute(user);
      const path = target.replace('/admin/', '');
      expect(service.canAccessRoute(path, user)).toBe(true);
    }
  });
});

describe('hasAction', () => {
  const canceller = {
    permissions: {
      billing: { invoices: { read: true, actions: { cancel: true, change_payment: false } } },
    },
  };

  it('reads the action tick, not the CRUD verbs', () => {
    expect(service.hasAction('billing', 'invoices', 'cancel', canceller)).toBe(true);
    expect(service.hasAction('billing', 'invoices', 'change_payment', canceller)).toBe(false);
    expect(service.hasAction('billing', 'invoices', 'cancel', receptionist)).toBe(false);
  });

  it('grants the superuser every tick and a named owner none', () => {
    expect(service.hasAction('staff', 'payrolls', 'lock', superuser)).toBe(true);
    expect(service.hasAction('staff', 'payrolls', 'lock', namedOwner)).toBe(false);
  });
});

describe('hasAllCenters', () => {
  it('is true for owners and for the explicit flag', () => {
    expect(service.hasAllCenters(owner)).toBe(true);
    expect(service.hasAllCenters({ role: 'Regional', permissions: { all_centers: true } })).toBe(true);
    expect(service.hasAllCenters(receptionist)).toBe(false);
  });
});
