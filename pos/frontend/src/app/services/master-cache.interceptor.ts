import { HttpEvent, HttpInterceptorFn, HttpRequest, HttpResponse } from '@angular/common/http';
import { Observable, of } from 'rxjs';
import { finalize, map, shareReplay, tap } from 'rxjs/operators';

/**
 * Remembers the big master lists for a few seconds, so moving between screens does
 * not download and rebuild them every time.
 *
 * Vendors, products and the service catalogue are fetched by
 * several screens (the till, inventory, appointments, reports) and again each time
 * a tab is opened. One response is now shared by every request for the same URL:
 *
 *  - a request already on its way is joined, not repeated;
 *  - a response younger than TTL is served from memory, with no network call;
 *  - ANY write (POST / PUT / PATCH / DELETE) anywhere empties the cache, so a sale,
 *    a price change or a new vendor is never hidden behind an old copy;
 *  - the cache key includes the sign-in token, so one user never sees another's data;
 *  - every caller gets its own copy of the data, so a screen that sorts or edits its
 *    list cannot change what the next screen receives.
 *
 * Other terminals' changes show up within TTL (15 s). Add the header
 * `X-No-Cache: 1` to a request to always go to the server.
 */
const TTL_MS = 15_000;
const MAX_ENTRIES = 40;

const MASTER_PATHS: RegExp[] = [
  /\/inventory\/api\/(vendors|products)\/$/,
  /\/services\/api\/master\/$/,
  // The centre list is deliberately NOT cached: it is small, and a centre switched on or off
  // on another terminal must show on the next screen opened, not up to 15 s later.
];

interface Entry { at: number; json: string; generation: number }

const entries = new Map<string, Entry>();
const pending = new Map<string, Observable<HttpEvent<unknown>>>();
/** Bumped by every write; a response that started before a write is never stored. */
let generation = 0;

function isMasterList(req: HttpRequest<unknown>): boolean {
  if (req.method !== 'GET' || req.responseType !== 'json' || req.headers.has('X-No-Cache')) return false;
  let path: string;
  try { path = new URL(req.url, 'http://x').pathname; } catch { return false; }
  return MASTER_PATHS.some(rx => rx.test(path));
}

function copyOf(ev: HttpEvent<unknown>): HttpEvent<unknown> {
  return ev instanceof HttpResponse && ev.body != null
    ? ev.clone({ body: JSON.parse(JSON.stringify(ev.body)) })
    : ev;
}

/** For tests. */
export function clearMasterCache(): void {
  entries.clear();
  pending.clear();
  generation++;
}

export const masterCacheInterceptor: HttpInterceptorFn = (req, next) => {
  if (req.method !== 'GET' && req.method !== 'HEAD' && req.method !== 'OPTIONS') {
    generation++;
    entries.clear();
    // Bump again when the write finishes: a list requested while it was running may
    // already hold the old data.
    return next(req).pipe(finalize(() => { generation++; entries.clear(); }));
  }
  if (!isMasterList(req)) return next(req);

  const key = `${req.headers.get('Authorization') ?? ''}|${req.urlWithParams}`;

  const hit = entries.get(key);
  if (hit && Date.now() - hit.at < TTL_MS && hit.generation === generation) {
    return of(new HttpResponse({ status: 200, statusText: 'OK', url: req.urlWithParams, body: JSON.parse(hit.json) }));
  }

  const joined = pending.get(key);
  if (joined) return joined.pipe(map(copyOf));

  const started = generation;
  const request$ = next(req).pipe(
    tap(ev => {
      if (ev instanceof HttpResponse && ev.status === 200 && ev.body != null && started === generation) {
        if (entries.size >= MAX_ENTRIES) entries.delete(entries.keys().next().value as string);
        entries.set(key, { at: Date.now(), json: JSON.stringify(ev.body), generation: started });
      }
    }),
    finalize(() => pending.delete(key)),
    shareReplay({ bufferSize: 1, refCount: false }),
  );
  pending.set(key, request$);
  return request$.pipe(map(copyOf));
};
