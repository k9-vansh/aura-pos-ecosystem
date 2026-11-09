import { AdminFilterService } from './admin-filter.service';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnDestroy, OnInit, ViewChild, inject } from '@angular/core';
import { RouterOutlet, RouterLink, RouterLinkActive, Router } from '@angular/router';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { AuthService } from '../services/auth';
import { ApiService } from '../services/api';

import { ChatComponent } from '../components/chat/chat.component';
import { BellComponent } from '../components/bell/bell.component';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';
import { ADMIN_SUBMODULE_ROUTES } from '../services/permission-tree';
import { ViewRefresh } from '../services/view-refresh';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin',
  imports: [RouterOutlet, RouterLink, RouterLinkActive, CommonModule, FormsModule, LocationSelectorComponent, ChatComponent, BellComponent],
  templateUrl: './admin.html',
  styleUrl: './admin.css',
})
export class AdminComponent implements OnInit, OnDestroy {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private locationMemory = inject(LocationMemory);
  authService = inject(AuthService);
  apiService = inject(ApiService);
  router = inject(Router);
  adminFilterService = inject(AdminFilterService);
  viewRefresh = inject(ViewRefresh);
  centers: any[] = [];

  /** The open page is redrawn after every request (see ViewRefresh). */
  onPageActivated(outlet: RouterOutlet) {
    // RouterOutlet has no public handle on the page's ComponentRef, only the private `activated`;
    // if a later Angular renames it this just stops redrawing after requests (nothing breaks).
    this.viewRefresh.attachRef((outlet as any)?.activated ?? null);
  }

