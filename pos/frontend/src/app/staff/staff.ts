import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectorRef, Component, DestroyRef, OnInit, OnDestroy, inject, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { ToastService } from '../services/toast.service';
import { CsvService } from '../services/csv.service';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-staff',
  imports: [CommonModule, FormsModule, LocationSelectorComponent],
  templateUrl: './staff.html',
  styleUrl: './staff.css'
})
export class StaffComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  apiService = inject(ApiService);
  toastService = inject(ToastService);
  csvService = inject(CsvService);
  cdr = inject(ChangeDetectorRef);

  isSaving = false;
  isOwner = false;

  /** Salary, commission and perk points: Edit on Staff > Staff Profile. */
  get canEditProfile(): boolean {
    const s = this.permissions?.staff;
    return this.isOwner || s?.profile?.update === true || s?.management?.update === true;
  }

  /** Payroll finalisation ticks on Staff > Payrolls (generate, lock, unlock, mark_paid). */
  canPayroll(action: string): boolean {
    return this.isOwner || this.permissions?.staff?.payrolls?.actions?.[action] === true;
  }
  hasGlobalAccess = false;
  permissions: any = {};
  centers: any[] = [];
  selectedFilterLocation: number | null = null;
  searchQuery = '';
  showInactive = false;

  staffMembers: any[] = [];
  activeStaffMembers: any[] = [];
  filteredStaff: any[] = [];
  selectedStaff: any = null;

  clients: any[] = [];
  services: any[] = [];

  // Selected Staff specific data
  serviceLogs: any[] = [];
  staffConsumptions: any[] = [];
  combinedLogs: any[] = [];
  revenueCollected = 0;
  
  // Modals
  showAddStaffModal = false;
  showAddLogModal = false;
  showTotalRevenueModal = false;
  showUsageModal = false;
  showCommissionModal = false;
  showToolModal = false;
  showToolReportModal = false;
  showTransferReportModal = false;
  showLogPerkModal = false;
  showPerkReportModal = false;

  // Forms
  newStaff: any = { first_name: '', last_name: '', center: null, gender: 'Female', designation: '', joining_date: '', phone: '', email: '', address: '', city: '', state: '', pin_code: '', aadhar_number: '', staff_code: '', app_password: '', salary: 0, commission_percentage: 0, product_commission_percentage: 0, allocated_points: 0 };
  newLog: any = { staff: null, client_name: '', service_name: '', service_type: 'Service', price: null, date: '', time: '' };
  newLogType: 'client' | 'staff' = 'client';
  newConsumptionLog: any = { staff: null, center: null, service_name: '', date: '', time: '', payment_method: 'Points', amount: null };

  // Reports
  revenueReport: any = null;
  usageReport: any = null;
  commissionReport: any[] = [];
  consumptionReport: any[] = [];
  reportStartDate = '';
  reportEndDate = '';
  reportCenterId: number | null = null;

  // UI State
  activeTab = 'profile';
  showReportsDropdown = false;
  showActionsDropdown = false;

  toggleReportsDropdown() {
    this.showReportsDropdown = !this.showReportsDropdown;
    this.showActionsDropdown = false;
  }

  toggleActionsDropdown() {
    this.showActionsDropdown = !this.showActionsDropdown;
    this.showReportsDropdown = false;
  }

  setTab(tab: string) {
    this.activeTab = tab;
    if (tab === 'management' && this.selectedStaff) {
      this.loadStaffTools(this.selectedStaff.id);
      this.loadStaffTransfers(this.selectedStaff.id);
    }
    if (tab === 'payrolls' && this.selectedStaff) {
      this.loadPayrolls();
    }
  }

  
  // Date range for selected staff logs
  logStartDate = '';
  logEndDate = '';
  logDatePreset = 'This Month'; // Just Today, This Month, Last 30 Days, Custom
  staffSummary: any = null;
  summaryView: 'services' | 'months' | 'bills' | 'promotions' = 'services';
  categoryFilter = 'All'; // All, Service, Product, Membership, Package, Value Card
  readonly categoryLabels: Record<string, string> = {
    'Service': 'Services', 'Product': 'Products', 'Membership': 'Memberships',
    'Package': 'Packages', 'Value Card': 'Value Cards'
  };

  get summaryServices(): any[] {
    const rows = this.staffSummary?.by_service || [];
    return this.categoryFilter === 'All' ? rows : rows.filter((r: any) => r.service_type === this.categoryFilter);
  }

  pickCategory(category: string) {
    this.categoryFilter = this.categoryFilter === category ? 'All' : category;
    this.summaryView = 'services';
  }
  reportMonth: string = '';

  // New Modals State
  showTransferModal = false;
  showIncentiveModal = false;

  newTransfer: any = { from_center: null, to_center: null, transfer_type: 'Temporary', start_date: '', end_date: '' };
  newTool: any = { tool_name: '', details: '', amount: 1, date_taken: '', expected_return_date: '' };
  
  incentiveReport: any[] = [];
  targetMultiplier = 5;
  incentivePercent = 5;
  staffTools: any[] = [];
  staffTransfers: any[] = [];
  allToolsReport: any[] = [];
  allTransfersReport: any[] = [];
  activityFeed: any[] = [];
  payrolls: any[] = [];
  designations: any[] = [];



  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.permissions = user.permissions || {};
      } catch (e) {}
    }

    // Default dates
    const today = todayLocal();
    this.logStartDate = today.slice(0, 8) + '01'; // the Performance tab opens on "This Month"
    this.logEndDate = today;
    this.reportStartDate = today;
    this.reportEndDate = today;
    this.newLog.date = today;
    
      this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.centers = data || [];
      if (this.hasGlobalAccess) {
        this.selectedFilterLocation = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true) ?? null;
      } else {
        const userStr = localStorage.getItem('user');
        if (userStr) {
          try {
            const user = JSON.parse(userStr);
            this.selectedFilterLocation = user?.center_id || null;
          } catch (e) {}
        }
        if (this.centers.length > 0 && !this.centers.some(c => c.id == this.selectedFilterLocation)) {
          this.selectedFilterLocation = this.centers[0].id;
        }
      }
      this.loadStaff();
      this.loadClientsAndServices();
      this.loadActivityFeed();
      this.loadDesignations();
    });
  }

  loadActivityFeed() {
    let filter: any = this.selectedFilterLocation;
    if (filter === 'null' || filter === null) {
      filter = undefined;
    }
    this.apiService.getStaffActivityFeed(filter).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.activityFeed = data;
      this.cdr.detectChanges();
    });
  }

  loadDesignations() {
    this.apiService.getDesignations().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => { this.designations = data; },
      error: (err: any) => { console.error('Failed to load designations', err); }
    });
  }
  
  // Designations Modal
  showDesignationsModal = false;
  newDesignation: any = { name: '', salary: 0, commission_percentage: 0, product_commission_percentage: 0 };
  
  openDesignationsModal() {
    this.newDesignation = { name: '', salary: 0, commission_percentage: 0, product_commission_percentage: 0 };
    this.showDesignationsModal = true;
  }
  
  saveDesignation() {
    if (!this.newDesignation.name) return;
    this.apiService.createDesignation(this.newDesignation).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.designations.push(res);
        this.newDesignation = { name: '', salary: 0, commission_percentage: 0, product_commission_percentage: 0 };
        this.toastService.showSuccess('Designation added');
      },
      error: (err) => this.toastService.showError('Failed to add designation')
    });
  }
  
  async deleteDesignation(id: number) {
    if (await this.confirmDialog.ask('Are you sure you want to delete this designation?')) {
      this.apiService.deleteDesignation(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.designations = this.designations.filter(d => d.id !== id);
          this.toastService.showSuccess('Designation deleted');
        },
        error: (err) => this.toastService.showError('Failed to delete designation')
      });
    }
  }

  loadClientsAndServices() {
    let filter: any = this.selectedFilterLocation;
    if (filter === 'null' || filter === null) filter = undefined;

    this.apiService.getClients(undefined, filter).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((res: any) => {
      const data = res && res.results ? res.results : res;
      this.clients = data;
    });

    this.apiService.getServices(filter).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {

      this.services = data;
    });
  }

  onLocationChange() {
    this.loadStaff();
    this.loadActivityFeed();
    this.loadClientsAndServices();
  }

  loadStaff() {
    let filter: any = this.selectedFilterLocation;
    if (filter === 'null' || filter === null) {
      filter = undefined;
    }
    this.apiService.getStaffMembers(filter, true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.staffMembers = data || [];
      this.activeStaffMembers = (data || []).filter((s: any) => s.is_active);
      this.applyFilters();
      if (this.selectedStaff) {
        const updated = this.staffMembers.find(s => s.id === this.selectedStaff.id);
        if (updated) {
          this.selectedStaff = { ...updated };
        } else if (this.filteredStaff.length > 0) {
          this.selectStaff(this.filteredStaff[0]);
        }
      } else if (this.filteredStaff.length > 0) {
        this.selectStaff(this.filteredStaff[0]);
      }
    });
  }

  applyFilters() {
    let list = this.staffMembers;
    if (this.searchQuery) {
      const q = this.searchQuery.toLowerCase();
      list = list.filter(s => 
        (s.first_name && s.first_name.toLowerCase().includes(q)) ||
        (s.last_name && s.last_name.toLowerCase().includes(q)) ||
        (s.phone && s.phone.includes(q)) ||
        (s.designation && s.designation.toLowerCase().includes(q))
      );
    }
    this.filteredStaff = list;
    if (!this.selectedStaff && this.filteredStaff.length > 0) {
      this.selectStaff(this.filteredStaff[0]);
    }
    this.cdr.detectChanges();
  }

  selectStaff(staff: any) {
    this.selectedStaff = { ...staff }; // clone for editing
    this.staffTools = [];
    this.staffTransfers = [];
    
    this.loadStaffLogs();
    
    if (this.activeTab === 'management') {
      this.loadStaffTools(this.selectedStaff.id);
      this.loadStaffTransfers(this.selectedStaff.id);
    } else if (this.activeTab === 'payrolls') {
      this.loadPayrolls();
    }
  }

  getInitials(staff: any): string {
    let i = (staff.first_name || '').charAt(0);
    if (staff.last_name) i += staff.last_name.charAt(0);
    return i.toUpperCase();
  }

  getAvatarBg(staff: any): string {
    if (!staff) return 'linear-gradient(135deg, #0284c7, #2563eb)';
    const name = (staff.first_name || '') + (staff.last_name || '') + (staff.id || '');
    const palettes = [
      'linear-gradient(135deg, #0284c7, #2563eb)', // Blue / Sky
      'linear-gradient(135deg, #7c3aed, #6366f1)', // Purple / Indigo
      'linear-gradient(135deg, #059669, #10b981)', // Emerald / Teal
      'linear-gradient(135deg, #d97706, #f59e0b)', // Amber / Gold
      'linear-gradient(135deg, #db2777, #f43f5e)', // Rose / Pink
      'linear-gradient(135deg, #0891b2, #06b6d4)', // Cyan / Sea
      'linear-gradient(135deg, #ea580c, #f97316)'  // Coral / Orange
    ];
    let hash = 0;
    for (let i = 0; i < name.length; i++) {
      hash = name.charCodeAt(i) + ((hash << 5) - hash);
    }
    const index = Math.abs(hash) % palettes.length;
    return palettes[index];
  }

  // Edit Staff inline
  updateStaffField(field: string, event: any) {
    if (!this.selectedStaff) return;
    const val = event.target.value;
    
    // Auto-fill logic when designation changes
    if (field === 'designation') {
      const selectedDesig = this.designations.find(d => d.name === val);
      if (selectedDesig) {
        this.selectedStaff.salary = selectedDesig.salary;
        this.selectedStaff.commission_percentage = selectedDesig.commission_percentage;
        this.selectedStaff.product_commission_percentage = selectedDesig.product_commission_percentage;
        
        // Save these additionally
        this.apiService.updateStaffMember(this.selectedStaff.id, {
          salary: selectedDesig.salary,
          commission_percentage: selectedDesig.commission_percentage,
          product_commission_percentage: selectedDesig.product_commission_percentage
        }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe();
      }
    }
    
    const payload = { [field]: val };
    
    if (field === 'salary' || field === 'commission_percentage' || field === 'product_commission_percentage') {
      this.selectedStaff[field] = parseFloat(val) || 0;
    } else {
      this.selectedStaff[field] = val;
    }

    this.apiService.updateStaffMember(this.selectedStaff.id, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.selectedStaff[field] = res[field];
        this.loadStaff(); // Refresh list to reflect changes
      },
      error: () => this.toastService.showError('Failed to update field')
    });
  }

  async deleteStaff() {
  if (!this.selectedStaff || this.isSaving) return;
  if (await this.confirmDialog.ask('Are you sure you want to delete this staff member?')) {
    this.isSaving = true;
    this.apiService.deleteStaffMember(this.selectedStaff.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.selectedStaff = null;
        this.loadStaff();
      },
      error: (err) => {
        this.isSaving = false;
        const detail = err?.error?.detail || err?.error?.error || JSON.stringify(err?.error) || 'Unknown error';
        this.toastService.showError('Failed to delete staff member: ' + detail);
      }
    });
  }
  }

  isDownloadingStaffTemplate = false;

  downloadStaffTemplate() {
    if (this.isDownloadingStaffTemplate) return;
    this.isDownloadingStaffTemplate = true;
    this.apiService.downloadStaffTemplate().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (blob: any) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'staff_import_template.xlsx';
        a.click();
        window.URL.revokeObjectURL(url);
        this.isDownloadingStaffTemplate = false;
      },
      error: (err: any) => {
        this.toastService.showError('Failed to download template.');
        this.isDownloadingStaffTemplate = false;
      }
    });
  }

  uploadStaff(event: any) {
    if (!event || !event.target) return;
    const target = event.target as HTMLInputElement;
    const file = target.files?.[0];
    if (!file) return;

    this.isSaving = true;
    this.apiService.uploadFile('staff/api/members/bulk_upload/', file, this.selectedFilterLocation || undefined).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.isSaving = false;
        this.toastService.showSuccess('Staff uploaded successfully.');
        this.loadStaff();
        if (event.target) (event.target as HTMLInputElement).value = '';
      },
      error: (e) => {
        this.isSaving = false;
        const errMsg = e.error?.error || e.error?.detail || e.message || 'Unknown error';
        this.toastService.showError('Failed to upload: ' + errMsg);
        if (event.target) (event.target as HTMLInputElement).value = '';
      }
    });
  }

  onImageUpload(event: any) {
    const file = event.target.files[0];
    if (file && this.selectedStaff) {
      const formData = new FormData();
      formData.append('image', file);
      this.apiService.uploadStaffImage(this.selectedStaff.id, formData).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.selectedStaff.image = res.image;
          this.loadStaff();
        },
        error: () => this.toastService.showError('Failed to upload image')
      });
    }
  }

  toggleActive() {
    if (!this.selectedStaff || this.isSaving) return;
    this.isSaving = true;
    const targetId = this.selectedStaff.id;
    const newStatus = !this.selectedStaff.is_active;
    this.apiService.updateStaffMember(targetId, { is_active: newStatus }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.isSaving = false;
        if (this.selectedStaff && this.selectedStaff.id === targetId) {
          this.selectedStaff.is_active = res.is_active;
        }
        // Sync locally immediately
        const m = this.staffMembers.find(s => s.id === targetId);
        if (m) m.is_active = res.is_active;
        const f = this.filteredStaff.find(s => s.id === targetId);
        if (f) f.is_active = res.is_active;

        this.toastService.showSuccess(res.is_active ? 'Staff member activated' : 'Staff member deactivated');
        this.loadStaff();
      },
      error: (err) => {
        this.isSaving = false;
        const detail = err?.error?.detail || err?.error?.error || JSON.stringify(err?.error) || 'Unknown error';
        this.toastService.showError('Failed to update status: ' + detail);
      }
    });
  }

  // --- Payroll Methods ---
  loadPayrolls() {
    if (!this.selectedStaff) return;
    this.apiService.getPayrolls(this.selectedFilterLocation || undefined).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(res => {
      this.payrolls = res.filter(p => p.staff === this.selectedStaff.id);
    });
  }

  async lockPayroll(id: number) {
    if (this.isSaving) return;
    if (await this.confirmDialog.ask('Are you sure you want to lock this payroll?')) {
      this.isSaving = true;
      this.apiService.lockPayroll(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.loadPayrolls();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.toastService.showError('Failed to lock payroll: ' + (err?.error?.detail || err?.error?.error || 'Unknown error'));
        }
      });
    }
  }

  async markPayrollPaid(id: number) {
    if (this.isSaving) return;
    if (await this.confirmDialog.ask('Are you sure you want to mark this payroll as paid?')) {
      this.isSaving = true;
      this.apiService.markPayrollPaid(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.loadPayrolls();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.toastService.showError('Failed to mark payroll as paid: ' + (err?.error?.detail || err?.error?.error || 'Unknown error'));
        }
      });
    }
  }

  // Logs
  setLogPreset(preset: string) {
    this.logDatePreset = preset;
    if (preset === 'Custom') return;
    const today = todayLocal();
    this.logEndDate = today;
    if (preset === 'Just Today') {
      this.logStartDate = today;
    } else if (preset === 'This Month') {
      this.logStartDate = today.slice(0, 8) + '01';
    } else if (preset === 'Last 30 Days') {
      const d = new Date(today + 'T00:00:00');
      d.setDate(d.getDate() - 29);
      this.logStartDate = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    }
    this.loadStaffLogs();
  }

  formatMonth(key: string): string {
    const [y, m] = key.split('-').map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString('en-IN', { month: 'short', year: 'numeric' });
  }

  loadStaffLogs() {
    if (!this.selectedStaff) return;
    
    // Reset arrays
    this.serviceLogs = [];
    this.staffConsumptions = [];
    this.combinedLogs = [];
    this.staffSummary = null;

    const staffId = this.selectedStaff.id;
    this.apiService.getStaffSummary(staffId, this.logStartDate, this.logEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: data => {
        if (this.selectedStaff?.id !== staffId) return; // a newer selection won
        this.staffSummary = data;
        this.cdr.detectChanges();
      },
      error: () => { this.staffSummary = null; }
    });

    let loadedServices = false;
    let loadedConsumptions = false;

    const combineAndSort = () => {
        if (loadedServices && loadedConsumptions) {
            const mappedServices = this.serviceLogs.map(log => ({
                ...log,
                is_perk: false
            }));
            const mappedConsumptions = this.staffConsumptions.map(log => ({
                ...log,
                is_perk: true,
                price: log.amount,
                service_type: 'Staff Perk'
            }));

            this.combinedLogs = [...mappedServices, ...mappedConsumptions].sort((a, b) => {
                const dateA = new Date(a.date + 'T' + (a.time || '00:00:00'));
                const dateB = new Date(b.date + 'T' + (b.time || '00:00:00'));
                return dateB.getTime() - dateA.getTime();
            });
            this.cdr.detectChanges();
        }
    };

    this.apiService.getServiceLogs(this.selectedStaff.id, this.logStartDate, this.logEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.serviceLogs = data;
      this.revenueCollected = data.reduce((sum: number, item: any) => sum + Number(item.price), 0);
      loadedServices = true;
      combineAndSort();
    });

    this.apiService.getStaffConsumptions(this.selectedStaff.id, this.logStartDate, this.logEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.staffConsumptions = data;
      loadedConsumptions = true;
      combineAndSort();
    });
  }

  // Add Staff Modal
  openAddStaff() {
    this.newStaff = { 
      first_name: '', last_name: '', center: this.selectedFilterLocation || null, gender: 'Female', 
      designation: '', joining_date: '', phone: '', email: '', address: '', city: '', state: '', pin_code: '', aadhar_number: '', staff_code: '', 
      app_password: '', salary: 0, commission_percentage: 0, 
      product_commission_percentage: 0, allocated_points: 0 
    };
    this.showAddStaffModal = true;
  }
  
  onNewStaffDesignationChange(val: string) {
    const selectedDesig = this.designations.find(d => d.name === val);
    if (selectedDesig) {
      this.newStaff.salary = selectedDesig.salary;
      this.newStaff.commission_percentage = selectedDesig.commission_percentage;
      this.newStaff.product_commission_percentage = selectedDesig.product_commission_percentage;
    }
  }
  
  saveNewStaff() {
    const payload = { ...this.newStaff };
    if (!payload.first_name || !payload.designation || !payload.center) {
      this.toastService.showError('First Name, Designation, and Center are required');
      return;
    }
    if (!payload.joining_date) {
      payload.joining_date = null;
    }
    
    this.isSaving = true;
    this.apiService.createStaffMember(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.showAddStaffModal = false;
        this.toastService.showSuccess('Staff member added.');
        this.loadStaff();
      },
      error: (err) => { this.isSaving = false; this.toastService.showError('Failed to create staff: ' + JSON.stringify(err.error)); }
    });
  }

  // Add Log Modal
  openAddLog() {
    this.newLogType = 'client';
    const today = todayLocal();
    this.newLog = { staff: this.selectedStaff ? this.selectedStaff.id : null, client_name: '', service_name: '', service_type: 'Service', price: null, date: today, time: '' };
    this.newConsumptionLog = { staff: this.selectedStaff ? this.selectedStaff.id : null, center: null, service_name: '', date: today, time: '', payment_method: 'Points', amount: null };
    this.showAddLogModal = true;
  }

  onServiceSelected(type: string) {
    if (type === 'client') {
       const service = this.services.find(s => s.name === this.newLog.service_name);
       if (service) {
           this.newLog.price = service.default_price || service.price || 0;
       }
    } else if (type === 'staff') {
       const service = this.services.find(s => s.name === this.newConsumptionLog.service_name);
       if (service) {
           this.newConsumptionLog.amount = service.default_price || service.price || 0;
       }
    }
  }

  saveNewLog() {
    if (this.newLogType === 'client') {
      if (!this.newLog.staff || !this.newLog.client_name || !this.newLog.service_name || !this.newLog.price || !this.newLog.date || !this.newLog.time) {
        this.toastService.showError('Please fill all required fields for the service log');
        return;
      }
      
      const staff = this.staffMembers.find(s => s.id == this.newLog.staff);
      if (!staff) return;
      this.newLog.center = staff.center;

      this.isSaving = true;
      this.apiService.createServiceLog(this.newLog).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.showAddLogModal = false;
          if (this.selectedStaff && this.selectedStaff.id == this.newLog.staff) {
            this.loadStaffLogs();
          }
          this.loadActivityFeed();
        },
        error: () => { this.isSaving = false; this.toastService.showError('Failed to save log.'); }
      });
    } else {
      // Save Consumption
      if (!this.newConsumptionLog.service_name || !this.newConsumptionLog.amount || !this.newConsumptionLog.date || !this.newConsumptionLog.time) {
        this.toastService.showError('Please fill all required fields for perk consumption');
        return;
      }

      const staff = this.staffMembers.find(s => s.id == this.newConsumptionLog.staff);
      if (!staff) return;
      this.newConsumptionLog.center = staff.center;

      this.isSaving = true;
      this.apiService.createStaffConsumption(this.newConsumptionLog).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.showAddLogModal = false;
          this.loadActivityFeed();
          if (this.selectedStaff && this.selectedStaff.id == this.newConsumptionLog.staff) {
            this.loadStaffLogs();
            this.loadStaff(); // This will refresh the list and we'll update selectedStaff inside loadStaff
          }
        },
        error: (err) => {
          this.isSaving = false;
          const msg = err?.error?.error || err?.error?.detail || 'Failed to save consumption log.';
          this.toastService.showError(msg);
        }
      });
    }
  }

  getSelectedStaffPoints(): number {
    if (this.selectedStaff) {
      return this.selectedStaff.allocated_points;
    }
    if (this.newConsumptionLog.staff) {
      const staff = this.staffMembers.find(s => s.id === this.newConsumptionLog.staff);
      return staff ? staff.allocated_points : 0;
    }
    return 0;
  }



  // Reports
  openTotalRevenue() {
    this.showTotalRevenueModal = true;
    this.loadRevenueReport();
  }

  loadRevenueReport() {
    let cid: any = this.reportCenterId;
    if (cid === 'null' || cid === null) cid = undefined;
    
    this.apiService.getRevenueReport(cid, this.reportStartDate, this.reportEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.revenueReport = data;
      this.cdr.detectChanges();
    });
  }

  openUsage() {
    this.showUsageModal = true;
    this.loadUsageReport();
  }

  openCommission() {
    this.showCommissionModal = true;
    this.commissionReport = [];
    this.loadCommissionReport();
  }

  openToolReport() {
    this.showToolReportModal = true;
    this.allToolsReport = [];
    // Load ALL tools across the system, no date or specific staff logic
    this.apiService.getStaffTools().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.allToolsReport = data;
      this.cdr.detectChanges();
    });
  }

  isToolOverdue(tool: any): boolean {
    if (tool.status === 'Returned' || !tool.expected_return_date) return false;
    const returnDate = new Date(tool.expected_return_date);
    const today = new Date();
    today.setHours(0, 0, 0, 0);
    return returnDate < today;
  }

  openTransferReport() {
    this.showTransferReportModal = true;
    this.allTransfersReport = [];
    this.apiService.getStaffTransfers().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.allTransfersReport = data;
      this.cdr.detectChanges();
    });
  }

  loadUsageReport() {
    this.apiService.getUsageReport(this.reportStartDate, this.reportEndDate, this.reportCenterId || undefined)
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
        this.usageReport = data;
        this.cdr.detectChanges();
      });
  }

  loadCommissionReport() {
    this.apiService.getCommissionReport(this.reportStartDate, this.reportEndDate)
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
        this.commissionReport = data;
        this.cdr.detectChanges();
      });
  }

  openPerkReport() {
    this.showPerkReportModal = true;
    this.consumptionReport = [];
    this.loadPerkReport();
  }

  loadPerkReport() {
    let cid: any = this.reportCenterId;
    if (cid === 'null' || cid === null) cid = undefined;
    
    this.apiService.getStaffConsumptionReport(cid, this.reportStartDate, this.reportEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.consumptionReport = data;
      this.cdr.detectChanges();
    });
  }

  // --- New Features ---
  openTransferModal() {
    this.newTransfer = { staff: this.selectedStaff ? this.selectedStaff.id : null, from_center: this.selectedStaff ? this.selectedStaff.center : null, to_center: null, transfer_type: 'Temporary', start_date: todayLocal(), end_date: '' };
    this.showTransferModal = true;
  }
  
  saveTransfer() {
    if (!this.newTransfer.staff) { this.toastService.showError('Please select a staff member'); return; }
    if (!this.newTransfer.to_center) { this.toastService.showError('Please select a destination center'); return; }
    
    const staff = this.staffMembers.find(s => s.id == this.newTransfer.staff);
    if (!staff) return;
    if (!this.newTransfer.from_center) this.newTransfer.from_center = staff.center;

    const payload = { ...this.newTransfer };
    if (!payload.end_date) payload.end_date = null;
    
    this.isSaving = true;
    this.apiService.createStaffTransfer(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
         const updatePayload: any = { center: payload.to_center };
         this.apiService.updateStaffMember(payload.staff, updatePayload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((updatedStaff) => {
             this.isSaving = false;
             this.showTransferModal = false;
             if (this.selectedStaff && this.selectedStaff.id == payload.staff) {
                 this.selectedStaff.center = updatedStaff.center;
                 this.selectedStaff.center_name = updatedStaff.center_name;
             }
             this.loadStaff();
             if (this.selectedStaff) this.loadStaffTransfers(this.selectedStaff.id);
         });
      },
      error: () => { this.isSaving = false; this.toastService.showError('Failed to create transfer'); }
    });
  }

  openToolModal() {
    this.newTool = { staff: this.selectedStaff ? this.selectedStaff.id : null, tool_name: '', details: '', amount: 1, date_taken: todayLocal(), expected_return_date: '' };
    if (this.selectedStaff) this.loadStaffTools(this.selectedStaff.id);
    else this.staffTools = [];
    this.showToolModal = true;
  }

  loadStaffTools(staffId?: number) {
    const id = staffId || (this.selectedStaff ? this.selectedStaff.id : null);
    if (!id) return;
    this.apiService.getStaffTools(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.staffTools = data;
      this.cdr.detectChanges();
    });
  }
  
  loadStaffTransfers(staffId?: number) {
    const id = staffId || (this.selectedStaff ? this.selectedStaff.id : null);
    if (!id) return;
    this.apiService.getStaffTransfers(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.staffTransfers = data;
      this.cdr.detectChanges();
    });
  }

  saveTool() {
    if (!this.newTool.staff) { this.toastService.showError('Please select a staff member'); return; }
    if (!this.newTool.tool_name) { this.toastService.showError('Tool name required'); return; }
    
    const payload = { ...this.newTool };
    if (!payload.expected_return_date) payload.expected_return_date = null;
    
    this.isSaving = true;
    this.apiService.createStaffTool(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
         this.isSaving = false;
         if (this.selectedStaff && this.selectedStaff.id == payload.staff) {
             this.loadStaffTools(this.selectedStaff.id);
         }
         this.newTool = { staff: this.newTool.staff, tool_name: '', details: '', amount: 1, date_taken: todayLocal(), expected_return_date: '' };
         // We can choose to close the modal here or leave it open
      },
      error: () => { this.isSaving = false; this.toastService.showError('Failed to save tool'); }
    });
  }

  markToolReturned(tool: any) {
    if (this.isSaving) return;
    this.isSaving = true;
    this.apiService.updateStaffTool(tool.id, { status: 'Returned', actual_return_date: todayLocal() }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.loadStaffTools();
      },
      error: () => {
        this.isSaving = false;
        this.toastService.showError('Failed to update tool');
      }
    });
  }

  openIncentiveModal() {
    this.showIncentiveModal = true;
    this.incentiveReport = [];
    this.loadIncentiveReport();
  }

  loadIncentiveReport() {
    if (this.reportMonth) {
       const [year, month] = this.reportMonth.split('-');
       const y = parseInt(year);
       const m = parseInt(month);
       // Start date is always 01
       const startDate = `${year}-${month}-01`;
       
       // Get last day of the month
       const lastDay = new Date(y, m, 0).getDate();
       const endDate = `${year}-${month}-${lastDay.toString().padStart(2, '0')}`;
       
       this.apiService.getIncentiveReport(startDate, endDate, undefined).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
           this.incentiveReport = data;
           this.cdr.detectChanges();
       });
    } else {
       // Optional: clear report if no month selected
    }
  }

  updateStaffSalary(staffId: number, newSalary: any) {
    if (this.isSaving) return;
    this.isSaving = true;
    this.apiService.updateStaffMember(staffId, { salary: newSalary }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        // reload the incentive report to recalculate based on new salary
        this.loadIncentiveReport();
      },
      error: () => {
        this.isSaving = false;
        this.toastService.showError('Failed to update salary');
      }
    });
  }

  exportIncentivesExcel() {
    if (!this.incentiveReport || this.incentiveReport.length === 0) {
      alert("No data to export");
      return;
    }
    const headers = ["Staff Name", "Location", "Salary", "Total Sales", "Multiplier", "Incentive %", "Incentive Amount"];
    const rows = this.incentiveReport.map((row: any) => [
      row.staff_name || '',
      row.center_name || '',
      row.salary || 0,
      row.total_sales || 0,
      row.multiplier || 0,
      row.incentive_percentage || 0,
      row.incentive_amount || 0
    ]);
    this.csvService.exportToCsv(`Staff_Incentive_Report_${this.reportMonth}.csv`, headers, rows);
  }

  exportRevenueExcel() {
    if (!this.revenueReport?.breakdown || this.revenueReport.breakdown.length === 0) return;
    const headers = ['STAFF', 'LOCATION', 'SERVICES', 'REVENUE'];
    const rows = this.revenueReport.breakdown.map((item: any) => [
      item.staff_name || '', item.location || '', item.services || 0, item.revenue || 0
    ]);
    this.csvService.exportToCsv('Total_Revenue_Report.csv', headers, rows);
  }

  exportUsageExcel() {
    if (!this.usageReport?.breakdown || this.usageReport.breakdown.length === 0) return;
    const headers = ['ITEM', 'TYPE', 'TIMES USED', 'REVENUE'];
    const rows = this.usageReport.breakdown.map((item: any) => [
      item.service_name || '', item.service_type || '', item.times_used || 0, item.revenue || 0
    ]);
    this.csvService.exportToCsv('Usage_Report.csv', headers, rows);
  }

  exportCommissionExcel() {
    if (!this.commissionReport || this.commissionReport.length === 0) return;
    const headers = ['STAFF', 'CENTER', 'SERVICE REV', 'PRODUCT REV', 'COMMISSION'];
    const rows = this.commissionReport.map((item: any) => [
      item.staff_name || '', item.center_name || '', item.service_revenue || 0, item.product_revenue || 0, item.total_commission || 0
    ]);
    this.csvService.exportToCsv('Payroll_Commission_Report.csv', headers, rows);
  }

  exportPerkExcel() {
    if (!this.consumptionReport || this.consumptionReport.length === 0) return;
    const headers = ['STAFF', 'CENTER', 'SERVICE', 'PAYMENT METHOD', 'TIMES USED', 'TOTAL SPENT'];
    const rows = this.consumptionReport.map((item: any) => [
      item.staff_name || '', item.center_name || '', item.service_name || '', item.payment_method === 'Points' ? 'Allocated Points' : 'Own Money', item.times_used || 0, item.total_amount || 0
    ]);
    this.csvService.exportToCsv('Staff_Perk_Consumption_Report.csv', headers, rows);
  }

  exportToolsExcel() {
    if (!this.allToolsReport || this.allToolsReport.length === 0) return;
    const headers = ['STAFF', 'CENTER', 'ITEM', 'DETAILS', 'QTY', 'TAKEN ON', 'RETURN BY', 'STATUS'];
    const rows = this.allToolsReport.map((t: any) => [
      t.staff_name || '', t.staff_center_name || 'N/A', t.tool_name || '', t.details || '-', t.amount || 0,
      t.date_taken ? t.date_taken.split('T')[0] : '', t.expected_return_date ? t.expected_return_date.split('T')[0] : 'N/A', t.status || ''
    ]);
    this.csvService.exportToCsv('Tool_Tracking_Report.csv', headers, rows);
  }

  exportTransfersExcel() {
    if (!this.allTransfersReport || this.allTransfersReport.length === 0) return;
    const headers = ['STAFF', 'FROM', 'TO', 'TYPE', 'START DATE', 'END DATE', 'STATUS'];
    const rows = this.allTransfersReport.map((tr: any) => [
      tr.staff_name || 'Staff #' + tr.staff, tr.from_center_name || '', tr.to_center_name || '', tr.transfer_type || '',
      tr.start_date ? tr.start_date.split('T')[0] : '', tr.end_date ? tr.end_date.split('T')[0] : '-', tr.status || ''
    ]);
    this.csvService.exportToCsv('Staff_Transfers_Report.csv', headers, rows);
  }



  exportExcel() {
    const headers = ['First Name', 'Last Name', 'Designation', 'Gender', 'Phone', 'Center', 'Joining Date', 'Aadhar', 'Status', 'Salary', 'Comm. %', 'Prod. Comm. %'];
    const rows = this.staffMembers.map(s => [
      s.first_name || '',
      s.last_name || '',
      s.designation || '',
      s.gender || '',
      s.phone || '',
      s.center_name || '',
      s.joining_date || '',
      s.aadhar_number || '',
      s.is_active ? 'Active' : 'Inactive',
      s.salary || 0,
      s.commission_percentage || 0,
      s.product_commission_percentage || 0
    ]);
    this.csvService.exportToCsv('Staff_Report', headers, rows);
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
