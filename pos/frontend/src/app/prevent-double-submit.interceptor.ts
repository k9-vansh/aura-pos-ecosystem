import { HttpInterceptorFn, HttpRequest, HttpHandlerFn, HttpEvent } from '@angular/common/http';
import { Observable } from 'rxjs';
import { finalize } from 'rxjs/operators';

/**
 * Guards against a double-CLICK, without swallowing a deliberate repeat.
 *
 * The previous implementation keyed on `method + url + JSON.stringify(body)`,
 * held the entry for a second AFTER completion, and returned the first
 * response to the second caller via shareReplay. That silently discarded
 * legitimate identical requests: two same-amount petty cash entries, or two
 * back-to-back identical walk-in bills, reported success while only one row
 * was created. `JSON.stringify(FormData)` is also `"{}"`, so every file upload
 * to the same URL collided with every other one.
 *
 * What changed:
 *   * Only requests genuinely in flight are blocked. Once one completes the
 *     key is released immediately, so a repeat is a new request.
 *   * A blocked request ERRORS rather than resolving with someone else's
 *     response, so the caller can never mistake it for a successful write.
 *   * FormData and any non-serialisable body are never deduplicated.
 *   * Server-side idempotency is the real protection for checkout; this is
 *     only about the button being clicked twice.
 */

const inFlight = new Set<string>();

// Paths where a rapid identical repeat is normal and must not be blocked.
const NEVER_DEDUPE = [
  '/chat/',        // sending the same message twice is legitimate
  '/api/login',    // a retry after a typo must not be swallowed
  '/invoices/quote/',  // a price check writes nothing; the newest one wins
];

export const preventDoubleSubmitInterceptor: HttpInterceptorFn = (
  req: HttpRequest<unknown>,
  next: HttpHandlerFn,
): Observable<HttpEvent<unknown>> => {
  if (req.method !== 'POST' && req.method !== 'PUT' && req.method !== 'PATCH') {
    return next(req);
  }

  const key = requestKey(req);
  if (key === null) {
    return next(req);
  }

  if (inFlight.has(key)) {
    // Reject rather than replaying another request's response. The caller sees
    // a real failure instead of a phantom success.
    return new Observable<HttpEvent<unknown>>((subscriber) => {
      subscriber.error({
        status: 429,
        statusText: 'Duplicate request',
        error: { detail: 'That request is already being processed.' },
        url: req.url,
        __duplicateSubmit: true,
      });
    });
  }

  inFlight.add(key);
  return next(req).pipe(finalize(() => inFlight.delete(key)));
};

function requestKey(req: HttpRequest<unknown>): string | null {
  if (NEVER_DEDUPE.some((fragment) => req.url.includes(fragment))) {
    return null;
  }
  // FormData, Blob and streams cannot be compared meaningfully.
  const body = req.body;
  if (
    body instanceof FormData ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    typeof body === 'function'
  ) {
    return null;
  }
  let serialised: string;
  try {
    serialised = JSON.stringify(body ?? null);
  } catch {
    return null;
  }
  return `${req.method} ${req.urlWithParams} ${serialised}`;
}
