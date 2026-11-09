import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { tap } from 'rxjs';
import { Router } from '@angular/router';
import { environment } from '../../environments/environment';
import { ModuleAccessService } from './module-access';
import { clearSession, getStoredToken, getStoredUser } from './session';
import { todayLocal } from './date-util';

@Injectable({
  providedIn: 'root',
})
export class AuthService {
  private http = inject(HttpClient);
  private router = inject(Router);
  private access = inject(ModuleAccessService);

  private apiUrl = `${environment.apiUrl}/accounts/api/login/`;

  /** Today's date, recomputed on read rather than frozen at construction. */
  get todayDate(): string {
    return todayLocal();
  }

  login(credentials: any) {
    return this.http.post<any>(this.apiUrl, credentials).pipe(
      tap((res) => {
        if (!res?.token) {
          return;
        }
        // A fresh sign-in must not inherit the previous operator's cart or
        // customer, which is a real risk on a shared salon terminal.
        clearSession();
        localStorage.setItem('token', res.token);
        localStorage.setItem('user', JSON.stringify(res));

        // Land them on the first screen their role can actually open.
        // Route selection now uses the same permission logic as the guard, so
        // the redirect can no longer point at a page the guard will refuse.
        this.router.navigate([this.access.firstAccessibleRoute(res)]);
      }),
    );
  }

  changePassword(payload: {
    current_password: string;
    new_password: string;
    confirm_password?: string;
  }) {
    return this.http
      .post<any>(`${environment.apiUrl}/accounts/api/change-password/`, payload)
      .pipe(
        tap((res) => {
          // The server rotates the token, so adopt the new one or the next
          // request 401s.
          if (res?.token) {
            localStorage.setItem('token', res.token);
          }
        }),
      );
  }

  logout() {
    clearSession();
    this.router.navigate(['/login']);
  }

  isLoggedIn(): boolean {
    return !!getStoredToken() && !!getStoredUser();
  }

  getToken(): string | null {
    return getStoredToken();
  }

  getCurrentUser(): any | null {
    return getStoredUser();
  }

  get isOwner(): boolean {
    return this.access.isOwner();
  }
}
