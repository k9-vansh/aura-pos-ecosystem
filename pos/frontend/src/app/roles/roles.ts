import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, HostListener, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { ToastService } from '../services/toast.service';
import { ModuleAccessService } from '../services/module-access';
import {
  PERMISSION_MODULES,
  VERBS,
  blankPermissions,
  blankSubmodule,
  normalisePermissions,
  verbsOf,
  withImpliedView,
  type ModuleDef,
  type PermissionVerb,
  type SubmoduleDef,
} from '../services/permission-tree';

/** What the role-holder will see, for the live preview. */
interface PreviewPage {
  label: string;
  detail: string;
}

type ModulePreset = 'none' | 'view' | 'full';
type ModuleState = 'none' | 'view' | 'partial' | 'full';

const VERB_LABEL: Record<PermissionVerb, string> = {
  read: 'View', create: 'Add', update: 'Edit', delete: 'Delete',
};

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-roles',
  imports: [CommonModule, FormsModule],
  templateUrl: './roles.html',
  styleUrl: './roles.css'
})
export class RolesComponent implements OnInit {
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private moduleAccess = inject(ModuleAccessService);
  toastService = inject(ToastService);
  apiService = inject(ApiService);
  cdr = inject(ChangeDetectorRef);

  /** The modules and submodules, straight from the shared tree. */
  readonly modules: readonly ModuleDef[] = PERMISSION_MODULES;
  readonly verbs: readonly PermissionVerb[] = VERBS;
  readonly verbLabel = VERB_LABEL;

  roles: any[] = [];
  loading = true;
  search = '';

  /** The role being edited: a copy, so nothing changes until Save. */
  draft: any = null;
  private baseline = '';
  isNew = false;
  isSaving = false;
  openModules = new Set<string>();
  preview: PreviewPage[] = [];

  isOwner = false;
  permissions: any = {};

