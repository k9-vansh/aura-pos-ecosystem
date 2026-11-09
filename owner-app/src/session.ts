import type { Me } from './types';

const KEY = 'owner.session';
const SALON = 'owner.salon.v2';

export interface Session {
  token: string;
  me: Me;
}

function read(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* private window or full storage: the app still works for this visit */
  }
}

export function loadSession(): Session | null {
  try {
    const raw = read(KEY);
    return raw ? (JSON.parse(raw) as Session) : null;
  } catch {
    return null;
  }
}

export function saveSession(session: Session) {
  write(KEY, JSON.stringify(session));
}

export function clearSession() {
  write(KEY, null);
  try {
    // The last figures shown belong to the person who signed in; they go with the sign-in.
    Object.keys(localStorage).filter((k) => k.startsWith('owner.cache.')).forEach((k) => localStorage.removeItem(k));
  } catch {
    /* nothing to clear */
  }
}

/** 'all' or a salon id, kept between visits. */
export function loadSalon(me: Me): string {
  const saved = read(SALON);
  if (saved === 'all' && me.salons.length > 1) return 'all';
  if (saved && me.salons.some((s) => String(s.id) === saved)) return saved;
  // Several salons start on the whole business, so the first screen already has figures to show.
  return me.salons.length > 1 ? 'all' : String(me.salons[0]?.id ?? 'all');
}

export function saveSalon(value: string) {
  write(SALON, value);
}
