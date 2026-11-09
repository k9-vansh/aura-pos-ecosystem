import type { Me } from './types';

const KEY = 'staff.session';

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

/** Only the sign-in key and the name are kept on the phone: never balances and never the PIN. */
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
}
