import { Injectable } from '@angular/core';
import { BehaviorSubject, Subject } from 'rxjs';
import { addDays, daysAgo, todayLocal } from '../services/date-util';

@Injectable({
  providedIn: 'root'
})
export class AdminFilterService {
  // Filter State
  selectedCenterId$ = new BehaviorSubject<number | null>(null);
  fromDate$ = new BehaviorSubject<string>(daysAgo(30));
  toDate$ = new BehaviorSubject<string>(todayLocal());

  // Event Triggers
  private applySubject = new Subject<void>();
  apply$ = this.applySubject.asObservable();

  private exportSubject = new Subject<void>();
  export$ = this.exportSubject.asObservable();

  // Getters for current state
  get currentCenterId() { return this.selectedCenterId$.value; }
  get currentFromDate() { return this.fromDate$.value; }
  get currentToDate() { return this.toDate$.value; }

  // Setters
  setCenterId(id: number | null) { this.selectedCenterId$.next(id); }
  setFromDate(date: string) { this.fromDate$.next(date); }
  setToDate(date: string) { this.toDate$.next(date); }

  /**
   * Roll the default window forward if the session has crossed midnight.
   *
   * `today` used to be computed once in the constructor of this root-provided
   * singleton, so a terminal left open overnight kept filtering to yesterday
   * indefinitely. Screens call this on init; it only moves dates that are
   * still at their defaults, so a filter the operator chose is left alone.
   */
  refreshDefaultsIfStale() {
    const today = todayLocal();
    if (this.toDate$.value === today) {
      return;
    }
    const looksDefault =
      this.toDate$.value < today &&
      this.fromDate$.value === addDays(this.toDate$.value, -30);
    if (looksDefault) {
      this.fromDate$.next(daysAgo(30));
      this.toDate$.next(today);
    }
  }

  // Actions
  triggerApply() {
    this.applySubject.next();
  }

  triggerExport() {
    this.exportSubject.next();
  }
}
