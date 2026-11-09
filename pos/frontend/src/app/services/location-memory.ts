import { Injectable } from '@angular/core';

/**
 * Remembers the location picked in any screen's location dropdown, so moving
 * to another screen opens on the same location instead of snapping back to
 * "All Locations" or the first centre.
 *
 * Stored under a `pos_` key so `clearSession()` wipes it on sign-out.
 */
@Injectable({ providedIn: 'root' })
export class LocationMemory {
  private static readonly KEY = 'pos_center_pref';

  /** Called by the location dropdown whenever the user picks a location. */
  remember(id: number | null): void {
    try {
      localStorage.setItem(LocationMemory.KEY, id === null ? 'all' : String(id));
    } catch {
      // Storage unavailable: the screen just falls back to its own default.
    }
  }

  /** Called by a multi-choice dropdown: several centres, one centre, or none (all). */
  rememberMany(ids: number[]): void {
    try {
      localStorage.setItem(LocationMemory.KEY, ids.length ? ids.join(',') : 'all');
    } catch {
      // Storage unavailable: the screen just falls back to its own default.
    }
  }

  /** The remembered choice of two or more centres, if every one still exists for this user. */
  pickMany(centers: any[], hasGlobalAccess: boolean): number[] | undefined {
    if (!hasGlobalAccess) return undefined;
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(LocationMemory.KEY);
    } catch {
      return undefined;
    }
    if (!raw || !raw.includes(',')) return undefined;
    const ids = raw.split(',').map(Number).filter(i => Number.isFinite(i) && (centers || []).some(c => c.id == i));
    return ids.length > 1 ? ids : undefined;
  }

  /**
   * The remembered location if it still applies to this user, else undefined
   * (meaning: keep the screen's own default). `null` means "All Locations" and
   * is only returned when the screen can show it (`allowAll`).
   * Users without all-centre access are never moved off their own centre.
   */
  pick(centers: any[], hasGlobalAccess: boolean, allowAll: boolean): number | null | undefined {
    if (!hasGlobalAccess) return undefined;
    let raw: string | null = null;
    try {
      raw = localStorage.getItem(LocationMemory.KEY);
    } catch {
      return undefined;
    }
    if (raw === null) return undefined;
    if (raw === 'all') return allowAll ? null : undefined;
    if (raw.includes(',')) return undefined;   // several centres: only a multi dropdown can show that
    const id = Number(raw);
    return Number.isFinite(id) && (centers || []).some(c => c.id == id) ? id : undefined;
  }
}
