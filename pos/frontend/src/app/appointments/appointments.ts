import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectorRef, Component, DestroyRef, OnInit, OnDestroy, inject, ChangeDetectionStrategy } from '@angular/core';
import { Router } from '@angular/router';
import { CommonModule, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { canSearchClients } from '../services/client-search-rule';
import { AuthService } from '../services/auth';
import { ToastService } from '../services/toast.service';
import { DragDropModule } from '@angular/cdk/drag-drop';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { toLocalDateString, todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';

function extractErrorMessage(err: any): string {
  const data = err?.error;
  if (!data) return err?.message || 'An unexpected error occurred.';
  if (typeof data === 'string') return data;
  // DRF often returns { detail: '...' } or { non_field_errors: [...] } or array
  if (data.detail) return data.detail;
  if (Array.isArray(data)) return data.join(' ');
  const msgs: string[] = [];
  for (const key of Object.keys(data)) {
    const v = data[key];
    msgs.push(Array.isArray(v) ? v.join(' ') : String(v));
  }
  return msgs.join(' ') || JSON.stringify(data);
}

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-appointments',
  standalone: true,
  imports: [CommonModule, FormsModule, DragDropModule, LocationSelectorComponent],
  templateUrl: './appointments.html',
  styleUrls: ['./appointments.css']
})
export class AppointmentsComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private locationMemory = inject(LocationMemory);
  centers: any[] = [];
  selectedCenterId: any = null;
  selectedDate: string = todayLocal();
  showCancelled: boolean = true;
  
  isOwner: boolean = false;
  hasGlobalAccess: boolean = false;
  permissions: any = {};

  appointments: any[] = [];
  staffMembers: any[] = [];
  activeStaffMembers: any[] = [];

  // KPIs
  totalAppointments = 0;
  unallocatedServices = 0;
  cancelledAppointments = 0;
  expectedRevenue = 0;

  // Branch hours (Admin > Settings > Appointments); 09:00-22:00 until loaded.
  dayStartMins = 9 * 60;
  daySpanMins = 13 * 60;
  defaultDuration = 45;
  openTime = '09:00';
  closeTime = '22:00';
  closedDays: string[] = [];

  // Time Slots (one column per hour of the branch day)
  timeSlots: string[] = [
    '09:00', '10:00', '11:00', '12:00', '13:00', '14:00',
    '15:00', '16:00', '17:00', '18:00', '19:00', '20:00', '21:00'
  ];

  servicesList: any[] = [];

  // Modal
  showModal = false;
  isEdit = false;
  isSaving = false;
  currentAppointment: any = {
    client_phone: '',
    client_name: '',
    date: '',
    start_time: '09:00',
    notes: '',
    status: 'Scheduled',
    services: []
  };
  recentVisits: any[] = [];
  
  // Search
  searchQuery: string = '';
  clientSearchResults: any[] = [];
  selectedClient: any = null;
  searchTimeout: any;
  
  newServiceForm = {
    service_name: '',
    time: '09:00',
    duration: 45,
    staff: null
  };

  constructor(
    private apiService: ApiService,
    private authService: AuthService,
    private toastService: ToastService,
    private cdr: ChangeDetectorRef,
    private router: Router
  ) {}

  ngOnInit(): void {
    // A booking made in the client app (or a change by another till) shows up here without a refresh. Skipped while the
    // tab is hidden, a form is open or a card is being dragged, so nothing under someone's hand ever moves.
    const live = setInterval(() => {
      if (document.hidden || this.showModal || this.isSaving || !this.selectedCenterId) return;
      if (document.querySelector('.cdk-drag-dragging')) return;
      this.loadAppointments();
    }, 8000);
    this.destroyRef.onDestroy(() => clearInterval(live));

    let user: any = null;
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try { user = JSON.parse(userStr); } catch (e) {}
    }
    this.isOwner = user?.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
    this.permissions = user?.permissions || {};
    
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.centers = data || [];
      if (this.hasGlobalAccess) {
        if (this.centers.length > 0 && !this.selectedCenterId) {
          this.selectedCenterId = this.locationMemory.pick(this.centers, this.hasGlobalAccess, false) ?? this.centers[0].id;
        }
      } else {
        // Ensure numeric ID
        const cid = user?.center_id;
        this.selectedCenterId = cid ? Number(cid) : (this.centers[0]?.id ?? null);
      }
      this.loadData();
    });
  }

  setToday() {
    this.selectedDate = todayLocal();
    this.loadAppointments();
  }

  prevDay() {
    const d = new Date(this.selectedDate);
    d.setDate(d.getDate() - 1);
    this.selectedDate = toLocalDateString(d);
    this.loadAppointments();
  }

  nextDay() {
    const d = new Date(this.selectedDate);
    d.setDate(d.getDate() + 1);
    this.selectedDate = toLocalDateString(d);
    this.loadAppointments();
  }


  loadData() {
    this.loadHours();
    this.loadStaff();
    this.loadAppointments();
    this.loadServices();
  }

  loadHours() {
    const cid = this.selectedCenterId ?? undefined;
    this.apiService.getAppointmentHours(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (h: any) => {
        const toMins = (t: string, d: number) => {
          const [hh, mm] = String(t || '').split(':').map(Number);
          return Number.isFinite(hh) ? hh * 60 + (mm || 0) : d;
        };
        const open = toMins(h?.open_time, 9 * 60);
        const close = Math.max(toMins(h?.close_time, 22 * 60), open + 60);
        const firstHour = Math.floor(open / 60);
        const lastHour = Math.ceil(close / 60);
        this.timeSlots = [];
        for (let hr = firstHour; hr < lastHour; hr++) this.timeSlots.push(`${String(hr).padStart(2, '0')}:00`);
        this.dayStartMins = firstHour * 60;
        this.daySpanMins = (lastHour - firstHour) * 60;
        this.defaultDuration = Number(h?.default_duration) || 45;
        this.openTime = h?.open_time || '09:00';
        this.closeTime = h?.close_time || '22:00';
        this.closedDays = h?.closed_days || [];
        this.buildAppointmentsMap();
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  /** The selected day is one of the branch's closed weekdays. */
  get isClosedDay(): boolean {
    const names = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    const d = new Date(this.selectedDate + 'T00:00:00');
    return this.closedDays.includes(names[d.getDay()]);
  }

  /** A ticked calendar action (cancel, restore, check_in, no_show, assign_staff); the server enforces it too. */
  canCalendar(action: string): boolean {
    return this.isOwner || this.permissions?.appointments?.calendar?.actions?.[action] === true;
  }

  loadServices() {
    const cid = this.selectedCenterId ?? undefined;
    this.apiService.getServices(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      // A service switched off at this branch cannot be booked or billed here.
      this.servicesList = (data || []).filter((s: any) => s.center_override?.is_active !== false);
      this.cdr.detectChanges();
    });
  }

  onDateChange(event: any) {
    this.selectedDate = event.target.value;
    this.loadAppointments();
  }

  loadStaff() {
    const cid = this.selectedCenterId ?? undefined;
    this.apiService.getStaffMembers(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.staffMembers = data;
      this.activeStaffMembers = data.filter(s => s.is_active);
      this.cdr.detectChanges();
    });
  }

  loadAppointments() {
    const cid = this.selectedCenterId ?? undefined;
    this.apiService.getAppointments(cid, this.selectedDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.appointments = data;
      this.calculateKPIs();
      this.buildAppointmentsMap();  // pre-build cache after data loads
      this.cdr.detectChanges();
    });
  }

  calculateKPIs() {
    this.totalAppointments = 0;
    this.unallocatedServices = 0;
    this.cancelledAppointments = 0;
    this.expectedRevenue = 0;

    for (let appt of this.appointments) {
      if (appt.status === 'Cancelled' || appt.status === 'No-show') {
        this.cancelledAppointments++;
        continue; // skip other stats if cancelled
      }
      this.totalAppointments++;
      for (let s of appt.services) {
        if (!s.staff) this.unallocatedServices++;
        this.expectedRevenue += parseFloat(s.price || 0);
      }
    }
  }

  // Memoized map: staffId (or 'null') -> appointment+service pairs
  private _appointmentsMap: Map<string, any[]> = new Map();

  buildAppointmentsMap() {
    this._appointmentsMap.clear();
    for (let appt of this.appointments) {
      if (!this.showCancelled && (appt.status === 'Cancelled' || appt.status === 'No-show')) continue;
      for (let s of appt.services) {
        const key = String(s.staff ?? 'null');
        if (!this._appointmentsMap.has(key)) this._appointmentsMap.set(key, []);
        this._appointmentsMap.get(key)!.push({ 
          appointment: appt, 
          service: s,
          leftStyle: this.calculateLeftStyle(s.time),
          widthStyle: this.calculateWidthStyle(s.duration, s.time)
        });
      }
    }
  }

  getAppointmentsForStaff(staffId: number | null): any[] {
    const key = String(staffId ?? 'null');
    return this._appointmentsMap.get(key) ?? [];
  }

  // Calculate left percentage based on time (9am is 0%, 9pm is 100% of 12 slots, but 13 columns means we divide by 13)
  calculateLeftStyle(time: string): string {
    if (!time) return '0%';
    const parts = time.split(':');
    const hours = parseInt(parts[0], 10);
    const mins = parseInt(parts[1], 10);
    const startMins = this.dayStartMins;
    const currentMins = hours * 60 + mins;
    const diff = currentMins - startMins;
    const percent = (diff / this.daySpanMins) * 100;
    return `${Math.max(0, Math.min(100, percent))}%`;
  }

  // Calculate width based on duration and ensure it doesn't overflow the timeline
  calculateWidthStyle(duration: number, time: string): string {
    let safeDuration = Number(duration) || 45; // fallback to 45 mins if invalid
    
    // If duration is suspiciously large (e.g., > 12 hours), it might have been saved in seconds
    if (safeDuration > 720) {
      safeDuration = Math.round(safeDuration / 60);
    }
    // Cap any remaining erroneously large durations to 8 hours max (480 mins)
    if (safeDuration > 480) {
      safeDuration = 480;
    }
    
    const widthPercent = (safeDuration / this.daySpanMins) * 100;
    
    let leftPercent = 0;
    if (time) {
      const parts = time.split(':');
      const hours = parseInt(parts[0], 10) || 0;
      const mins = parseInt(parts[1], 10) || 0;
      const startMins = this.dayStartMins;
      const currentMins = hours * 60 + mins;
      const diff = currentMins - startMins;
      leftPercent = (diff / this.daySpanMins) * 100;
    }
    
    // Cap width so it doesn't go beyond 100% of the timeline
    const maxAllowedWidth = 100 - Math.max(0, leftPercent);
    const finalWidth = Math.min(widthPercent, maxAllowedWidth);
    
    return `${Math.max(0, finalWidth)}%`;
  }

  // Modal logic
  openNewModal() {
    this.isEdit = false;
    this.searchQuery = '';
    this.selectedClient = null;
    this.clientSearchResults = [];
    this.currentAppointment = {
      client_phone: '',
      client_name: '',
      date: this.selectedDate,
      start_time: '09:00',
      notes: '',
      status: 'Scheduled',
      services: [],
      center: this.selectedCenterId
    };
    this.recentVisits = [];
    this.showModal = true;
  }

  openEditModal(appt: any) {
    this.isEdit = true;
    this.searchQuery = appt.client_name;
    this.selectedClient = null;
    this.clientSearchResults = [];
    this.currentAppointment = JSON.parse(JSON.stringify(appt));
    this.showModal = true;
    this.searchRecentVisits();
  }

  onSearchInput(event: any) {
    const val = event.target.value;
    if (this.searchTimeout) {
      clearTimeout(this.searchTimeout);
    }
    if (!canSearchClients(val)) {
      this.clientSearchResults = [];
      return;
    }
    this.searchTimeout = setTimeout(() => {
      this.apiService.getClients(val).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((res: any) => {
        const data = res && res.results ? res.results : res;
        this.clientSearchResults = data || [];
        this.cdr.detectChanges();
      });
    }, 300);
  }

  selectClient(c: any) {
    if (c.is_blacklisted) {
      alert('This client is blacklisted and cannot book appointments.');
      this.searchQuery = '';
      this.clientSearchResults = [];
      return;
    }
    this.selectedClient = c;
    this.currentAppointment.client_phone = c.phone;
    this.currentAppointment.client_name = c.name || `${c.first_name || ''} ${c.last_name || ''}`.trim();
    this.clientSearchResults = [];
    this.searchQuery = this.currentAppointment.client_name;
    this.searchRecentVisits();
  }

  sendToBilling() {
    if (this.currentAppointment.id) {
      this.showModal = false;
      this.router.navigate(['/admin/billing'], { queryParams: { appointment_id: this.currentAppointment.id } });
    } else {
      alert("Please save this appointment first before creating an invoice.");
    }
  }

  searchRecentVisits() {
    if (this.currentAppointment.client_phone.length >= 10) {
      this.apiService.getAppointments(this.selectedCenterId, undefined, this.currentAppointment.client_phone)
        .pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          // Exclude current appt
          this.recentVisits = data.filter(a => a.id !== this.currentAppointment.id);
        });
    }
  }

  formatTime12(time24: string): string {
    if (!time24) return '--:--';
    const parts = time24.split(':');
    let hours = parseInt(parts[0], 10);
    const mins = parseInt(parts[1], 10);
    if (isNaN(hours) || isNaN(mins)) return time24;
    const ampm = hours >= 12 ? 'PM' : 'AM';
    hours = hours % 12;
    hours = hours ? hours : 12;
    const strMins = mins < 10 ? '0' + mins : String(mins);
    return `${hours}:${strMins} ${ampm}`;
  }

  getServiceEndTime(startTime: string, duration: number): string {
    const end24 = this.addMinutesToTime(startTime, Number(duration) || 45);
    return this.formatTime12(end24);
  }

  formatTimeRange(startTime: string, duration: number): string {
    if (!startTime) return '';
    return `${this.formatTime12(startTime)} - ${this.getServiceEndTime(startTime, duration)}`;
  }

  getAppointmentEndTime(): string {
    if (!this.currentAppointment.services || this.currentAppointment.services.length === 0) {
      return this.formatTime12(this.currentAppointment.start_time);
    }
    const last = this.currentAppointment.services[this.currentAppointment.services.length - 1];
    return this.getServiceEndTime(last.time, last.duration);
  }

  getTotalDurationMinutes(): number {
    if (!this.currentAppointment.services) return 0;
    return this.currentAppointment.services.reduce((acc: number, s: any) => acc + (Number(s.duration) || 0), 0);
  }

  getTotalDurationFormatted(): string {
    const totalMins = this.getTotalDurationMinutes();
    if (totalMins <= 0) return '0 mins';
    const hours = Math.floor(totalMins / 60);
    const mins = totalMins % 60;
    if (hours > 0 && mins > 0) {
      return `${hours}h ${mins}m`;
    } else if (hours > 0) {
      return `${hours}h`;
    }
    return `${mins} mins`;
  }

  getTotalEstimatedPrice(): number {
    if (!this.currentAppointment.services) return 0;
    return this.currentAppointment.services.reduce((acc: number, s: any) => acc + (parseFloat(s.price) || 0), 0);
  }

  addService() {
    if (this.newServiceForm.service_name) {
      // Find selected service to get its price and duration
      const selected = this.servicesList.find(s => s.name === this.newServiceForm.service_name);
      let price = 0;
      let duration = this.defaultDuration;
      
      if (selected) {
        price = selected.center_override?.price ?? selected.default_price ?? 0;
        duration = selected.duration_mins || this.defaultDuration;
      }
      
      // Calculate start time based on existing services
      let nextStartTime = this.currentAppointment.start_time || '09:00';
      if (this.currentAppointment.services.length > 0) {
        const lastService = this.currentAppointment.services[this.currentAppointment.services.length - 1];
        nextStartTime = this.addMinutesToTime(lastService.time, Number(lastService.duration) || 45);
      }

      this.currentAppointment.services.push({
        service: selected?.id ?? null,     // linked to the catalogue, not matched by name
        service_name: this.newServiceForm.service_name,
        time: nextStartTime,
        duration: duration,
        price: price,
        staff: this.newServiceForm.staff || null
      });

      this.recalculateTimes();

      this.newServiceForm = {
        service_name: '',
        time: this.addMinutesToTime(nextStartTime, duration),
        duration: 45,
        staff: null
      };
    }
  }

  removeService(index: number) {
    this.currentAppointment.services.splice(index, 1);
    this.recalculateTimes();
  }

  onStartTimeChange() {
    if (this.currentAppointment.services.length > 0) {
      this.currentAppointment.services[0].time = this.currentAppointment.start_time;
      this.recalculateTimes();
    } else {
      this.newServiceForm.time = this.currentAppointment.start_time;
    }
  }

  // A manually edited service time is kept; only the services after it are re-chained.
  onServiceTimeChange(index: number) {
    if (index === 0 && this.currentAppointment.services[0].time) {
      this.currentAppointment.start_time = this.currentAppointment.services[0].time;
    }
    this.recalculateTimes(index + 1);
  }

  recalculateTimes(fromIndex = 0) {
    if (!this.currentAppointment.services || this.currentAppointment.services.length === 0) return;
    for (let i = fromIndex; i < this.currentAppointment.services.length; i++) {
      if (i === 0) {
        if (!this.currentAppointment.services[0].time) {
          this.currentAppointment.services[0].time = this.currentAppointment.start_time || '09:00';
        }
      } else {
        const prev = this.currentAppointment.services[i - 1];
        this.currentAppointment.services[i].time = this.addMinutesToTime(prev.time, Number(prev.duration) || 45);
      }
    }
    // Update the form's default time for the next added service
    const lastService = this.currentAppointment.services[this.currentAppointment.services.length - 1];
    this.newServiceForm.time = this.addMinutesToTime(lastService.time, Number(lastService.duration) || 45);
  }

  addMinutesToTime(timeStr: string, minutesToAdd: number): string {
    if (!timeStr) return '09:00';
    const parts = timeStr.split(':');
    let hours = parseInt(parts[0], 10) || 0;
    let mins = parseInt(parts[1], 10) || 0;
    
    mins += (Number(minutesToAdd) || 0);
    hours += Math.floor(mins / 60);
    mins = mins % 60;
    hours = hours % 24;
    
    const hStr = hours.toString().padStart(2, '0');
    const mStr = mins.toString().padStart(2, '0');
    return `${hStr}:${mStr}`;
  }

  saveAppointment() {
    if (this.isSaving) return;
    this.isSaving = true;
    
    if (!this.currentAppointment.client_name || !this.currentAppointment.client_phone || this.currentAppointment.services.length === 0) {
      alert("Please fill in client name, phone number, and add at least one service!");
      this.isSaving = false;
      return;
    }

    if (this.selectedClient && this.selectedClient.is_blacklisted) {
      alert("This client is blacklisted and cannot book appointments.");
      this.isSaving = false;
      return;
    }

    if (!this.currentAppointment.center) {
      this.currentAppointment.center = this.selectedCenterId;
    }

    if (this.isEdit) {
      this.apiService.updateAppointment(this.currentAppointment.id, this.currentAppointment).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.showModal = false;
          this.isSaving = false;
          this.toastService.showSuccess('Appointment updated successfully!');
          this.loadAppointments();
        },
        error: (err) => {
          this.toastService.showError(extractErrorMessage(err));
          this.isSaving = false;
        }
      });
    } else {
      this.apiService.createAppointment(this.currentAppointment).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.showModal = false;
          this.isSaving = false;
          this.toastService.showSuccess('Appointment created successfully!');
          this.loadAppointments();
        },
        error: (err) => {
          this.toastService.showError(extractErrorMessage(err));
          this.isSaving = false;
        }
      });
    }
  }

  private setStatus(status: string, done: string) {
    if (this.isEdit && !this.isSaving) {
      this.isSaving = true;
      this.apiService.updateAppointment(this.currentAppointment.id, {status}).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.showModal = false;
          this.toastService.showSuccess(done);
          this.loadAppointments();
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError(extractErrorMessage(err));
          this.cdr.detectChanges();
        }
      });
    }
  }

  cancelAppointment() { this.setStatus('Cancelled', 'Appointment cancelled.'); }

  restoreAppointment() { this.setStatus('Scheduled', 'Appointment restored to scheduled.'); }

  checkInAppointment() { this.setStatus('Checked-in', 'Client checked in.'); }

  markNoShow() { this.setStatus('No-show', 'Marked as no-show.'); }


  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
