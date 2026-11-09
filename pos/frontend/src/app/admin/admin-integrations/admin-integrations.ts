import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { ToastService } from '../../services/toast.service';

/**
 * Admin > Integrations: the company mailbox and WhatsApp, who is told about
 * what, and a log of every message sent (F14, D5, D6, B5.13).
 *
 * Secrets are never sent back to the browser: a saved secret shows as a mask,
 * and leaving the box alone keeps it. WhatsApp ships empty, so nothing is sent
 * (or charged) until the owner fills it in.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-integrations',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-integrations.html',
  styleUrls: ['../admin-sap-sync/admin-sap-sync.css', '../admin-approvals/admin-approvals.css', './admin-integrations.css'],
})
export class AdminIntegrationsComponent implements OnInit {
  private destroyRef = inject(DestroyRef);
  private api = inject(ApiService);
  private toast = inject(ToastService);
  private cdr = inject(ChangeDetectorRef);

  permissions: any = {};
  isOwner = false;
  tab: 'services' | 'notifications' | 'messages' = 'services';
  loadError = '';
  isLoading = false;

  services: any[] = [];
  /** What is typed in each service's fields, by service key. */
  forms: Record<string, Record<string, string>> = {};
  testTo: Record<string, string> = {};
  busy = '';

  events: any[] = [];
  clientEvents: any[] = [];
  roles: any[] = [];
  users: any[] = [];

  messages: any[] = [];
  counts: any = {};
  msgStatus = '';
  msgChannel = '';

  get canEdit(): boolean {
    return this.isOwner || this.permissions?.admin?.integrations?.update === true;
  }
  get canTest(): boolean {
    return this.isOwner || this.permissions?.admin?.integrations?.actions?.send_test === true;
  }

  ngOnInit() {
    try {
      const user = JSON.parse(localStorage.getItem('user') || '{}');
      this.permissions = user.permissions || {};
      this.isOwner = user.is_superuser === true;
    } catch { /* read-only if the session is unreadable */ }
    this.loadServices();
  }

  setTab(tab: 'services' | 'notifications' | 'messages') {
    this.tab = tab;
    if (tab === 'services') this.loadServices();
    if (tab === 'notifications') this.loadRules();
    if (tab === 'messages') this.loadMessages();
  }

  private fail(err: any, fallback: string) {
    const e = err?.error || {};
    this.loadError = (Array.isArray(e.detail) ? e.detail[0] : e.detail) || fallback;
    this.isLoading = false;
    this.cdr.markForCheck();
  }

  // ─── Services ──────────────────────────────────────────────────────────
  loadServices() {
    this.isLoading = true;
    this.loadError = '';
    this.api.get('notifications/api/integrations/').pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.services = res || [];
        for (const s of this.services) this.resetForm(s);
        this.isLoading = false;
        this.cdr.markForCheck();
      },
      error: (err: any) => this.fail(err, 'Could not load the integrations.'),
    });
  }

  private resetForm(service: any) {
    this.forms[service.key] = Object.fromEntries(service.fields.map((f: any) => [f.name, f.value || '']));
  }

  saveService(service: any) {
    if (this.busy) return;
    this.busy = service.key;
    this.api.put(`notifications/api/integrations/${service.key}/`, {
      enabled: service.enabled, values: this.forms[service.key],
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.busy = '';
        Object.assign(service, res);
        this.resetForm(service);
        this.toast.showSuccess('Saved');
        this.cdr.markForCheck();
      },
      error: (err: any) => {
        this.busy = '';
        const e = err?.error || {};
        this.toast.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || 'Could not save.');
        this.cdr.markForCheck();
      },
    });
  }

  testService(service: any) {
    const to = (this.testTo[service.key] || '').trim();
    if (!to || this.busy) return;
    this.busy = 'test-' + service.key;
    this.api.post(`notifications/api/integrations/${service.key}/test/`, { to })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.busy = '';
          service.last_test_ok = true;
          service.last_test_message = res?.message || 'Test message sent.';
          service.last_test_at = new Date().toISOString();
          this.cdr.markForCheck();
        },
        error: (err: any) => {
          this.busy = '';
          service.last_test_ok = false;
          service.last_test_message = err?.error?.message || err?.error?.detail || 'The test failed.';
          service.last_test_at = new Date().toISOString();
          this.cdr.markForCheck();
        },
      });
  }

  // ─── Notifications ─────────────────────────────────────────────────────
  loadRules() {
    this.isLoading = true;
    this.loadError = '';
    this.api.get('notifications/api/rules/').pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.events = res.events || [];
        this.clientEvents = res.client_events || [];
        this.roles = res.roles || [];
        this.users = res.users || [];
        this.isLoading = false;
        this.cdr.markForCheck();
      },
      error: (err: any) => this.fail(err, 'Could not load the notification rules.'),
    });
  }

  saveRule(ev: any) {
    this.api.put('notifications/api/rules/', {
      event: ev.event, enabled: ev.enabled, bell: ev.bell, email: ev.email, whatsapp: ev.whatsapp,
      role_ids: ev.role_ids, user_ids: ev.user_ids,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.toast.showSuccess('Saved'); },
      error: (err: any) => {
        this.toast.showError(err?.error?.detail || 'Could not save.');
        this.loadRules();
      },
    });
  }

  toggleId(ev: any, field: 'role_ids' | 'user_ids', id: number) {
    const list: number[] = ev[field] || [];
    ev[field] = list.includes(id) ? list.filter(x => x !== id) : [...list, id];
    this.saveRule(ev);
  }

  has(ev: any, field: 'role_ids' | 'user_ids', id: number): boolean {
    return (ev[field] || []).includes(id);
  }

  // ─── Messages ──────────────────────────────────────────────────────────
  loadMessages() {
    this.isLoading = true;
    this.loadError = '';
    this.api.get('notifications/api/deliveries/', { status: this.msgStatus, channel: this.msgChannel })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.messages = res.results || [];
          this.counts = res.counts || {};
          this.isLoading = false;
          this.cdr.markForCheck();
        },
        error: (err: any) => this.fail(err, 'Could not load the message log.'),
      });
  }

  trackByKey(_: number, item: any) { return item?.key ?? item?.event ?? item?.id ?? item?.name; }
  trackById(_: number, item: any) { return item?.id; }
}
