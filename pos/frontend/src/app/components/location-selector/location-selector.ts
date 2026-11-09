import {
  Component, Input, Output, EventEmitter, ChangeDetectionStrategy, ChangeDetectorRef, OnDestroy, OnInit, inject,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { LocationMemory } from '../../services/location-memory';
import { CenterScope } from '../../services/center-scope';
import { todayLocal } from '../../services/date-util';

/**
 * The location dropdown.
 *
 * Single mode (the default) picks one location or "All Locations".
 *
 * With `multi` it lets any number of locations be ticked. One tick is the same
 * as picking that location; two or more leave `selectedCenterId` at null (like
 * "All Locations") and put the choice in `selectedCenterIds` and in
 * `CenterScope`, which is what makes the data requests carry `center_ids`. A
 * screen therefore needs no change to its own loading code: it reloads on
 * `selectionChange`, and what it reads comes back narrowed to the ticked ones.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-location-selector',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './location-selector.html',
  styleUrl: './location-selector.css'
})
export class LocationSelectorComponent implements OnInit, OnDestroy {
  todayDate: string = todayLocal();
  /** Locations shown A-Z by name; the parent's own array is left untouched. */
  sortedCenters: any[] = [];
  private _centers: any[] = [];
  private locationMemory = inject(LocationMemory);
  private scope = inject(CenterScope);
  private cdr = inject(ChangeDetectorRef);
  private restored = false;

  @Input() set centers(value: any[]) {
    this._centers = value || [];
    this.sortedCenters = [...this._centers].sort((a, b) =>
      this.nameOf(a).localeCompare(this.nameOf(b), undefined, { sensitivity: 'base', numeric: true }));
    this.restoreRememberedChoice();
  }
  get centers(): any[] { return this._centers; }

  private nameOf(c: any): string {
    return String(c?.display_name || c?.center_name || c?.name || '');
  }
  @Input() hasGlobalAccess: boolean = false;
  /** Let several locations be ticked. */
  @Input() multi: boolean = false;

  private _selectedCenterId: number | null = null;
  @Input() set selectedCenterId(value: number | null) {
    this._selectedCenterId = value;
    if (!this.multi) return;
    // The screen set a location itself (its default, or a link): follow it,
    // unless it is just echoing back a many-location choice as null.
    const current = this.committed.length === 1 ? this.committed[0] : null;
    if (value !== current && !(value === null && this.committed.length > 1)) {
      this.committed = value === null ? [] : [value];
      this.pending = [...this.committed];
      this.scope.set(this.committed);
    }
  }
  get selectedCenterId(): number | null { return this._selectedCenterId; }
  @Output() selectedCenterIdChange = new EventEmitter<number | null>();

  /** The locations ticked (multi mode); empty means all. */
  @Input() set selectedCenterIds(value: number[]) {
    if (!this.multi || !Array.isArray(value)) return;
    this.committed = [...value];
    this.pending = [...value];
  }
  get selectedCenterIds(): number[] { return this.committed; }
  @Output() selectedCenterIdsChange = new EventEmitter<number[]>();
  @Output() selectionChange = new EventEmitter<void>();

  isOpen = false;
  /** What is applied, and what is ticked in the open list but not yet applied. */
  private committed: number[] = [];
  pending: number[] = [];

  ngOnInit() {
    this.restoreRememberedChoice();
  }

  ngOnDestroy() {
    // A screen that cannot show several locations must never inherit the choice.
    if (this.multi) this.scope.clear();
  }

  /** Coming back to a screen with several locations remembered: tick them again. */
  private restoreRememberedChoice() {
    if (!this.multi || this.restored || !this._centers.length) return;
    this.restored = true;
    const saved = this.locationMemory.pickMany(this._centers, this.hasGlobalAccess);
    if (!saved) return;
    this.committed = saved;
    this.pending = [...saved];
    this.scope.set(saved);
    this._selectedCenterId = null;
    // After this change-detection pass, so the screen's own first load is not raced.
    setTimeout(() => {
      this.selectedCenterIdChange.emit(null);
      this.selectedCenterIdsChange.emit(this.committed);
      this.selectionChange.emit();
      this.cdr.markForCheck();
    });
  }

  get selectedCenterName() {
    if (this.multi) {
      if (this.committed.length > 1) return `${this.committed.length} locations`;
      if (this.committed.length === 1) return this.centerName(this.committed[0]);
      return this.hasGlobalAccess ? 'All Locations' : 'Select Location';
    }
    if (this.selectedCenterId === null) return 'All Locations';
    return this.centerName(this.selectedCenterId);
  }

  private centerName(id: number): string {
    const center = this.centers.find(c => c.id === id);
    return center ? (center.display_name || center.center_name || center.name) : 'Select Location';
  }

  toggleOpen() {
    if (this.isOpen) {
      this.close();
      return;
    }
    this.pending = [...this.committed];
    this.isOpen = true;
  }

  /** Closing the list with the backdrop applies what is ticked, as Apply does. */
  close() {
    if (this.multi && this.isOpen) {
      this.apply();
      return;
    }
    this.isOpen = false;
  }

  // ── single choice ──
  selectCenter(id: number | null) {
    this._selectedCenterId = id;
    this.locationMemory.remember(id);
    this.selectedCenterIdChange.emit(id);
    this.selectionChange.emit();
    this.isOpen = false;
  }

  // ── several choices ──
  isPicked(id: number): boolean {
    return this.pending.includes(id);
  }

  togglePick(id: number) {
    this.pending = this.isPicked(id) ? this.pending.filter(i => i !== id) : [...this.pending, id];
  }

  pickAll() {
    if (!this.hasGlobalAccess) return;
    this.pending = [];
    this.apply();
  }

  apply() {
    this.isOpen = false;
    if (!this.hasGlobalAccess && !this.pending.length) {
      return; // someone without all-location access cannot clear the choice
    }
    const same = this.pending.length === this.committed.length
      && this.pending.every(i => this.committed.includes(i));
    this.committed = [...this.pending];
    this.scope.set(this.committed);
    this.locationMemory.rememberMany(this.committed);
    this._selectedCenterId = this.committed.length === 1 ? this.committed[0] : null;
    this.selectedCenterIdChange.emit(this._selectedCenterId);
    this.selectedCenterIdsChange.emit(this.committed);
    if (!same) this.selectionChange.emit();
  }
}
