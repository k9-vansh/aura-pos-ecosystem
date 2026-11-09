import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, Input, OnChanges, OnDestroy, SimpleChanges, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import Chart from 'chart.js/auto';
import { Subscription } from 'rxjs';
import { ApiService } from '../../services/api';
import { CsvService } from '../../services/csv.service';
import { formatCompact } from '../../services/money';
import { AnalyticsReportsComponent } from '../analytics-reports/analytics-reports';

export type UsageSection = 'overview' | 'offers' | 'coupons' | 'memberships' | 'cards' | 'packages' | 'cashback'
  | 'customers' | 'whatsapp' | 'forecast' | 'verify';

const BLUE = '#2aa3ae';
const RED = '#e11d48';
const GOLD = '#f59a2c';
const VIOLET = '#7c5cdc';
const PALETTE = ['#2aa3ae', '#2563eb', '#f59a2c', '#7c5cdc', '#ec4899', '#9be15d', '#0e7490', '#e11d48'];

/** The server section each tab reads (memberships and cards are the dashboard's own reports). */
const ENDPOINT: Partial<Record<UsageSection, string>> = {
  overview: 'overview', offers: 'offers', coupons: 'coupons', packages: 'packages', cashback: 'cashback',
  customers: 'customers', whatsapp: 'whatsapp', forecast: 'forecast', verify: 'verify',
};

/** Which filters a tab does not use, so the page can say so instead of silently ignoring them. */
const IGNORES: Record<UsageSection, string[]> = {
  overview: [], offers: [], coupons: [], packages: ['status'], cashback: ['status', 'q'], customers: ['status', 'q'],
  whatsapp: ['perk', 'status', 'customer'], forecast: ['status'], verify: ['perk', 'q', 'status'],
  memberships: ['perk', 'q', 'status', 'customer'], cards: ['perk', 'q', 'status', 'customer'],
};

export const PERK_CHOICES = [
  { key: 'Discount', label: 'Discounts' }, { key: 'FlatPrice', label: 'Flat price' }, { key: 'Trigger', label: 'Trigger deals' },
  { key: 'Cashback', label: 'Cashback' }, { key: 'Coupon', label: 'Brand coupons' }, { key: 'Membership', label: 'Memberships' },
  { key: 'Package', label: 'Packages' }, { key: 'ValueCard', label: 'Value cards' },
];

/**
 * Dashboard > Usage Report: every marketing thing in one place for the marketing team -
 * offers, brand coupons, memberships, value cards, packages, cashback, customers, WhatsApp,
 * what is coming up, and checks that the numbers agree. The dashboard's own centre and date
 * pickers drive it; this adds the filters that only matter to marketing.
 */
@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-usage-report',
  imports: [CommonModule, FormsModule, AnalyticsReportsComponent],
  templateUrl: './usage-report.html',
  styleUrl: './usage-report.css',
})
export class UsageReportComponent implements OnChanges, OnDestroy {
  private api = inject(ApiService);
  private cdr = inject(ChangeDetectorRef);
  private destroyRef = inject(DestroyRef);
  private csv = inject(CsvService);

  @Input() centerId: number | undefined;
  @Input() startDate = '';
  @Input() endDate = '';
  @Input() refresh = 0;
  @Input() live = 0;

  readonly sections: { key: UsageSection; label: string }[] = [
    { key: 'overview', label: 'Overview' }, { key: 'offers', label: 'Offers' }, { key: 'coupons', label: 'Brand coupons' },
    { key: 'memberships', label: 'Memberships' }, { key: 'cards', label: 'Value cards' }, { key: 'packages', label: 'Packages' },
    { key: 'cashback', label: 'Cashback' }, { key: 'customers', label: 'Customers' }, { key: 'whatsapp', label: 'WhatsApp' },
    { key: 'forecast', label: 'Forecast' }, { key: 'verify', label: 'Verify' },
  ];
  readonly perkChoices = PERK_CHOICES;
  readonly customerChoices = [{ key: '', label: 'All customers' }, { key: 'new', label: 'New customers' },
    { key: 'repeat', label: 'Returning customers' }, { key: 'member', label: 'Members' }];
  readonly statusChoices = [{ key: '', label: 'Any state' }, { key: 'running', label: 'Running now' },
    { key: 'upcoming', label: 'Not started' }, { key: 'ended', label: 'Ended' }, { key: 'off', label: 'Switched off' }];

