import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { ToastService } from '../../services/toast.service';

/**
 * Admin > Approvals: which things need an approver, who the approvers are for
 * each branch, and the history of every request (F10-F13).
 *
 * The free limits ("a discount up to X% needs no approval") are values in
 * Admin > Settings > Approvals; this page links to them rather than copying them.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-approvals',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-approvals.html',
  styleUrls: ['../admin-sap-sync/admin-sap-sync.css', './admin-approvals.css'],
})
export class AdminApprovalsComponent implements OnInit {
  private destroyRef = inject(DestroyRef);
  private api = inject(ApiService);
  private toast = inject(ToastService);
  private cdr = inject(ChangeDetectorRef);

  permissions: any = {};
  isOwner = false;

  tab: 'rules' | 'history' = 'rules';

  // Rules
  policies: any[] = [];
  users: any[] = [];
  centers: any[] = [];
  loadError = '';
  isLoading = false;
  savingKind = '';
  /** The two till controls that, when off, let a bill go through at any price or discount. */
  get uncontrolledTillKinds(): string[] {
    return this.policies
      .filter(p => (p.kind === 'discount' || p.kind === 'price_change') && !p.enabled)
      .map(p => (p.label || p.kind).toLowerCase());
  }

  /** What is typed in each policy's "add approver" row. */
  draft: Record<string, { user_id: number | null; center_id: number | 'all' }> = {};

  // History
  rows: any[] = [];
  count = 0;
  page = 1;
  statusFilter = '';
  kindFilter = '';

  get canEdit(): boolean {
    return this.isOwner || this.permissions?.admin?.approvals?.update === true;
  }

  ngOnInit() {
    try {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      this.permissions = user.permissions || {};
      this.isOwner = user.is_superuser === true;
    } catch { /* an unreadable session shows read-only */ }
    this.loadPolicies();
  }

  setTab(tab: 'rules' | 'history') {
    this.tab = tab;
    if (tab === 'history') this.loadHistory();
  }

  loadPolicies() {
    this.isLoading = true;
    this.api.get('approvals/api/policies/').pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.policies = (res.policies || []).filter((p: any) => p.available);
        this.users = res.users || [];
        this.centers = res.centers || [];
        for (const p of this.policies) {
          this.draft[p.kind] = this.draft[p.kind] || { user_id: null, center_id: 'all' };
        }
        this.isLoading = false;
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.isLoading = false;
        this.loadError = err?.error?.detail || 'Could not load the approval rules.';
        this.cdr.markForCheck();
      },
    });
  }

  userName(id: number): string {
    const u = this.users.find(x => x.id === id);
    return u ? (u.full_name || u.email) : `User ${id}`;
  }

  centerName(id: number | null): string {
    if (id === null || id === undefined) return 'All branches';
    const c = this.centers.find(x => x.id === id);
    return c ? c.name : `Branch ${id}`;
  }

  addApprover(policy: any) {
    const d = this.draft[policy.kind];
    if (!d?.user_id) return;
    const centerId = d.center_id === 'all' ? null : Number(d.center_id);
    const exists = policy.approvers.some((a: any) => a.user_id === Number(d.user_id) && a.center_id === centerId);
    if (!exists) policy.approvers = [...policy.approvers, { user_id: Number(d.user_id), center_id: centerId }];
    d.user_id = null;
    this.cdr.markForCheck();
  }

  removeApprover(policy: any, row: any) {
    policy.approvers = policy.approvers.filter((a: any) => a !== row);
    this.cdr.markForCheck();
  }

  save(policy: any) {
    if (this.savingKind) return;
    this.savingKind = policy.kind;
    this.api.put(`approvals/api/policies/${policy.kind}/`, {
      enabled: policy.enabled, approvers: policy.approvers,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.savingKind = '';
        this.toast.showSuccess('Saved');
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.savingKind = '';
        const e = err?.error || {};
        this.toast.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || e.approvers || 'Could not save.');
        this.cdr.markForCheck();
      },
    });
  }

  loadHistory() {
    this.isLoading = true;
    this.api.get('approvals/api/history/', {
      status: this.statusFilter, kind: this.kindFilter, page: this.page,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.rows = res.results || [];
        this.count = res.count || 0;
        this.isLoading = false;
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.isLoading = false;
        this.loadError = err?.error?.detail || 'Could not load the history.';
        this.cdr.markForCheck();
      },
    });
  }

  filterChanged() {
    this.page = 1;
    this.loadHistory();
  }

  get pages(): number {
    return Math.max(Math.ceil(this.count / 40), 1);
  }

  goTo(page: number) {
    if (page < 1 || page > this.pages) return;
    this.page = page;
    this.loadHistory();
  }

  trackByIndex(i: number) { return i; }
  trackById(_: number, item: any) { return item?.id ?? item?.kind; }
}
