/**
 * Development environment.
 *
 * Points at a LOCAL backend. It used to point at http://38.45.94.56:4091, so
 * `ng serve` on a developer machine talked to the live production server over
 * plain HTTP, carrying real auth tokens and customer phone numbers.
 *
 * To develop against a remote backend, set it in the shell rather than editing
 * this file:
 *   API_URL=http://your-host:4091 npm start
 */
declare const process: { env?: Record<string, string | undefined> } | undefined;

const fromEnv =
  typeof process !== 'undefined' && process?.env ? process.env['API_URL'] : undefined;

function readOverride(): string | undefined {
  try {
    const v = typeof localStorage !== 'undefined' ? localStorage.getItem('API_URL') : null;
    return v && /^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?\/?$/.test(v) ? v : undefined;
  } catch {
    return undefined;
  }
}

export const environment = {
  production: false,
  // Development only: `localStorage.API_URL = 'http://localhost:8002'` points this browser at another
  // local backend (a test copy) without rebuilding. environment.prod.ts has no such override.
  apiUrl: (readOverride() || fromEnv || 'http://localhost:8001').replace(/\/+$/, ''),
};
