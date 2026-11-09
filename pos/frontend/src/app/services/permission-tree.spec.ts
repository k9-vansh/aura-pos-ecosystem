import { describe, expect, it } from 'vitest';
import { blankPermissions, normalisePermissions } from './permission-tree';

/**
 * Action ticks (Cancel a bill, Lock payroll, ...) live beside the CRUD verbs as
 * `permissions[module][sub].actions`. The Roles screen loads a saved role through
 * normalisePermissions, so a tick lost there would be silently removed the next
 * time the role is saved.
 */
describe('action ticks', () => {
  it('offers every tick the tree defines, all off', () => {
    const blank = blankPermissions();
    expect(blank['billing']['invoices']['actions']).toEqual({ cancel: false, change_payment: false });
    expect(blank['staff']['payrolls']['actions']['mark_paid']).toBe(false);
    // Submodules without ticks carry no `actions` key.
    expect(blank['billing']['new_invoice']['actions']).toBeUndefined();
  });

  it('keeps the ticks a saved role holds', () => {
    const loaded = normalisePermissions({
      billing: { invoices: { read: true, actions: { cancel: true } } },
    });
    expect(loaded['billing']['invoices']['read']).toBe(true);
    expect(loaded['billing']['invoices']['actions']).toEqual({ cancel: true, change_payment: false });
  });

  it('drops ticks the tree no longer offers', () => {
    const loaded = normalisePermissions({
      billing: { invoices: { read: true, actions: { cancel: true, delete_everything: true } } },
    });
    expect(loaded['billing']['invoices']['actions']['delete_everything']).toBeUndefined();
  });

  it('never turns a ticked verb into a ticked action', () => {
    const loaded = normalisePermissions({
      staff: { payrolls: { read: true, create: true, update: true, delete: true } },
    });
    expect(Object.values(loaded['staff']['payrolls']['actions']).some(Boolean)).toBe(false);
  });
});
