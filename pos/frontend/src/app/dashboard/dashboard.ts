import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { debounceTime } from 'rxjs';
import { AfterViewInit, ChangeDetectorRef, Component, DestroyRef, OnInit, OnDestroy, inject, ChangeDetectionStrategy } from '@angular/core';
import { CommonModule, CurrencyPipe, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import Chart from 'chart.js/auto';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { AnalyticsReportsComponent } from './analytics-reports/analytics-reports';
import { UsageReportComponent } from './usage-report/usage-report';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';
import { formatChartValue, formatCompact } from '../services/money';
import { ModuleAccessService } from '../services/module-access';

Chart.defaults.font.family = 'Inter, sans-serif';
Chart.defaults.color = '#9ca3af';
Chart.defaults.scale.grid.color = 'rgba(0,0,0,0.035)';
// @ts-ignore
Chart.defaults.scale.grid.borderDash = [5, 5];
Chart.defaults.plugins.tooltip.backgroundColor = 'rgba(17, 24, 39, 0.95)';
Chart.defaults.plugins.tooltip.padding = 12;
Chart.defaults.plugins.tooltip.cornerRadius = 8;
Chart.defaults.plugins.tooltip.displayColors = false;
Chart.defaults.plugins.tooltip.titleFont = { size: 13, family: 'Inter, sans-serif', weight: 'bold' };
Chart.defaults.plugins.tooltip.bodyFont = { size: 12, family: 'Inter, sans-serif', weight: 'normal' };
Chart.defaults.plugins.tooltip.filter = (tooltipItem: any) => tooltipItem.label !== '';
Chart.defaults.elements.bar.borderRadius = 4;
Chart.defaults.elements.bar.borderSkipped = false;
Chart.defaults.elements.line.tension = 0.4;
Chart.defaults.elements.line.borderWidth = 3;
Chart.defaults.interaction = { mode: 'index', intersect: false } as any;
Chart.defaults.layout = { padding: { left: 2, right: 8, top: 6, bottom: 0 } };
(Chart.defaults as any).datasets = (Chart.defaults as any).datasets || {};
(Chart.defaults as any).datasets.bar = (Chart.defaults as any).datasets.bar || {};
(Chart.defaults as any).datasets.bar.minBarLength = 0;
(Chart.defaults as any).datasets.bar.maxBarThickness = 30;

const dataLabelsPlugin = {
  id: 'dataLabelsPlugin',
  afterDatasetsDraw(chart: any) {
    const { ctx, chartArea } = chart;
    if (!chartArea) return;
    if ((chart.data.labels || []).length > 12) return;   // too many points: the tooltip says it
    
    const pointsByIndex = new Map<number, any[]>();
    
    chart.data.datasets.forEach((dataset: any, i: number) => {
      const meta = chart.getDatasetMeta(i);
      if (meta.hidden || (meta.type !== 'bar' && meta.type !== 'line')) return;
      
      meta.data.forEach((element: any, index: number) => {
        const rawVal = dataset.data[index];
        const labelText = chart.data.labels ? chart.data.labels[index] : 'x';
        if (rawVal == null || rawVal === 0 || labelText === '') return;
        
        // The tooltips on this same chart use `toLocaleString('en-IN')`, so a
        // figure appeared as 12,34,567 in the tooltip and 1,234,567 on the
        // label. `formatChartValue` gives both Indian grouping.
        let valStr = String(rawVal);
        if (typeof rawVal === 'number') {
          valStr = formatChartValue(rawVal);
        }

        if (!pointsByIndex.has(index)) pointsByIndex.set(index, []);
        
        let color = dataset.borderColor || dataset.backgroundColor || '#64748b';
        if (Array.isArray(color)) color = color[0] || '#64748b';

        pointsByIndex.get(index)!.push({
          valStr: valStr,
          x: element.x, // store individual x in case of slight offset
          y: element.y,
          color: color
        });
      });
    });

    ctx.save();
    ctx.font = 'bold 9px Inter, sans-serif';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';

    const padX = 6;
    const padY = 3;
    const h = 10 + padY * 2;
    const spacing = 2; 

    pointsByIndex.forEach((points, index) => {
      // Sort top to bottom
      points.sort((a, b) => a.y - b.y);
      
      let currentY = -9999;
      
      points.forEach(p => {
        let desiredY = p.y - h - 5; 
        
        if (desiredY < chartArea.top) desiredY = p.y + 6;
        
        if (desiredY < currentY + h + spacing) {
          desiredY = currentY + h + spacing;
        }
        
        // Constrain to bottom of chart Area just in case
        if (desiredY + h > chartArea.bottom) {
           desiredY = chartArea.bottom - h;
           // We can't perfectly avoid overlap if squeezed at the bottom, but it's rare
        }

        const w = ctx.measureText(p.valStr).width + padX * 2;
        let lx = p.x - w / 2;
        let ly = desiredY;
        
        // Draw soft background pill
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = p.color;
        ctx.beginPath();
        ctx.roundRect(lx, ly, w, h, 4);
        ctx.fill();
        
        // Draw text
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = p.color;
        ctx.fillText(p.valStr, lx + w / 2, ly + h / 2 + 1);
        
        currentY = ly;
      });
    });
    ctx.restore();
  }
};

/** The look every chart in this module shares: flat teal bars, a thin line for counts, no gradient
 *  areas, no axis titles. The older tabs build their charts in a dozen places; they all pass through
 *  here so none of them keeps its own style. */
const TEAL = '#2aa3ae';
const SERIES = ['#2aa3ae', '#7c5cdc', '#f59a2c', '#ec4899', '#9aa5b8'];
function tidyConfig(cfg: any): any {
  if (!cfg || cfg.type === 'doughnut' || cfg.type === 'pie') return cfg;
  const sets: any[] = cfg.data?.datasets || [];
  const main = sets.filter(s => s.yAxisID !== 'y1');
  sets.forEach((ds: any, i: number) => {
    if (ds.yAxisID === 'y1') {                       // a count or average riding on the right axis
      Object.assign(ds, { type: 'line', borderColor: '#f59a2c', backgroundColor: 'rgba(245,154,44,0)', borderWidth: 1.5,
        fill: false, order: -1, pointRadius: 2, pointBackgroundColor: '#f59a2c', pointBorderColor: '#f59a2c', tension: 0.3 });
      return;
    }
    if (Array.isArray(ds.backgroundColor)) return;   // one colour per bar (category charts): leave as set
    const colour = main.length === 1 ? TEAL : SERIES[i % SERIES.length];
    ds.borderColor = colour;
    if ((ds.type || cfg.type) === 'line') {
      Object.assign(ds, { backgroundColor: colour + '22', fill: true, pointBackgroundColor: colour, pointBorderColor: colour, pointBorderWidth: 1 });
    } else {
      Object.assign(ds, { backgroundColor: colour, borderWidth: 0, borderRadius: 3, maxBarThickness: 26 });
      delete ds.fill;
    }
  });
  const o = cfg.options || (cfg.options = {});
  o.animation = { duration: 400 };
  o.layout = { padding: { top: 16, right: 6, bottom: 0, left: 0 } };
  Object.values(o.scales || {}).forEach((s: any) => { if (s && s.title) s.title.display = false; });
  return cfg;
}
function makeChart(el: any, cfg: any): Chart { return new Chart(el, tidyConfig(cfg)); }

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-dashboard',
  imports: [CommonModule, FormsModule, LocationSelectorComponent, AnalyticsReportsComponent, UsageReportComponent],
  templateUrl: './dashboard.html',
  styleUrl: './dashboard.css',
  providers: [CurrencyPipe, DatePipe]
})

