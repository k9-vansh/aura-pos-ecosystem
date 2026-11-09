/**
 * Production environment.
 *
 * `apiUrl` was an empty string with a "update this" comment that never got
 * updated. angular.json swaps this file in for production builds, so every
 * request resolved same-origin and 404'd - the doubled-slash
 * `//accounts/api/login/` 404s in the server log are this bug.
 *
 * Set it via the build, not by editing this file, so the value cannot drift:
 *   API_URL=https://api.yourdomain.com npm run build
 * Falling back to same-origin is correct only if a reverse proxy on the
 * frontend host forwards /accounts, /billing, /finance ... to Django.
 */
declare const process: { env?: Record<string, string | undefined> } | undefined;

function resolveApiUrl(): string {
  // Injected at build time when present.
  const fromEnv =
    typeof process !== 'undefined' && process?.env ? process.env['API_URL'] : undefined;
  if (fromEnv) {
    return stripTrailingSlashes(fromEnv);
  }

  // Injected at runtime by index.html or a config script, so one build can be
  // promoted across environments without rebuilding.
  const fromWindow = (globalThis as any)?.__POS_API_URL__;
  if (typeof fromWindow === 'string' && fromWindow.length > 0) {
    return stripTrailingSlashes(fromWindow);
  }

  // Same-origin. Only valid behind a reverse proxy.
  return '';
}

function stripTrailingSlashes(value: string): string {
  return value.replace(/\/+$/, '');
}

export const environment = {
  production: true,
  apiUrl: resolveApiUrl(),
};
