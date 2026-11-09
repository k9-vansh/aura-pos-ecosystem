/**
 * Session teardown.
 *
 * `logout()` used to remove only `token` and `user`. Everything else stayed in
 * localStorage - including `billing_draft` and `held_carts`, which hold the
 * customer's name and phone number and the contents of their bill. On a shared
 * salon terminal the next person to sign in was shown the previous customer's
 * cart.
 *
 * Anything session- or customer-scoped belongs in SESSION_KEYS so there is one
 * place to add to when new local state is introduced.
 */

/** Keys wiped on sign-out. */
export const SESSION_KEYS = [
  'token',
  'user',
  // Work in progress that belongs to whoever was signed in.
  'billing_draft',
  'held_carts',
  // Screen state that leaks which customer/center was last worked on.
  'selectedCenterId',
  'billing_last_client',
  'pos_recent_clients',
] as const;

/** Prefixes wiped on sign-out, for keys with a dynamic suffix. */
const SESSION_KEY_PREFIXES = ['billing_draft_', 'held_carts_', 'pos_'];

export function clearSession(): void {
  for (const key of SESSION_KEYS) {
    try {
      localStorage.removeItem(key);
    } catch {
      // Storage can be unavailable in private mode; nothing to clean up then.
    }
  }

  try {
    const dynamic: string[] = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (key && SESSION_KEY_PREFIXES.some((prefix) => key.startsWith(prefix))) {
        dynamic.push(key);
      }
    }
    dynamic.forEach((key) => localStorage.removeItem(key));
  } catch {
    // Ignore: enumeration is best-effort.
  }

  // sessionStorage is per-tab, but a shared terminal often keeps one tab open.
  try {
    sessionStorage.clear();
  } catch {
    // Ignore.
  }
}

export function getStoredUser(): any | null {
  try {
    const raw = localStorage.getItem('user');
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function getStoredToken(): string | null {
  try {
    return localStorage.getItem('token');
  } catch {
    return null;
  }
}

export function getDefaultCenterId(centers: any[]): number | null {
  if (!centers || centers.length === 0) return null;
  const saved = localStorage.getItem('globalSelectedCenterId');
  if (saved) {
    const parsed = parseInt(saved, 10);
    if (!isNaN(parsed) && centers.find((c: any) => c.id === parsed)) {
      return parsed;
    }
  }
  return centers[0].id;
}
