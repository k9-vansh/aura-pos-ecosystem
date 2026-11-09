import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';

/**
 * Admin > Master PINs.
 *
 * Each branch has a master PIN that changes every day. When a client has forgotten their own PIN, the manager rings
 * the person who holds this page, who reads the branch's PIN out. Whoever uses it at the till is recorded.
 * Only roles with the "Master PINs" tick can open this page.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-master-pins',
  standalone: true,
  imports: [CommonModule, FormsModule],
  template: `
    <div class="mp-wrap">
      <div class="mp-top">
        <div>
          <h3>Master PINs of {{ date | date: 'dd MMM yyyy' }}</h3>
          <p>They change every day. Give one only to a manager who has a client at the till who cannot remember their own PIN.</p>
        </div>
        <input class="mp-search" placeholder="Search a branch" [(ngModel)]="query">
      </div>
      <p class="mp-error" *ngIf="error">{{ error }}</p>
      <p class="mp-empty" *ngIf="!error && loaded && !shown.length">No branch matches.</p>
      <div class="mp-grid">
        <div class="mp-card" *ngFor="let p of shown; trackBy: trackById">
          <span class="mp-name">{{ p.center }}</span>
          <span class="mp-pin" (click)="reveal[p.center_id] = !reveal[p.center_id]" title="Click to show or hide">
            {{ reveal[p.center_id] ? p.pin : '••••' }}
          </span>
        </div>
      </div>
    </div>
  `,
  styles: [`
    .mp-wrap { padding: 16px 4px; }
    .mp-top { display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 14px; }
    .mp-top h3 { margin: 0 0 4px; font-size: 16px; }
    .mp-top p { margin: 0; font-size: 12.5px; color: var(--text-secondary, #64748b); max-width: 520px; }
    .mp-search { padding: 8px 12px; border-radius: 8px; border: 1px solid var(--border-strong, #cbd5e1); min-width: 220px; font-family: inherit; }
    .mp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 10px; }
    .mp-card { display: flex; align-items: center; justify-content: space-between; gap: 10px; padding: 12px 14px;
      background: var(--surface, #fff); border: 1px solid var(--border, #e2e8f0); border-radius: 10px; }
    .mp-name { font-weight: 600; font-size: 13px; }
    .mp-pin { font-size: 20px; font-weight: 800; letter-spacing: 4px; cursor: pointer; user-select: none; }
    .mp-error { color: #b91c1c; font-weight: 600; }
    .mp-empty { color: var(--text-secondary, #64748b); }
  `],
})
export class AdminMasterPinsComponent implements OnInit {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  pins: { center_id: number; center: string; pin: string }[] = [];
  date = '';
  query = '';
  error = '';
  loaded = false;
  reveal: Record<number, boolean> = {};

  get shown() {
    const q = this.query.trim().toLowerCase();
    return q ? this.pins.filter((p) => p.center.toLowerCase().includes(q)) : this.pins;
  }

  trackById(_: number, p: { center_id: number }) { return p.center_id; }

  ngOnInit() {
    this.api.get('clients/api/master-pins/').pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.pins = res?.pins || [];
        this.date = res?.date || '';
        this.loaded = true;
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.error = err?.error?.detail || 'Could not load the master PINs.';
        this.loaded = true;
        this.cdr.markForCheck();
      },
    });
  }
}
