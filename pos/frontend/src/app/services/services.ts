import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { ToastService } from '../services/toast.service';
import { CsvService } from '../services/csv.service';
import { AdminFilterService } from '../admin/admin-filter.service';
import { todayLocal } from './date-util';
import { ServiceRecipeComponent } from './service-recipe.component';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-services',
  standalone: true,
  imports: [CommonModule, FormsModule, ServiceRecipeComponent],
  templateUrl: './services.html',
  styleUrls: ['./services.css'],
})
export class ServicesComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  toastService = inject(ToastService);

  api = inject(ApiService);
  csvService = inject(CsvService);
  cdr = inject(ChangeDetectorRef);
  adminFilterService = inject(AdminFilterService);
  centers: any[] = [];
  selectedCenter: number | null = null;
  services: any[] = [];
  currentPage: number = 1;
  pageSize: number = 500;

  // What the table shows: the services, searched and sorted. Rebuilt when the list, the search or
  // the sort changes, not on every screen refresh.
  searchTerm = '';
  sortKey = 'name';
  sortDir: 'asc' | 'desc' = 'asc';
  visibleServices: any[] = [];

  applyView() {
    const term = this.searchTerm.trim().toLowerCase();
    const found = term
      ? this.services.filter(s => [s.name, s.service_code, s.category, s.sub_category, s.brand, s.hsn_code, s.sac_code]
          .some(v => (v || '').toString().toLowerCase().includes(term)))
      : [...this.services];
    const numeric = this.sortKey === 'default_price' || this.sortKey === 'tax_percentage';
    const sign = this.sortDir === 'asc' ? 1 : -1;
    found.sort((a, b) => {
      if (numeric) return sign * ((Number(a[this.sortKey]) || 0) - (Number(b[this.sortKey]) || 0));
      return sign * (a[this.sortKey] || '').toString().localeCompare((b[this.sortKey] || '').toString(), undefined, { sensitivity: 'base', numeric: true });
    });
    this.visibleServices = found;
    if (this.currentPage > this.totalPages) this.currentPage = 1;
  }

  onSearch() {
    this.currentPage = 1;
    this.applyView();
  }

  sortBy(key: string) {
    if (this.sortKey === key) this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
    else { this.sortKey = key; this.sortDir = 'asc'; }
    this.applyView();
  }

  get paginatedServices() {
    const startIndex = (this.currentPage - 1) * this.pageSize;
    return this.visibleServices.slice(startIndex, startIndex + this.pageSize);
  }

  get totalPages() {
    return Math.ceil(this.visibleServices.length / this.pageSize) || 1;
  }

  nextPage() {
    if (this.currentPage < this.totalPages) {
      this.currentPage++;
    }
  }

  prevPage() {
    if (this.currentPage > 1) {
      this.currentPage--;
    }
  }
  showAddModal: boolean = false;
  isEditing = false;
  editingId: number | null = null;
  newService: any = {
    service_code: '',
    name: '',
    brand: '',
    category: '',
    sub_category: '',
    sac_code: '',
    hsn_code: '',
    default_price: 0,
    tax_percentage: 5,
    duration_mins: 0,
    level: 'Organisation',
    centers: []
  };

  isOwner = false;
  hasGlobalAccess = false;
  permissions: any = {};
  isSaving = false;

  // The recipe (what the service uses) is edited in its own dialog. It is the same for every
  // branch, so changing it needs an all-branches role with Services > Edit.
  recipeFor: any = null;
  get canEditRecipe(): boolean {
    return this.hasGlobalAccess && (this.isOwner || this.permissions.admin?.services?.update === true);
  }

  openRecipe(s: any) { this.recipeFor = s; }

  recipeClosed(saved: boolean) {
    this.recipeFor = null;
    if (saved) {
      this.toastService.showSuccess('Recipe saved');
      this.loadServices();
    }
    this.cdr.markForCheck();
  }

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

    this.api.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.centers = data || [];

      this.loadServices();
    });

    // Another centre was picked in the location dropdown: show that centre's prices. This page never
    // listened, so it went on showing the first centre's prices under the new centre's name.
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => this.loadServices());
  }

  loadServices() {
    const cid = this.adminFilterService.currentCenterId ?? undefined;
    this.api.getServices(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.services = data || [];
        this.currentPage = 1;
      this.applyView();
      this.cdr.detectChanges();
    });
  }

  saveOverride(s: any) {
    if (!this.adminFilterService.currentCenterId) {
      this.toastService.showError('Please select a center first');
      return;
    }
    const price = s.center_override ? s.center_override.price : s.default_price;
    const isActive = s.center_override ? s.center_override.is_active : true;
    this.api.overrideCenterService(this.adminFilterService.currentCenterId, s.id, price, isActive).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.toastService.showSuccess('Override saved');
        this.loadServices();
      },
      error: (err) => {
        this.toastService.showError('Could not save the price: ' + this.why(err));
        this.loadServices();
      }
    });
  }

  openAddModal() {
    this.showAddModal = true;
    this.isEditing = false;
    this.editingId = null;
    this.newService = { service_code: '', name: '', brand: '', category: '', sub_category: '', sac_code: '', hsn_code: '', default_price: 0, tax_percentage: 5, duration_mins: 0, level: 'Organisation', centers: [] };
  }

  editService(s: any) {
    this.showAddModal = true;
    this.isEditing = true;
    this.editingId = s.id;
    this.newService = {
      ...s,
      // With a branch chosen the price edited here IS that branch's price (the server stores it as the
      // branch override), so the form must start from it, not from the central price.
      default_price: this.adminFilterService.currentCenterId && s.price != null ? s.price : s.default_price,
      level: s.level || 'Organisation',
      centers: s.centers ? [...s.centers] : []
    };
  }

  async deleteService(s: any) {
    if (await this.confirmDialog.ask('Are you sure you want to delete this service?')) {
      this.api.deleteService(s.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadServices();
        },
        error: (err) => {
          this.toastService.showError('Failed to delete service: ' + (err?.error ? JSON.stringify(err.error) : err));
        }
      });
    }
  }

  addCenterFromSelect(value: string) {
    if (!value) return;
    if (!this.newService.centers) this.newService.centers = [];

    if (value === 'all') {
      this.newService.centers = this.centers.map(c => c.id);
      return;
    }

    const centerId = parseInt(value, 10);
    if (!isNaN(centerId) && !this.newService.centers.includes(centerId)) {
      this.newService.centers.push(centerId);
    }
  }

  removeCenter(centerId: number) {
    if (!this.newService.centers) return;
    this.newService.centers = this.newService.centers.filter((id: number) => id !== centerId);
  }

  /** What the server said went wrong, as a sentence (not the raw JSON). */
  private why(err: any): string {
    const body = err?.error;
    if (typeof body === 'string' && body.length < 300) return body;
    if (body?.detail) return String(body.detail);
    if (body?.error) return String(body.error);
    if (body && typeof body === 'object') {
      const parts = Object.entries(body).map(([k, v]) => `${k.replace(/_/g, ' ')}: ${Array.isArray(v) ? v.join(' ') : v}`);
      if (parts.length) return parts.join(' · ');
    }
    return err?.message || 'please try again.';
  }

  clearCenters() { this.newService.centers = []; }

  /** Every centre is picked: shown as one "All centers" pill. */
  get allCentersPicked(): boolean { return this.coversAll(this.newService.centers); }

  /** True when `ids` holds every centre there is. */
  coversAll(ids: number[] | undefined): boolean {
    const picked = ids || [];
    return this.centers.length > 1 && this.centers.every(c => picked.includes(c.id));
  }

  namesOf(ids: number[]): string { return (ids || []).map(id => this.getCenterName(id)).join(', '); }

  /** A few names, then "+ N more": a service at 70 of 78 branches used to print all 70. */
  branchSummary(ids: number[]): string {
    const list = ids || [];
    if (list.length <= 3) return this.namesOf(list);
    return `${this.namesOf(list.slice(0, 3))} + ${list.length - 3} more`;
  }

  getCenterName(id: number): string {
    const center = this.centers.find(c => c.id === id);
    return center ? center.center_name || center.display_name : 'Unknown Center';
  }

  addService() {
    if (!this.newService.name) {
      this.toastService.showError('Service name is required');
      return;
    }
    const payload: any = {
      service_code: this.newService.service_code,
      name: this.newService.name,
      brand: this.newService.brand,
      category: this.newService.category,
      sub_category: this.newService.sub_category,
      sac_code: this.newService.sac_code,
      hsn_code: this.newService.hsn_code,
      default_price: this.newService.default_price,
      tax_percentage: this.newService.tax_percentage,
      duration_mins: this.newService.duration_mins,
      level: this.newService.level,
      centers: this.newService.centers
    };

    if (this.isEditing && this.editingId) {
      this.api.updateService(this.editingId, payload, this.adminFilterService.currentCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.toastService.showSuccess('Service updated');
          this.showAddModal = false;
          this.loadServices();
        },
        error: (err) => {
          this.toastService.showError('Could not update the service: ' + this.why(err));
        }
      });
    } else {
      this.api.createService(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.toastService.showSuccess('Service created');
          this.showAddModal = false;
          this.loadServices();
        },
        error: (err) => {
          this.toastService.showError('Could not create the service: ' + this.why(err));
        }
      });
    }
  }

  onPriceChange(s: any, value: any) {
    if (!s.center_override) s.center_override = { price: null, is_active: true };
    s.center_override.price = value === '' || value === null ? null : Number(value);
  }

  uploadServices(event: any) {
    const file = event.target.files[0];
    if (!file) return;
    this.isSaving = true;
    // Center-aware upload
    this.api.uploadFile('services/api/master/bulk_upload/', file, this.adminFilterService.currentCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.isSaving = false;
        const msg = res.message || 'File uploaded successfully';
        // Say which branches the rows went to. The server reports it;
        // every caller used to discard it.
        const where = this.api.describeImportTargets(res);
        const full = where ? `${msg} ${where}` : msg;
        this.toastService.showSuccess(full);
        if (res.warnings && res.warnings.length > 0) {
          console.warn('Bulk upload warnings:', res.warnings);
        }
        this.loadServices();
        event.target.value = '';
      },
      error: (e: any) => {
        this.isSaving = false;
        const errMsg = e.error?.error || e.error?.detail || e.message || 'Unknown error';
        this.toastService.showError('Failed to upload: ' + errMsg);
        event.target.value = '';
      }
    });
  }

  downloadTemplate() {
    // Column headers exactly matching the expected upload template format
    const headers = [
      'S.No',
      'Brand',
      'Category',
      'Sub Category',
      'Service Name',
      'Price',
      'HSN Code',
      'Tax'
    ];
    // Example row to guide the user
    const exampleRow = [
      'GS1',          // S.No
      'Other',        // Brand
      'Hair',         // Category
      'Hair Cut',     // Sub Category
      'Sample Service Name', // Service Name (REQUIRED)
      '500',          // Price
      '999721',       // HSN Code
      '5%'            // Tax
    ];
    this.csvService.exportToCsv('service_upload_template.csv', headers, [exampleRow]);
  }

  exportExcel() {
    const headers = ['Category', 'Name', 'Duration (mins)', 'Price (Rs)', 'Member Price', 'Description'];
    const rows = this.services.map(s => [
      s.category || '',
      s.name || '',
      s.duration_minutes || 0,
      s.price || 0,
      s.member_price || 0,
      s.description || ''
    ]);
    this.csvService.exportToCsv('Services_Report', headers, rows);
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
