import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, Input, OnChanges, OnDestroy, SimpleChanges, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import Chart from 'chart.js/auto';
import { Subscription } from 'rxjs';
import { ApiService } from '../../services/api';
import { formatCompact } from '../../services/money';

export type AnalyticsTab = 'revenues' | 'clients' | 'services' | 'products' | 'memberships' | 'value-cards';

// The Overview page's colours, so these tabs sit in the same family.
const BLUE = '#2aa3ae';
const RED = '#e11d48';
const GOLD = '#f59a2c';
const SLATE = '#9aa5b8';
const PALETTE = ['#2aa3ae', '#2563eb', '#f59a2c', '#7c5cdc', '#ec4899', '#9be15d', '#0e7490', '#e11d48',
  '#8aa1b8', '#b9a07a', '#6b7a90', '#4a9cc0', '#d1b26f', '#5b6573', '#c084fc'];

/** Writes each stacked bar's total above it (the New vs repeat chart). */
const stackTotals = {
  id: 'stackTotals',
  afterDatasetsDraw(chart: any) {
    const { ctx } = chart;
    const sets = chart.data.datasets;
    ctx.save();
    ctx.font = '600 11px sans-serif';
    ctx.fillStyle = '#8b8e96';
    ctx.textAlign = 'center';
    chart.data.labels.forEach((_: any, i: number) => {
      const total = sets.reduce((a: number, d: any) => a + (Number(d.data[i]) || 0), 0);
      const top = Math.min(...sets.map((_d: any, k: number) => chart.getDatasetMeta(k).data[i]?.y ?? Infinity));
      const x = chart.getDatasetMeta(0).data[i]?.x;
      if (total && x !== undefined && isFinite(top)) ctx.fillText(String(total), x, top - 6);
    });
    ctx.restore();
  },
};

