import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiError } from './api';

export interface Loaded<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  /** The figures on screen are the last ones saved on this phone, not fresh from the server. */
  stale: boolean;
  reload: () => void;
}

const PREFIX = 'owner.cache.';
const LIVE_MS = 10000;

function readCache<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function writeCache(key: string, value: unknown) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* storage full: the next visit simply starts without a saved copy */
  }
}

/**
 * Fetches `path` with `params`. The last answer for the same request is shown at once (marked stale) while the fresh
 * one loads, so a slow connection or none still shows something useful. `enabled=false` waits.
 */
export function useApi<T>(path: string, params: Record<string, string | number | null | undefined>, enabled = true, live = true): Loaded<T> {
  const key = `${path}?${JSON.stringify(params)}`;
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null; loading: boolean; stale: boolean }>(() => ({
    key,
    data: readCache<T>(key),
    error: null,
    loading: enabled,
    stale: true,
  }));
  const [tick, setTick] = useState(0);
  const latest = useRef(key);
  latest.current = key;

  // A different request: show its saved copy (or nothing) straight away, not the previous request's figures.
  if (state.key !== key) setState({ key, data: readCache<T>(key), error: null, loading: enabled, stale: true });

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    setState((s) => (s.key === key ? { ...s, loading: true, error: null } : s));
    api<T>(path, { params, signal: controller.signal })
      .then((data) => {
        writeCache(key, data);
        setState((s) => (s.key === key ? { key, data, error: null, loading: false, stale: false } : s));
      })
      .catch((err: unknown) => {
        if ((err as Error).name === 'AbortError') return;
        const text = err instanceof ApiError ? err.message : 'Something went wrong. Try again.';
        setState((s) => (s.key === key ? { ...s, error: text, loading: false } : s));
      });
    return () => controller.abort();
    // `params` is covered by `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, tick]);

  // Live: ask again every few seconds, and the moment the app comes back to the front, and redraw only if the figures
  // changed. A screen with a form on it passes live=false so what is being typed is never replaced.
  useEffect(() => {
    if (!enabled || !live) return;
    const controller = new AbortController();
    let busy = false;
    const refresh = () => {
      if (document.hidden || busy) return;
      busy = true;
      api<T>(path, { params, signal: controller.signal })
        .then((data) => {
          writeCache(key, data);
          setState((s) => (s.key !== key || (!s.stale && !s.error && JSON.stringify(s.data) === JSON.stringify(data)) ? s : { key, data, error: null, loading: false, stale: false }));
        })
        .catch(() => undefined)   // a missed beat is not worth an error on screen: the next one tries again
        .finally(() => { busy = false; });
    };
    const timer = setInterval(refresh, LIVE_MS);
    document.addEventListener('visibilitychange', refresh);
    window.addEventListener('online', refresh);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
      window.removeEventListener('online', refresh);
      controller.abort();
    };
    // `params` is covered by `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, enabled, live]);

  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { data: state.data, error: state.error, loading: state.loading, stale: state.stale, reload };
}
