import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { catchError } from 'rxjs/operators';
import { throwError } from 'rxjs';
import { ToastService } from './services/toast.service';

/**
 * Turns an HTTP failure into a message a salon operator can act on.
 *
 * Previously every unmatched status fell through to "An unknown error
 * occurred." - including 401 and 404, so an expired session produced a
 * meaningless toast immediately before the redirect to login. It also toasted
 * failures from background polling, and rendered nested DRF error objects as
 * "[object Object]".
 */

// Statuses the auth interceptor already handles with a redirect, or that a
// caller is expected to handle itself.
const SILENT_STATUSES = new Set([401, 0]);

// 503 answers of the server's report limits (pos_backend/heavy_reports.py) that carry a message worth showing.
const REPORT_LIMIT_CODES = new Set(['reports_busy', 'report_too_big']);

export const errorInterceptor: HttpInterceptorFn = (req, next) => {
  const toastService = inject(ToastService);

  return next(req).pipe(
    catchError((error: HttpErrorResponse | any) => {
      // A blocked double-submit is not a server error worth shouting about.
      if (error?.__duplicateSubmit) {
        return throwError(() => error);
      }

      const isBackground = req.headers.has('X-Background-Request');
      const status = error?.status ?? 0;

      // "This bill needs approval" is a question the till answers by asking an
      // approver, not an error to shout about.
      const asksApproval = status === 409 && error?.error?.code === 'approval_required';

      // A refused READ is a part of the page the role has no access to (a side
      // panel, a figure), not something the person did, so it is not shouted as
      // an error. A refused action (save, delete, cancel...) still is.
      const refusedRead = status === 403 && req.method === 'GET';

      if (!isBackground && !asksApproval && !refusedRead && !SILENT_STATUSES.has(status)) {
        toastService.showError(describe(error));
      } else if (!isBackground && status === 0) {
        toastService.showError(
          'Cannot reach the server. Check the network connection and try again.',
        );
      }

      return throwError(() => error);
    }),
  );
};

function describe(error: HttpErrorResponse | any): string {
  if (error?.error instanceof ErrorEvent) {
    return error.error.message || 'Something went wrong in the browser.';
  }

  const status: number = error?.status ?? 0;

  switch (status) {
    case 400:
      return flattenValidationErrors(error?.error) || 'Please check the values entered.';
    case 403:
      return (
        pickDetail(error?.error) ||
        'You do not have permission to do that. Ask an owner if you need access.'
      );
    case 404:
      return pickDetail(error?.error) || 'That record no longer exists. Refresh and try again.';
    case 405:
      return pickDetail(error?.error) || 'That action is not allowed on this record.';
    case 409:
      return (
        pickDetail(error?.error) ||
        'Someone else changed this record first. Refresh to see the current version.'
      );
    case 429:
      return pickDetail(error?.error) || 'Too many attempts. Wait a moment and try again.';
    case 503:
      // A big report that was turned away or stopped on purpose says why (HEAVY_REPORT_* on the server): "many
      // reports are being built, try again" / "choose a shorter period or fewer centres".
      if (REPORT_LIMIT_CODES.has(error?.error?.code)) {
        return pickDetail(error?.error);
      }
      return 'The server is not responding. Try again in a moment.';
    case 502:
    case 504:
      return 'The server is not responding. Try again in a moment.';
    default:
      if (status >= 500) {
        return 'Something went wrong on the server. Nothing was saved — please try again.';
      }
      return pickDetail(error?.error) || `Request failed (${status}).`;
  }
}

function pickDetail(payload: any): string {
  if (typeof payload === 'string') {
    return payload;
  }
  if (payload && typeof payload === 'object') {
    for (const key of ['detail', 'error', 'message']) {
      const value = payload[key];
      if (typeof value === 'string' && value) {
        return value;
      }
    }
  }
  return '';
}

/**
 * Flatten a DRF validation payload into one readable line.
 *
 * Recurses into nested objects and arrays, which the previous version did not -
 * it called `.join()` on values that could be objects and produced
 * "[object Object]".
 */
function flattenValidationErrors(payload: any, depth = 0): string {
  if (depth > 4 || payload == null) {
    return '';
  }
  if (typeof payload === 'string') {
    return payload;
  }
  if (Array.isArray(payload)) {
    return payload
      .map((entry) => flattenValidationErrors(entry, depth + 1))
      .filter(Boolean)
      .join(' ');
  }
  if (typeof payload !== 'object') {
    return String(payload);
  }

  const direct = pickDetail(payload);
  if (direct) {
    return direct;
  }

  const parts: string[] = [];
  for (const [field, value] of Object.entries(payload)) {
    const text = flattenValidationErrors(value, depth + 1);
    if (!text) {
      continue;
    }
    // Field-name prefixes help on forms but read as noise on a single message.
    parts.push(field === 'non_field_errors' ? text : `${humanise(field)}: ${text}`);
  }
  return parts.join(' | ');
}

function humanise(field: string): string {
  return field.replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}
