import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Component, OnInit, inject, ChangeDetectorRef, DestroyRef, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { DomSanitizer, SafeHtml } from '@angular/platform-browser';
import { ApiService } from '../services/api';
import Chart from 'chart.js/auto';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';
import { AuthService } from '../services/auth';

interface ModuleCard {
  title: string;
  subtitle: string;
  route: string;
  icon: SafeHtml;
  color: string;
  visible: boolean;
}

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-home',
  imports: [CommonModule, FormsModule, LocationSelectorComponent],
  templateUrl: './home.html',
  styleUrl: './home.css',
})
export class HomeComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private locationMemory = inject(LocationMemory);
  private auth = inject(AuthService);
  router = inject(Router);
  sanitizer = inject(DomSanitizer);
  apiService = inject(ApiService);
  cdr = inject(ChangeDetectorRef);
  
  isOwner = false;
  hasGlobalAccess = false;
  permissions: any = {};
  username = 'User';
  modules: ModuleCard[] = [];
  
  monthlyRevenue: number = 0;
  /** True when the role grants no module at all. */
  noAccess = false;
  // 0 until the centre's own target arrives; a made-up figure showed as "Target" before
  monthlyTarget: number = 0;
  todayCollection: number = 0;
  todayCollectionWithTax: number = 0;
  todayCollectionWithoutTax: number = 0;
  includeTaxes: boolean = true;
  
  revenuePath: string = 'M0,100 L400,100';
  revenueLabels: string[] = ['1st', '10th', '20th', '30th'];
  
  centers: any[] = [];
  selectedFilterLocation: any = null;
  
  isEditingTarget = false;
  tempMonthlyTarget = 0;
  
  summaryData: any = null;
  
  chartInstance: any = null;
  
  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.permissions = user.permissions || {};
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.username = user.full_name || user.first_name || user.username || user.email || 'User';
      } catch (e) {}
    }

    const hasModuleReadAccess = (modName: string) => {
      const mod = this.permissions[modName];
      if (!mod || typeof mod !== 'object') return false;
      return Object.values(mod).some((sub: any) => sub && sub.read === true);
    };

    this.modules = [
      {
        title: 'Dashboard',
        subtitle: 'Live revenue & performance overview',
        route: '/admin/dashboard',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="M3 3v16a2 2 0 0 0 2 2h16" /><path d="M7 11l4-4 4 4 4-4" />'),
        color: '#65ded2',
        visible: this.isOwner || hasModuleReadAccess('overview')
      },
      {
        title: 'Finance',
        subtitle: 'Expenses, taxes & petty cash',
        route: '/admin/finance',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="M3 3v16a2 2 0 0 0 2 2h16" /><path d="M7 16h3v-4h3v4h3v-8h3" />'),
        color: '#22c55e',
        visible: this.isOwner || hasModuleReadAccess('finance')
      },
      {
        title: 'Billing',
        subtitle: 'Create invoices & manage payments',
        route: '/admin/billing',
        icon: this.sanitizer.bypassSecurityTrustHtml('<rect width="20" height="14" x="2" y="5" rx="2" /><line x1="2" y1="10" x2="22" y2="10" />'),
        color: '#a78bfa',
        visible: this.isOwner || hasModuleReadAccess('billing')
      },
      {
        title: 'Appointments',
        subtitle: 'Staff scheduling & client bookings',
        route: '/admin/appointments',
        icon: this.sanitizer.bypassSecurityTrustHtml('<rect width="18" height="18" x="3" y="4" rx="2" /><line x1="16" y1="2" x2="16" y2="6" /><line x1="8" y1="2" x2="8" y2="6" /><line x1="3" y1="10" x2="21" y2="10" />'),
        color: '#f59e0b',
        visible: this.isOwner || hasModuleReadAccess('appointment') || hasModuleReadAccess('appointments')
      },
      {
        title: 'Staff',
        subtitle: 'Team records, payroll & service logs',
        route: '/admin/staff',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M22 21v-2a4 4 0 0 0-3-3.87" /><path d="M16 3.13a4 4 0 0 1 0 7.75" />'),
        color: '#34d399',
        visible: this.isOwner || hasModuleReadAccess('staff')
      },
      {
        title: 'Inventory',
        subtitle: 'Manage stock, products & supplies',
        route: '/admin/inventory',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" /><path d="m3.3 7 8.7 5 8.7-5" /><path d="M12 22V12" />'),
        color: '#60a5fa',
        visible: this.isOwner || hasModuleReadAccess('inventory')
      },
      {
        title: 'Marketing',
        subtitle: 'Packages, memberships & campaigns',
        route: '/admin/marketing',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="m3 11 18-5v12L3 13v-2z" /><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6" />'),
        color: '#f472b6',
        visible: this.isOwner || hasModuleReadAccess('marketing')
      },
      {
        title: 'Admin',
        subtitle: 'Users, centres, roles & services',
        route: '/admin/centers',
        icon: this.sanitizer.bypassSecurityTrustHtml('<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />'),
        color: '#fb923c',
        visible: this.isOwner || hasModuleReadAccess('admin')
      },
    ];

    // A role that grants nothing has no figures to show, only a notice.
    this.noAccess = !this.isOwner && !this.modules.some(m => m.visible);
    if (this.noAccess) {
      this.cdr.detectChanges();
      return;
    }

    this.apiService.getCenters(true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.centers = Array.isArray(data) ? data : (data.results || []);
      if (!this.hasGlobalAccess) {
        const u = localStorage.getItem('user');
        if (u) {
          const uObj = JSON.parse(u);
          this.selectedFilterLocation = uObj?.center_id || null;
        }
        if (this.centers.length > 0 && !this.centers.some((c:any) => c.id == this.selectedFilterLocation)) {
          this.selectedFilterLocation = this.centers[0].id;
        }
      } else {
        const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
        if (remembered !== undefined) this.selectedFilterLocation = remembered;
      }
      this.loadDynamicData();
      this.cdr.detectChanges();
    });
  }

  onLocationChange() {
    this.loadDynamicData();
  }

  loadDynamicData() {
    const centerId = !this.selectedFilterLocation ? '' : this.selectedFilterLocation;
    const today = new Date();
    const y = today.getFullYear();
    const m = String(today.getMonth() + 1).padStart(2, '0');
    const d = String(today.getDate()).padStart(2, '0');
    const todayStr = `${y}-${m}-${d}`;
    const range = this.periodRange();

    const params: any = { start_date: range.start, end_date: range.end };
    if (centerId) params.center_id = centerId;

    // Get Monthly Revenue and Target (and full summary)
    this.apiService.get('salon_admin/api/dashboard/summary/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.summaryData = res;
        this.monthlyRevenue = res?.revenue || 0;
        this.monthlyTarget = res?.target || 0;
        this.cdr.detectChanges();
      },
      error: () => {}
    });

    // Get Today's Collection
    this.apiService.get('salon_admin/api/dashboard/summary/', { start_date: todayStr, end_date: todayStr, ...(centerId && {center_id: centerId}) }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.todayCollectionWithTax = res?.revenue || 0;
        this.todayCollectionWithoutTax = res?.revenue_without_tax || 0;
        this.updateTodayCollection();
      },
      error: () => {}
    });
  }

  saveMonthlyTarget() {
    if (!this.selectedFilterLocation) {
      alert("Please select a specific center first to set its target.");
      this.isEditingTarget = false;
      return;
    }
    
    // PATCH to backend
    this.apiService.patch(`salon_admin/api/centers/${this.selectedFilterLocation}/`, { monthly_target: this.tempMonthlyTarget }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.monthlyTarget = this.tempMonthlyTarget;
        this.isEditingTarget = false;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        alert("Failed to save target. Make sure you have permission.");
        console.error(err);
      }
    });
  }

  updateTodayCollection() {
    this.todayCollection = this.includeTaxes ? this.todayCollectionWithTax : this.todayCollectionWithoutTax;
    this.cdr.detectChanges();
  }

  generateChart(data: any[], attempt = 0) {
    const canvas = document.getElementById('homeRevenueChart') as HTMLCanvasElement;
    // The strip may not have been laid out yet; wait for it rather than drawing into nothing.
    if (!canvas || canvas.clientWidth === 0 || canvas.clientHeight === 0) {
      if (attempt < 20) setTimeout(() => this.generateChart(data, attempt + 1), 150);
      return;
    }

    // Amounts arrive as strings ("12345.00"); keep valid days only, in date order.
    data = (data || [])
      .map(d => ({ day: d.day, revenue: Number(d.revenue) }))
      .filter(d => d.day && Number.isFinite(d.revenue))
      .sort((a, b) => String(a.day).localeCompare(String(b.day)));

    if (this.chartInstance) {
      this.chartInstance.destroy();
      this.chartInstance = null;
    }

    if (!data || data.length === 0) {
      // If no data, show a flat line with 0 for today
      data = [{ day: todayLocal(), revenue: 0 }];
    }

    const c2d = canvas.getContext('2d');
    if (!c2d) return;

    // A quiet strip of dots: one per day, no axes, like a timeline.
    const dark = document.body.getAttribute('data-theme') === 'dark';
    const color = dark ? '#dfe5f0' : '#2b2d31';
    const grad = c2d.createLinearGradient(0, 0, 0, 120);
    grad.addColorStop(0, dark ? 'rgba(223, 229, 240, 0.12)' : 'rgba(43, 45, 49, 0.10)');
    grad.addColorStop(1, dark ? 'rgba(223, 229, 240, 0)' : 'rgba(43, 45, 49, 0)');

    const labels = data.map(d => {
      try { return new Date(d.day).toLocaleDateString('en-IN', { day: '2-digit', month: 'short' }); }
      catch { return d.day; }
    });
    const revs = data.map(d => d.revenue);
    
    // Fix: If there's only 1 data point, Chart.js can't draw a line. Inject a starting point.
    if (labels.length === 1) {
      labels.unshift('Start');
      revs.unshift(0);
    }

    this.chartInstance = new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: 'Invoices per day',
          data: revs,
          borderColor: color,
          backgroundColor: grad,
          borderWidth: 1.25,
          fill: true,
          pointRadius: 2.5,
          pointHoverRadius: 6,
          pointBackgroundColor: color,
          pointBorderColor: color,
          pointHoverBackgroundColor: '#c6f432',
          pointHoverBorderColor: color,
          pointHoverBorderWidth: 2,
          tension: 0.35
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15, 23, 42, 0.95)',
            titleColor: '#ffffff',
            titleFont: { size: 13, family: "'Inter', sans-serif", weight: 'bold' as const },
            bodyColor: '#e2e8f0',
            bodyFont: { size: 12, family: "'Inter', sans-serif" },
            bodySpacing: 6,
            padding: 12,
            cornerRadius: 8,
            borderColor: 'rgba(255, 255, 255, 0.1)',
            borderWidth: 1,
            usePointStyle: true,
            boxPadding: 6,
            callbacks: {
              label: (ctx: any) => ' ₹' + (ctx.parsed.y || 0).toLocaleString('en-IN')
            }
          }
        },
        layout: { padding: { top: 8, bottom: 6, left: 6, right: 6 } },
        scales: {
          x: { display: false, grid: { display: false } },
          y: { display: false, beginAtZero: true, grid: { display: false } }
        }
      }
    });
  }

  getGaugePath(): string {
    if (this.periodTarget <= 0 || this.monthlyRevenue <= 0) {
      return 'M 10 50';
    }
    const p = Math.min(this.monthlyRevenue / this.periodTarget, 1);
    const theta = Math.PI * (1 - p);
    const x = 50 + 40 * Math.cos(theta);
    const y = 50 - 40 * Math.sin(theta);
    return `M 10 50 A 40 40 0 0 1 ${x} ${y}`;
  }

  getGaugeCircle(): {x: number, y: number} {
    if (this.monthlyTarget <= 0 || this.monthlyRevenue <= 0) {
      return {x: 10, y: 50};
    }
    const p = Math.min(this.monthlyRevenue / this.monthlyTarget, 1);
    const theta = Math.PI * (1 - p);
    const x = 50 + 40 * Math.cos(theta);
    const y = 50 - 40 * Math.sin(theta);
    return {x, y};
  }

  navigate(route: string) {
    this.router.navigate([route]);
  }

  get visibleModules(): ModuleCard[] {
    return this.modules.filter(m => m.visible);
  }

  // ── Overview page presentation (display only; no data is changed here) ──

  /** 12400 -> {v:'12.4', u:'k'}; the dot-matrix numerals need short figures. */
  private compactParts(value: any): { v: string; u: string } {
    const n = Number(value) || 0;
    const trim = (x: number) => x.toFixed(1).replace(/\.0$/, '');
    if (n >= 10000000) return { v: trim(n / 10000000), u: 'Cr' };
    if (n >= 100000) return { v: trim(n / 100000), u: 'L' };
    if (n >= 1000) return { v: trim(n / 1000), u: 'k' };
    return { v: String(Math.round(n)), u: '' };
  }

  get todayLabel(): string {
    return new Date().toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' });
  }

  get monthLabel(): string {
    return new Date().toLocaleDateString('en-IN', { month: 'short' });
  }

  get revenueParts() { return this.compactParts(this.monthlyRevenue); }
  get todayParts() { return this.compactParts(this.todayCollection); }
  get toCollectParts() { return this.compactParts(this.summaryData?.money_in?.to_collect); }

  get achievedPct(): number {
    const target = this.periodTarget;
    return target > 0 ? Math.min(Math.round((this.monthlyRevenue / target) * 100), 999) : 0;
  }

  /** Where the month stands against a straight-line run to the target. */
  get paceStatus(): string {
    if (this.periodTarget <= 0) return 'No target set';
    if (this.achievedPct >= 100) return 'Target met';
    if (this.period !== 'thisMonth') return 'Below target';
    const now = new Date();
    const daysInMonth = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    const expected = (now.getDate() / daysInMonth) * 100;
    return this.achievedPct >= expected * 0.9 ? 'On track' : 'Behind pace';
  }

  get billsCount(): string {
    const n = this.summaryData?.money_in?.bills;
    return n === undefined || n === null ? '–' : String(n);
  }

  get clientsCount(): string {
    const n = this.summaryData?.total_clients;
    return n === undefined || n === null ? '–' : String(n);
  }

  /** 40 dots, lit in proportion to the share of the target reached. */
  get progressDots(): boolean[] {
    const lit = Math.round((Math.min(this.achievedPct, 100) / 100) * 40);
    return Array.from({ length: 40 }, (_, i) => i < lit);
  }

  /** The ruler under the day's collection: a long tick every fifth mark. */
  readonly tickMarks: boolean[] = Array.from({ length: 50 }, (_, i) => i % 5 === 0);

  /** Share of what was billed that has actually been received. */
  get collectedPct(): number {
    const mi = this.summaryData?.money_in;
    const received = Number(mi?.real_money_received) || 0;
    const open = Number(mi?.to_collect) || 0;
    return received + open > 0 ? Math.round((received / (received + open)) * 100) : 100;
  }

  // ── Change password ──

  showPassword = false;
  pw = { current: '', next: '', confirm: '' };
  pwError = '';
  pwDone = false;
  pwSaving = false;

  openPassword() {
    this.pw = { current: '', next: '', confirm: '' };
    this.pwError = '';
    this.pwDone = false;
    this.showPassword = true;
  }

  closePassword() {
    this.showPassword = false;
    this.cdr.detectChanges();
  }

  submitPassword() {
    this.pwError = '';
    if (!this.pw.current || !this.pw.next) { this.pwError = 'Enter your current and your new password.'; return; }
    if (this.pw.next !== this.pw.confirm) { this.pwError = 'The new passwords do not match.'; return; }
    this.pwSaving = true;
    this.auth.changePassword({
      current_password: this.pw.current,
      new_password: this.pw.next,
      confirm_password: this.pw.confirm,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.pwSaving = false; this.pwDone = true; this.cdr.detectChanges(); },
      error: (err: any) => {
        this.pwSaving = false;
        const e = err?.error || {};
        const first = (v: any) => (Array.isArray(v) ? v[0] : v);
        this.pwError = first(e.current_password) || first(e.new_password) || first(e.confirm_password)
          || e.detail || 'Could not change the password.';
        this.cdr.detectChanges();
      },
    });
  }

  // ── Period selector and sales mix ──

  period: 'thisMonth' | 'lastMonth' | 'past6' | 'past12' = 'thisMonth';
  readonly periods = [
    { v: 'thisMonth', l: 'This month' },
    { v: 'lastMonth', l: 'Last month' },
    { v: 'past6', l: 'Past 6 months' },
    { v: 'past12', l: 'Past 12 months' },
  ];

  onPeriodChange() {
    this.loadDynamicData();
  }

  private ymd(d: Date): string {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }

  /** The dates the page is showing, as the API wants them. */
  private periodRange(): { start: string; end: string } {
    const now = new Date();
    switch (this.period) {
      case 'lastMonth':
        return { start: this.ymd(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
                 end: this.ymd(new Date(now.getFullYear(), now.getMonth(), 0)) };
      case 'past6':
        return { start: this.ymd(new Date(now.getFullYear(), now.getMonth() - 5, 1)), end: this.ymd(now) };
      case 'past12':
        return { start: this.ymd(new Date(now.getFullYear(), now.getMonth() - 11, 1)), end: this.ymd(now) };
      default:
        // To the END of the month: the API projects the run rate to the end of the range
        // it is asked for, and treats a range that ends today as finished.
        return { start: this.ymd(new Date(now.getFullYear(), now.getMonth(), 1)),
                 end: this.ymd(new Date(now.getFullYear(), now.getMonth() + 1, 0)) };
    }
  }

  get periodLabel(): string {
    return this.periods.find(p => p.v === this.period)?.l || '';
  }

  /** The monthly target, times the months the period covers. */
  get periodTarget(): number {
    const months = this.period === 'past6' ? 6 : this.period === 'past12' ? 12 : 1;
    return (Number(this.monthlyTarget) || 0) * months;
  }

  get startLabel(): string {
    const [y, m, d] = this.periodRange().start.split('-').map(Number);
    return new Date(y, m - 1, d).toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
  }

  get projection(): number | null {
    return this.period === 'thisMonth' && this.summaryData?.projected != null ? Number(this.summaryData.projected) : null;
  }

  /** Indian digit grouping, no decimals: 1379037 -> 13,79,037. */
  inr(value: any): string {
    return (Math.round(Number(value) || 0)).toLocaleString('en-IN');
  }

  /** Where the period's money came from: the dashboard's own revenue breakdown. */
  get salesMix(): { label: string; value: number; pct: number }[] {
    const b = this.summaryData?.revenue_breakdown || {};
    const rows = [
      { label: 'Services', value: Number(b.service) || 0 },
      { label: 'Value cards', value: Number(b.card) || 0 },
      { label: 'Products', value: Number(b.product) || 0 },
      { label: 'Advances', value: Number(b.advance) || 0 },
      { label: 'Memberships', value: Number(b.membership) || 0 },
      { label: 'Packages', value: Number(b.package) || 0 },
    ];
    const total = rows.reduce((sum, r) => sum + r.value, 0);
    return rows
      .map(r => ({ ...r, pct: total > 0 ? Math.round((r.value / total) * 100) : 0 }))
      .sort((a, c) => c.value - a.value);
  }

  formatRevenue(val: any): string {
    if (!val || Number(val) <= 0) return '';
    const num = Number(val);
    if (num >= 10000000) return ` - ₹${(num / 10000000).toFixed(1)} Cr`;
    if (num >= 100000) return ` - ₹${(num / 100000).toFixed(1)} L`;
    if (num >= 1000) return ` - ₹${(num / 1000).toFixed(1)} k`;
    return ` - ₹${num.toFixed(0)}`;
  }
}
