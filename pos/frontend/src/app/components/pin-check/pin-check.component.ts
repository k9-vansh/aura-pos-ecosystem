import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, EventEmitter, Input, Output, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';

/**
 * The till's PIN box.
 *
 * The client's card, advance or wallet is used only when the person at the till has the client's 4-digit PIN (sent to
 * the client when they bought it, shown in their app) or, if the client has forgotten it, the branch's master PIN for
 * the day (given by whoever holds the "Master PINs" tick). The server checks the PIN and answers with a short-lived
 * pass; the till sends the pass with the payment. The PIN is never shown here.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-pin-check',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="pc-overlay">
      <div class="pc-card" role="dialog" aria-modal="true">
        <div class="pc-head">
          <strong>{{ master ? 'Master PIN' : 'Client PIN' }}</strong>
          <button class="pc-x" (click)="closed.emit()" aria-label="Close">&times;</button>
        </div>
        <p class="pc-hint" *ngIf="!master">
          Ask <b>{{ clientName || 'the client' }}</b> for their 4-digit PIN. It was sent to them when they bought
          their card or advance, and it is in their app.
        </p>
        <p class="pc-hint" *ngIf="master">
          Enter today's master PIN of this branch. Whoever holds the Master PINs permission can give it. Its use is
          written down against your name.
        </p>

        <input class="pc-input" type="password" inputmode="numeric" maxlength="4" autocomplete="off" autofocus
          [(ngModel)]="pin" (keyup.enter)="verify()" placeholder="••••" aria-label="PIN">

        <p class="pc-error" *ngIf="error">{{ error }}</p>
        <p class="pc-ok" *ngIf="info">{{ info }}</p>

        <div class="pc-actions">
          <button class="pc-btn" [disabled]="busy || pin.length !== 4" (click)="verify()">{{ busy ? 'Checking…' : 'Confirm' }}</button>
        </div>
        <div class="pc-links">
          <button class="pc-link" (click)="toggleMaster()">{{ master ? 'Use the client PIN' : 'Client forgot it? Use the master PIN' }}</button>
          <button class="pc-link" *ngIf="!master" [disabled]="busy" (click)="send()">Send a new PIN to the client</button>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .pc-overlay { position: fixed; inset: 0; background: rgba(15,23,42,.45); display: flex; align-items: center;
      justify-content: center; z-index: 3100; }
    .pc-card { width: min(380px, calc(calc(100vw / var(--ui-zoom)) - 32px)); background: var(--surface, #fff); border-radius: 14px;
      padding: 20px; box-shadow: 0 20px 50px rgba(15,23,42,.3); font-family: inherit; }
    .pc-head { display: flex; align-items: center; justify-content: space-between; font-size: 16px; margin-bottom: 10px; }
    .pc-x { border: 0; background: none; font-size: 22px; line-height: 1; cursor: pointer; color: var(--text-secondary, #64748b); }
    .pc-hint { font-size: 12.5px; color: var(--text-secondary, #64748b); margin: 0 0 12px; line-height: 1.45; }
    .pc-input { width: 100%; text-align: center; font-size: 28px; letter-spacing: 12px; padding: 10px; border-radius: 10px;
      border: 1px solid var(--border-strong, #cbd5e1); background: var(--input-bg, #f8fafc); font-family: inherit; }
    .pc-error { color: #b91c1c; font-size: 12.5px; margin: 10px 0 0; font-weight: 600; }
    .pc-ok { color: #15803d; font-size: 12.5px; margin: 10px 0 0; font-weight: 600; }
    .pc-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
    .pc-btn { border: 0; border-radius: 8px; padding: 9px 20px; font-size: 13px; font-weight: 700; cursor: pointer; background: #0f172a; color: #fff; }
    .pc-btn:disabled { opacity: .45; cursor: default; }
    .pc-links { display: flex; flex-direction: column; gap: 6px; margin-top: 12px; }
    .pc-link { border: 0; background: none; padding: 2px 0; text-align: left; font-size: 12.5px; font-weight: 600; cursor: pointer;
      color: var(--blue, #2563eb); font-family: inherit; }
    .pc-link:disabled { opacity: .5; cursor: default; }
  `],
})
export class PinCheckComponent {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  @Input() clientId = 0;
  @Input() clientName = '';
  @Input() centerId = 0;
  /** The pass the server gave for a good PIN. */
  @Output() verified = new EventEmitter<string>();
  @Output() closed = new EventEmitter<void>();

  pin = '';
  master = false;
  busy = false;
  error = '';
  info = '';

  toggleMaster() {
    this.master = !this.master;
    this.pin = '';
    this.error = '';
    this.info = '';
  }

  private fail(err: any, fallback: string) {
    const body = err?.error || {};
    this.error = (Array.isArray(body.detail) ? body.detail[0] : body.detail) || fallback;
    if (typeof body.attempts_left === 'number') this.error += ` (${body.attempts_left} tries left)`;
    this.busy = false;
    this.cdr.markForCheck();
  }

  verify() {
    if (this.busy || this.pin.length !== 4) return;
    this.busy = true;
    this.error = '';
    this.info = '';
    this.api.post('clients/api/pin/verify/', { client: this.clientId, center: this.centerId, pin: this.pin, master: this.master })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.busy = false;
          this.verified.emit(res.token);
        },
        error: (err: any) => { this.pin = ''; this.fail(err, 'Could not check the PIN. Try again.'); },
      });
  }

  send() {
    if (this.busy) return;
    this.busy = true;
    this.error = '';
    this.info = '';
    this.api.post('clients/api/pin/send/', { client: this.clientId, center: this.centerId })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.busy = false;
          this.info = `A new PIN was sent to the client (${(res.sent_to || []).join(', ') || 'queued'}). The old one no longer works.`;
          this.cdr.markForCheck();
        },
        error: (err: any) => this.fail(err, 'Could not send a PIN.'),
      });
  }
}
