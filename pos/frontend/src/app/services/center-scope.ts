import { Injectable } from '@angular/core';
import { HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';

/**
 * The centres ticked in a screen's location dropdown, when more than one is.
 *
 * A screen whose dropdown allows several choices (`multi`) puts the choice here
 * while it is open and takes it away when it closes, so a screen that cannot
 * show several centres (billing, a form) is never affected by what another
 * screen picked.
 */
@Injectable({ providedIn: 'root' })
export class CenterScope {
  private _ids: number[] = [];

  /** Two or more centres, else empty (one centre is sent as `center_id`). */
  get ids(): number[] {
    return this._ids;
  }

  set(ids: number[]): void {
    const clean = Array.from(new Set((ids || []).filter(i => Number.isFinite(i))));
    this._ids = clean.length > 1 ? clean : [];
  }

  clear(): void {
    this._ids = [];
  }
}

/**
 * Reads of lists and reports that understand `center_ids` (the backend narrows
 * them to the picked centres). Anything addressed to one record (`/123/`) and
 * anything that already names a single centre is left alone.
 */
const SCOPED_PATHS: RegExp[] = [
  /\/finance\/api\//,
  /\/salon_admin\/api\/dashboard\//,
  /\/billing\/(invoices|advances|change-logs)\/$/,
  /\/(staff|inventory|marketing|appointments|services)\/api\//,
  /\/clients\/api\/clients\/$/,
  /\/sap-sync\/api\/$/,
];

export const centerScopeInterceptor: HttpInterceptorFn = (req, next) => {
  const scope = inject(CenterScope);
  if (req.method !== 'GET' || !scope.ids.length) {
    return next(req);
  }
  let path: string;
  try {
    path = new URL(req.url, 'http://x').pathname;
  } catch {
    return next(req);
  }
  const query = new URL(req.url, 'http://x').searchParams;
  const named = (query.get('center_id') || req.params.get('center_id') || '').trim();
  const alreadyScoped = query.has('center_ids') || req.params.has('center_ids');
  if (alreadyScoped || (named && named !== 'null' && named !== 'undefined')) {
    return next(req);
  }
  if (/\/\d+(\/|$)/.test(path) || !SCOPED_PATHS.some(rx => rx.test(path))) {
    return next(req);
  }
  return next(req.clone({ params: req.params.set('center_ids', scope.ids.join(',')) }));
};
