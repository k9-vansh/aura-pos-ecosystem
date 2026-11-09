import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnDestroy, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { Subject } from 'rxjs';
import { takeUntil, finalize } from 'rxjs/operators';
import { todayLocal, daysAgo } from '../../services/date-util';

const MODULE_LABELS: Record<string, string> = {
  USERS: 'Users', CLIENTS: 'Clients', STAFF: 'Staff', CENTRES: 'Centres',
  BILLING: 'Billing', APPOINTMENTS: 'Appointments', SERVICES: 'Services',
  INVENTORY: 'Inventory', MARKETING: 'Marketing', FINANCE: 'Finance',
  ROLES: 'Roles', DASHBOARD: 'Dashboard', SYSTEM: 'System', AUTH: 'Auth',
};

const ACTION_LABELS: Record<string, string> = {
  LOGIN: 'Login', LOGOUT: 'Logout', LOGIN_FAILED: 'Failed login', CREATE: 'Create', UPDATE: 'Update',
  DELETE: 'Delete', CANCEL: 'Cancel', REFUND: 'Refund', CLOSE_SHIFT: 'Close shift', LOCK: 'Lock',
  MARK_PAID: 'Mark paid', PAYMENT: 'Payment', OVERRIDE: 'Price override', CARRY_OVER: 'Carry over',
  CHECKOUT: 'Checkout', AUDIT: 'Stock audit', CAMPAIGN: 'Campaign',
};

const CRITICAL = new Set(['DELETE', 'CANCEL', 'REFUND', 'LOGIN_FAILED']);
const WARNING  = new Set(['LOGIN', 'LOGOUT', 'LOCK', 'CLOSE_SHIFT', 'MARK_PAID']);

/** The quick filters across the top: what each one asks the server for. */
const PILLS: Record<string, { actions?: string[]; modules?: string[]; outcome?: string }> = {
  all: {},
  security: { actions: ['LOGIN', 'LOGOUT', 'LOGIN_FAILED'] },
  accounts: { modules: ['USERS', 'STAFF', 'CLIENTS', 'ROLES'] },
  finance: { modules: ['BILLING', 'FINANCE'] },
  failed: { outcome: 'failed' },
};

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-logs',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './logs.html',
  styleUrl: './logs.css',
})
export class LogsComponent implements OnInit, OnDestroy {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  logs: any[] = [];
  loading = false;

  pill = 'all';
  selectedAction = '';
  selectedModule = '';

  availableActions: { value: string; count: number }[] = [];
  availableModules: { value: string; count: number }[] = [];
  failedCount = 0;
  facetTotal = 0;

  /** Actions can be combined: ?action=Create&action=Delete */
  selectedActions = new Set<string>();

  // Everyone who appears in the log, as a clickable list.
  logUsers: any[] = [];
  usersLoading = false;
  showUserPanel = true;
  selectedUserId: number | string | null = null;
  selectedUserName = '';
  userSearch = '';

  // Dates are held until Apply is pressed.
  pendingStartDate = '';
  pendingEndDate = '';
  startDate = '';
  endDate = '';
  searchQuery = '';

  totalCount = 0;
  hasNext = false;
  hasPrev = false;
  currentPage = 1;

  readonly MODULE_LABELS = MODULE_LABELS;
  readonly ACTION_LABELS = ACTION_LABELS;

  private destroy$ = new Subject<void>();

  constructor(private api: ApiService, private cdr: ChangeDetectorRef) {}

  ngOnInit() {
    this.pendingEndDate = todayLocal();
    this.pendingStartDate = daysAgo(7);
    this.startDate = this.pendingStartDate;
    this.endDate = this.pendingEndDate;
    this.fetchLogs();
    this.fetchFacets();
    this.fetchLogUsers();
  }

  ngOnDestroy() {
    this.destroy$.next();
    this.destroy$.complete();
  }

  // ── What the server is asked for ────────────────────────────────────────
  private actionsAsked(): string[] {
    const chosen = Array.from(this.selectedActions);
    if (!chosen.length && this.selectedAction) chosen.push(this.selectedAction);
    if (chosen.length) return chosen;
    return PILLS[this.pill]?.actions ?? [];
  }

  private modulesAsked(): string[] {
    if (this.selectedModule) return [this.selectedModule];
    return PILLS[this.pill]?.modules ?? [];
  }

