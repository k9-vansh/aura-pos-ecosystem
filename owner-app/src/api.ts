import { loadSession } from './session';

const BASE = `${((import.meta.env.VITE_API_URL as string | undefined) ?? '').replace(/\/$/, '')}/owner/api`;

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message);
  }
}

let onSignedOut: () => void = () => {};
export function setSignedOutHandler(fn: () => void) {
  onSignedOut = fn;
}

type Params = Record<string, string | number | null | undefined>;

function message(data: unknown, status: number): string {
  if (data && typeof data === 'object') {
    const body = data as Record<string, unknown>;
    const first = body.detail ?? Object.values(body)[0];
    const text = Array.isArray(first) ? first[0] : first;
    if (typeof text === 'string') return text;
  }
  if (status === 429) return 'Too many tries. Wait a minute and try again.';
  if (status === 503) return 'The server is busy with a big report. Try again in a moment.';
  return 'Something went wrong. Try again.';
}

export async function api<T>(path: string, opts: { method?: string; params?: Params; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const query = Object.entries(opts.params ?? {})
    .filter(([, v]) => v !== null && v !== undefined && v !== '')
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join('&');
  const token = loadSession()?.token;
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}${query ? `?${query}` : ''}`, {
      method: opts.method ?? 'GET',
      signal: opts.signal,
      headers: {
        Accept: 'application/json',
        ...(opts.body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(token ? { Authorization: `Token ${token}` } : {}),
      },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    });
  } catch (err) {
    if ((err as Error).name === 'AbortError') throw err;
    throw new ApiError('No connection. Showing the last figures we had.', 0);
  }
  const data = await res.json().catch(() => null);
  if (res.status === 401 && token) {
    onSignedOut();
    throw new ApiError('Please sign in again.', 401);
  }
  if (!res.ok) throw new ApiError(message(data, res.status), res.status);
  return data as T;
}
