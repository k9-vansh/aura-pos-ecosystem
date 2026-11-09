import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, EventEmitter, Input, OnInit, Output, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../services/api';

/**
 * The till's "waiting for approval" card (F12).
 *
 * The cashier asked; the approvers were told in the POS, by e-mail and by
 * WhatsApp. This watches the requests - live over the user's socket, with a
 * 5-second poll behind it - and tells the till when every one is approved
 * (so it can finish the bill) or one is rejected / lapses (so the cashier can
 * change the bill or ask again).
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-approval-wait',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="aw-overlay">
      <div class="aw-card">
        <div class="aw-head">
          <span class="aw-dot" [class.done]="allApproved" [class.bad]="anyClosed"></span>
          <strong>{{ allApproved ? 'Approved' : anyClosed ? 'Not approved' : 'Waiting for approval' }}</strong>
        </div>

        <div class="aw-row" *ngFor="let r of requests; trackBy: trackById">
          <div class="aw-summary">{{ r.summary }}</div>
          <span class="aw-status" [ngClass]="'s-' + r.status">{{ label(r) }}</span>
        </div>

        <p class="aw-hint" *ngIf="!allApproved && !anyClosed">
          The approvers have been told in the POS, by e-mail and on WhatsApp. This screen finishes the bill as
          soon as one of them says yes.
        </p>
        <p class="aw-hint" *ngIf="anyClosed">
          Change the bill (for example a smaller discount) or ask again.
        </p>

        <div class="aw-actions">
          <button class="aw-btn ghost" (click)="close()">{{ anyClosed || allApproved ? 'Close' : 'Cancel request' }}</button>
          <button class="aw-btn" *ngIf="anyClosed" (click)="askAgain.emit()">Ask again</button>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .aw-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: flex;
      align-items: center; justify-content: center; z-index: 3000; animation: aw-fade 140ms ease; }
    @keyframes aw-fade { from { opacity: 0; } to { opacity: 1; } }
    .aw-card { width: min(440px, calc(calc(100vw / var(--ui-zoom)) - 32px)); background: var(--surface, #fff); border-radius: 14px;
      padding: 20px; box-shadow: 0 20px 50px rgba(15,23,42,.3); font-family: inherit; }
    .aw-head { display: flex; align-items: center; gap: 10px; font-size: 15px; margin-bottom: 14px; }
    .aw-dot { width: 10px; height: 10px; border-radius: 50%; background: #f59e0b; animation: aw-pulse 1.2s infinite; }
    .aw-dot.done { background: #16a34a; animation: none; }
    .aw-dot.bad { background: #dc2626; animation: none; }
    @keyframes aw-pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }
    .aw-row { display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 10px 0; border-top: 1px solid var(--border, #e2e8f0); }
    .aw-summary { font-size: 13px; color: var(--text-primary, #0f172a); }
    .aw-status { font-size: 11px; font-weight: 700; padding: 3px 9px; border-radius: 999px; white-space: nowrap;
      background: #fef3c7; color: #92400e; }
    .aw-status.s-approved { background: #dcfce7; color: #166534; }
    .aw-status.s-rejected, .aw-status.s-expired, .aw-status.s-cancelled { background: #fee2e2; color: #991b1b; }
    .aw-hint { font-size: 12px; color: var(--text-secondary, #64748b); margin: 10px 0 0; line-height: 1.45; }
    .aw-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
    .aw-btn { border: 0; border-radius: 8px; padding: 8px 16px; font-size: 13px; font-weight: 600; cursor: pointer;
      background: #0f172a; color: #fff; }
    .aw-btn.ghost { background: var(--bg-2, #f1f5f9); color: var(--text-primary, #0f172a); }
  `],
})
export class ApprovalWaitComponent implements OnInit {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  /** The requests the server opened (as returned by `request_approval`). */
  @Input() requests: any[] = [];
  @Output() approved = new EventEmitter<number[]>();
  @Output() closed = new EventEmitter<number[]>();
  @Output() askAgain = new EventEmitter<void>();

  private told = false;

  get allApproved() { return this.requests.length > 0 && this.requests.every(r => r.status === 'approved'); }
  get anyClosed() { return this.requests.some(r => ['rejected', 'expired', 'cancelled'].includes(r.status)); }

  ngOnInit() {
    this.api.userEvents$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((m: any) => {
      if (m?.type === 'approval_update') this.poll();
    });
    const timer = setInterval(() => this.poll(), 5000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  poll() {
    if (this.allApproved || this.anyClosed) return;
    for (const r of this.requests) {
      if (r.status !== 'pending') continue;
      this.api.get(`approvals/api/requests/${r.id}/`, {}, { headers: { 'X-Background-Request': '1' } })
        .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
          next: (fresh: any) => {
            r.status = fresh.status;
            r.decided_by_name = fresh.decided_by_name;
            this.cdr.markForCheck();
            if (this.allApproved && !this.told) {
              this.told = true;
              this.approved.emit(this.requests.map(x => x.id));
            }
          },
          error: () => { /* keep waiting; the next poll tries again */ },
        });
    }
  }

  label(r: any): string {
    if (r.status === 'approved') return r.decided_by_name ? `Approved by ${r.decided_by_name}` : 'Approved';
    if (r.status === 'rejected') return r.decided_by_name ? `Rejected by ${r.decided_by_name}` : 'Rejected';
    if (r.status === 'expired') return 'No answer - lapsed';
    if (r.status === 'cancelled') return 'Withdrawn';
    return 'Waiting';
  }

  close() {
    const open = this.requests.filter(r => r.status === 'pending');
    for (const r of open) {
      this.api.post(`approvals/api/requests/${r.id}/cancel/`, {}).pipe(takeUntilDestroyed(this.destroyRef)).subscribe();
    }
    this.closed.emit(this.requests.filter(r => r.status === 'approved').map(r => r.id));
  }

  trackById(_: number, r: any) { return r.id; }
}