  section: UsageSection = 'overview';
  perks: string[] = [];
  q = '';
  customer = '';
  status = '';

  data: Partial<Record<UsageSection, any>> = {};
  loading: Partial<Record<UsageSection, boolean>> = {};
  error: Partial<Record<UsageSection, string>> = {};
  /** Bumped when a filter changes, so the embedded dashboard reports reload too. */
  tick = 0;
  openCheck: string | null = null;
  /** Coupon / cashback / package tables can be long: only the first rows until asked. */
  showAll: Record<string, boolean> = {};

  private charts: Record<string, Chart> = {};
  private subs: Record<string, Subscription> = {};
  private qTimer: any = null;
  /** Sections not yet loaded since the last Refresh: their next load skips the server's kept answers. */
  private stale = new Set<string>();

  ngOnChanges(ch: SimpleChanges) {
    const liveOnly = !!ch['live'] && !ch['live'].firstChange && Object.keys(ch).length === 1;
    // The Refresh button and the live refresh ask for today's figures, not an answer the server kept.
    if ([ch['refresh'], ch['live']].some(c => !!c && !c.firstChange)) this.stale = new Set(this.sections.map(s => s.key));
    if (liveOnly) { this.reload(true); return; }
    this.reload(false);
  }

  ngOnDestroy() {
    Object.values(this.subs).forEach(s => s.unsubscribe());
    this.destroyCharts();
    clearTimeout(this.qTimer);
  }

  // ─── choosing what to see ───────────────────────────────────────────────

  setSection(s: UsageSection) {
    this.section = s;
    this.openCheck = null;
    this.destroyCharts();
    this.ensure(s);
    this.cdr.markForCheck();
  }

  togglePerk(key: string) {
    this.perks = this.perks.includes(key) ? this.perks.filter(p => p !== key) : [...this.perks, key];
    this.filtersChanged();
  }

  onQ() {
    clearTimeout(this.qTimer);
    this.qTimer = setTimeout(() => this.filtersChanged(), 350);
  }

  filtersChanged() {
    this.reload(false);
  }

  clearFilters() {
    this.perks = [];
    this.q = '';
    this.customer = '';
    this.status = '';
    this.filtersChanged();
  }

  get anyFilter(): boolean {
    return this.perks.length > 0 || !!this.q.trim() || !!this.customer || !!this.status;
  }

  /** Filters that are on but this tab does not use. */
  get ignored(): string[] {
    const off = IGNORES[this.section] || [];
    const named: Record<string, string> = { perk: 'kind', q: 'name', status: 'state', customer: 'customer type' };
    return off.filter(k => (k === 'perk' && this.perks.length) || (k === 'q' && this.q.trim())
      || (k === 'status' && this.status) || (k === 'customer' && this.customer)).map(k => named[k]);
  }

  // ─── loading ────────────────────────────────────────────────────────────

  private params(fresh = false) {
    return {
      center_id: this.centerId, start_date: this.startDate, end_date: this.endDate,
      perk: this.perks.join(','), q: this.q.trim(), customer: this.customer, status: this.status,
      ...(fresh ? { fresh: 1 } : {}),
    };
  }

  private reload(silent: boolean) {
    if (!silent) {
      this.data = {};
      this.error = {};
      this.destroyCharts();
      this.tick++;
    }
    this.ensure(this.section, silent);
    // The Verify badge should be right whichever tab is open.
    if (this.section !== 'verify') this.ensure('verify', true);
    this.cdr.markForCheck();
  }

