import { AdminFilterService } from '../admin-filter.service';
import { AdminRangeBarComponent } from '../admin-range-bar/admin-range-bar';
import { PinCheckComponent } from '../../components/pin-check/pin-check.component';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';

import { CommonModule, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../services/api';
import { CsvService } from '../../services/csv.service';
import { ToastService } from '../../services/toast.service';
import { toLocalDateString, todayLocal } from '../../services/date-util';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-admin-bills',
  standalone: true,
  imports: [CommonModule, FormsModule, AdminRangeBarComponent, PinCheckComponent],
  templateUrl: './admin-bills.html',
  styleUrl: './admin-bills.css',
})
export class AdminBillsComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  permissions: any = {};
  isOwner = false;

  /** The tick on Admin > Bills or on Billing > Invoice History; the server enforces the same. */
  get canCancel(): boolean {
    return this.isOwner || this.permissions?.admin?.bills?.actions?.cancel === true
      || this.permissions?.billing?.invoices?.actions?.cancel === true;
  }
  /** Same-day cancel only (owner decisions #45, F19); the server enforces it too. */
  isSameDay(invoice: any): boolean {
    const made = invoice?.finalized_at || invoice?.created_at;
    if (!made) return false;
    return new Date(made).toDateString() === new Date().toDateString();
  }

  get canChangePayment(): boolean {
    return this.isOwner || this.permissions?.admin?.bills?.actions?.change_payment === true
      || this.permissions?.billing?.invoices?.actions?.change_payment === true;
  }
  hasGlobalAccess = false;

  apiService = inject(ApiService);
  adminFilterService = inject(AdminFilterService);
  cdr = inject(ChangeDetectorRef);
  csvService = inject(CsvService);

  centers: any[] = [];
  searchInvoiceNo: string = '';
  
  
  
  
  
  currentPage: number = 1;
  totalPages: number = 1;
  
  invoices: any[] = [];
  isLoading = false;

  // Modal State
  selectedInvoice: any = null;
  showModal = false;
  paymentMethodChange: string = 'Cash';

  ngOnInit() {
    this.adminFilterService.apply$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(() => {
      this.getInvoices();
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
    this.setDefaultDates();
    this.getInvoices();
  }

  setDefaultDates() {
    const today = new Date();
    this.adminFilterService.setToDate(toLocalDateString(today));
    
    // Default from date = 30 days ago
    
    
    
  }

  loadCenters() {
    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.centers = Array.isArray(data) ? data : (data.results || []);
        this.getInvoices();
      },
      error: err => {
        console.error('Failed to load centers', err);
        this.getInvoices();
      }
    });
  }

  /** Enter in the search box: look for what was typed, from the first page. (It used to clear the box first, so the search never ran.) */
  searchInvoices() {
    this.currentPage = 1;
    this.getInvoices();
  }

  /** Counts and value of every bill the filters match, from the server (the list below is one page of them). */
  summary: { count: number; paid: number; cancelled: number; refunded: number; value: number } | null = null;

  private loadSummary() {
    const url = `billing/invoices/summary/?center_id=${this.adminFilterService.currentCenterId || ''}&start_date=${this.adminFilterService.currentFromDate || ''}&end_date=${this.adminFilterService.currentToDate || ''}&invoice_number=${encodeURIComponent(this.searchInvoiceNo || '')}&exclude_drafts=true`;
    this.apiService.get(url).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.summary = {
          count: Number(data?.count) || 0, paid: Number(data?.paid) || 0, cancelled: Number(data?.cancelled) || 0,
          refunded: Number(data?.refunded) || 0, value: Number(data?.value) || 0,
        };
        this.cdr.detectChanges();
      },
      error: () => { this.summary = null; this.cdr.detectChanges(); },   // the cards fall back to the page on screen
    });
  }

  nextPage() {
    if (this.currentPage < this.totalPages) {
      this.currentPage++;
      this.getInvoices();
    }
  }

  prevPage() {
    if (this.currentPage > 1) {
      this.currentPage--;
      this.getInvoices();
    }
  }

  getInvoices() {
    this.isLoading = true;
    this.cdr.detectChanges();
    this.loadSummary();
    let url = `billing/invoices/?center_id=${this.adminFilterService.currentCenterId || ''}&start_date=${this.adminFilterService.currentFromDate || ''}&end_date=${this.adminFilterService.currentToDate || ''}&invoice_number=${this.searchInvoiceNo || ''}&exclude_drafts=true&page_size=500&page=${this.currentPage}`;
    this.apiService.get(url).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        let rawInvoices = Array.isArray(data) ? data : (data.results || []);
        if (!Array.isArray(data) && data.count) {
          this.totalPages = Math.ceil(data.count / 500);
        }
        
        // Pre-compute template values to dramatically improve performance
        this.invoices = rawInvoices.map((inv: any) => {
           inv.clientNameStr = this.getClientName(inv);
           inv.billerNameStr = this.getBiller(inv);
           inv.taxVal = this.getTax(inv);
           inv.statusStr = inv.status || 'paid';
           inv.serviceCount = this.getServiceCount(inv);
           inv.productCount = this.getProductCount(inv);
           inv.membCount = this.getMembershipCount(inv);
           inv.othersCount = this.getOthersCount(inv);
           inv.redemptionVal = this.getRedemption(inv);
           return inv;
        });

        this.isLoading = false;
        this.cdr.detectChanges();
        this.cdr.detectChanges();
      },
      error: err => {
        console.error('Failed to get invoices', err);
        this.isLoading = false;
        this.cdr.detectChanges();
      }
    });
  }

  openInvoiceModal(invoice: any) {
    this.selectedInvoice = JSON.parse(JSON.stringify(invoice));
    if (this.selectedInvoice && this.selectedInvoice.payments) {
      this.selectedInvoice.payments.forEach((p: any) => {
        p.new_payment_method = p.payment_method;
      });
    }
    this.showModal = true;
  }

  actionConfirmType: 'cancel' | 'refund' | 'change_payment' | null = null;
  cancelReason: string = '';
  backdateValue: string = '';

  closeInvoiceModal() {
    this.showModal = false;
    this.selectedInvoice = null;
    this.actionConfirmType = null;
    this.cancelReason = '';
  }

  confirmAction(type: 'cancel' | 'refund' | 'change_payment') {
    this.actionConfirmType = type;
  }

  cancelActionConfirm() {
    this.actionConfirmType = null;
  }

  executeAction() {
    if (!this.selectedInvoice || !this.actionConfirmType) return;
    
    if (!this.cancelReason.trim()) {
      alert('Please enter a reason for this action.');
      return;
    }

    this.isLoading = true;
    
    if (this.actionConfirmType === 'cancel') {
      this.apiService.post(`billing/invoices/${this.selectedInvoice.id}/cancel/`, { reason: this.cancelReason }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.actionConfirmType = null;
          this.closeInvoiceModal();
          this.getInvoices();
        },
        error: (err: any) => {
          this.isLoading = false;
          console.error(err);
          alert(err.error?.reason || 'Failed to cancel invoice.');
        }
      });
    } else if (this.actionConfirmType === 'refund') {
      this.apiService.post(`billing/invoices/${this.selectedInvoice.id}/refund/`, { reason: this.cancelReason }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.actionConfirmType = null;
          this.closeInvoiceModal();
          this.getInvoices();
        },
        error: (err: any) => {
          this.isLoading = false;
          console.error(err);
          alert(err.error?.reason || 'Failed to refund invoice.');
        }
      });
    }
  }

  updatePaymentMethod(payment: any) {
    if (!this.selectedInvoice || !payment.new_payment_method) return;
    if (payment.new_payment_method === payment.payment_method) return;

    const pass = this.pinToken;
    this.pinToken = null;
    this.apiService.post(`billing/invoices/${this.selectedInvoice.id}/change_payment/`, {
      payment_id: payment.id,
      payment_method: payment.new_payment_method,
      ...(pass ? { pin_token: pass } : {}),
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        alert('Payment method updated successfully.');
        payment.payment_method = payment.new_payment_method;
        this.getInvoices();
      },
      error: (err: any) => {
        // Moving a payment onto the client's advance or wallet needs their PIN (or the branch's master PIN).
        if (err?.status === 400 && err.error && typeof err.error === 'object' && 'pin_required' in err.error) {
          this.pinFor = payment;
          this.cdr.detectChanges();
          return;
        }
        console.error(err);
        alert(err.error?.detail || 'Failed to update payment method.');
      }
    });
  }

  /** The PIN box, opened when a payment is moved onto the client's advance or wallet. */
  pinFor: any = null;
  private pinToken: string | null = null;

  onPinVerified(token: string) {
    const payment = this.pinFor;
    this.pinFor = null;
    this.pinToken = token;
    this.cdr.detectChanges();
    if (payment) this.updatePaymentMethod(payment);
  }

  onPinClosed() {
    this.pinFor = null;
    this.cdr.detectChanges();
  }

  get pinClientId(): number {
    const c = this.selectedInvoice?.client;
    return (typeof c === 'object' ? c?.id : c) || 0;
  }

  get pinCenterId(): number {
    const c = this.selectedInvoice?.center;
    return (typeof c === 'object' ? c?.id : c) || 0;
  }

  // Formatting helpers
  getBiller(invoice: any): string {
    // The person who raised the bill at the till; older bills may have none recorded.
    return invoice.created_by_name || (invoice.staff ? `Staff #${invoice.staff}` : '-');
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

  getServiceCount(invoice: any): number {
    return invoice.items?.filter((i: any) => {
      if (!i.content_type) return false;
      const ct = typeof i.content_type === 'string' ? i.content_type.toLowerCase() : (i.content_type.app_label || '').toLowerCase();
      return !ct.includes('inventory') && !ct.includes('marketing');
    }).reduce((acc: number, item: any) => acc + item.quantity, 0) || 0;
  }

  getProductCount(invoice: any): number {
    return invoice.items?.filter((i: any) => {
      if (!i.content_type) return false;
      const ct = typeof i.content_type === 'string' ? i.content_type.toLowerCase() : (i.content_type.app_label || '').toLowerCase();
      return ct.includes('inventory');
    }).reduce((acc: number, item: any) => acc + item.quantity, 0) || 0;
  }

  getMembershipCount(invoice: any): number {
    return invoice.items?.filter((i: any) => {
      if (!i.content_type) return false;
      const ct = typeof i.content_type === 'string' ? i.content_type.toLowerCase() : (i.content_type.app_label || '').toLowerCase();
      return ct.includes('membership') || ct.includes('package') || ct.includes('valuecard');
    }).reduce((acc: number, item: any) => acc + item.quantity, 0) || 0;
  }

  getOthersCount(invoice: any): number {
    return invoice.items?.filter((i: any) => !i.content_type).reduce((acc: number, item: any) => acc + item.quantity, 0) || 0;
  }
  
  getTax(invoice: any): number {
      return (parseFloat(invoice.cgst) || 0) + (parseFloat(invoice.sgst) || 0);
  }

  getRedemption(invoice: any): number {
      if (!invoice || !invoice.payments) return 0;
      let total = 0;
      for (const p of invoice.payments) {
          if (p.payment_method && (p.payment_method.toLowerCase().includes('card') || p.payment_method.toLowerCase().includes('redeem') || p.payment_method.toLowerCase() === 'membership')) {
              // Only count actual value card / gift card redemptions if defined
              if (p.payment_method.toLowerCase().includes('value') || p.payment_method.toLowerCase().includes('gift') || p.payment_method.toLowerCase().includes('membership')) {
                 total += parseFloat(p.amount) || 0;
              }
          }
      }
      return total;
  }
  
  getGross(invoice: any): number {
      return parseFloat(invoice.total_amount) || 0;
  }
  
  getTotalPaidByMethod(method: string, invoice: any): number {
      return invoice.payments?.filter((p: any) => {
          return (p.payment_method || '').toLowerCase().includes(method.toLowerCase());
      }).reduce((acc: number, p: any) => acc + parseFloat(p.amount), 0) || 0;
  }

  getPaidAmount(invoice: any): number {
      return parseFloat(invoice.paid_amount) || 0;
  }

  // The cards describe EVERY bill the filters match (summary), not only the page on screen.
  // If the summary could not be read they fall back to the page, and say so.
  get cardsAreForThePage(): boolean {
    return this.summary === null;
  }

  getTotalBills() {
    return this.summary ? this.summary.count : this.invoices.length;
  }

  /** What was sold: cancelled and refunded bills are not income. */
  getTotalValue() {
    if (this.summary) return this.summary.value;
    return this.invoices
      .filter(inv => inv.status !== 'cancelled' && inv.status !== 'refunded')
      .reduce((sum, inv) => sum + (Number(inv.total_amount) || 0), 0);
  }

  getPaidCount() {
    return this.summary ? this.summary.paid : this.invoices.filter(inv => !inv.status || inv.status === 'paid' || inv.status === 'partial').length;
  }

  getCancelledCount() {
    return this.summary ? this.summary.cancelled : this.invoices.filter(inv => inv.status === 'cancelled').length;
  }

  getRefundedCount() {
    return this.summary ? this.summary.refunded : this.invoices.filter(inv => inv.status === 'refunded').length;
  }

  exportExcel() {
    const headers = ['Bill No', 'Date', 'Biller', 'Client', 'Services', 'Products', 'Memb.', 'Others', 'Subtotal', 'Discount', 'Tax', 'Total', 'Redemption', 'Status'];
    const rows = this.invoices.map(inv => [
      inv.id,
      inv.created_at ? inv.created_at.split('T')[0] : '',
      this.getBiller(inv),
      this.getClientName(inv),
      this.getServiceCount(inv),
      this.getProductCount(inv),
      this.getMembershipCount(inv),
      this.getOthersCount(inv),
      inv.subtotal || 0,
      inv.discount || 0,
      this.getTax(inv),
      inv.total_amount || 0,
      this.getRedemption(inv),
      inv.status || ''
    ]);
    this.csvService.exportToCsv('Admin_Bills_Report', headers, rows);
  }

  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }
}
