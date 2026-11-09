import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { api, ApiError } from '../api';
import { Icon } from './Icon';

export type CardKind = '' | 'accent' | 'black' | 'cream';

export function Card({ title, aside, children, className = '', kind = '' }: { title?: string; aside?: ReactNode; children: ReactNode; className?: string; kind?: CardKind }) {
  return (
    <section className={`card${kind ? ` ${kind}` : ''} ${className}`}>
      {title ? (
        <h2>
          <span>{title}</span>
          {aside ? <span className="aside">{aside}</span> : null}
        </h2>
      ) : null}
      {children}
    </section>
  );
}

export function Skeleton({ h = 120 }: { h?: number }) {
  return <div className="skeleton" style={{ height: h }} />;
}

export function Notice({ kind = '', children, action }: { kind?: '' | 'bad' | 'good'; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`notice ${kind}`} role={kind === 'bad' ? 'alert' : 'status'}>
      {children} {action}
    </div>
  );
}

export function Empty({ icon, children }: { icon: string; children: ReactNode }) {
  return (
    <div className="empty">
      <span className="chip accent"><Icon name={icon} size={22} /></span>
      <p>{children}</p>
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg full" role="group">
      {options.map((o) => (
        <button key={o.id} aria-pressed={value === o.id} onClick={() => onChange(o.id)}>{o.label}</button>
      ))}
    </div>
  );
}

const LIVE_MS = 5000;

/** Fetches `path` on mount (and again on `reload`), then keeps it live: every few seconds, and the moment the app comes
 *  back to the front, it asks again and redraws only if something changed. Nothing is kept on the phone: a balance or a
 *  PIN shown from yesterday could be wrong, so the app asks every time. */
export function useLoad<T>(path: string) {
  const [state, setState] = useState<{ data: T | null; error: string | null; loading: boolean }>({ data: null, error: null, loading: true });
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController();
    setState((s) => ({ ...s, error: null, loading: true }));
    api<T>(path, { signal: controller.signal })
      .then((data) => alive.current && setState({ data, error: null, loading: false }))
      .catch((err: unknown) => {
        if ((err as Error).name === 'AbortError') return;
        setState((s) => ({ ...s, loading: false, error: err instanceof ApiError ? err.message : 'Something went wrong. Try again.' }));
      });
    return () => {
      alive.current = false;
      controller.abort();
    };
  }, [path, tick]);
  useEffect(() => {
    const controller = new AbortController();
    let busy = false;
    const refresh = () => {
      if (document.hidden || busy) return;
      busy = true;
      api<T>(path, { signal: controller.signal })
        .then((data) => setState((s) => (JSON.stringify(s.data) === JSON.stringify(data) ? s : { data, error: null, loading: false })))
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
  }, [path]);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  return { ...state, reload };
}

/** What a screen shows while it has no data yet, or could not get any. */
export function Waiting({ error, loading, reload, h = 200 }: { error: string | null; loading: boolean; reload: () => void; h?: number }) {
  if (error) {
    return (
      <Notice kind="bad" action={<button className="link" onClick={reload}>Try again</button>}>{error}</Notice>
    );
  }
  return loading ? <Skeleton h={h} /> : null;
}
