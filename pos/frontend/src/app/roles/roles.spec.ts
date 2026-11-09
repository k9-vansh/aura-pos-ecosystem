import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { ComponentFixture, TestBed } from '@angular/core/testing';

import { RolesComponent } from './roles';
import { PERMISSION_MODULES } from '../services/permission-tree';

describe('RolesComponent', () => {
  let component: RolesComponent;
  let fixture: ComponentFixture<RolesComponent>;
  let http: HttpTestingController;

  const module = (key: string) => PERMISSION_MODULES.find((m) => m.key === key)!;
  const sub = (mod: string, key: string) => module(mod).submodules.find((s) => s.key === key)!;

  beforeEach(async () => {
    localStorage.setItem('user', JSON.stringify({ is_superuser: true, permissions: {} }));
    await TestBed.configureTestingModule({
      imports: [RolesComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])]
    }).compileComponents();

    http = TestBed.inject(HttpTestingController);
    fixture = TestBed.createComponent(RolesComponent);
    component = fixture.componentInstance;
    fixture.detectChanges();
    http.expectOne((r) => r.url.endsWith('/salon_admin/api/roles/')).flush([
      { id: 1, name: 'Cashier', users_count: 2, permissions: { billing: { new_invoice: { read: true, create: true } } } },
      { id: 2, name: 'Empty', users_count: 0, permissions: {} },
    ]);
    await fixture.whenStable();
  });

  afterEach(() => localStorage.removeItem('user'));

  it('should create and list the roles', () => {
    expect(component).toBeTruthy();
    expect(component.roles.length).toBe(2);
    expect(component.roleSummary(component.roles[0])).toBe('1 module');
    expect(component.roleSummary(component.roles[1])).toBe('No access yet');
  });

  it('shows every module of the shared tree', () => {
    expect(component.modules.map((m) => m.key)).toEqual(PERMISSION_MODULES.map((m) => m.key));
    expect(component.modules.length).toBeGreaterThanOrEqual(10);
  });

  it('opens a role as a copy and reports no unsaved change until something is touched', async () => {
    await component.open(component.roles[0], true);
    expect(component.draft.name).toBe('Cashier');
    expect(component.dirty).toBe(false);
    component.toggleVerb(module('billing'), sub('billing', 'invoices'), 'read');
    expect(component.dirty).toBe(true);
  });

  it('Add, Edit or Delete switch View on; switching View off clears the rest', async () => {
    await component.open(component.roles[1], true);
    const inventory = module('inventory');
    const products = sub('inventory', 'products');
    component.toggleVerb(inventory, products, 'update');
    expect(component.has('inventory', 'products', 'update')).toBe(true);
    expect(component.has('inventory', 'products', 'read')).toBe(true);
    component.toggleVerb(inventory, products, 'read');
    expect(component.has('inventory', 'products', 'read')).toBe(false);
    expect(component.has('inventory', 'products', 'update')).toBe(false);
  });

  it('a special permission switches View on', async () => {
    await component.open(component.roles[1], true);
    component.toggleAction(module('billing'), sub('billing', 'invoices'), 'cancel');
    expect(component.hasAction('billing', 'invoices', 'cancel')).toBe(true);
    expect(component.has('billing', 'invoices', 'read')).toBe(true);
  });

  it('a report submodule only offers View', () => {
    const tax = sub('finance', 'tax_report');
    expect(component.applies(tax, 'read')).toBe(true);
    expect(component.applies(tax, 'create')).toBe(false);
    component.toggleVerb(module('finance'), tax, 'delete');   // ignored: not applicable
    expect(component.has('finance', 'tax_report', 'delete')).toBe(false);
  });

  it('presets set a whole module and the state follows', async () => {
    await component.open(component.roles[1], true);
    const finance = module('finance');
    expect(component.moduleState(finance)).toBe('none');
    component.setPreset(finance, 'view');
    expect(component.moduleState(finance)).toBe('view');
    component.setPreset(finance, 'full');
    expect(component.moduleState(finance)).toBe('full');
    expect(component.has('finance', 'incentives', 'delete')).toBe(true);
    expect(component.hasAction('finance', 'pettycash', 'amend_closing')).toBe(true);
    component.toggleVerb(finance, sub('finance', 'incentives'), 'delete');
    expect(component.moduleState(finance)).toBe('partial');
    component.setPreset(finance, 'none');
    expect(component.moduleState(finance)).toBe('none');
  });

  it('the preview lists only what the role can open', async () => {
    await component.open(component.roles[0], true);
    const labels = component.preview.map((p) => p.label);
    expect(labels).toContain('Home');
    expect(labels).toContain('Billing');
    expect(labels).not.toContain('Finance');
    expect(labels).not.toContain('Admin');
  });

  it('refuses a duplicate or empty name', async () => {
    component.newRole();
    await fixture.whenStable();
    component.draft.name = '';
    expect(component.nameProblem).toBeTruthy();
    component.draft.name = ' cashier ';
    expect(component.nameProblem).toContain('already exists');
    component.draft.name = 'Brand new';
    expect(component.nameProblem).toBe('');
    expect(component.canSave).toBe(true);
  });

  it('saves a new role with View implied', async () => {
    component.newRole();
    await fixture.whenStable();
    component.draft.name = 'Stock keeper';
    component.toggleVerb(module('inventory'), sub('inventory', 'checkout'), 'create');
    component.saveRole();
    const req = http.expectOne((r) => r.url.endsWith('/salon_admin/api/roles/') && r.method === 'POST');
    expect(req.request.body.name).toBe('Stock keeper');
    expect(req.request.body.permissions.inventory.checkout.read).toBe(true);
    req.flush({ id: 9 });
    http.expectOne((r) => r.url.endsWith('/salon_admin/api/roles/') && r.method === 'GET').flush([]);
  });
});
