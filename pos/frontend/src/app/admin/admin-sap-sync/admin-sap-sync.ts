import { AdminFilterService } from '../admin-filter.service';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Subject, catchError, of, switchMap } from 'rxjs';
import { ApiService } from '../../services/api';
import { ToastService } from '../../services/toast.service';

/**
 * Admin > SAP Sync: every POS document on its way to SAP.
 *
 * Postings are background jobs (the outbox), so a bill never waits on SAP.
 * This page is where someone sees what SAP refused - in SAP's own words -
 * and either retries it after the SAP team fixes the master data, or marks it
 * as done by hand when the SAP team posted it themselves.
 */
type SyncState = 'attention' | 'retrying' | 'waiting' | 'done' | 'all';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-sap-sync',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-sap-sync.html',
  styleUrl: './admin-sap-sync.css',
})
export class AdminSapSyncComponent implements OnInit {
  private destroyRef = inject(DestroyRef);
  apiService = inject(ApiService);
  adminFilterService = inject(AdminFilterService);
  toastService = inject(ToastService);
  cdr = inject(ChangeDetectorRef);

  permissions: any = {};
  isOwner = false;

  state: SyncState = 'attention';
  search = '';
  page = 1;
  pages = 1;
  count = 0;
  rows: any[] = [];
  summary: any = {};
  isLoading = false;
  loadError = '';
  busyId: number | null = null;

  /** The row whose "mark as done" note is open. */
  resolvingId: number | null = null;
  resolveNote = '';

  readonly tabs: { key: SyncState; label: string }[] = [
    { key: 'attention', label: 'Needs attention' },
    { key: 'retrying', label: 'Retrying' },
    { key: 'waiting', label: 'Waiting' },
    { key: 'done', label: 'Posted' },
    { key: 'all', label: 'All' },
  ];

  private loads$ = new Subject<void>();

  get canRetry(): boolean {
    return this.isOwner || this.permissions?.admin?.sap_sync?.actions?.retry === true;
  }

  get canResolve(): boolean {
    return this.isOwner || this.permissions?.admin?.sap_sync?.actions?.resolve === true;
  }

  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
      } catch (e) {}
    }
    // switchMap: a newer load cancels a slower older one (branch changes).
    this.loads$.pipe(
      switchMap(() => {
        this.isLoading = true;
        this.loadError = '';
        this.cdr.markForCheck();
        return this.apiService.get('sap-sync/api/', {
          status: this.state,
          center_id: this.adminFilterService.currentCenterId,
          q: this.search.trim(),
          page: this.page,
        }).pipe(catchError((err) => {
          this.loadError = err?.error?.detail || 'Could not load the SAP postings.';
          return of(null);
        }));
      }),
      takeUntilDestroyed(this.destroyRef),
    ).subscribe((res) => {
      this.isLoading = false;
      if (res) {
        this.rows = res.results || [];
        this.summary = res.summary || {};
        this.count = res.count || 0;
        this.page = res.page || 1;
        this.pages = res.pages || 1;
      }
      this.cdr.markForCheck();
    });
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.page = 1;
      this.load();
    });
    this.load();
  }

  load() {
    this.loads$.next();
  }

  setState(state: SyncState) {
    this.state = state;
    this.page = 1;
    this.resolvingId = null;
    this.load();
  }

  onSearch() {
    this.page = 1;
    this.load();
  }

  goTo(page: number) {
    if (page < 1 || page > this.pages) return;
    this.page = page;
    this.load();
  }

  stateLabel(state: string): string {
    return ({
      attention: 'Needs attention', retrying: 'Retrying', waiting: 'Waiting', done: 'Posted',
    } as Record<string, string>)[state] || state;
  }

  retry(row: any) {
    if (this.busyId) return;
    this.busyId = row.id;
    this.apiService.post(`sap-sync/api/${row.id}/retry/`, {})
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.busyId = null;
          this.toastService.showSuccess('Queued again - it posts within a minute');
          this.load();
        },
        error: (err) => {
          this.busyId = null;
          this.toastService.showError(err?.error?.detail || 'Could not retry this posting.');
          this.cdr.markForCheck();
        },
      });
  }

  openResolve(row: any) {
    this.resolvingId = this.resolvingId === row.id ? null : row.id;
    this.resolveNote = '';
  }

  resolve(row: any) {
    const note = this.resolveNote.trim();
    if (!note || this.busyId) return;
    this.busyId = row.id;
    this.apiService.post(`sap-sync/api/${row.id}/resolve/`, { note })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.busyId = null;
          this.resolvingId = null;
          this.toastService.showSuccess('Marked as done');
          this.load();
        },
        error: (err) => {
          this.busyId = null;
          this.toastService.showError(err?.error?.detail || err?.error?.note || 'Could not mark it as done.');
          this.cdr.markForCheck();
        },
      });
  }

  trackById(index: number, item: any): any {
    return item?.id ?? item?.key ?? index;
  }
}
