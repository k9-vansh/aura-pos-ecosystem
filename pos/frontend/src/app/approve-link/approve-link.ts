import { ChangeDetectionStrategy, ChangeDetectorRef, Component, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { ActivatedRoute } from '@angular/router';
import { environment } from '../../environments/environment';

/**
 * The page behind the Approve / Reject link in an approver's e-mail or WhatsApp
 * message. No sign-in: the single-use link is the credential (D11).
 *
 * Opening the link only shows the request - mail scanners open links - and a
 * button press decides. The link works once, for the time set in Settings
 * (minutes for a till approval, days for a stock request).
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-approve-link',
  standalone: true,
  imports: [CommonModule],
  template: `
    <div class="wrap">
      <div class="card">
        <div class="brand">Approval request</div>

        <ng-container *ngIf="state === 'loading'"><p class="muted">Opening the request...</p></ng-container>

        <ng-container *ngIf="state === 'ready'">
          <h1>{{ info.requested_by }} at {{ info.branch }}</h1>
          <p class="what">{{ info.summary }}</p>
          <p class="muted" *ngIf="info.reason">Reason: {{ info.reason }}</p>
          <p class="muted">Asked {{ info.created_at | date:'dd MMM, HH:mm' }}</p>
          <div class="actions">
            <button class="btn approve" [disabled]="busy" (click)="decide('approve')">Approve</button>
            <button class="btn reject" [disabled]="busy" (click)="decide('reject')">Reject</button>
          </div>
        </ng-container>

        <ng-container *ngIf="state === 'done'">
          <h1>{{ decision === 'approve' ? 'Approved' : 'Rejected' }}</h1>
          <p class="muted">{{ info?.summary }}</p>
          <p class="muted">The person who asked has been told. You can close this page.</p>
        </ng-container>

        <ng-container *ngIf="state === 'error'">
          <h1>This link cannot be used</h1>
          <p class="muted">{{ message }}</p>
          <p class="muted">Open the POS and use the bell to decide.</p>
        </ng-container>
      </div>
    </div>
  `,
  styles: [`
    .wrap { min-height: calc(100vh / var(--ui-zoom)); display: flex; align-items: center; justify-content: center; padding: 16px;
      background: var(--bg-2, #f1f5f9); font-family: inherit; }
    .card { width: min(460px, 100%); background: var(--surface, #fff); border-radius: 16px; padding: 28px;
      box-shadow: 0 20px 50px rgba(15,23,42,.12); }
    .brand { font-size: 11px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase;
      color: var(--text-secondary, #64748b); margin-bottom: 12px; }
    h1 { font-size: 19px; margin: 0 0 8px; color: var(--text-primary, #0f172a); }
    .what { font-size: 15px; margin: 6px 0 10px; color: var(--text-primary, #0f172a); line-height: 1.45; }
    .muted { font-size: 13px; color: var(--text-secondary, #64748b); margin: 4px 0; line-height: 1.45; }
    .actions { display: flex; gap: 10px; margin-top: 22px; }
    .btn { flex: 1; border: 0; border-radius: 10px; padding: 12px 0; font-size: 14px; font-weight: 700; cursor: pointer; }
    .btn.approve { background: #0f172a; color: #fff; }
    .btn.reject { background: var(--bg-2, #f1f5f9); color: var(--text-primary, #0f172a); border: 1px solid var(--border, #e2e8f0); }
    .btn:disabled { opacity: .5; cursor: default; }
  `],
})
export class ApproveLinkComponent implements OnInit {
  private http = inject(HttpClient);
  private route = inject(ActivatedRoute);
  private cdr = inject(ChangeDetectorRef);

  state: 'loading' | 'ready' | 'done' | 'error' = 'loading';
  info: any = null;
  message = '';
  decision = '';
  busy = false;
  private token = '';

  ngOnInit() {
    this.token = this.route.snapshot.paramMap.get('token') || '';
    this.http.get(`${environment.apiUrl}/approvals/api/link/${encodeURIComponent(this.token)}/`).subscribe({
      next: (res: any) => { this.info = res; this.state = 'ready'; this.cdr.markForCheck(); },
      error: (err) => this.fail(err),
    });
  }

  decide(action: 'approve' | 'reject') {
    if (this.busy) return;
    this.busy = true;
    this.http.post(`${environment.apiUrl}/approvals/api/link/${encodeURIComponent(this.token)}/`, { action }).subscribe({
      next: () => { this.busy = false; this.decision = action; this.state = 'done'; this.cdr.markForCheck(); },
      error: (err) => { this.busy = false; this.fail(err); },
    });
  }

  private fail(err: any) {
    const detail = err?.error?.detail;
    this.message = (Array.isArray(detail) ? detail[0] : detail) || 'The link is not valid.';
    this.state = 'error';
    this.cdr.markForCheck();
  }
}
