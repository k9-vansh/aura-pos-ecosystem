import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { CsvService } from '../services/csv.service';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-clients',
  standalone: true,
  imports: [CommonModule, FormsModule],
  templateUrl: './clients.html',
  styleUrls: ['./clients.css']
})
export class ClientsComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  apiService = inject(ApiService);
  csvService = inject(CsvService);
  cdr = inject(ChangeDetectorRef);

  searchPhone: string = '';
  client: any = null;
  clients: any[] = [];
  centers: any[] = [];
  selectedCenterId: number | null = null;
  invoices: any[] = [];
  advances: any[] = [];
  serviceLogs: any[] = [];
  isSaving: boolean = false;
  currentPage: number = 1;
  totalPages: number = 1;
  totalRecords: number = 0;
  searchTimeout: any;
  
  activeTab: 'profile' | 'perks' | 'notes' = 'profile';
  timelineFilter: 'all' | 'invoices' | 'advances' | 'services' = 'all';
  advanceBalance: number = 0;

  showCarryOverModal = false;
  carryOverSource: any = null;
  carryOverType: 'package' | 'value_card' | 'membership' = 'package';
  carryOverTargetId: number | null = null;
  carryOverNewExpiry: string = '';

  isOwner = false;
  /** A ticked action under Admin > Clients (blacklist, merge, bulk_upload); the server enforces it too. */
  canClientAction(action: string): boolean {
    return this.isOwner || this.permissions?.admin?.clients?.actions?.[action] === true;
  }

  showDuplicates = false;
  duplicatesLoading = false;
  duplicates: any[] = [];
  mergingId: number | null = null;

  toggleDuplicates() {
    this.showDuplicates = !this.showDuplicates;
    if (this.showDuplicates) this.loadDuplicates();
  }

  loadDuplicates() {
    this.duplicatesLoading = true;
    this.apiService.getClientDuplicates().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (rows) => { this.duplicates = rows || []; this.duplicatesLoading = false; this.cdr.detectChanges(); },
      error: () => { this.duplicates = []; this.duplicatesLoading = false; this.cdr.detectChanges(); }
    });
  }

  async mergeDuplicate(keep: any, dup: any) {
    if (!keep || !await this.confirmDialog.ask(`Merge client #${dup.id} into #${keep.id}? Bills, perks and wallets move to #${keep.id} and #${dup.id} is archived.`)) return;
    this.mergingId = dup.id;
    this.apiService.mergeClient(keep.id, dup.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.mergingId = null; this.loadDuplicates(); this.loadClients(this.currentPage); },
      error: (e) => {
        this.mergingId = null;
        this.cdr.detectChanges();
        alert('Merge failed: ' + (e.error?.detail || e.error?.from_id || e.message));
      }
    });
  }
  hasGlobalAccess = false;
  permissions: any = {};

  ngOnInit(): void {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
      } catch (e) {}
    }

    this.loadCenters();
    // listen for clients update from other components/tabs
    window.addEventListener('storage', (e: StorageEvent) => {
      if (e.key === 'clients_updated') this.loadClients();
    });
    // in-page event for same SPA
    window.addEventListener('clients_updated', () => { this.loadClients(); });
  }

  loadCenters() {
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any[]) => {
      this.centers = data || [];
      // Only auto-select the first center if not an owner (owners default to 'All Locations' = null)
      if (!this.selectedCenterId) {
        const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
        if (remembered !== undefined) this.selectedCenterId = remembered;
      }
      if (this.centers.length > 0 && !this.selectedCenterId && !this.isOwner) {
        this.selectedCenterId = this.centers[0].id;
      }
      this.loadClients();
    });
  }

  loadClients(page: number = 1) {
    this.currentPage = page;
    const centerFilter = this.selectedCenterId || undefined;
    this.apiService.getClients(this.searchPhone, centerFilter, this.currentPage).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      if (data && data.results) {
        this.clients = data.results || [];
        this.totalRecords = data.count || 0;
        this.totalPages = Math.ceil(this.totalRecords / 50) || 1;
      } else {
        this.clients = data || [];
        this.totalRecords = this.clients.length;
        this.totalPages = 1;
      }
      
      // If we paginated or the list refreshed, ensure the selected client is in the current view
      // If not, auto-select the first client in the new list to avoid confusion.
      if (this.clients.length > 0) {
        const stillExists = this.clients.find(c => c.id === this.client?.id);
        if (!stillExists) {
          this.selectClient(this.clients[0]);
        }
      } else {
        this.client = null;
      }

      this.cdr.detectChanges();
    });
  }

  nextPage() {
    if (this.currentPage < this.totalPages) this.loadClients(this.currentPage + 1);
  }

  prevPage() {
    if (this.currentPage > 1) this.loadClients(this.currentPage - 1);
  }

  onCenterChange() {
    this.loadClients(1);
  }

  searchClient() {
    if (this.searchTimeout) clearTimeout(this.searchTimeout);
    this.searchTimeout = setTimeout(() => {
      this.loadClients(1);
    }, 300);
  }

  selectClient(c: any) {
    this.client = { ...c };
    // Ensure `center` is the PK so the form select binds to it
    if (c.center) {
      this.client.center = c.center;
    } else if (c.center_detail) {
      this.client.center = c.center_detail.id;
    } else {
      this.client.center = null;
    }
    // Load invoice, advance, and service log history for this client
    if (this.client.id) {
      // History is a convenience: if it cannot be read, show none rather than fail.
      this.apiService.getInvoices(this.client.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (d: any[]) => { this.invoices = d || []; this.cdr.detectChanges(); },
        error: () => { this.invoices = []; },
      });

      this.apiService.getAdvances(this.client.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (d: any[]) => {
          this.advances = d || [];
          this.advanceBalance = this.advances.reduce((sum, a) => sum + (+a.amount), 0);
          this.cdr.detectChanges();
        },
        error: () => { this.advances = []; this.advanceBalance = 0; },
      });

      this.apiService.getClientServiceHistory(this.client.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
        this.serviceLogs = d || [];
        this.cdr.detectChanges();
      });

      this.apiService.getClientProfile(this.client.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((prof: any) => {
        this.client.ltv = prof.ltv;
        this.client.last_visit = prof.last_visit;
        this.cdr.detectChanges();
      });

    }
  }

  
  getTotalPerks(): number {
    if (!this.client) return 0;
    return (this.client.active_memberships?.length || 0) + 
           (this.client.active_packages?.length || 0) + 
           (this.client.active_value_cards?.length || 0);
  }

  getTotalSpend(): number {
    return this.invoices.reduce((sum, inv) => sum + ((inv.status !== 'cancelled' && inv.status !== 'refunded') ? +inv.total_amount : 0), 0);
  }

  getTimelineEmpty(): boolean {
    if (this.timelineFilter === 'all') return this.invoices.length === 0 && this.advances.length === 0 && this.serviceLogs.length === 0;
    if (this.timelineFilter === 'invoices') return this.invoices.length === 0;
    if (this.timelineFilter === 'advances') return this.advances.length === 0;
    if (this.timelineFilter === 'services') return this.serviceLogs.length === 0;
    return true;
  }

  isExpired(expiryDate: string | null | undefined): boolean {
    if (!expiryDate) return false;
    return new Date(expiryDate) < new Date();
  }

  getPackageServiceKeys(p: any): string[] {
    return Object.keys(p.services_remaining || {});
  }

  openCarryOver(type: 'package' | 'value_card' | 'membership', item: any) {
    this.carryOverType = type;
    this.carryOverSource = item;
    this.carryOverTargetId = null;
    this.carryOverNewExpiry = '';
    this.showCarryOverModal = true;
  }

  submitCarryOver() {
    if (!this.carryOverSource) return;
    const payload: any = {
      source_type: this.carryOverType,
      source_id: this.carryOverSource.id,
      target_id: this.carryOverTargetId,
      new_expiry: this.carryOverNewExpiry
    };
    this.apiService.carryOverClientPerk(this.client.id, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        alert("Carry Over Successful!");
        this.showCarryOverModal = false;
        this.selectClient(this.client);
      },
      error: (err: any) => {
        alert("Failed to carry over: " + JSON.stringify(err.error || err.message));
      }
    });
  }

  getServiceNameFromPackage(p: any, svcId: string): string {
    if (!p || !p.package_detail || !p.package_detail.services_json) return 'Service #' + svcId;
    const items = p.package_detail.services_json;
    if (Array.isArray(items)) {
      const match = items.find(it => it.service?.toString() === svcId);
      if (match && match.service_name) return match.service_name;
    }
    return 'Service #' + svcId;
  }

  getMembershipProgress(m: any): number {
    if (!m.expiry_date || !m.membership_detail?.expiry_days) return 100;
    const start = new Date(m.created_at || m.expiry_date).getTime(); // Fallback if created_at missing
    const end = new Date(m.expiry_date).getTime();
    const now = new Date().getTime();
    if (now >= end) return 100;
    // Simple approx
    return 100; // Will refine later if needed
  }

  getCardProgress(v: any): number {
    if (!v.balance || !v.value_card_detail?.value) return 0;
    const pct = (+v.balance / +v.value_card_detail.value) * 100;
    return Math.min(Math.max(pct, 0), 100);
  }

  newClient() {
    this.invoices = [];
    this.advances = [];
    this.serviceLogs = [];
    this.client = {
      phone: this.searchPhone || '',
      first_name: '',
      last_name: '',
      email: '',
      birthday: '',
      gst_number: '',
      address: '',
      pin_code: '',
      notes: '',
      dnd_status: 'NOT ON DND',
      whatsapp_consent: true,
      sms_consent: true,
      center: this.selectedCenterId || null
    };
  }

  saveClient() {
    if (!this.client || this.isSaving) return;

    // Basic client-side validation
    if (!this.client.phone || String(this.client.phone).trim() === '') {
      alert('Phone is required');
      return;
    }
    if (!this.client.first_name || String(this.client.first_name).trim() === '') {
      alert('First name is required');
      return;
    }

    this.isSaving = true;

    const makeErrMsg = (err: any) => {
      console.error('API error', err);
      if (!err) return 'Unknown error';
      if (err.error) {
        try {
          return typeof err.error === 'string' ? err.error : JSON.stringify(err.error);
        } catch (e) {
          return String(err.error);
        }
      }
      return err.message || JSON.stringify(err);
    };

    if (this.client.id) {
      this.apiService.updateClient(this.client.id, this.client).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          alert('Client updated!');
          this.isSaving = false;
          this.loadClients();
        },
        error: (err) => {
          alert('Failed to update client: ' + makeErrMsg(err));
          this.isSaving = false;
        }
      });
    } else {
      // ensure center is set for new client
      this.client.center = this.client.center || this.selectedCenterId || null;
      this.apiService.createClient(this.client).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.client = res;
          alert('Client created!');
          this.isSaving = false;
          this.loadClients();
        },
        error: (err) => {
          alert('Failed to create client: ' + makeErrMsg(err));
          this.isSaving = false;
        }
      });
    }
  }

  uploadClients(event: any) {
    const file = event.target.files[0];
    if (!file) return;
    this.isSaving = true;
    this.apiService.uploadFile('clients/api/clients/bulk_upload/', file).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.isSaving = false;
        alert(res.message || 'File uploaded successfully');
        this.loadClients();
        event.target.value = '';
      },
      error: (e) => {
        this.isSaving = false;
        alert('Failed to upload file: ' + (e.error?.error || e.message));
        event.target.value = '';
      }
    });
  }

  downloadTemplate() {
    const headers = ['phone', 'first_name', 'last_name', 'email', 'birthday', 'gender', 'gst_number', 'notes'];
    this.csvService.exportToCsv('clients_template.csv', headers, []);
  }

  exportExcel() {
    const headers = ['Phone', 'First Name', 'Last Name', 'Email', 'Birthday', 'Gender', 'GST Number', 'Center', 'DND Status', 'Blacklisted', 'Blacklist Reason'];
    const rows = this.clients.map(c => [
      c.phone || '',
      c.first_name || '',
      c.last_name || '',
      c.email || '',
      c.birthday || '',
      c.gender || '',
      c.gst_number || '',
      c.center_detail ? (c.center_detail.display_name || c.center_detail.name) : '',
      c.dnd_status || '',
      c.is_blacklisted ? 'Yes' : 'No',
      c.blacklist_reason || ''
    ]);
    this.csvService.exportToCsv('Clients_Report', headers, rows);
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