/**
 * The six analytics tabs of the dashboard: Revenues, Clients, Services, Products,
 * Memberships and Value Cards. Every figure comes from /salon_admin/api/analytics/*,
 * which works from the bills themselves, so these tie to Finance and the Overview.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-analytics-reports',
  imports: [CommonModule],
  templateUrl: './analytics-reports.html',
  styleUrl: './analytics-reports.css',
})
export class AnalyticsReportsComponent implements OnChanges, OnDestroy {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);

  @Input() tab: AnalyticsTab = 'revenues';
  @Input() centerId: number | undefined;
  @Input() startDate = '';
  @Input() endDate = '';
  /** Changes whenever the parent's centre selection does. */
  @Input() refresh = 0;
  /** Changes when a bill is paid: reload in place, without clearing the page. */
  @Input() live = 0;
  /** A section title, when a tab holds more than one report. */
  @Input() heading = '';
  /** The explanatory banner is shown once per tab. */
  @Input() showNote = true;

  data: any = null;
  loading = false;
  error = '';
  showHelp = false;
  renewalFilter: 'soon' | 'expiring' | 'lapsed' = 'soon';
  cardCallFilter: 'expiring' | 'expired' = 'expiring';
  private charts: Record<string, Chart> = {};
  private sub?: Subscription;

  private static readonly MODE_LABELS: Record<string, string> = {
    cash: 'Cash', credit_card: 'Card', upi: 'UPI / other', google_pay: 'Google Pay', phone_pe: 'PhonePe',
    paytm: 'Paytm', bharat_pe: 'BharatPe', cheque_net_banking: 'Cheque / net banking', nearbuy: 'NearBuy',
    value_card: 'Value card', cashback_wallet: 'Cashback wallet', advance: 'Advance', gift_card: 'Gift card', other: 'Other',
  };

  ngOnChanges(ch: SimpleChanges) {
    const liveOnly = !!ch['live'] && !ch['live'].firstChange && Object.keys(ch).length === 1;
    this.load(liveOnly);
  }
  ngOnDestroy() { this.sub?.unsubscribe(); this.destroyCharts(); }

  load(silent = false) {
    this.sub?.unsubscribe();
    if (!silent) {
      this.destroyCharts();
      this.loading = true;
      this.error = '';
      this.data = null;
      this.cdr.markForCheck();
    }
    const tab = this.tab;
    this.sub = this.api.get(`salon_admin/api/analytics/${tab}/`, {
      center_id: this.centerId, start_date: this.startDate, end_date: this.endDate,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        if (tab !== this.tab) return;
        if (silent) this.destroyCharts();
        this.data = res;
        this.loading = false;
        this.cdr.markForCheck();
        setTimeout(() => { this.drawCharts(); }, 60);
      },
      error: err => {
        if (silent) return;                    // keep what is on screen if a quiet refresh fails
        this.loading = false;
        this.error = err?.status === 403 ? 'You do not have access to Analytics.' : 'Could not load this report. Please try again.';
        this.cdr.markForCheck();
      },
    });
  }

  // ─── formatting ─────────────────────────────────────────────────────────
  inr(v: any, decimals = 0): string {
    const n = Number(v);
    if (!isFinite(n)) return '₹0';
    const abs = Math.abs(n).toLocaleString('en-IN', { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
    return (n < 0 ? '−₹' : '₹') + abs;
  }
  compact(v: any): string { return '₹' + formatCompact(v); }
  num(v: any): string { return Number(v || 0).toLocaleString('en-IN'); }
  pct(v: any): string { return `${Math.round(Number(v) || 0)}%`; }
  modeLabel(m: string): string { return AnalyticsReportsComponent.MODE_LABELS[m] || m; }
  dmy(iso: string): string {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }).replace(/ /g, '-');
  }
  color(i: number): string { return PALETTE[i % PALETTE.length]; }
  change(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return `${v >= 0 ? '▲' : '▼'} ${Math.abs(Math.round(v))}%`;
  }
  /** movement cell in the Services table: 'new', 'level' or a percentage */
  move(v: any): { text: string; cls: string } {
    if (v === 'new') return { text: 'new', cls: 'mv-new' };
    if (v === 'level') return { text: 'level', cls: 'mv-level' };
    const n = Number(v);
    return { text: this.change(n), cls: n >= 0 ? 'mv-up' : 'mv-down' };
  }

  // ─── Revenues helpers ───────────────────────────────────────────────────
  get targetPct(): number {
    const t = Number(this.data?.kpis?.target) || 0;
    return t > 0 ? Math.round((Number(this.data.kpis.collection_excl_tax) / t) * 100) : 0;
  }
  get paceNote(): string {
    const p = this.data?.period; const k = this.data?.kpis;
    if (!p || !k || !(Number(k.target) > 0)) return 'No monthly target is set for this centre.';
    const gone = Math.round((p.days_elapsed / p.days) * 100);
    const pacePct = Math.round((Number(k.pace) / Number(k.target)) * 100);
    return `${gone}% of the period gone · on pace for ${this.compact(k.pace)} (${pacePct}%)`;
  }
  get lapsedList(): any[] {
    return this.renewalFilter === 'expiring' ? [] : (this.renewalFilter === 'soon' ? (this.data?.lapsed || []).filter((r: any) => r.days <= 90) : (this.data?.lapsed || []));
  }
  get expiringList(): any[] { return this.renewalFilter === 'lapsed' ? [] : (this.data?.expiring || []); }
  get cardCalls(): any[] { return (this.cardCallFilter === 'expiring' ? this.data?.expiring : this.data?.expired) || []; }
  get hasData(): boolean { return !!this.data; }
  trackIdx(i: number) { return i; }

  // ─── charts ─────────────────────────────────────────────────────────────
  private destroyCharts() {
    Object.values(this.charts).forEach(c => c.destroy());
    this.charts = {};
  }

  private make(id: string, config: any) {
    const el = document.getElementById(id) as HTMLCanvasElement | null;
    if (!el) return;
    this.charts[id]?.destroy();
    this.charts[id] = new Chart(el, config);
  }

  private base(extra: any = {}): any {
    const axisTick = { color: '#8b8e96', font: { size: 10 } };
    return {
      responsive: true, maintainAspectRatio: false, animation: { duration: 400 }, layout: { padding: { top: 4, right: 4, bottom: 0, left: 0 } },
      interaction: { mode: 'index', intersect: false },
      plugins: { legend: { display: false }, tooltip: { displayColors: true } },
      scales: {
        x: { grid: { display: false }, ticks: axisTick },
        y: { beginAtZero: true, suggestedMax: 100, ticks: { ...axisTick, callback: (v: any) => this.compact(v) }, grid: { color: 'rgba(0,0,0,0.05)' } },
      },
      ...extra,
    };
  }

  /** Bars are the default view of every Revenues chart; each can be switched to a line. */
  rev: Record<string, 'bar' | 'line'> = { daily: 'bar', dow: 'bar', hours: 'bar', trend: 'bar' };

  setMode(key: string, mode: 'bar' | 'line') {
    this.rev[key] = mode;
    this.drawRevenueChart(key);
    this.cdr.markForCheck();
  }

  /** Axis numbers without the rupee sign: 1.5L, 50k. */
  private axis(v: any): string { return formatCompact(v); }

  private drawRevenueChart(key: string) {
    const d = this.data;
    if (!d || this.tab !== 'revenues') return;
    const mode = this.rev[key];
    const money = (c: any) => ` ${this.inr(c.parsed.y)}`;
    const ids: Record<string, string> = { daily: 'anDaily', dow: 'anDow', trend: 'anTrend', hours: 'anHours' };
    const tick = { color: '#8b8e96', font: { size: 10 } };
    const yMoney = { beginAtZero: true, suggestedMax: 100, ticks: { ...tick, callback: (v: any) => this.axis(v) }, grid: { color: 'rgba(0,0,0,0.05)' } };
    let labels: string[] = [], values: number[] = [], colors: string[] = [], tooltip: any = {}, yAxis: any = yMoney, dataset = '';
    let xTicks: any = { ...tick, autoSkip: true, maxRotation: 0 };

    if (key === 'daily') {
      labels = d.daily.map((r: any) => r.label); values = d.daily.map((r: any) => r.value);
      colors = d.daily.map((r: any) => r.weekend ? RED : BLUE); dataset = 'Sales value';
      xTicks = { ...tick, autoSkip: true, maxRotation: 0, maxTicksLimit: 16 };
      tooltip = { callbacks: { title: (i: any) => `${d.daily[i[0].dataIndex].label} (${d.daily[i[0].dataIndex].weekday})`, label: money } };
    } else if (key === 'dow') {
      labels = d.dow.map((r: any) => r.day); values = d.dow.map((r: any) => r.value);
      colors = d.dow.map((r: any) => r.weekend ? RED : BLUE); dataset = 'Average per day';
      tooltip = { callbacks: { label: money } };
    } else if (key === 'trend') {
      labels = d.trend.map((r: any) => r.month + (r.partial ? '*' : '')); values = d.trend.map((r: any) => r.value);
      colors = d.trend.map((r: any) => r.partial ? 'rgba(42,163,174,0.55)' : BLUE); dataset = 'Collection excl. tax';
      tooltip = { callbacks: { label: money } };
    } else {
      const top3 = [...d.hours].sort((a: any, b: any) => b.invoices - a.invoices).slice(0, 3).map((h: any) => h.hour);
      labels = d.hours.map((h: any) => h.label); values = d.hours.map((h: any) => h.invoices);
      colors = d.hours.map((h: any) => top3.includes(h.hour) && h.invoices > 0 ? BLUE : SLATE); dataset = 'Invoices';
      yAxis = { beginAtZero: true, suggestedMax: 10, ticks: { ...tick, precision: 0 }, grid: { color: 'rgba(0,0,0,0.05)' } };
      tooltip = { callbacks: { label: (c: any) => ` ${c.parsed.y} invoices` } };
    }

    const bar = mode === 'bar';
    this.make(ids[key], {
      type: bar ? 'bar' : 'line',
      data: { labels, datasets: [bar
        ? { label: dataset, data: values, backgroundColor: colors, borderRadius: 3, maxBarThickness: key === 'hours' ? 22 : 26, minBarLength: 0 }
        : { label: dataset, data: values, borderColor: BLUE, backgroundColor: 'rgba(42,163,174,0.12)', fill: true, tension: 0.3,
            borderWidth: 2, pointRadius: 2, pointBackgroundColor: colors, pointBorderColor: colors }] },
      options: this.base({
        plugins: { legend: { display: false }, tooltip },
        scales: { x: { grid: { display: false }, ticks: xTicks }, y: yAxis },
      }),
    });
  }

  private drawCharts() {
    const d = this.data;
    if (!d) return;
    const tipMoney = { callbacks: { label: (c: any) => ` ${c.dataset.label ? c.dataset.label + ': ' : ''}${this.inr(c.parsed.y)}` } };
    if (this.tab === 'revenues') {
      ['daily', 'dow', 'trend', 'hours'].forEach(k => this.drawRevenueChart(k));
    } else if (this.tab === 'clients') {
      this.make('anWeeks', {
        type: 'bar',
        data: { labels: d.weeks.map((w: any) => w.label), datasets: [
          { label: 'New', data: d.weeks.map((w: any) => w.new), backgroundColor: BLUE, stack: 's', maxBarThickness: 26, minBarLength: 0 },
          { label: 'Repeat', data: d.weeks.map((w: any) => w.repeat), backgroundColor: SLATE, stack: 's', maxBarThickness: 26, minBarLength: 0 }] },
        plugins: [stackTotals],
        options: this.base({ plugins: { legend: { display: true, position: 'bottom' } }, layout: { padding: { top: 22 } },
          scales: { x: { stacked: true, grid: { display: false }, ticks: { color: '#8b8e96' } }, y: { stacked: true, beginAtZero: true, ticks: { precision: 0, color: '#8b8e96' }, grid: { color: 'rgba(0,0,0,0.05)' } } } }),
      });
    } else if (this.tab === 'memberships') {
      this.make('anMemChart', {
        type: 'bar',
        data: { labels: d.chart.map((r: any) => r.month + (r.partial ? '*' : '')), datasets: [
          { label: 'Saved by members', data: d.chart.map((r: any) => r.saved), backgroundColor: BLUE, maxBarThickness: 26, minBarLength: 0 },
          { label: 'Membership sales', data: d.chart.map((r: any) => r.sold), backgroundColor: GOLD, maxBarThickness: 26, minBarLength: 0 }] },
        options: this.base({ plugins: { legend: { display: true, position: 'bottom' }, tooltip: tipMoney } }),
      });
    } else if (this.tab === 'value-cards') {
      this.make('anCardChart', {
        type: 'bar',
        data: { labels: d.chart.map((r: any) => r.month + (r.partial ? '*' : '')), datasets: [
          { label: 'Cards sold', data: d.chart.map((r: any) => r.sold), backgroundColor: GOLD, maxBarThickness: 26, minBarLength: 0 },
          { label: 'Credit spent', data: d.chart.map((r: any) => r.spent), backgroundColor: BLUE, maxBarThickness: 26, minBarLength: 0 }] },
        options: this.base({ plugins: { legend: { display: true, position: 'bottom' }, tooltip: tipMoney } }),
      });
    }
  }
}
