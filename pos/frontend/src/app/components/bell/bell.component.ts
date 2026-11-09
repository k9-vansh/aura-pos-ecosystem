import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, ElementRef, HostListener, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { ApiService } from '../../services/api';
import { ToastService } from '../../services/toast.service';

/**
 * The notification bell (B5.13): the user's notifications, and - for an
 * approver - the approvals waiting for them with Approve / Reject.
 *
 * New items arrive live over the user's own socket; a slow poll covers a
 * dropped socket. Approving here is the same decision as the e-mail link: the
 * first one wins.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-bell',
  standalone: true,
  imports: [CommonModule],
  template: `
    <button class="bell-btn" title="Notifications" (click)="toggle($event)" [class.has-new]="badge > 0">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
        stroke-linecap="round" stroke-linejoin="round">
        <path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0" />
      </svg>
      <span class="bell-badge" *ngIf="badge > 0">{{ badge > 99 ? '99+' : badge }}</span>
    </button>

    <div class="bell-panel" *ngIf="open" (click)="$event.stopPropagation()">
      <div class="bell-head">
        <strong>Notifications</strong>
        <button class="link-btn" *ngIf="unread > 0" (click)="markAll()">Mark all read</button>
      </div>

      <div class="bell-section" *ngIf="pending.length">
        <div class="bell-label">Waiting for your decision</div>
        <div class="bell-item approval" *ngFor="let a of pending; trackBy: trackById">
          <div class="bell-title">{{ a.requested_by_name }} · {{ a.center_name }}</div>
          <div class="bell-body">{{ a.summary }}</div>
          <div class="bell-meta">{{ a.created_at | date:'dd-MM, HH:mm' }}</div>
          <div class="bell-actions">
            <button class="act approve" [disabled]="busyId === a.id" (click)="decide(a, 'approve')">Approve</button>
            <button class="act reject" [disabled]="busyId === a.id" (click)="decide(a, 'reject')">Reject</button>
          </div>
        </div>
      </div>

      <div class="bell-section">
        <div class="bell-label" *ngIf="pending.length && items.length">Earlier</div>
        <div class="bell-item" *ngFor="let n of items; trackBy: trackById" [class.unread]="!n.read"
          (click)="read(n)">
          <div class="bell-title">{{ n.title }}</div>
          <div class="bell-body">{{ n.body }}</div>
          <div class="bell-meta">{{ n.created_at | date:'dd-MM, HH:mm' }}</div>
        </div>
        <div class="bell-empty" *ngIf="!items.length && !pending.length">You're all caught up.</div>
      </div>
    </div>
  `,
  styles: [`
    :host { position: relative; display: inline-block; }
    .bell-btn { display: flex; align-items: center; justify-content: center; width: 32px; height: 32px;
      border: 1px solid var(--border); background: var(--surface); border-radius: 8px; cursor: pointer;
      color: var(--text-secondary); transition: all 150ms ease; position: relative; box-shadow: var(--shadow-xs); }
    .bell-btn:hover { background: var(--bg-2); color: var(--text-primary); }
    .bell-btn.has-new svg { animation: bell-ring 1.2s ease 1; transform-origin: 50% 0; }
    @keyframes bell-ring { 0%,100% { transform: rotate(0); } 20% { transform: rotate(14deg); }
      40% { transform: rotate(-12deg); } 60% { transform: rotate(8deg); } 80% { transform: rotate(-4deg); } }
    .bell-badge { position: absolute; top: -4px; right: -4px; background: var(--red, #ef4444); color: #fff;
      font-size: 10px; font-weight: 800; min-width: 18px; height: 18px; border-radius: 9px; display: flex;
      align-items: center; justify-content: center; padding: 0 4px; }
    .bell-panel { position: absolute; right: 0; top: 40px; width: 360px; max-height: calc(70vh / var(--ui-zoom)); overflow-y: auto;
      background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
      box-shadow: 0 12px 32px rgba(15,23,42,.18); z-index: 1000; animation: bell-in 140ms ease; }
    @keyframes bell-in { from { opacity: 0; transform: translateY(-4px); } to { opacity: 1; transform: none; } }
    .bell-head { display: flex; align-items: center; justify-content: space-between; padding: 12px 14px;
      border-bottom: 1px solid var(--border); font-size: 13px; }
    .link-btn { background: none; border: 0; color: var(--text-secondary); font-size: 12px; cursor: pointer; }
    .link-btn:hover { color: var(--text-primary); text-decoration: underline; }
    .bell-label { padding: 8px 14px 4px; font-size: 10px; font-weight: 700; letter-spacing: .05em;
      text-transform: uppercase; color: var(--text-secondary); }
    .bell-item { padding: 10px 14px; border-bottom: 1px solid var(--border); cursor: default; }
    .bell-item.unread { background: color-mix(in srgb, var(--surface) 92%, #3b82f6); cursor: pointer; }
    .bell-item.approval { background: color-mix(in srgb, var(--surface) 92%, #f59e0b); }
    .bell-title { font-size: 12.5px; font-weight: 600; color: var(--text-primary); }
    .bell-body { font-size: 12px; color: var(--text-secondary); margin-top: 2px; line-height: 1.35; }
    .bell-meta { font-size: 10.5px; color: var(--text-secondary); margin-top: 4px; opacity: .8; }
    .bell-actions { display: flex; gap: 8px; margin-top: 8px; }
    .act { flex: 1; border: 0; border-radius: 8px; padding: 6px 0; font-size: 12px; font-weight: 600; cursor: pointer; }
    .act.approve { background: #0f172a; color: #fff; }
    .act.reject { background: var(--bg-2, #f1f5f9); color: var(--text-primary); border: 1px solid var(--border); }
    .act:disabled { opacity: .5; cursor: default; }
    .bell-empty { padding: 28px 14px; text-align: center; font-size: 12.5px; color: var(--text-secondary); }
    @media (max-width: 384px) { .bell-panel { position: fixed; left: 8px; right: 8px; width: auto; top: 56px; } }
  `],
})
export class BellComponent implements OnInit {
  private api = inject(ApiService);
  private toast = inject(ToastService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);
  private host = inject(ElementRef);

  open = false;
  items: any[] = [];
  pending: any[] = [];
  unread = 0;
  busyId: number | null = null;

  get badge(): number {
    return this.unread + this.pending.length;
  }

  ngOnInit() {
    this.refresh();
    // Live: a new notification or an approval decision arrives on the user's socket.
    this.api.userEvents$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe((msg: any) => {
      if (msg?.type === 'notification' && msg.notification) {
        this.toast.show(msg.notification.title, 'info');
      }
      this.refresh();
    });
    // A dropped socket must not leave the bell stale.
    const timer = setInterval(() => this.refresh(), 60000);
    this.destroyRef.onDestroy(() => clearInterval(timer));
  }

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event) {
    if (this.open && !this.host.nativeElement.contains(event.target)) {
      this.open = false;
      this.cdr.markForCheck();
    }
  }

  toggle(event: Event) {
    event.stopPropagation();
    this.open = !this.open;
    if (this.open) this.refresh();
    this.cdr.markForCheck();
  }

  refresh() {
    const quiet = { headers: { 'X-Background-Request': '1' } };
    this.api.get('notifications/api/bell/', {}, quiet).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.items = res?.results || [];
        this.unread = res?.unread || 0;
        this.cdr.markForCheck();
      },
      error: () => { /* a signed-out or offline bell stays quiet */ },
    });
    this.api.get('approvals/api/inbox/', {}, quiet).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.pending = res?.pending || [];
        this.cdr.markForCheck();
      },
      error: () => { /* not an approver, or offline */ },
    });
  }

  read(n: any) {
    if (n.read) return;
    n.read = true;
    this.unread = Math.max(this.unread - 1, 0);
    this.api.post('notifications/api/bell/read/', { ids: [n.id] }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe();
  }

  markAll() {
    this.api.post('notifications/api/bell/read/', { all: true }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.items.forEach(n => (n.read = true));
      this.unread = 0;
      this.cdr.markForCheck();
    });
  }

  decide(a: any, action: 'approve' | 'reject') {
    if (this.busyId) return;
    this.busyId = a.id;
    this.api.post(`approvals/api/requests/${a.id}/decide/`, { action }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.busyId = null;
        this.toast.showSuccess(action === 'approve' ? 'Approved' : 'Rejected');
        this.refresh();
      },
      error: (err: any) => {
        this.busyId = null;
        const e = err?.error || {};
        const detail = Array.isArray(e.detail) ? e.detail[0] : e.detail;
        this.toast.showError(detail || 'Could not record that decision.');
        this.refresh();
      },
    });
  }

  trackById(_: number, item: any) {
    return item?.id;
  }
}