  // ── access to this screen ─────────────────────────────────────────────
  get canCreate(): boolean { return this.isOwner || this.permissions?.admin?.roles?.create === true; }
  get canUpdate(): boolean { return this.isOwner || this.permissions?.admin?.roles?.update === true; }
  get canDelete(): boolean { return this.isOwner || this.permissions?.admin?.roles?.delete === true; }
  /** May this person change the role on screen (a new role needs Add, an existing one Edit)? */
  get canEditDraft(): boolean { return this.isNew ? this.canCreate : this.canUpdate; }

  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
      } catch { /* an unreadable session shows an empty screen */ }
    }
    this.loadRoles();
  }

  loadRoles(selectId: number | null = null) {
    this.apiService.getRoles().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.roles = Array.isArray(data) ? data : (data?.results ?? []);
        this.loading = false;
        if (selectId !== null) {
          const role = this.roles.find((r) => r.id === selectId);
          if (role) this.open(role, true);
        }
        this.cdr.detectChanges();
      },
      error: () => { this.loading = false; this.cdr.detectChanges(); },
    });
  }

  // ── the list ──────────────────────────────────────────────────────────
  get filteredRoles(): any[] {
    const q = this.search.trim().toLowerCase();
    return q ? this.roles.filter((r) => String(r.name).toLowerCase().includes(q)) : this.roles;
  }

  /** "6 modules" style line for a role card. */
  roleSummary(role: any): string {
    const perms = normalisePermissions(role?.permissions);
    const count = this.modules.filter((m) => m.submodules.some((s) => perms[m.key]?.[s.key]?.read)).length;
    return count === 0 ? 'No access yet' : `${count} module${count === 1 ? '' : 's'}`;
  }

  // ── opening, creating, leaving ───────────────────────────────────────
  get dirty(): boolean {
    return !!this.draft && JSON.stringify(this.snapshot()) !== this.baseline;
  }

  private snapshot() {
    return { name: this.draft.name, description: this.draft.description ?? '', permissions: this.draft.permissions };
  }

  private load(draft: any, isNew: boolean) {
    this.draft = draft;
    this.isNew = isNew;
    this.baseline = JSON.stringify(this.snapshot());
    // Open the modules that hold something; open the first one for an empty role.
    this.openModules = new Set(this.modules.filter((m) => this.moduleState(m) !== 'none').map((m) => m.key));
    if (this.openModules.size === 0 && this.modules.length) this.openModules.add(this.modules[0].key);
    this.recompute();
    this.cdr.detectChanges();
  }

  async open(role: any, force = false) {
    if (!force && this.draft?.id === role.id) return;
    if (!force && !(await this.confirmLeave())) return;
    this.load({
      id: role.id,
      name: role.name,
      description: role.description ?? '',
      users_count: role.users_count ?? 0,
      permissions: withImpliedView(normalisePermissions(role.permissions)),
    }, false);
  }

  async newRole() {
    if (!(await this.confirmLeave())) return;
    this.load({ name: '', description: '', users_count: 0, permissions: blankPermissions() }, true);
  }

  private async confirmLeave(): Promise<boolean> {
    if (!this.dirty) return true;
    return this.confirmDialog.ask('You have unsaved changes to this role. Leave without saving?');
  }

  async discard() {
    if (!this.draft) return;
    if (this.dirty && !(await this.confirmDialog.ask('Discard your changes to this role?'))) return;
    if (this.isNew) {
      this.draft = null;
      this.preview = [];
      this.cdr.detectChanges();
      return;
    }
    const original = this.roles.find((r) => r.id === this.draft.id);
    if (original) this.open(original, true);
  }

  @HostListener('window:beforeunload', ['$event'])
  warnOnClose(event: BeforeUnloadEvent) {
    if (this.dirty) { event.preventDefault(); event.returnValue = ''; }
  }

  // ── reading the grid ─────────────────────────────────────────────────
  entry(mod: string, sub: string): any {
    return this.draft?.permissions?.[mod]?.[sub] ?? blankSubmodule({ key: sub, label: sub });
  }

  verbsFor(sub: SubmoduleDef): readonly PermissionVerb[] { return verbsOf(sub); }

  applies(sub: SubmoduleDef, verb: PermissionVerb): boolean { return verbsOf(sub).includes(verb); }

  has(mod: string, sub: string, verb: PermissionVerb): boolean {
    return this.draft?.permissions?.[mod]?.[sub]?.[verb] === true;
  }

  hasAction(mod: string, sub: string, action: string): boolean {
    return this.draft?.permissions?.[mod]?.[sub]?.actions?.[action] === true;
  }

  /** Does the role hold anything at all on this submodule? */
  holds(mod: string, sub: SubmoduleDef): boolean {
    const e = this.draft?.permissions?.[mod]?.[sub.key];
    if (!e) return false;
    return VERBS.some((v) => e[v] === true)
      || (sub.actions ?? []).some((a) => e.actions?.[a.key] === true);
  }

  /** Is this submodule fully granted: every verb that applies and every action? */
  isFull(mod: string, sub: SubmoduleDef): boolean {
    const e = this.draft?.permissions?.[mod]?.[sub.key];
    if (!e) return false;
    return verbsOf(sub).every((v) => e[v] === true)
      && (sub.actions ?? []).every((a) => e.actions?.[a.key] === true);
  }

  moduleState(module: ModuleDef): ModuleState {
    const held = module.submodules.filter((s) => this.holds(module.key, s));
    if (held.length === 0) return 'none';
    if (module.submodules.every((s) => this.isFull(module.key, s))) return 'full';
    const viewOnly = module.submodules.every((s) => {
      const e = this.draft.permissions[module.key]?.[s.key];
      return e?.read === true && !(['create', 'update', 'delete'] as const).some((v) => e[v] === true)
        && !(s.actions ?? []).some((a) => e.actions?.[a.key] === true);
    });
    return viewOnly ? 'view' : 'partial';
  }

  /** "3 of 10" for a module header. */
  moduleCount(module: ModuleDef): string {
    const held = module.submodules.filter((s) => this.holds(module.key, s)).length;
    return `${held} of ${module.submodules.length}`;
  }

  stateLabel(state: ModuleState): string {
    return { none: 'No access', view: 'View only', partial: 'Custom', full: 'Full access' }[state];
  }

  /** Is this verb ticked for every submodule of the module where it applies (the column header)? */
  columnOn(module: ModuleDef, verb: PermissionVerb): boolean {
    const applicable = module.submodules.filter((s) => this.applies(s, verb));
    return applicable.length > 0 && applicable.every((s) => this.has(module.key, s.key, verb));
  }

  columnApplies(module: ModuleDef, verb: PermissionVerb): boolean {
    return module.submodules.some((s) => this.applies(s, verb));
  }

  // ── changing the grid ────────────────────────────────────────────────
  private ensure(mod: string, sub: SubmoduleDef): any {
    const perms = this.draft.permissions;
    perms[mod] ??= {};
    perms[mod][sub.key] ??= blankSubmodule(sub);
    return perms[mod][sub.key];
  }

  private touched() {
    withImpliedView(this.draft.permissions);
    this.recompute();
    this.cdr.detectChanges();
  }

  toggleVerb(mod: ModuleDef, sub: SubmoduleDef, verb: PermissionVerb) {
    if (!this.canEditDraft || !this.applies(sub, verb)) return;
    const e = this.ensure(mod.key, sub);
    const next = e[verb] !== true;
    e[verb] = next;
    if (verb === 'read' && !next) {
      // Without View nothing else makes sense: take Add/Edit/Delete and the ticks off too.
      for (const v of VERBS) e[v] = false;
      if (e.actions) for (const key of Object.keys(e.actions)) e.actions[key] = false;
    }
    this.touched();
  }

  toggleAction(mod: ModuleDef, sub: SubmoduleDef, action: string) {
    if (!this.canEditDraft) return;
    const e = this.ensure(mod.key, sub);
    e.actions = { ...(e.actions ?? {}), [action]: e.actions?.[action] !== true };
    this.touched();
  }

  toggleColumn(mod: ModuleDef, verb: PermissionVerb) {
    if (!this.canEditDraft) return;
    const on = !this.columnOn(mod, verb);
    for (const sub of mod.submodules) {
      if (!this.applies(sub, verb)) continue;
      const e = this.ensure(mod.key, sub);
      e[verb] = on;
      if (verb === 'read' && !on) {
        for (const v of VERBS) e[v] = false;
        if (e.actions) for (const key of Object.keys(e.actions)) e.actions[key] = false;
      }
    }
    this.touched();
  }

  setPreset(mod: ModuleDef, preset: ModulePreset) {
    if (!this.canEditDraft) return;
    for (const sub of mod.submodules) {
      const e = this.ensure(mod.key, sub);
      const applicable = verbsOf(sub);
      for (const v of VERBS) e[v] = preset === 'full' ? applicable.includes(v) : (preset === 'view' && v === 'read');
      for (const a of sub.actions ?? []) {
        e.actions = { ...(e.actions ?? {}), [a.key]: preset === 'full' };
      }
    }
    if (preset !== 'none') this.openModules.add(mod.key);
    this.touched();
  }

  setAllCentres(on: boolean) {
    if (!this.canEditDraft) return;
    this.draft.permissions.all_centers = on;
    this.touched();
  }

  clearAll() {
    if (!this.canEditDraft) return;
    const all = this.draft.permissions.all_centers === true;
    this.draft.permissions = blankPermissions();
    this.draft.permissions.all_centers = all;
    this.touched();
  }

  /** Start from another role's grants (the name and description stay). */
  copyFrom(roleId: string) {
    if (!this.canEditDraft || !roleId) return;
    const source = this.roles.find((r) => r.id === Number(roleId));
    if (!source) return;
    this.draft.permissions = withImpliedView(normalisePermissions(source.permissions));
    this.openModules = new Set(this.modules.filter((m) => this.moduleState(m) !== 'none').map((m) => m.key));
    this.touched();
  }

  toggleOpen(mod: string) {
    if (this.openModules.has(mod)) this.openModules.delete(mod); else this.openModules.add(mod);
    this.cdr.detectChanges();
  }

  expandAll(open: boolean) {
    this.openModules = open ? new Set(this.modules.map((m) => m.key)) : new Set();
    this.cdr.detectChanges();
  }

  // ── what the role-holder will see ────────────────────────────────────
  /**
   * The menu a person with this role gets: the same rule the app itself uses
   * (ModuleAccessService), so the preview cannot disagree with reality.
   */
  recompute() {
    if (!this.draft) { this.preview = []; return; }
    const user = { is_superuser: false, permissions: this.draft.permissions };
    const pages: PreviewPage[] = [{ label: 'Home', detail: this.draft.permissions.overview?.home?.read ? 'with revenue figures' : 'without revenue figures' }];

    for (const mod of this.modules) {
      if (!mod.route || mod.key === 'overview') continue;
      if (!this.moduleAccess.canAccessRoute(mod.route, user)) continue;
      const tabs = mod.submodules.filter((s) => this.draft.permissions[mod.key]?.[s.key]?.read).map((s) => s.label);
      pages.push({ label: mod.label, detail: mod.submodules.length > 1 ? tabs.join(', ') : '' });
    }

    const adminPages = (this.modules.find((m) => m.key === 'admin')?.submodules ?? [])
      .filter((s) => s.route && this.draft.permissions.admin?.[s.key]?.read)
      .map((s) => s.label);
    if (adminPages.length) pages.push({ label: 'Admin', detail: adminPages.join(', ') });
    if (this.draft.permissions.chat?.messages?.read) pages.push({ label: 'Chat', detail: '' });
    this.preview = pages;
  }

  // ── saving and deleting ──────────────────────────────────────────────
  get nameProblem(): string {
    const name = (this.draft?.name ?? '').trim();
    if (!name) return 'Give the role a name.';
    const clash = this.roles.some((r) => r.id !== this.draft?.id && String(r.name).trim().toLowerCase() === name.toLowerCase());
    return clash ? 'A role with this name already exists.' : '';
  }

  get canSave(): boolean {
    return !!this.draft && this.canEditDraft && !this.isSaving && !this.nameProblem && (this.isNew || this.dirty);
  }

  saveRole() {
    if (!this.canSave) return;
    this.isSaving = true;
    const body = {
      name: this.draft.name.trim().replace(/\s+/g, ' '),
      description: this.draft.description ?? '',
      permissions: withImpliedView(this.draft.permissions),
    };
    const request = this.isNew
      ? this.apiService.createRole(body)
      : this.apiService.updateRole(this.draft.id, body);
    request.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (saved: any) => {
        this.isSaving = false;
        this.toastService.showSuccess(this.isNew ? 'Role created' : 'Role saved. People who hold it get the change on their next action.');
        this.loadRoles(saved?.id ?? this.draft.id);
      },
      error: (err: any) => {
        this.isSaving = false;
        this.toastService.showError(this.explain(err?.error) || 'The role could not be saved.');
        this.cdr.detectChanges();
      },
    });
  }

  /** A readable line out of a Django REST error body. */
  private explain(payload: any): string {
    if (!payload) return '';
    if (typeof payload === 'string') return payload;
    if (Array.isArray(payload)) return payload.map((p) => this.explain(p)).filter(Boolean).join(' ');
    const parts: string[] = [];
    for (const [key, value] of Object.entries(payload)) {
      const text = this.explain(value);
      if (text) parts.push(key === 'detail' || key === 'non_field_errors' ? text : `${key}: ${text}`);
    }
    return parts.join(' | ');
  }

  async deleteRole() {
    if (!this.draft?.id || !this.canDelete) return;
    if (this.draft.users_count > 0) {
      this.toastService.showError(`${this.draft.users_count} user(s) hold this role. Give them another role first.`);
      return;
    }
    if (!(await this.confirmDialog.ask(`Delete the role "${this.draft.name}"? This cannot be undone.`))) return;
    this.isSaving = true;
    this.apiService.deleteRole(this.draft.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.toastService.showSuccess('Role deleted');
        this.draft = null;
        this.preview = [];
        this.loadRoles();
      },
      error: (err: any) => {
        this.isSaving = false;
        this.toastService.showError(this.explain(err?.error) || 'The role could not be deleted.');
        this.cdr.detectChanges();
      },
    });
  }

  trackByKey(_: number, item: { key: string }): string { return item.key; }
  trackById(_: number, item: any): any { return item?.id ?? _; }
  trackByIndex(index: number): number { return index; }
}