  loadCenters() {
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.centers = Array.isArray(data) ? data : (data.results || []);
        // A centre that was just deactivated can no longer be the one the pages are filtered to.
        const chosen = this.adminFilterService.currentCenterId;
        if (chosen !== null && !this.centers.some((c: any) => c.id === chosen)) {
          this.adminFilterService.setCenterId(null);
        }
        if (this.adminFilterService.currentCenterId === null) {
          const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
          if (remembered) this.adminFilterService.setCenterId(remembered);
        }
        this.cdr.detectChanges();
      },
      error: err => console.error('Failed to load centers', err)
    });
  }

  cdr = inject(ChangeDetectorRef);
  permissions: any = {};
  isOwner = false;
  hasGlobalAccess = false;
  displayName = 'User';
  displayRole = 'Staff';
  displayInitials = 'U';
  displayFirstName = 'User';

  get greeting(): string {
    const h = new Date().getHours();
    if (h < 12) return 'morning';
    if (h < 17) return 'afternoon';
    return 'evening';
  }

  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.displayName = user.full_name || user.email || 'User';
        this.displayRole = user.designation || user.role || (user.is_superuser ? 'Super Admin' : 'Staff');
        this.displayFirstName = this.displayName.trim().split(' ')[0];
        const parts = this.displayName.trim().split(' ');
        this.displayInitials = parts.length >= 2
          ? (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
          : this.displayName.slice(0, 2).toUpperCase();
      } catch (e) {
        console.error('Failed to parse user from localStorage', e);
      }
    }
    
    // Theme logic
    const savedTheme = localStorage.getItem('theme') || 'light';
    this.currentTheme = (savedTheme === 'default' || savedTheme === 'light') ? 'light' : savedTheme;
    this.applyTheme();

    this.loadCenters();
    // A centre added, changed, activated or deactivated on the Centres page: reload the list now.
    this.apiService.centersChanged.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => this.loadCenters());
    this.startGlobalPolling();
  }

  currentTheme = 'light';

  toggleTheme() {
    if (this.currentTheme === 'light') {
      this.currentTheme = 'dark';
    } else if (this.currentTheme === 'dark') {
      this.currentTheme = 'colorful';
    } else {
      this.currentTheme = 'light';
    }
    const change = () => this.applyTheme();
    const doc: any = document;
    if (typeof doc.startViewTransition === 'function') {
      // the browser cross-fades the old page into the new theme
      doc.startViewTransition(change);
    } else {
      const root = document.documentElement;
      root.classList.add('theme-fade');
      change();
      setTimeout(() => root.classList.remove('theme-fade'), 600);
    }
    localStorage.setItem('theme', this.currentTheme);
  }

  applyTheme() {
    if (this.currentTheme === 'light' || this.currentTheme === 'default') {
      document.body.removeAttribute('data-theme');
    } else {
      document.body.setAttribute('data-theme', this.currentTheme);
    }
  }

  /**
   * Where the sidebar's Admin entry leads: the first admin page this role can
   * open. It always went to Centres, so a role granted only Bills or Settings
   * was sent to a page the route guard then refused.
   */
  get adminHomeRoute(): string {
    for (const [route, sub] of Object.entries(ADMIN_SUBMODULE_ROUTES)) {
      if (this.isOwner || this.permissions?.admin?.[sub]?.read === true) {
        return `/admin/${route}`;
      }
    }
    return '/admin/centers';
  }

  hasModuleReadAccess(modName: string): boolean {
    const mod = this.permissions[modName];
    if (!mod || typeof mod !== 'object') return false;
    return Object.values(mod).some((sub: any) => sub && sub.read === true);
  }

  logout() {
    this.authService.logout();
  }

  get isAdminPage(): boolean {
    const url = this.router.url;
    return url.includes('/admin/centers') || 
           url.includes('/admin/users') || 
           url.includes('/admin/roles') ||
           url.includes('/admin/services') ||
           url.includes('/admin/clients') ||
           url.includes('/admin/bills') ||
           url.includes('/admin/changes') ||
           url.includes('/admin/manager-discounts') ||
           url.includes('/admin/master-pins') ||
           url.includes('/admin/settings') ||
           url.includes('/admin/sap-sync') ||
           url.includes('/admin/approvals') ||
           url.includes('/admin/integrations');
  }

  get isInventoryPage(): boolean {
    return this.router.url.includes('/admin/inventory');
  }

  get isBillingPage(): boolean {
    return this.router.url.includes('/admin/billing');
  }

  get isMarketingPage(): boolean {
    return this.router.url.includes('/admin/marketing');
  }

  get isStaffPage(): boolean {
    return this.router.url.includes('/admin/staff');
  }

  get isAppointmentsPage(): boolean {
    return this.router.url.includes('/admin/appointments');
  }

  get isDashboardPage(): boolean {
    return this.router.url.includes('/admin/dashboard');
  }

  get isHomePage(): boolean {
    return this.router.url.includes('/admin/home');
  }

  get isFinancePage(): boolean {
    return this.router.url.includes('/admin/finance');
  }

  get isLogsPage(): boolean {
    return this.router.url.includes('/admin/logs');
  }

  get hideAdminHeader(): boolean {
    return this.isInventoryPage || this.isMarketingPage || this.isStaffPage || this.isAppointmentsPage || this.isBillingPage || this.isDashboardPage || this.isHomePage || this.isFinancePage || this.isLogsPage;
  }

  // --- Global State ---
  unreadChatCount = 0;
  lowStockCount = 0;
  private globalPollingInterval: any;
  consecutiveGlobalErrors = 0;
  private pollTicks = 0;
  private lastLowStockAt = 0;
  private readonly onVisible = () => {
    // Coming back to the tab: refresh the badges at once instead of waiting.
    if (!document.hidden && this.globalPollingInterval) {
      this.fetchUnreadChatCount();
      if (Date.now() - this.lastLowStockAt > 60000) this.fetchLowStockCount();
    }
  };

  startGlobalPolling() {
    this.consecutiveGlobalErrors = 0;
    this.fetchUnreadChatCount();
    this.fetchLowStockCount();
    this.pollTicks = 0;
    this.globalPollingInterval = setInterval(() => {
      // A tab in the background does not need badges; skip the requests.
      if (document.hidden) return;
      this.pollTicks++;
      this.fetchUnreadChatCount(); // unread chat every 30 seconds
      // Low stock downloads the whole low-stock list just to count it, so it
      // runs every 5 minutes (every 10th tick) rather than every 30 seconds.
      if (this.pollTicks % 10 === 0) this.fetchLowStockCount();
    }, 30000);
    document.addEventListener('visibilitychange', this.onVisible);
  }

  stopGlobalPolling() {
    if (this.globalPollingInterval) {
      clearInterval(this.globalPollingInterval);
      this.globalPollingInterval = null;
    }
    document.removeEventListener('visibilitychange', this.onVisible);
  }

  /** Chat is a grantable module (Chat > Messages); the server refuses it otherwise. */
  get canChat(): boolean {
    return this.isOwner || this.permissions?.chat?.messages?.read === true;
  }

  fetchUnreadChatCount() {
    if (!this.canChat) {
      return;
    }
    this.apiService.get("accounts/api/chat/unread/", undefined, { headers: { "X-Background-Request": "true" } }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.consecutiveGlobalErrors = 0;
        this.unreadChatCount = res.count || 0;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.handleGlobalError(err);
      }
    });
  }

  fetchLowStockCount() {
    this.lastLowStockAt = Date.now();
    // The badge sits on the Inventory entry; a role without Inventory has no
    // badge to show, and asking anyway was refused on every refresh.
    if (!this.isOwner && !this.hasModuleReadAccess('inventory')) {
      return;
    }
    this.apiService.get("inventory/api/low_stock/", { count_only: 1 }, { headers: { "X-Background-Request": "true" } }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.consecutiveGlobalErrors = 0;
        this.lowStockCount = res?.count ?? 0;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.handleGlobalError(err);
      }
    });
  }

  handleGlobalError(err: any) {
    this.consecutiveGlobalErrors++;
    if (this.consecutiveGlobalErrors > 5) {
      console.error('Global polling failed repeatedly. Stopping.', err);
      this.stopGlobalPolling();
    }
  }

  ngOnDestroy() {
    this.stopGlobalPolling();
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
