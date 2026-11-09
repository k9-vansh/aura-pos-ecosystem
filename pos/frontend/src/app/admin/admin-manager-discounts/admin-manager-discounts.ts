import { AdminFilterService } from '../admin-filter.service';
import { AdminRangeBarComponent } from '../admin-range-bar/admin-range-bar';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';

import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { CsvService } from '../../services/csv.service';
import { todayLocal } from '../../services/date-util';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-manager-discounts',
  standalone: true,
  imports: [CommonModule, FormsModule, AdminRangeBarComponent],
  templateUrl: './admin-manager-discounts.html',
  styleUrl: './admin-manager-discounts.css',
})
export class AdminManagerDiscountsComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  permissions: any = {};
  isOwner = false;
  hasGlobalAccess = false;

  apiService = inject(ApiService);
  adminFilterService = inject(AdminFilterService);
  cdr = inject(ChangeDetectorRef);
  csvService = inject(CsvService);

  centers: any[] = [];
  
  invoices: any[] = [];
  isLoading = false;
  
  

  ngOnInit() {
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.getDiscountedInvoices();
    });
    this.adminFilterService.export$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.exportExcel();
    });

    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
      } catch (e) {}
    }
    const today = new Date();
    this.adminFilterService.setToDate(this.formatDate(today));
    const start = new Date();
    start.setDate(start.getDate() - 30);
    this.adminFilterService.setFromDate(this.formatDate(start));

    this.getDiscountedInvoices();
  }

  formatDate(date: Date): string {
    const d = new Date(date);
    let month = '' + (d.getMonth() + 1);
    let day = '' + d.getDate();
    const year = d.getFullYear();
    if (month.length < 2) month = '0' + month;
    if (day.length < 2) day = '0' + day;
    return [year, month, day].join('-');
  }

  loadCenters() {
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.centers = Array.isArray(data) ? data : (data.results || []);
        if (this.centers.length && !this.adminFilterService.currentCenterId) {
          this.adminFilterService.setCenterId(this.centers[0].id);
        }
        this.getDiscountedInvoices();
      },
      error: err => {
        console.error('Failed to load centers', err);
        this.getDiscountedInvoices();
      }
    });
  }

  getDiscountedInvoices() {
    this.isLoading = true;
    
    let url = `billing/invoices/?center_id=${this.adminFilterService.currentCenterId || ''}&manager_discount=true`;
    if (this.adminFilterService.currentFromDate) url += `&start_date=${this.adminFilterService.currentFromDate}`;
    if (this.adminFilterService.currentToDate) url += `&end_date=${this.adminFilterService.currentToDate}`;
    
    this.apiService.get(url).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.invoices = Array.isArray(data) ? data : (data.results || []);
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: err => {
        console.error('Failed to get discounted invoices', err);
        this.isLoading = false;
        this.cdr.detectChanges();
      }
    });
  }

  getClientName(invoice: any): string {
    if (invoice.client && invoice.client.first_name) {
      return `${invoice.client.first_name} ${invoice.client.last_name || ''}`.trim();
    }
    return 'Unknown';
  }

  getClientPhone(invoice: any): string {
    return invoice.client?.phone || '';
  }

  /** Who raised the bill at the till. */
  billedBy(invoice: any): string {
    return invoice.created_by_name || (invoice.staff ? `Staff #${invoice.staff}` : '-');
  }

  /** "services.servicemaster" -> "Service", "inventory.product" -> "Product". */
  typeLabel(item: any): string {
    const model = `${item.content_type || ''}`.split('.').pop() || '';
    const names: Record<string, string> = {
      servicemaster: 'Service', product: 'Product', membership: 'Membership',
      package: 'Package', valuecard: 'Value card', advancepayment: 'Advance',
    };
    return names[model.toLowerCase()] || 'Service';
  }

  /** The discount as a share of the whole line (price x quantity), not of one unit. */
  itemPercent(item: any, amount: number): number {
    const line = (parseFloat(item.unit_price) || 0) * (parseFloat(item.quantity) || 1);
    return line ? (parseFloat(amount as any) || 0) / line * 100 : 0;
  }

  getGross(invoice: any): number {
      return parseFloat(invoice.total_amount) || 0;
  }

  exportExcel() {
    const headers = ['Invoice No', 'Date/Time', 'Client', 'Phone', 'Type', 'Item', 'Billed By', 'Price', 'Discount', 'Discount %', 'Net Price'];
    const rows: any[][] = [];
    
    this.invoices.forEach(inv => {
      // Invoice level discount
      if (inv.discount > 0) {
        rows.push([
          inv.id,
          inv.created_at ? inv.created_at.replace('T', ' ').substring(0, 16) : '',
          this.getClientName(inv),
          this.getClientPhone(inv),
          'Overall Bill',
          'Full Invoice',
          this.billedBy(inv),
          inv.subtotal || 0,
          inv.discount || 0,
          (inv.subtotal ? ((inv.discount / inv.subtotal) * 100).toFixed(0) : 0) + '%',
          inv.total_amount || 0
        ]);
      }
      
      // Item Level Discounts
      if (inv.items && inv.items.length) {
        inv.items.forEach((item: any) => {
          if (item.discount > 0) {
            rows.push([
              inv.id,
              inv.created_at ? inv.created_at.replace('T', ' ').substring(0, 16) : '-',
              this.getClientName(inv),
              this.getClientPhone(inv),
              this.typeLabel(item),
              item.description || 'Item',
              this.billedBy(inv),
              item.unit_price || 0,
              item.discount || 0,
              Math.round(this.itemPercent(item, item.discount)) + '%',
              item.total_price || 0
            ]);
          }
        });
      }
    });
    
    this.csvService.exportToCsv('Manager_Discounts.csv', headers, rows);
  }

  getTotalDiscountsCount(): number {
    return this.invoices.length;
  }

  getTotalDiscountValue(): number {
    let sum = 0;
    this.invoices.forEach(inv => {
      if (inv.items && inv.items.length) {
        inv.items.forEach((item: any) => {
          if (item.manager_discount > 0) sum += parseFloat(item.manager_discount) || 0;
        });
      }
    });
    return sum;
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