  /** The query string shared by the list, the counts and the people. */
  private query(withActions = true): string {
    const parts: string[] = [];
    if (withActions) this.actionsAsked().forEach(a => parts.push(`action=${encodeURIComponent(a)}`));
    this.modulesAsked().forEach(m => parts.push(`module=${encodeURIComponent(m)}`));
    const outcome = PILLS[this.pill]?.outcome;
    if (outcome) parts.push(`outcome=${outcome}`);
    if (this.startDate) parts.push(`start_date=${this.startDate}`);
    if (this.endDate) parts.push(`end_date=${this.endDate}`);
    if (this.searchQuery.trim()) parts.push(`search=${encodeURIComponent(this.searchQuery.trim())}`);
    if (this.selectedUserId !== null && this.selectedUserId !== '') parts.push(`user_id=${encodeURIComponent(String(this.selectedUserId))}`);
    return parts.join('&');
  }

  fetchLogs() {
    this.destroy$.next();           // drop any request still in flight
    this.loading = true;
    this.cdr.detectChanges();

    const q = this.query();
    const path = `audit_logs/logs/?page=${this.currentPage}${q ? '&' + q : ''}`;

    this.api.get(path)
      .pipe(
        takeUntil(this.destroy$),
        finalize(() => { this.loading = false; this.cdr.detectChanges(); })
      )
      .subscribe({
        next: (data: any) => {
          const rows = Array.isArray(data) ? data : (data.results ?? []);
          this.logs = rows.map((row: any) => ({
            ...row,
            _actionClass: this.getActionClass(row),
            _payloadText: this.formatPayload(row.description),
            _scopeText: this.scopeText(row.ip_scope),
          }));
          this.totalCount = Array.isArray(data) ? rows.length : (data.count ?? 0);
          this.hasNext = !Array.isArray(data) && !!data.next;
          this.hasPrev = !Array.isArray(data) && !!data.previous;
        },
        error: () => {
          this.logs = [];
          this.totalCount = 0;
          this.hasNext = false;
          this.hasPrev = false;
        }
      });
  }

