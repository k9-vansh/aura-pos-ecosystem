import { HttpErrorResponse, HttpInterceptorFn } from '@angular/common/http';
import { inject } from '@angular/core';
import { Router } from '@angular/router';
import { throwError } from 'rxjs';
import { catchError } from 'rxjs/operators';
import { clearSession } from './services/session';

/**
 * Attaches the auth token and handles session expiry.
 *
 * The Cache-Control / Pragma / Expires headers this interceptor used to set on
 * EVERY request were removed. They are not CORS-simple headers, so each API
 * call triggered a preflight OPTIONS followed by the real request - roughly
 * double the round trips, on a POS where latency is felt at the counter.
 *
 * Cache-busting is handled where it belongs instead: the API is served with
 * no-store by the backend for data endpoints, and mutations naturally bypass
 * the HTTP cache.
 */
export const authInterceptor: HttpInterceptorFn = (req, next) => {
  const router = inject(Router);
  const token = localStorage.getItem('token');

  const isAuthEndpoint = req.url.includes('/api/login/');
  let request = req;

  if (token && !isAuthEndpoint) {
    request = req.clone({
      setHeaders: { Authorization: `Token ${token}` },
    });
  }

  return next(request).pipe(
    catchError((error: HttpErrorResponse) => {
      // 401 means the token is invalid, expired or revoked. Clear everything
      // and send the operator back to login.
      if (error.status === 401 && !isAuthEndpoint) {
        clearSession();
        router.navigate(['/login'], {
          queryParams: { reason: 'session-expired' },
        });
      }
      return throwError(() => error);
    }),
  );
};
