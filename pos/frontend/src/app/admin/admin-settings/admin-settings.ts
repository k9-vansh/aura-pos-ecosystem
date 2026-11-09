import { AdminFilterService } from '../admin-filter.service';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { ToastService } from '../../services/toast.service';
import { Subject, catchError, of, switchMap } from 'rxjs';

/**
 * Admin > Settings: the business values the POS reads, in one place.
 *
 * Every row comes from the server (`/app-settings/api/`), so a new setting
 * appears here as soon as the backend registers it - nothing is listed by hand.
 * The branch picked in the admin bar chooses which branch's overrides are shown;
 * "All centers" edits the value every branch uses unless it has its own.
 */
type Scope = 'global' | 'branch';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-settings',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './admin-settings.html',
  styleUrl: './admin-settings.css',
})
export class AdminSettingsComponent implements OnInit {
  private destroyRef = inject(DestroyRef);
  apiService = inject(ApiService);
  adminFilterService = inject(AdminFilterService);
  toastService = inject(ToastService);
  cdr = inject(ChangeDetectorRef);

  permissions: any = {};
  isOwner = false;

  settings: any[] = [];
  groups: string[] = [];
  centerId: number | null = null;
  canChangeGlobal = false;
  isLoading = false;
  isSaving = false;
  loadError = '';
  fieldErrors: Record<string, string> = {};

  /** Unsaved edits, keyed `<setting key>|<scope>`. `null` means "back to default". */
  edits: Record<string, any> = {};

  get canUpdate(): boolean {
    return this.isOwner || this.permissions?.admin?.settings?.update === true;
  }

  get hasEdits(): boolean {
    return Object.keys(this.edits).length > 0;
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
    // switchMap: a newer load cancels one still in flight, so a slow answer
    // for the previous centre can never overwrite the centre just picked.
    this.loads$.pipe(
      switchMap((centerId) => {
        this.isLoading = true;
        this.loadError = '';
        return this.apiService.get('app-settings/api/', { center_id: centerId }).pipe(
          catchError((err) => {
            this.loadError = err?.error?.detail || 'Could not load settings.';
            return of(null);
          }),
        );
      }),
      takeUntilDestroyed(this.destroyRef),
    ).subscribe((res) => {
      if (res) {
        this.applyResponse(res);
      } else {
        this.isLoading = false;
        this.cdr.markForCheck();
      }
    });
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.load();
    });
    this.load();
  }

  private loads$ = new Subject<number | null>();

  load() {
    this.loads$.next(this.adminFilterService.currentCenterId);
  }

  private applyResponse(res: any) {
    this.settings = res?.settings || [];
    this.centerId = res?.center_id ?? null;
    this.canChangeGlobal = res?.can_change_global === true;
    this.groups = [...new Set(this.settings.map((s) => s.group))];
    this.edits = {};
    this.fieldErrors = {};
    this.isLoading = false;
    this.cdr.markForCheck();
  }

  settingsIn(group: string): any[] {
    return this.settings.filter((s) => s.group === group);
  }

  private editKey(s: any, scope: Scope): string {
    return `${s.key}|${scope}`;
  }

  /** What the input shows: the unsaved edit, else what is in force at that scope. */
  shown(s: any, scope: Scope): any {
    const k = this.editKey(s, scope);
    if (k in this.edits) {
      return this.edits[k] ?? (scope === 'branch' ? s.global_effective : s.server_default);
    }
    return scope === 'branch' ? s.branch_effective : s.global_effective;
  }

  /** True if this scope has its own saved value (or an unsaved one). */
  isSet(s: any, scope: Scope): boolean {
    const k = this.editKey(s, scope);
    if (k in this.edits) {
      return this.edits[k] !== null;
    }
    const stored = scope === 'branch' ? s.branch_value : s.global_value;
    return stored !== null && stored !== undefined;
  }

  isEdited(s: any, scope: Scope): boolean {
    return this.editKey(s, scope) in this.edits;
  }

  canEdit(scope: Scope): boolean {
    return this.canUpdate && (scope === 'branch' || this.canChangeGlobal);
  }

  onChange(s: any, scope: Scope, value: any) {
    if (s.type === 'int' || s.type === 'decimal' || s.type === 'text') {
      value = value === null || value === undefined ? '' : String(value).trim();
      if (value === '') {
        return; // an empty box is not a value; "Back to default" clears one
      }
    }
    this.edits = { ...this.edits, [this.editKey(s, scope)]: value };
    delete this.fieldErrors[s.key];
  }

  /** Branch: follow the all-centers value. Global: back to the server default. */
  reset(s: any, scope: Scope) {
    this.edits = { ...this.edits, [this.editKey(s, scope)]: null };
  }

  discard() {
    this.edits = {};
    this.fieldErrors = {};
  }

  save() {
    if (!this.hasEdits || this.isSaving) {
      return;
    }
    const changes = Object.entries(this.edits).map(([k, value]) => {
      const [key, scope] = k.split('|');
      return { key, center: scope === 'branch' ? this.centerId : null, value };
    });
    this.isSaving = true;
    this.apiService.patch(this.queryPath(), { changes })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.isSaving = false;
          this.toastService.showSuccess('Settings saved');
          this.applyResponse(res);
        },
        error: (err) => {
          this.isSaving = false;
          const errors = err?.error?.errors;
          if (errors && typeof errors === 'object') {
            this.fieldErrors = errors;
            this.toastService.showError('Some values were not accepted. Nothing was saved.');
          } else {
            this.toastService.showError(err?.error?.detail || 'Could not save settings.');
          }
          this.cdr.markForCheck();
        },
      });
  }

  /** PATCH answers with the same view as GET, so ask for the same branch. */
  private queryPath(): string {
    return this.centerId ? `app-settings/api/?center_id=${this.centerId}` : 'app-settings/api/';
  }

  /** Readable form of a value, for "default" and "all centers" hints. */
  display(s: any, value: any): string {
    if (value === null || value === undefined) {
      return '-';
    }
    if (s.type === 'bool') {
      return value ? 'On' : 'Off';
    }
    if (s.type === 'choice') {
      return s.choices.find((c: any) => c.value === value)?.label ?? String(value);
    }
    return `${value}${s.unit ? ' ' + s.unit : ''}`;
  }

  trackByKey(index: number, item: any): any {
    return item?.key ?? index;
  }

  trackByIndex(index: number): number {
    return index;
  }
}