  /** The values worth offering as filters, for the current range. */
  fetchFacets() {
    const parts: string[] = [];
    if (this.startDate) parts.push(`start_date=${this.startDate}`);
    if (this.endDate) parts.push(`end_date=${this.endDate}`);
    if (this.selectedUserId !== null && this.selectedUserId !== '') parts.push(`user_id=${encodeURIComponent(String(this.selectedUserId))}`);

    this.api.get(`audit_logs/logs/facets/${parts.length ? '?' + parts.join('&') : ''}`)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: (data: any) => {
          this.availableActions = data?.actions ?? [];
          this.availableModules = data?.modules ?? [];
          this.failedCount = data?.outcomes?.failed ?? 0;
          this.facetTotal = data?.total ?? 0;
          this.cdr.detectChanges();
        },
        error: () => {
          this.availableActions = [];
          this.availableModules = [];
          this.cdr.detectChanges();
        },
      });
  }

  /** Everyone in the log, plus "Failed sign-ins" and "Not signed in" once each. */
  fetchLogUsers() {
    this.usersLoading = true;
    const parts: string[] = [];
    if (this.startDate) parts.push(`start_date=${this.startDate}`);
    if (this.endDate) parts.push(`end_date=${this.endDate}`);

    this.api.get(`audit_logs/logs/users/${parts.length ? '?' + parts.join('&') : ''}`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        finalize(() => { this.usersLoading = false; this.cdr.detectChanges(); })
      )
      .subscribe({
        next: (data: any) => { this.logUsers = data?.results ?? []; },
        error: () => { this.logUsers = []; }
      });
  }

  // ── The people list ─────────────────────────────────────────────────────
  selectUser(user: any) {
    if (this.selectedUserId === user.user_id) {
      this.clearUser();
      return;
    }
    this.selectedUserId = user.user_id;
    this.selectedUserName = user.name || user.email || '';
    this.currentPage = 1;
    this.fetchLogs();
    this.fetchFacets();
  }

  clearUser() {
    this.selectedUserId = null;
    this.selectedUserName = '';
    this.currentPage = 1;
    this.fetchLogs();
    this.fetchFacets();
  }

  get filteredLogUsers(): any[] {
    const needle = this.userSearch.trim().toLowerCase();
    if (!needle) return this.logUsers;
    return this.logUsers.filter(u => `${u.name ?? ''} ${u.email ?? ''} ${u.role ?? ''}`.toLowerCase().includes(needle));
  }

  personInitial(u: any): string {
    if (u.kind === 'failed_login') return '!';
    if (u.kind === 'anonymous') return '?';
    return (u.name || 'U').charAt(0).toUpperCase();
  }

  // ── Filters ─────────────────────────────────────────────────────────────
  setPill(name: string) {
    this.pill = name;
    this.selectedModule = '';
    this.selectedAction = '';
    this.selectedActions.clear();
    this.currentPage = 1;
    this.fetchLogs();
  }

  onModuleChange() {
    this.pill = 'all';
    this.currentPage = 1;
    this.fetchLogs();
  }

  toggleAction(action: string) {
    if (this.selectedActions.has(action)) this.selectedActions.delete(action);
    else this.selectedActions.add(action);
    this.selectedAction = '';
    this.currentPage = 1;
    this.fetchLogs();
  }

  isActionSelected(action: string): boolean { return this.selectedActions.has(action); }

  /** Apply: commit the dates and search, refetch from page 1. */
  onFilterChange() {
    this.startDate = this.pendingStartDate;
    this.endDate = this.pendingEndDate;
    this.currentPage = 1;
    this.fetchLogs();
    this.fetchFacets();
    this.fetchLogUsers();
  }

  clearFilters() {
    this.pill = 'all';
    this.selectedAction = '';
    this.selectedModule = '';
    this.startDate = '';
    this.endDate = '';
    this.pendingStartDate = '';
    this.pendingEndDate = '';
    this.searchQuery = '';
    this.currentPage = 1;
    this.selectedActions.clear();
    this.selectedUserId = null;
    this.selectedUserName = '';
    this.userSearch = '';
    this.fetchLogs();
    this.fetchFacets();
    this.fetchLogUsers();
  }

  get hasActiveFilters(): boolean {
    return !!(this.pill !== 'all' || this.selectedAction || this.selectedModule || this.startDate
              || this.endDate || this.searchQuery || this.selectedActions.size || this.selectedUserId);
  }

  selectedLog: any = null;
  selectLog(log: any) {
    this.selectedLog = (this.selectedLog?.id === log.id) ? null : log;
  }

  goNext() { if (this.hasNext && !this.loading) { this.currentPage++; this.fetchLogs(); } }
  goPrev() { if (this.hasPrev && !this.loading) { this.currentPage--; this.fetchLogs(); } }

  // ── Row presentation ────────────────────────────────────────────────────
  getActionClass(row: any): string {
    if (row.outcome && row.outcome !== 'success') return 'badge-red';
    const a = row.action;
    if (CRITICAL.has(a)) return 'badge-red';
    if (WARNING.has(a)) return 'badge-orange';
    if (a === 'CREATE') return 'badge-green';
    if (a === 'UPDATE') return 'badge-blue';
    if (a === 'CHECKOUT' || a === 'AUDIT') return 'badge-purple';
    if (a === 'PAYMENT' || a === 'MARK_PAID') return 'badge-teal';
    return 'badge-grey';
  }

  actionLabel(a: string): string { return ACTION_LABELS[a] || a; }
  moduleLabel(k: string) { return MODULE_LABELS[k] || k; }

  private scopeText(scope: string): string {
    if (scope === 'loopback') return 'this computer';
    if (scope === 'private') return 'salon network';
    if (scope === 'public') return 'internet';
    return '';
  }

  /** The recorded body, tidied, secrets removed, kept short. */
  private formatPayload(raw: string): string {
    if (!raw) return '';
    try {
      const obj = JSON.parse(raw);
      if (typeof obj !== 'object' || obj === null) return String(raw).slice(0, 400);
      const clean: any = Array.isArray(obj) ? obj : { ...obj };
      if (!Array.isArray(clean)) for (const k of ['password', 'pin', 'token', 'auth_token']) delete clean[k];
      const text = JSON.stringify(clean, null, 2);
      return text === '{}' ? '' : (text.length > 1200 ? text.slice(0, 1200) + '\n…' : text);
    } catch {
      return String(raw).slice(0, 400);
    }
  }

  trackById(index: number, item: any): any { return item?.id ?? index; }
  trackByValue(index: number, item: any): any { return item?.value ?? item?.user_id ?? index; }
}
