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
  selector: 'app-admin-changes',
  standalone: true,
  imports: [CommonModule, FormsModule, AdminRangeBarComponent],
  templateUrl: './admin-changes.html',
  styleUrl: './admin-changes.css',
})
export class AdminChangesComponent implements OnInit {
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
  
  changeLogs: any[] = [];
  isLoading = false;
  
  

  ngOnInit() {
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.getChangeLogs();
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
    start.setMonth(start.getMonth() - 1);
    this.adminFilterService.setFromDate(this.formatDate(start));

    this.getChangeLogs();
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
        this.getChangeLogs();
      },
      error: err => {
        console.error('Failed to load centers', err);
        this.getChangeLogs();
      }
    });
  }

  getChangeLogs() {
    this.isLoading = true;
    let url = `billing/change-logs/?center_id=${this.adminFilterService.currentCenterId || ''}`;
    if (this.adminFilterService.currentFromDate) url += `&start_date=${this.adminFilterService.currentFromDate}`;
    if (this.adminFilterService.currentToDate) url += `&end_date=${this.adminFilterService.currentToDate}`;
    this.apiService.get(url).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.changeLogs = Array.isArray(data) ? data : (data.results || []);
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: err => {
        console.error('Failed to get change logs', err);
        this.isLoading = false;
        this.cdr.detectChanges();
      }
    });
  }

  exportExcel() {
    const headers = ['Bill Date', 'Salon', 'Bill-No', 'Bill Amount', 'User', 'Changed On', 'Action'];
    const rows = this.changeLogs.map(log => [
      log.bill_date ? log.bill_date.replace('T', ' ').substring(0, 16) : '-',
      log.center_name || '',
      log.invoice || '',
      log.bill_amount || 0,
      log.user_name || 'System',
      log.created_at ? log.created_at.replace('T', ' ').substring(0, 16) : '-',
      log.action || ''
    ]);
    this.csvService.exportToCsv('Admin_Changes_Log.csv', headers, rows);
  }

  getCancelRefundCount(): number {
    return this.changeLogs.filter(log => log.action === 'Cancel Bill' || log.action === 'Refund').length;
  }

  getLostValue(): number {
    return this.changeLogs
      .filter(log => log.action === 'Cancel Bill' || log.action === 'Refund')
      .reduce((sum, log) => sum + (parseFloat(log.bill_amount) || 0), 0);
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