  private ensure(s: UsageSection, silent = false) {
    const path = ENDPOINT[s];
    if (!path || (this.data[s] && !silent)) {
      if (this.data[s]) setTimeout(() => this.draw(), 40);
      return;
    }
    this.subs[s]?.unsubscribe();
    if (!silent) { this.loading[s] = true; this.error[s] = ''; }
    this.subs[s] = this.api.get(`salon_admin/api/usage-report/${path}/`, this.params(this.stale.delete(s)))
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: res => {
          this.data[s] = res;
          this.loading[s] = false;
          this.cdr.markForCheck();
          if (s === this.section) setTimeout(() => this.draw(), 60);
        },
        error: err => {
          this.loading[s] = false;
          if (!silent) this.error[s] = err?.status === 403 ? 'You do not have access to the Usage Report.' : 'Could not load this report. Please try again.';
          this.cdr.markForCheck();
        },
      });
  }

  get d(): any { return this.data[this.section]; }
  get isLoading(): boolean { return !!this.loading[this.section]; }
  get err(): string { return this.error[this.section] || ''; }
  get verifyFailed(): number { return this.data['verify']?.summary?.failed ?? 0; }
  get verifyKnown(): boolean { return !!this.data['verify']; }
  /** The dashboard's own reports shown inside this tab. */
  get embedded(): 'memberships' | 'value-cards' | null {
    return this.section === 'memberships' ? 'memberships' : this.section === 'cards' ? 'value-cards' : null;
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
  pct(v: any): string { return `${Math.round((Number(v) || 0) * 10) / 10}%`; }
  dmy(iso: string): string {
    if (!iso || iso === 'None') return '—';
    const d = new Date(iso.length === 10 ? iso + 'T00:00:00' : iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short', year: 'numeric' }).replace(/ /g, '-');
  }
  change(v: number | null | undefined): string {
    if (v === null || v === undefined) return '—';
    return `${v >= 0 ? '▲' : '▼'} ${Math.abs(Math.round(v))}%`;
  }
  /** up = good news (green), down = bad (red), unless `inverse` (a cost going up is not good). */
  tone(v: number | null | undefined, inverse = false): string {
    if (v === null || v === undefined || v === 0) return '';
    return (v > 0) !== inverse ? 'up' : 'down';
  }
  color(i: number): string { return PALETTE[i % PALETTE.length]; }
  rows(list: any[] | undefined, key: string, n = 12): any[] {
    if (!list) return [];
    return this.showAll[key] ? list : list.slice(0, n);
  }
  more(list: any[] | undefined, key: string, n = 12): boolean {
    return !!list && list.length > n && !this.showAll[key];
  }
  expand(key: string) { this.showAll[key] = true; this.cdr.markForCheck(); }
  statusText(s: string): string {
    return ({ running: 'Running', ended: 'Ended', upcoming: 'Not started', off: 'Switched off' } as any)[s] || s;
  }
  perkLabel(key: string): string {
    return this.perkChoices.find(p => p.key === key)?.label || key;
  }
  checkTone(status: string): string { return status === 'ok' ? 'ok' : status === 'fail' ? 'fail' : 'info'; }
  checkMark(status: string): string { return status === 'ok' ? '✓' : status === 'fail' ? '✕' : 'i'; }
  toggleCheck(key: string) { this.openCheck = this.openCheck === key ? null : key; }
  print() { window.print(); }

  // ─── charts ─────────────────────────────────────────────────────────────

  private destroyCharts() {
    Object.values(this.charts).forEach(c => c.destroy());
    this.charts = {};
  }

  private make(id: string, config: any) {
    const el = document.getElementById(id) as HTMLCanvasElement | null;
    if (!el) return;
    this.charts[id]?.destroy();
    this.charts[id] = new Chart(el, {
      ...config,
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { position: 'bottom', labels: { boxWidth: 10, font: { size: 11 } } } },
        scales: config.type === 'doughnut' ? undefined : {
          x: { grid: { display: false }, ticks: { font: { size: 10 } } },
          y: { beginAtZero: true, ticks: { font: { size: 10 }, callback: (v: any) => formatCompact(v) } },
        },
        ...(config.options || {}),
      },
    });
  }

  draw() {
    this.destroyCharts();
    const d = this.d;
    if (!d || !d.enabled) return;
    const labels = (d.trend || []).map((t: any) => t.month);
    switch (this.section) {
      case 'overview':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Sales on marketing bills', data: d.trend.map((t: any) => t.linked_sales), backgroundColor: BLUE, borderRadius: 4 },
          { label: 'Marketing cost', data: d.trend.map((t: any) => t.cost), backgroundColor: RED, borderRadius: 4 }] } });
        this.make('urMix', { type: 'doughnut', data: { labels: d.mix.map((m: any) => m.label), datasets: [{
          data: d.mix.map((m: any) => m.sales), backgroundColor: d.mix.map((_: any, i: number) => this.color(i)) }] } });
        break;
      case 'offers':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Sales on offer bills', data: d.trend.map((t: any) => t.net_sales), backgroundColor: BLUE, borderRadius: 4 },
          { label: 'Discount given', data: d.trend.map((t: any) => t.discount), backgroundColor: RED, borderRadius: 4 },
          { label: 'Cashback given', data: d.trend.map((t: any) => t.cashback), backgroundColor: VIOLET, borderRadius: 4 }] } });
        break;
      case 'coupons':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Rs paid by coupons', data: d.trend.map((t: any) => t.applied), backgroundColor: GOLD, borderRadius: 4 }] } });
        break;
      case 'packages':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Sold (Rs)', data: d.trend.map((t: any) => t.revenue), backgroundColor: BLUE, borderRadius: 4 },
          { label: 'Sessions used', data: d.trend.map((t: any) => t.sessions_used), backgroundColor: GOLD, borderRadius: 4, yAxisID: 'y1' }] },
          options: { scales: { x: { grid: { display: false } }, y: { beginAtZero: true }, y1: { beginAtZero: true, position: 'right', grid: { display: false } } } } });
        break;
      case 'cashback':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Given', data: d.trend.map((t: any) => t.earned), backgroundColor: BLUE, borderRadius: 4 },
          { label: 'Spent', data: d.trend.map((t: any) => t.spent), backgroundColor: GOLD, borderRadius: 4 },
          { label: 'Expired', data: d.trend.map((t: any) => t.expired), backgroundColor: RED, borderRadius: 4 }] } });
        break;
      case 'customers':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'All sales', data: d.trend.map((t: any) => t.sales), backgroundColor: '#c9d3e0', borderRadius: 4 },
          { label: 'Sales on marketing bills', data: d.trend.map((t: any) => t.linked_sales), backgroundColor: BLUE, borderRadius: 4 }] } });
        break;
      case 'whatsapp':
        this.make('urTrend', { type: 'bar', data: { labels, datasets: [
          { label: 'Messages', data: d.trend.map((t: any) => t.messages), backgroundColor: '#25a05a', borderRadius: 4 }] } });
        break;
      case 'forecast': {
        const s = d.sales;
        if (s && s.history?.length) {
          const proj = s.projection || [];
          const all = [...s.history.map((h: any) => h.month), ...proj.map((p: any) => p.month)];
          this.make('urOutlook', { type: 'bar', data: { labels: all, datasets: [
            { label: 'Sales on marketing bills', data: [...s.history.map((h: any) => h.linked), ...proj.map(() => null)], backgroundColor: BLUE, borderRadius: 4 },
            { label: 'Rough outlook', data: [...s.history.map(() => null), ...proj.map((p: any) => p.linked)], backgroundColor: 'rgba(124,92,220,.55)', borderRadius: 4 }] } });
        }
        break;
      }
    }
  }

  // ─── downloads ──────────────────────────────────────────────────────────

  /** The table of the open section as a spreadsheet. */
  download(which = '') {
    const d = this.d;
    if (!d) return;
    const f = d.frame ? `${d.frame.start}_to_${d.frame.end}` : '';
    const out = (name: string, headers: string[], rows: any[][]) =>
      this.csv.exportToCsv(`Usage_${name}_${f}.csv`, headers, rows);
    switch (this.section) {
      case 'offers':
        out('offers', ['Offer', 'Kind', 'Rule', 'State', 'Bills', 'Customers', 'New customers', 'Sales before discount', 'Discount',
          'Cashback', 'Cost %', 'Sales after discount', 'Verdict'],
        d.rows.map((r: any) => [r.name, r.type, r.terms, r.status, r.bills, r.customers, r.new_customers, r.sales_before, r.discount,
          r.cashback, r.cost_pct, r.net_sales, r.verdict]));
        break;
      case 'coupons':
        out('coupons', ['Offer', 'Brand', 'Rule', 'State', 'Codes', 'Used', 'Unused', 'Withdrawn', 'Used in period', 'Paid by coupons',
          'Lost on small bills', 'Brand owes (all time)', 'Note'],
        d.rows.map((r: any) => [r.name, r.brand, r.terms, r.status, r.codes, r.used, r.unused, r.void, r.used_in_period, r.applied,
          r.wasted, r.brand_owes, r.note]));
        break;
      case 'packages':
        out('packages', ['Package', 'Price', 'Sold', 'Revenue', 'Sessions sold', 'Sessions used', 'Customers holding', 'Sessions left',
          'Value left', 'Expiring in 30 days', 'Lapsed unused (90 days)', 'Utilisation %'],
        d.rows.map((r: any) => [r.name, r.price, r.sold, r.revenue, r.sessions_sold, r.sessions_used, r.holders, r.sessions_left,
          r.value_left, r.expiring_30, r.lapsed_90, r.utilisation ?? '']));
        break;
      case 'cashback':
        out('cashback_holders', ['Customer', 'Phone', 'Balance', 'Expiring in 30 days'],
          d.top_holders.map((r: any) => [r.name, r.phone, r.balance, r.expiring_30]));
        break;
      case 'customers':
        out('customers_by_centre', ['Centre', 'Bills', 'Marketing bills', '% marketing', 'Sales on marketing bills', 'All sales'],
          d.by_centre.map((r: any) => [r.centre, r.bills, r.linked, r.linked_pct, r.linked_sales, r.sales]));
        break;
      case 'whatsapp':
        out('whatsapp', ['Sent on', 'Message', 'Centre', 'Messages', 'Delivered', 'Sent', 'Not sent', 'Responded in 7 days', 'Sales in 7 days'],
          d.rows.map((r: any) => [r.sent_on, r.message, r.centre, r.messages, r.delivered, r.sent, r.not_sent, r.responded ?? '', r.sales_7d ?? '']));
        break;
      case 'verify':
        out('checks', ['Check', 'Result', 'Should be', 'Is', 'Difference', 'Rows out'],
          d.checks.map((c: any) => [c.title, c.status === 'ok' ? 'Agrees' : c.status === 'fail' ? 'Does not agree' : 'Info',
            c.expected ?? '', c.actual ?? '', c.difference ?? '', c.bad ?? '']));
        break;
      case 'forecast':
        out('coming_up', ['What', 'Within days', 'Count', 'Customers', 'Amount', 'Meaning'],
          d.due.flatMap((g: any) => g.horizons.map((h: any) => [g.kind, h.days, h.count, h.customers, h.amount, h.unit])));
        break;
      case 'overview':
        out('overview', ['Figure', 'This period', 'Previous period'], [
          ['Sales on marketing bills', d.head.linked_sales, d.head.linked_sales_prev],
          ['Marketing cost', d.head.cost, d.head.cost_prev], ['Brand coupons applied', d.head.coupons_applied, d.head.coupons_applied_prev],
          ['Memberships, packages and cards sold', d.head.sold_value, d.head.sold_value_prev]]);
        break;
    }
  }
}