export class DashboardComponent implements OnInit, AfterViewInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private locationMemory = inject(LocationMemory);
  private moduleAccess = inject(ModuleAccessService);
  apiService = inject(ApiService);
  cdr = inject(ChangeDetectorRef);
  chartType: 'line' | 'bar' = 'bar';

  setChartType(type: 'line' | 'bar') {
    if (this.chartType === type) return;
    this.chartType = type;
    this.onFilterChange();
  }

  toggleChartType() {
    this.chartType = this.chartType === 'line' ? 'bar' : 'line';
    this.onFilterChange();
  }

  downloadChart(chartId: string, fileName: string) {
    const canvas = document.getElementById(chartId) as HTMLCanvasElement;
    if (canvas) {
      const link = document.createElement('a');
      const tempCanvas = document.createElement('canvas');
      tempCanvas.width = canvas.width;
      tempCanvas.height = canvas.height;
      const ctx = tempCanvas.getContext('2d');
      if (ctx) {
        ctx.fillStyle = '#ffffff'; // white background
        ctx.fillRect(0, 0, tempCanvas.width, tempCanvas.height);
        ctx.drawImage(canvas, 0, 0);
      }
      link.href = tempCanvas.toDataURL('image/png');
      link.download = `${fileName}.png`;
      link.click();
    }
  }


  activeTab: string = 'summary';

  /** The tabs, in the order they are shown. Each is its own permission (Dashboard > <tab>). */
  private static readonly TABS = ['summary', 'revenues', 'clients', 'finance', 'services_products', 'staff', 'usage_report'];

  /** May this person open that tab? A role can be given one tab and not the others. */
  canTab(tab: string): boolean {
    return this.moduleAccess.can('dashboard', tab, 'read');
  }
  /** Bumped when the filters change, so the report sections inside each tab reload. */
  refreshTick = 0;
  /** Bumped when a bill is paid anywhere we can see, so the open tab refreshes quietly. */
  liveTick = 0;
  isLoading: boolean = false;
  isOwner: boolean = false;
  centers: any[] = [];
  selectedCenter: any = null;

  startDate: string = '';
  endDate: string = '';
  currentRange: string = 'thisMonth';

  // Data properties
  summaryData: any = null;

  private static readonly PAYMENT_LABELS: Record<string, string> = {
    cash: 'Cash', credit_card: 'Credit card', upi: 'UPI', google_pay: 'Google Pay', phone_pe: 'PhonePe',
    paytm: 'Paytm', bharat_pe: 'BharatPe', cheque_net_banking: 'Cheque / net banking', nearbuy: 'NearBuy',
    value_card: 'Value card', cashback_wallet: 'Cashback wallet', advance: 'Advance', gift_card: 'Gift card', other: 'Other',
  };
  paymentLabel(bucket: string): string {
    return DashboardComponent.PAYMENT_LABELS[bucket] || bucket;
  }
  trackByMethod(_: number, p: any) { return p.method; }
  revenuesData: any = null;
  clientsData: any = null;
  financeData: any = null;
  staffData: any = null;

  // Chart instances to destroy before re-rendering
  hasGlobalAccess: boolean = false;
  financeDataKeys: string[] = [];
  servicesProductsData: any = null;
  charts: any = {};

  selectedDrillDownMonth: string | null = null;
  drilledMonthDailyData: any[] | null = null;
  selectedDrillDownDay: string | null = null;

  selectedClientDrillDownMonth: string | null = null;

  ngOnInit() {
    // Open on the first tab this person may see (not always Overview).
    const first = DashboardComponent.TABS.find(t => this.canTab(t));
    if (first) this.activeTab = first;
    this.apiService.dashboardUpdates$.pipe(debounceTime(1500), takeUntilDestroyed(this.destroyRef))
      .subscribe(() => { this.liveTick++; this.cdr.markForCheck(); });
    this.applyChartTheme();
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true);
      } catch (e) { }
    }

    const today = new Date();
    const y = today.getFullYear();
    const m = today.getMonth();
    const d = today.getDate();
    this.startDate = `${y}-${String(m + 1).padStart(2, '0')}-01`;
    this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;

    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.centers = Array.isArray(data) ? data : (data.results || []);
      if (!this.hasGlobalAccess) {
        const u = localStorage.getItem('user');
        if (u) {
          const uObj = JSON.parse(u);
          this.selectedCenter = uObj?.center_id || null;
        }
        if (this.centers.length > 0 && !this.centers.some((c: any) => c.id == this.selectedCenter)) {
          this.selectedCenter = this.centers[0].id;
        }
      } else {
        const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
        if (remembered !== undefined) this.selectedCenter = remembered;
      }
      this.loadTabData();
      this.cdr.detectChanges();
    });
  }

  ngAfterViewInit() { }

  setTab(tab: string) {
    this.activeTab = tab;
    this.destroyCharts();
    this.loadTabData();
  }


  setDateRange(range: string) {
    this.currentRange = range;
    const today = new Date();
    const y = today.getFullYear();
    const m = today.getMonth();
    const d = today.getDate();

    if (range === 'thisMonth') {
      this.startDate = `${y}-${String(m + 1).padStart(2, '0')}-01`;
      this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'lastMonth') {
      const first = new Date(y, m - 1, 1);
      const last = new Date(y, m, 0);
      this.startDate = `${first.getFullYear()}-${String(first.getMonth() + 1).padStart(2, '0')}-01`;
      this.endDate = `${last.getFullYear()}-${String(last.getMonth() + 1).padStart(2, '0')}-${String(last.getDate()).padStart(2, '0')}`;
    } else if (range === 'last30') {
      const from = new Date(y, m, d - 29);
      this.startDate = `${from.getFullYear()}-${String(from.getMonth() + 1).padStart(2, '0')}-${String(from.getDate()).padStart(2, '0')}`;
      this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'past3Months') {
      const past = new Date(y, m - 2, 1);
      this.startDate = `${past.getFullYear()}-${String(past.getMonth() + 1).padStart(2, '0')}-01`;
      this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'past6Months') {
      const past = new Date(y, m - 5, 1);
      this.startDate = `${past.getFullYear()}-${String(past.getMonth() + 1).padStart(2, '0')}-01`;
      this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'past12Months') {
      const past = new Date(y, m - 11, 1);
      this.startDate = `${past.getFullYear()}-${String(past.getMonth() + 1).padStart(2, '0')}-01`;
      this.endDate = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    }
    this.onFilterChange();
  }

  getTotal(arr: any[], key: string) { return (arr || []).reduce((s, i) => s + (i[key] || 0), 0); }
  getBest(arr: any[], key: string) {
    if (!arr || !arr.length) return '';
    const best = (arr || []).reduce((max, item) => (item[key] || 0) > (max[key] || 0) ? item : max, arr[0]);
    return best.day || best.month || best.hour || '';
  }
  getAvg(arr: any[], key: string) {
    if (!arr || !arr.length) return 0;
    return this.getTotal(arr, key) / arr.length;
  }

  /** Typing a date is a custom range: no preset stays highlighted. */
  onCustomDates() {
    this.currentRange = 'custom';
    this.onFilterChange();
  }

  onFilterChange() {
    this.refreshTick++;
    this.destroyCharts();
    this.loadTabData();
  }

  // --- CHART DATA AGGREGATOR ---
  // Aggregates a dictionary of { "YYYY-MM-DD": { prop1: val, prop2: val } } into monthly buckets
  aggregateToMonthly(dataObj: any): { aggregated: any, labels: string[] } {
    if (!dataObj) return { aggregated: {}, labels: [] };
    const monthlyData: any = {};
    const labelsSet = new Set<string>();

    Object.keys(dataObj).forEach(dateStr => {
      if (!dateStr || dateStr.length < 7) return;
      const date = new Date(dateStr);
      if (isNaN(date.getTime())) return;

      const monthName = date.toLocaleString('default', { month: 'short', year: '2-digit' }); // e.g., 'Jan 26'
      if (!monthlyData[monthName]) {
        monthlyData[monthName] = {};
        labelsSet.add(monthName);
      }

      const dailyMetrics = dataObj[dateStr];
      Object.keys(dailyMetrics).forEach(metricKey => {
        if (typeof dailyMetrics[metricKey] === 'object' && dailyMetrics[metricKey] !== null) {
           if (!monthlyData[monthName][metricKey]) monthlyData[monthName][metricKey] = {};
           const innerMetrics = dailyMetrics[metricKey];
           Object.keys(innerMetrics).forEach(k => {
             if (typeof innerMetrics[k] === 'number') {
               monthlyData[monthName][metricKey][k] = (monthlyData[monthName][metricKey][k] || 0) + innerMetrics[k];
             }
           });
        } else if (typeof dailyMetrics[metricKey] === 'number') {
           monthlyData[monthName][metricKey] = (monthlyData[monthName][metricKey] || 0) + dailyMetrics[metricKey];
        }
      });
    });

    const sortedLabels = Array.from(labelsSet).sort((a, b) => {
      const [m1, y1] = a.split(' ');
      const [m2, y2] = b.split(' ');
      return new Date(`${m1} 1, 20${y1}`).getTime() - new Date(`${m2} 1, 20${y2}`).getTime();
    });

    return { aggregated: monthlyData, labels: sortedLabels };
  }

  getCenterId(): number | undefined {
    return (this.selectedCenter === 'null' || !this.selectedCenter) ? undefined : parseInt(this.selectedCenter, 10);
  }

  loadTabData() {
    this.isLoading = true;
    const cid = this.getCenterId();
    const params = { center_id: cid, start_date: this.startDate, end_date: this.endDate };

    if (this.activeTab === 'summary') {
      this.apiService.get('salon_admin/api/dashboard/summary/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.summaryData = res;
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderSummaryCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else if (this.activeTab === 'revenues') {
      this.apiService.get('salon_admin/api/dashboard/revenues/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.revenuesData = res;
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderRevenuesCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else if (this.activeTab === 'clients') {
      this.apiService.get('salon_admin/api/dashboard/clients/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.clientsData = res;
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderClientsCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else if (this.activeTab === 'finance') {
      this.apiService.get('salon_admin/api/dashboard/finance/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.financeData = res;
          this.financeDataKeys = Object.keys(res || {});
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderFinanceCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else if (this.activeTab === 'services_products') {
      this.apiService.get('salon_admin/api/dashboard/services_products/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.servicesProductsData = res;
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderServicesProductsCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else if (this.activeTab === 'staff') {
      this.apiService.get('salon_admin/api/dashboard/staff/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res) => {
          this.staffData = res;
          this.isLoading = false;
          this.cdr.detectChanges();
          setTimeout(() => this.renderStaffCharts(), 150);
        },
        error: () => { this.isLoading = false; this.cdr.detectChanges(); }
      });
    } else {
      // The Usage Report loads its own sections; the page has nothing to fetch for it.
      this.isLoading = false;
    }
  }

  destroyCharts() {
    Object.keys(this.charts).forEach(key => {
      if (this.charts[key]) this.charts[key].destroy();
    });
    this.charts = {};
  }

  // Pads single-point data so Chart.js draws a real line instead of a floating dot
  padSinglePoint(labels: any[], ...dataArrays: any[][]): { labels: any[], dataArrays: any[][] } {
    if (this.chartType === 'line' && labels && labels.length === 1) {
      const newLabels = ['', labels[0], ''];
      const newDataArrays = dataArrays.map(arr => {
        const val = arr && arr.length > 0 ? arr[0] : 0;
        const ghostVal = typeof val === 'number' && val > 0 ? val * 0.95 : 0;
        return [ghostVal, val, ghostVal];
      });
      return { labels: newLabels, dataArrays: newDataArrays };
    }
    return { labels, dataArrays };
  }

  // --- SUMMARY CHARTS ---
  renderSummaryCharts() {
    if (!this.summaryData) return;
    // Donut
    const ctxDonut = document.getElementById('revenuePieChart') as HTMLCanvasElement;
    if (ctxDonut) {
      const breakdown = this.summaryData.revenue_breakdown || { service: 0, product: 0, membership: 0, package: 0, card: 0, advance: 0 };

      const donutTotal = [breakdown.service, breakdown.product, breakdown.membership, breakdown.package, breakdown.card, breakdown.advance]
        .reduce((a: number, b: any) => a + (Number(b) || 0), 0);
      const centerTotal = {
        id: 'centerTotal',
        afterDraw: (chart: any) => {
          const { ctx, chartArea } = chart;
          if (!chartArea) return;
          const cx = (chartArea.left + chartArea.right) / 2;
          const cy = (chartArea.top + chartArea.bottom) / 2;
          const dark = document.body.getAttribute('data-theme') === 'dark';
          ctx.save();
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillStyle = dark ? '#8f98a8' : '#8b8e96';
          ctx.font = "600 12px 'Outfit', sans-serif";
          ctx.fillText('Total', cx, cy - 16);
          ctx.fillStyle = dark ? '#eef1f6' : '#1b1c20';
          ctx.font = "700 22px 'Outfit', sans-serif";
          const lakh = donutTotal >= 100000 ? (donutTotal / 100000).toFixed(2).replace(/\.?0+$/, '') + ' L' : Math.round(donutTotal).toLocaleString('en-IN');
          ctx.fillText('\u20B9' + lakh, cx, cy + 8);
          ctx.restore();
        },
      };
      this.charts['donut'] = makeChart(ctxDonut, {
        type: 'doughnut',
        data: {
          labels: ['Services', 'Products', 'Memberships', 'Packages', 'Value Cards', 'Advances'],
          datasets: [{
            data: [breakdown.service, breakdown.product, breakdown.membership, breakdown.package, breakdown.card, breakdown.advance],
            backgroundColor: ['#3fb6c0', '#9be15d', '#f59a2c', '#7c5cdc', '#ec4899', '#2563eb'],
            borderWidth: 0,
            spacing: 4,
            borderRadius: 10,
            hoverOffset: 8,
          }]
        },
        options: { responsive: true, maintainAspectRatio: false, cutout: '74%', animation: { duration: 900, easing: 'easeOutQuart' }, plugins: { legend: { position: 'bottom' } } },
        plugins: [centerTotal]
      });
    }

    // Bar
    const ctxBar = document.getElementById('summaryLineChart') as HTMLCanvasElement;
    if (ctxBar) {
      const labels = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
      const data = this.summaryData.weekday_counts || [0, 0, 0, 0, 0, 0, 0];
      const peak = data.indexOf(Math.max(...data));
      const isBar = this.chartType === 'bar';
      this.charts['sumBar'] = makeChart(ctxBar, {
        type: this.chartType as any,
        data: {
          labels,
          datasets: [{
            label: 'Invoice Count',
            data,
            borderColor: '#3fb6c0',
            backgroundColor: (context: any) => {
              if (isBar) return context.dataIndex === peak ? '#c6e22d' : 'rgba(63, 182, 192, 0.3)';
              const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300);
              grad.addColorStop(0, 'rgba(63, 182, 192, 0.34)');
              grad.addColorStop(1, 'rgba(63, 182, 192, 0)');
              return grad;
            },
            fill: true,
            pointBackgroundColor: (c: any) => (c.dataIndex === peak ? '#c6e22d' : '#ffffff'),
            pointBorderColor: '#3fb6c0',
            pointRadius: (c: any) => (c.dataIndex === peak ? 4 : 2),
            pointBorderWidth: 2.5,
          }]
        },
        options: { responsive: true, maintainAspectRatio: false, animation: { duration: 900, easing: 'easeOutQuart' }, scales: { x: { offset: isBar || labels.length <= 1 } } }, plugins: [dataLabelsPlugin]
      });
    }
  }

  // --- Overview hero: display helpers (nothing here changes data) ---
  private compactParts(value: any): { v: string; u: string } {
    const n = Number(value) || 0;
    const trim = (x: number) => x.toFixed(1).replace(/\.0$/, '');
    if (n >= 10000000) return { v: trim(n / 10000000), u: 'Cr' };
    if (n >= 100000) return { v: trim(n / 100000), u: 'L' };
    if (n >= 1000) return { v: trim(n / 1000), u: 'k' };
    return { v: String(Math.round(n)), u: '' };
  }
  get revParts() { return this.compactParts(this.summaryData?.revenue); }
  get recvParts() { return this.compactParts(this.summaryData?.money_in?.real_money_received); }
  get collectParts() { return this.compactParts(this.summaryData?.money_in?.to_collect); }
  get stockParts() { return this.compactParts(this.summaryData?.inventory?.amount); }

  get collectedPct(): number {
    const mi = this.summaryData?.money_in;
    const received = Number(mi?.real_money_received) || 0;
    const open = Number(mi?.to_collect) || 0;
    return received + open > 0 ? Math.round((received / (received + open)) * 100) : 100;
  }

  get repeatShare(): number {
    const total = Number(this.summaryData?.total_clients) || 0;
    return total > 0 ? Math.round(((Number(this.summaryData?.repeat_clients) || 0) / total) * 100) : 0;
  }

  get paceLabel(): string {
    if (!(Number(this.summaryData?.target) > 0)) return 'No target set';
    if (this.targetPct >= 100) return 'Target met';
    const now = new Date();
    const dim = new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate();
    return this.targetPct >= (now.getDate() / dim) * 90 ? 'On track' : 'Behind pace';
  }

  /** n of `count` dots lit, for the small dotted bars. */
  dotRow(pct: number, count = 12): boolean[] {
    const lit = Math.round((Math.min(Math.max(pct, 0), 100) / 100) * count);
    return Array.from({ length: count }, (_, i) => i < lit);
  }
  readonly tickMarks: boolean[] = Array.from({ length: 40 }, (_, i) => i % 5 === 0);

  /** The gauge on the lead card: a half circle of radius 80 (arc length 251). */
  get gaugeDash251(): string { return `${(this.targetPct / 100) * 251.3} 251.3`; }
  get needleX(): number { return 100 + 66 * Math.cos(Math.PI * (1 - this.targetPct / 100)); }
  get needleY(): number { return 100 - 66 * Math.sin(Math.PI * (1 - this.targetPct / 100)); }

  /** Invoices per weekday as a small line, from the same counts the weekday chart uses. */
  get sparkPoints(): string {
    const d: number[] = (this.summaryData?.weekday_counts || [0, 0, 0, 0, 0, 0, 0]).map((x: any) => Number(x) || 0);
    const max = Math.max(...d, 1);
    return d.map((v, i) => `${(i / (d.length - 1)) * 100},${28 - (v / max) * 24}`).join(' ');
  }

  /** Share of the target reached, for the gauge on the Total Revenue tile. */
  get targetPct(): number {
    const target = Number(this.summaryData?.target) || 0;
    const rev = Number(this.summaryData?.revenue) || 0;
    return target > 0 ? Math.min(100, Math.round((rev / target) * 100)) : 0;
  }

  /** The gauge arc is a half circle of radius 40: length 125.66. */
  get gaugeDash(): string {
    return `${(this.targetPct / 100) * 125.66} 125.66`;
  }

  // --- Chart theme: one look for every chart on the page ---
  private applyChartTheme() {
    // The app runs at a CSS zoom (html { zoom: .8 }). Chart.js reads the pointer in zoomed pixels
    // but lays the chart out in unzoomed ones, so hovering Thursday showed Wednesday and the error
    // grew towards the right. Put the pointer back in the chart's own units, once per event.
    if (!(Chart as any)._zoomFixRegistered) {
      (Chart as any)._zoomFixRegistered = true;
      Chart.register({
        id: 'zoomPointerFix',
        beforeEvent(chart: any, args: any) {
          const e = args.event;
          if (!e || e._zoomFixed || e.x == null) return;
          const c: HTMLCanvasElement = chart.canvas;
          const z = c.clientWidth ? c.getBoundingClientRect().width / c.clientWidth : 1;
          if (z > 0 && Math.abs(z - 1) > 0.01) {
            e.x = e.x / z;
            e.y = e.y / z;
          }
          e._zoomFixed = true;
        },
      } as any);
    }
    const d: any = Chart.defaults;
    d.font.family = "'Outfit', 'Inter', sans-serif";
    d.font.size = 10;
    d.color = '#8a8f9a';
    d.borderColor = 'rgba(128, 128, 128, 0.14)';
    // A click anywhere along a month selects the nearest point, not only the dot itself.
    d.interaction = { mode: 'nearest', axis: 'x', intersect: false };
    if (d.scale) {
      d.scale.grid = Object.assign(d.scale.grid || {}, { color: 'rgba(128, 128, 128, 0.14)', borderDash: [3, 6], drawTicks: false });
      d.scale.border = Object.assign(d.scale.border || {}, { display: false });
      d.scale.ticks = Object.assign(d.scale.ticks || {}, { padding: 4, maxRotation: 0, autoSkip: true, maxTicksLimit: 12 });
    }
    d.elements.line.tension = 0.3;
    d.elements.line.borderWidth = 2;
    d.elements.point.radius = 2;
    d.elements.point.hoverRadius = 4;
    d.elements.point.hoverBorderWidth = 2;
    d.elements.bar.borderRadius = 4;
    d.elements.bar.borderSkipped = false;
    d.plugins.legend.labels.usePointStyle = true;
    d.plugins.legend.labels.boxWidth = 7;
    // A line or bar chart with one series needs no legend (it only repeated the card title in a big
    // circle). Doughnuts keep theirs: their legend items are slices, which carry no datasetIndex.
    d.plugins.legend.labels.filter = (item: any, data: any) => item.datasetIndex === undefined || (data.datasets?.length ?? 0) > 1;
    d.plugins.legend.labels.font = { size: 10 };
    d.plugins.legend.labels.padding = 8;
    Object.assign(d.plugins.tooltip, {
      backgroundColor: 'rgba(20, 22, 30, 0.92)',
      titleColor: '#ffffff',
      bodyColor: '#e5e7eb',
      padding: 12,
      cornerRadius: 14,
      boxPadding: 6,
      usePointStyle: true,
      borderColor: 'rgba(255, 255, 255, 0.08)',
      borderWidth: 1,
    });
  }

  // --- Month chips: pick any month (the current one too) without hitting a chart point ---
  get revenueMonthChips(): string[] {
    return (this.revenuesData?.monthly || []).map((m: any) => m.month).filter((m: any) => !!m);
  }

  get clientMonthChips(): string[] {
    return Object.keys(this.clientsData?.monthly_breakdown || {});
  }

  /** "Oct-2026" -> first and last day of that month. */
  private monthBounds(monthStr: string): { sd: string; ed: string } | null {
    const [mon, yy] = (monthStr || '').split('-');
    const mNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    const m = mNames.indexOf(mon) + 1;
    if (!m || !yy) return null;
    const last = new Date(parseInt(yy), m, 0).getDate();
    const mm = String(m).padStart(2, '0');
    return { sd: `${yy}-${mm}-01`, ed: `${yy}-${mm}-${String(last).padStart(2, '0')}` };
  }

  drillIntoMonth(monthStr: string) {
    const b = this.monthBounds(monthStr);
    if (!b) return;
    this.selectedDrillDownMonth = monthStr;
    this.selectedDrillDownDay = null;
    this.fetchDrillDownData('month', b.sd, b.ed);
    this.cdr.detectChanges();
  }

  drillIntoClientMonth(monthStr: string) {
    const b = this.monthBounds(monthStr);
    if (!b) return;
    this.selectedClientDrillDownMonth = monthStr;
    this.fetchClientDrillDownData('month', b.sd, b.ed);
    this.cdr.detectChanges();
  }

  // --- DRILL-DOWN LOGIC ---
  resetDrillDownMonth() {
    this.selectedDrillDownMonth = null;
    this.drilledMonthDailyData = null;
    this.selectedDrillDownDay = null; // cascade reset
    // Re-render original data
    if (this.revenuesData) {
      this.renderRevenuesCharts(this.revenuesData.daily, this.revenuesData.hourly);
    }
    this.cdr.detectChanges();
  }

  resetDrillDownDay() {
    this.selectedDrillDownDay = null;
    // We might still be drilled down into a month, so re-render with the month's hourly data
    if (this.selectedDrillDownMonth) {
      // Re-fetch the month's data to get its hourly aggregation, or we can just fetch the month again
      const [mon, yy] = this.selectedDrillDownMonth.split('-');
      const y = yy;
      const mNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
      const m = mNames.indexOf(mon) + 1;
      const sd = `${y}-${String(m).padStart(2, '0')}-01`;
      const ed = new Date(parseInt(y), m, 0);
      const edStr = `${y}-${String(m).padStart(2, '0')}-${String(ed.getDate()).padStart(2, '0')}`;
      this.fetchDrillDownData('month', sd, edStr);
    } else {
      if (this.revenuesData) {
        this.renderRevenuesCharts(this.revenuesData.daily, this.revenuesData.hourly);
      }
    }
    this.cdr.detectChanges();
  }

  fetchDrillDownData(level: 'month'|'day', sd: string, ed: string) {
    const cid = this.getCenterId();
    const params = { center_id: cid, start_date: sd, end_date: ed };
    this.apiService.get('salon_admin/api/dashboard/revenues/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        if (level === 'month') {
          // Keep original monthly data, but replace daily and hourly
          this.drilledMonthDailyData = res.daily;
          this.renderRevenuesCharts(res.daily, res.hourly);
        } else if (level === 'day') {
          // Keep whatever daily data is currently showing, but replace hourly
          // But actually renderRevenuesCharts uses the class properties, so we pass overrides
          this.renderRevenuesCharts(this.drilledMonthDailyData, res.hourly);
        }
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  resetClientDrillDownMonth() {
    this.selectedClientDrillDownMonth = null;
    this.renderClientsCharts();
    this.cdr.detectChanges();
  }

  fetchClientDrillDownData(level: 'month', sd: string, ed: string) {
    const cid = this.getCenterId();
    const params = { center_id: cid, start_date: sd, end_date: ed };
    this.apiService.get('salon_admin/api/dashboard/clients/', params).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.renderClientsCharts(res.daily_footfall);
        this.cdr.detectChanges();
      },
      error: () => {}
    });
  }

  // --- REVENUES CHARTS ---
  renderRevenuesCharts(dailyDataOverride: any = null, hourlyDataOverride: any = null) {
    if (!this.revenuesData) return;
    if (this.charts['revMonthlyChart']) this.charts['revMonthlyChart'].destroy();
    if (this.charts['revDailyChart']) this.charts['revDailyChart'].destroy();
    if (this.charts['revHourlyChart']) this.charts['revHourlyChart'].destroy();

    const renderLine = (id: string, labels: any[], data: any[], color: string, label: string, onClickHandler?: (e: any, elements: any[], chart: any) => void) => {
      const ctx = document.getElementById(id) as HTMLCanvasElement;
      if (!ctx) return;
      // Pad single-point to force line rendering
      const padded = this.padSinglePoint(labels, data);
      const paddedLabels = padded.labels;
      const paddedData = padded.dataArrays[0];
      this.charts[id] = makeChart(ctx, {
        type: this.chartType as any,
        data: {
          labels: paddedLabels,
          datasets: [{ 
            label, 
            data: paddedData, 
            borderColor: color,
            backgroundColor: (context: any) => {
              const canvasCtx = context.chart.ctx;
              const grad = canvasCtx.createLinearGradient(0, 0, 0, 200);
              grad.addColorStop(0, color + '66');
              grad.addColorStop(1, color + '00');
              return grad;
            },
            fill: true,
            pointBackgroundColor: '#ffffff',
            pointBorderColor: color,
            pointBorderWidth: 1.5,
            pointRadius: (ctx: any) => ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1 ? (paddedLabels[0] === '' ? 0 : 2) : 2,
            pointHoverRadius: 4
          }]
        },
        options: { 
          responsive: true, 
          maintainAspectRatio: false, 
          animation: { duration: 800, easing: 'easeOutQuart' },
          scales: { x: { offset: this.chartType === 'bar' || paddedLabels.length <= 1 } },
          onClick: onClickHandler ? (e: any, elements: any, chart: any) => {
            // Remap index back to original if padded
            if (!elements || !elements.length) return;
            let idx = elements[0].index;
            if (paddedLabels[0] === '') idx = idx - 1; // offset for ghost
            const fakeMeta = { ...elements[0], index: idx };
            onClickHandler(e, [fakeMeta], chart);
          } : undefined,
          onHover: onClickHandler ? (e: any, elements: any) => {
            if (e.native && e.native.target) {
              (e.native.target as HTMLElement).style.cursor = elements.length ? 'pointer' : 'default';
            }
          } : undefined
        }, 
        plugins: [dataLabelsPlugin]
      });
    };

    const rawMonthlyLabels = (this.revenuesData.monthly || []).map((d: any) => d.month);
    const rawMonthlyData = (this.revenuesData.monthly || []).map((d: any) => d.revenue);
    const monthlyLabels = rawMonthlyLabels; // Keep original for drill-down index mapping
    renderLine('revMonthlyChart', monthlyLabels, rawMonthlyData, '#f59e0b', 'Monthly Revenue', (e: any, elements: any, chart: any) => {
      if (!elements || !elements.length) return;
      const monthStr = monthlyLabels[elements[0].index]; // e.g. "Mar-2026"
      if (!monthStr) return; // ghost point
      this.drillIntoMonth(monthStr);
    });

    let dailyData = dailyDataOverride || this.revenuesData.daily || [];
    let dailyTitle = 'Daily Revenue';
    
    // Only aggregate if we are not drilled down, AND range is 6m/12m
    if (!this.selectedDrillDownMonth && (this.currentRange === 'past3Months' || this.currentRange === 'past6Months' || this.currentRange === 'past12Months')) {
      const weeklyMap: any = {};
      const weeklyOrder: string[] = [];
      
      dailyData.forEach((d: any) => {
        const date = new Date(d.day);
        if (isNaN(date.getTime())) return;
        
        // Find Monday of the week
        const dayOfWeek = date.getDay();
        const diff = date.getDate() - dayOfWeek + (dayOfWeek === 0 ? -6 : 1);
        const weekStart = new Date(date.setDate(diff));
        const weekStr = weekStart.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
        
        if (weeklyMap[weekStr] === undefined) {
          weeklyMap[weekStr] = 0;
          weeklyOrder.push(weekStr);
        }
        weeklyMap[weekStr] += (d.revenue || 0);
      });
      
      dailyData = weeklyOrder.map(k => ({ day: `Week of ${k}`, revenue: weeklyMap[k], originalDate: null }));
      dailyTitle = 'Weekly Revenue';
    }

    const dailyLabels = dailyData.map((d: any) => d.day);
    renderLine('revDailyChart', dailyLabels, dailyData.map((d: any) => d.revenue), '#10b981', dailyTitle, (e: any, elements: any, chart: any) => {
      if (!elements || !elements.length) return;
      const index = elements[0].index;
      const dayStr = dailyLabels[index]; 
      
      if (dayStr.startsWith('Week of')) {
        // Can't easily drill down a week directly to a day without calculating dates
        return; 
      }
      this.selectedDrillDownDay = dayStr;
      this.fetchDrillDownData('day', dayStr, dayStr);
      this.cdr.detectChanges();
    });

    const hourlyData = hourlyDataOverride || this.revenuesData.hourly || [];
    renderLine('revHourlyChart', hourlyData.map((d: any) => d.hour), hourlyData.map((d: any) => d.revenue), '#3b82f6', 'Hourly Revenue');
  }

  // --- CLIENTS CHARTS ---
  renderClientsCharts(dailyDataOverride?: any) {
    if (!this.clientsData || !this.clientsData.trends) return;

    let trendData = this.clientsData.trends;
    let labels = Object.keys(trendData);
    // Note: backend always returns already-monthly trend data (e.g. "Jan-2026"),
    // so no re-aggregation is needed regardless of the selected range.

    const ctxBreakdown = document.getElementById('clientBreakdownChart') as HTMLCanvasElement;
    if (ctxBreakdown && this.clientsData.monthly_breakdown) {
      const breakdownData = this.clientsData.monthly_breakdown;
      const rawBLabels = Object.keys(breakdownData);
      const rawNew = rawBLabels.map(l => breakdownData[l]?.new || 0);
      const rawRepeat = rawBLabels.map(l => breakdownData[l]?.repeat || 0);
      const rawMember = rawBLabels.map(l => breakdownData[l]?.member || 0);
      const rawNonMember = rawBLabels.map(l => breakdownData[l]?.non_member || 0);

      const { labels: bLabels, dataArrays: bDataArrays } = this.padSinglePoint(rawBLabels, rawNew, rawRepeat, rawMember, rawNonMember);
      const [newClients, repeatClients, memberInvoices, nonMemberInvoices] = bDataArrays;

      this.charts['breakdown'] = makeChart(ctxBreakdown, {
        type: this.chartType as any,
        data: {
          labels: bLabels,
          datasets: [
            { 
              label: 'New Clients', 
              data: newClients, 
              borderColor: '#0ea5e9', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#0ea5e966'); grad.addColorStop(1, '#0ea5e900'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => bLabels[0] === '' && (ctx.dataIndex === 0 || ctx.dataIndex === bLabels.length - 1) ? 0 : 2
            },
            { 
              label: 'Repeat Clients', 
              data: repeatClients, 
              borderColor: '#6366f1', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#6366f166'); grad.addColorStop(1, '#6366f100'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => bLabels[0] === '' && (ctx.dataIndex === 0 || ctx.dataIndex === bLabels.length - 1) ? 0 : 2
            },
            { 
              label: 'Members Invoices', 
              data: memberInvoices, 
              borderColor: '#10b981', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#10b98166'); grad.addColorStop(1, '#10b98100'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => bLabels[0] === '' && (ctx.dataIndex === 0 || ctx.dataIndex === bLabels.length - 1) ? 0 : 2
            },
            { 
              label: 'Non members Invoices', 
              data: nonMemberInvoices, 
              borderColor: '#f97316', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#f9731666'); grad.addColorStop(1, '#f9731600'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => bLabels[0] === '' && (ctx.dataIndex === 0 || ctx.dataIndex === bLabels.length - 1) ? 0 : 2
            }
          ]
        },
        options: { 
          responsive: true, 
          maintainAspectRatio: false, 
          animation: { duration: 800, easing: 'easeOutQuart' },
          plugins: { legend: { display: true, position: 'bottom', labels: { usePointStyle: true, boxWidth: 8, font: { size: 11 } } } },
          layout: { padding: { top: 30 } },
          scales: { x: { offset: this.chartType === 'bar' || bLabels.length <= 1 } },
          onClick: (e: any, elements: any[], chart: any) => {
            if (!elements || elements.length === 0) return;
            const index = elements[0].index;
            const rawBLabels = Object.keys(this.clientsData.monthly_breakdown);
            // Adjust index if padded
            const adjustedIndex = bLabels[0] === '' ? index - 1 : index;
            const monthStr = rawBLabels[adjustedIndex];
            if (!monthStr) return;
            this.drillIntoClientMonth(monthStr);
          }
        },
        plugins: [dataLabelsPlugin]
      });
    }

    const renderCombo = (id: string, prop: string, color: string) => {
      const ctx = document.getElementById(id) as HTMLCanvasElement;
      if (!ctx) return;
      const rawNewCounts = labels.map(l => trendData[l]?.[prop]?.new || 0);
      const rawRepeatCounts = labels.map(l => trendData[l]?.[prop]?.repeat || 0);
      const rawAvg = labels.map(l => {
        const c = trendData[l]?.[prop]?.count || 0;
        const r = trendData[l]?.[prop]?.revenue || 0;
        return c > 0 ? parseFloat((r / c).toFixed(2)) : 0;
      });

      const { labels: paddedLabels, dataArrays } = this.padSinglePoint(labels, rawNewCounts, rawRepeatCounts, rawAvg);
      const [newCounts, repeatCounts, avg] = dataArrays;
      const ghostFirst = paddedLabels[0] === '';

      this.charts[id] = makeChart(ctx, {
        type: this.chartType as any,
        data: {
          labels: paddedLabels,
          datasets: [
            { 
              type: this.chartType as any, 
              label: 'New Clients', 
              data: newCounts, 
              borderColor: '#0ea5e9', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#0ea5e966'); grad.addColorStop(1, '#0ea5e900'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1) ? 0 : 2,
              yAxisID: 'y' 
            } as any,
            { 
              type: this.chartType as any, 
              label: 'Repeat Clients', 
              data: repeatCounts, 
              borderColor: '#6366f1', 
              backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#6366f166'); grad.addColorStop(1, '#6366f100'); return grad; },
              fill: true,
              pointBackgroundColor: '#ffffff',
              pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1) ? 0 : 2,
              yAxisID: 'y' 
            } as any,
            { 
              type: this.chartType as any, 
              label: 'Avg Spend (Right Axes)', 
              data: avg, 
              borderColor: '#10b981', 
              pointBackgroundColor: '#ffffff', 
              pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1) ? 0 : 2,
              yAxisID: 'y1' 
            } as any
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: { duration: 800, easing: 'easeOutQuart' },
          plugins: { legend: { display: true, position: 'bottom', labels: { usePointStyle: true, boxWidth: 8, font: { size: 11 } } } },
          layout: { padding: { top: 30 } },
          scales: {
            x: { offset: this.chartType === 'bar' || paddedLabels.length <= 1 },
            y: { type: 'linear', position: 'left' },
            y1: { type: 'linear', position: 'right', grid: { drawOnChartArea: false } }
          }
        },
        plugins: [dataLabelsPlugin]
      });
    };

    renderCombo('femaleTrendChart', 'female', '#ec4899');
    renderCombo('maleTrendChart', 'male', '#0ea5e9');
    renderCombo('unknownTrendChart', 'unknown', '#8b5cf6');

    const ctxDaily = document.getElementById('clientDailyChart') as HTMLCanvasElement;
    if (ctxDaily && this.clientsData.daily_footfall) {
      let dailyData = dailyDataOverride || this.clientsData.daily_footfall;
      let dailyTitle = 'Daily Client Footfall';

      if (!this.selectedClientDrillDownMonth && (this.currentRange === 'past3Months' || this.currentRange === 'past6Months' || this.currentRange === 'past12Months')) {
        const weeklyMap: any = {};
        const weeklyOrder: string[] = [];
        
        dailyData.forEach((d: any) => {
          const date = new Date(d.day);
          if (isNaN(date.getTime())) return;
          
          const dayOfWeek = date.getDay();
          const diff = date.getDate() - dayOfWeek + (dayOfWeek === 0 ? -6 : 1);
          const weekStart = new Date(date.setDate(diff));
          const weekStr = weekStart.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
          
          if (weeklyMap[weekStr] === undefined) {
            weeklyMap[weekStr] = 0;
            weeklyOrder.push(weekStr);
          }
          weeklyMap[weekStr] += (d.count || 0);
        });
        
        dailyData = weeklyOrder.map(k => ({ day: `Week of ${k}`, count: weeklyMap[k] }));
        dailyTitle = 'Weekly Client Footfall';
      }

      const rawDLabels = dailyData.map((d: any) => d.day);
      const rawDCounts = dailyData.map((d: any) => d.count);
      const { labels: dLabels, dataArrays: dArrays } = this.padSinglePoint(rawDLabels, rawDCounts);
      const dCounts = dArrays[0];
      const ghostFirst = dLabels[0] === '';
      
      this.charts['clientDaily'] = makeChart(ctxDaily, {
        type: this.chartType as any,
        data: {
          labels: dLabels,
          datasets: [{ 
            label: dailyTitle, 
            data: dCounts, 
            borderColor: '#3b82f6',
            backgroundColor: (context: any) => { const grad = context.chart.ctx.createLinearGradient(0, 0, 0, 300); grad.addColorStop(0, '#3b82f666'); grad.addColorStop(1, '#3b82f600'); return grad; },
            fill: true,
            pointBackgroundColor: '#ffffff',
            pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === dLabels.length - 1) ? 0 : 2
          }]
        },
        options: { 
          responsive: true, 
          maintainAspectRatio: false, 
          animation: { duration: 800, easing: 'easeOutQuart' },
          plugins: { legend: { display: false } },
          layout: { padding: { top: 30 } },
          scales: { x: { offset: this.chartType === 'bar' || dLabels.length <= 1 } }
        },
        plugins: [dataLabelsPlugin]
      });
    }
  }

  // --- FINANCE CHARTS ---
  renderFinanceCharts() {
    if (!this.financeData) return;
    
    let currentFinanceData = this.financeData;
    let months = Object.keys(currentFinanceData);
    // Note: backend already returns monthly-keyed data (e.g. "Jan-2026"),
    // so no re-aggregation is needed regardless of the selected range.

    const makeFinBarLine = (id: string, labels: string[], dataKey: string, barColor: string) => {
      try {
        const el = document.getElementById(id) as HTMLCanvasElement;
        if (!el) return;
        const c2d = el.getContext('2d')!;
        const grad = c2d.createLinearGradient(0, 0, 0, 250);
        grad.addColorStop(0, barColor + 'ff');
        grad.addColorStop(1, barColor + '00');

        const rawRevData = labels.map(m => currentFinanceData[m]?.[dataKey]?.revenue || 0);
        const rawCountData = labels.map(m => currentFinanceData[m]?.[dataKey]?.count || 0);

        const { labels: paddedLabels, dataArrays } = this.padSinglePoint(labels, rawRevData, rawCountData);
        const [revData, countData] = dataArrays;
        const ghostFirst = paddedLabels[0] === '';

        this.charts[id] = makeChart(el, {
          type: this.chartType as any,
          data: {
            labels: paddedLabels,
            datasets: [
              {
                type: this.chartType as any,
                label: 'Revenue',
                data: revData,
                borderColor: barColor,
                backgroundColor: grad,
                fill: true,
                pointBackgroundColor: '#ffffff',
                pointBorderColor: barColor,
                pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1) ? 0 : 2,
                yAxisID: 'y'
              },
              {
                type: this.chartType as any,
                label: 'Counts',
                data: countData,
                borderColor: '#3b82f6',
                backgroundColor: '#3b82f6',
                pointBackgroundColor: '#60a5fa',
                pointBorderColor: '#ffffff',
                pointBorderWidth: 1.5,
                pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === paddedLabels.length - 1) ? 0 : 2,
                borderWidth: 2,
                yAxisID: 'y1'
              }
            ]
          },
          options: {
            responsive: true, animation: { duration: 800, easing: 'easeOutQuart' },
            maintainAspectRatio: false,
            plugins: { legend: { display: false } },
            scales: {
              x: {
                offset: this.chartType === 'bar' || labels.length <= 1,
                grid: { display: false },
                ticks: { color: '#6b7280', font: { size: 10 } }
              },
              y: {
                type: 'linear',
                display: true,
                position: 'left',
                title: { display: true, text: 'Revenue', color: '#9ca3af', font: { size: 10 } },
                ticks: {
                  // `(v/1000)+'k'` gave 1500k for fifteen lakh.
                  callback: (v: any) => formatCompact(v),
                  color: '#9ca3af', font: { size: 10 }
                },
                grid: { display: true, color: 'rgba(0,0,0,0.035)' }
              },
              y1: {
                type: 'linear',
                display: true,
                position: 'right',
                title: { display: true, text: 'Counts', color: '#9ca3af', font: { size: 10 } },
                grid: { drawOnChartArea: false },
                ticks: {
                  color: '#9ca3af', font: { size: 10 },
                  stepSize: 1
                }
              }
            }
          },
          plugins: [dataLabelsPlugin]
        });
      } catch (e) { console.error('Error rendering', id, e); }
    };

    makeFinBarLine('finServicesChart', months, 'services', '#0d5d73');
    makeFinBarLine('finMembershipsChart', months, 'memberships', '#a5d6e9');
    makeFinBarLine('finProductsChart', months, 'products', '#86efac');
    makeFinBarLine('finPackagesChart', months, 'packages', '#6366f1');
    makeFinBarLine('finValueCardsChart', months, 'value_cards', '#f59e0b');
    makeFinBarLine('finAdvancesChart', months, 'advances', '#6b7280');
  }

  // --- STAFF CHARTS ---
  renderStaffCharts() {
    if (!this.staffData) return;
    const ctxMonth = document.getElementById('staffThisMonthChart') as HTMLCanvasElement;
    if (ctxMonth) {
      const rawLabels = (this.staffData.table || []).map((s: any) => s.name).slice(0, 10);
      const rawData = (this.staffData.table || []).map((s: any) => s.revenue || 0).slice(0, 10);
      const { labels, dataArrays } = this.padSinglePoint(rawLabels, rawData);
      const [data] = dataArrays;
      const ghostFirst = labels[0] === '';
      this.charts['staffMonth'] = makeChart(ctxMonth, {
        type: this.chartType as any,
        data: { labels, datasets: [{ 
          label: 'Revenue', 
          data, 
          borderColor: '#8b5cf6',
          backgroundColor: (context: any) => {
            const canvasCtx = context.chart.ctx;
            const grad = canvasCtx.createLinearGradient(0, 0, 0, 300);
            grad.addColorStop(0, '#8b5cf666');
            grad.addColorStop(1, '#8b5cf600');
            return grad;
          },
          fill: true,
          pointBackgroundColor: '#ffffff',
          pointBorderColor: '#8b5cf6',
          pointRadius: (ctx: any) => ghostFirst && (ctx.dataIndex === 0 || ctx.dataIndex === labels.length - 1) ? 0 : 2
        }] },
        options: { responsive: true, maintainAspectRatio: false, animation: { duration: 800, easing: 'easeOutQuart' }, scales: { x: { offset: this.chartType === 'bar' || labels.length <= 1 } } }, plugins: [dataLabelsPlugin]
      });
    }

    const ctx6 = document.getElementById('staffSixMonthsChart') as HTMLCanvasElement;
    if (ctx6) {
      const rawLabels6 = (this.staffData.trends || []).map((t: any) => t.month);
      const rawData6 = (this.staffData.trends || []).map((t: any) => t.revenue || 0);
      const { labels: labels6, dataArrays: dataArrays6 } = this.padSinglePoint(rawLabels6, rawData6);
      const [data6] = dataArrays6;
      const ghostFirst6 = labels6[0] === '';
      this.charts['staff6'] = makeChart(ctx6, {
        type: this.chartType as any,
        data: { labels: labels6, datasets: [{ 
          label: 'Total Staff Revenue', 
          data: data6, 
          borderColor: '#10b981',
          backgroundColor: (context: any) => {
            const canvasCtx = context.chart.ctx;
            const grad = canvasCtx.createLinearGradient(0, 0, 0, 300);
            grad.addColorStop(0, '#10b98166');
            grad.addColorStop(1, '#10b98100');
            return grad;
          },
          tension: 0.4, 
          fill: true,
          pointBackgroundColor: '#ffffff',
          pointBorderColor: '#10b981',
          pointRadius: (ctx: any) => ghostFirst6 && (ctx.dataIndex === 0 || ctx.dataIndex === labels6.length - 1) ? 0 : 2
        }] },
        options: { responsive: true, maintainAspectRatio: false, animation: { duration: 800, easing: 'easeOutQuart' }, scales: { x: { offset: this.chartType === 'bar' || labels6.length <= 1 } } }, plugins: [dataLabelsPlugin]
      });
    }
  }
  // --- Service / product category lists (a ranked bar list rather than a canvas chart) ---
  spTotal(list: any[]): number { return (list || []).reduce((a, i) => a + (Number(i.revenue) || 0), 0); }
  spBar(item: any, list: any[]): number {
    const max = Math.max(1, ...(list || []).map(i => Number(i.revenue) || 0));
    return Math.max(0, ((Number(item.revenue) || 0) / max) * 100);
  }
  spShare(item: any, list: any[]): number {
    const total = this.spTotal(list);
    return total ? Math.round(((Number(item.revenue) || 0) / total) * 100) : 0;
  }

  /** The category lists are drawn as HTML (see the ranked lists in the template); nothing to paint. */
  renderServicesProductsCharts() {}
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }


  expandedChartId: string | null = null;
  expandedChartTitle: string = '';
  expandedChartInstance: any = null;

  expandChart(chartId: string, title: string) {
    this.expandedChartId = chartId;
    this.expandedChartTitle = title;
    this.cdr.detectChanges(); // force render modal

    setTimeout(() => {
      const originalCanvas = document.getElementById(chartId) as HTMLCanvasElement;
      if (!originalCanvas) return;
      const originalChart = Chart.getChart(originalCanvas);
      const expandedCanvas = document.getElementById('expandedChartCanvas') as HTMLCanvasElement;
      
      if (originalChart && expandedCanvas) {
        if (this.expandedChartInstance) {
          this.expandedChartInstance.destroy();
        }
        
        // We can't just stringify the config because it loses functions/plugins.
        // We will create a new config with the same data and options.
        const config: any = {
          type: (originalChart.config as any).type,
          data: originalChart.config.data,
          options: Object.assign({}, originalChart.config.options),
          plugins: originalChart.config.plugins
        };
        
        // Improve styling for big screen
        if (config.options) {
          config.options.maintainAspectRatio = false;
          config.options.animation = { duration: 0 }; // fast render
        }
        
        this.expandedChartInstance = makeChart(expandedCanvas, config);
      }
    }, 50);
  }

  closeExpandedChart() {
    if (this.expandedChartInstance) {
      this.expandedChartInstance.destroy();
      this.expandedChartInstance = null;
    }
    this.expandedChartId = null;
  }

  downloadExpandedChart() {
    if (this.expandedChartId) {
      this.downloadChart('expandedChartCanvas', this.expandedChartTitle + '_export');
    }
  }
}
