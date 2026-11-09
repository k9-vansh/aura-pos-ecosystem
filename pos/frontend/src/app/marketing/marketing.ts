import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
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
  selector: 'app-marketing',
  imports: [CommonModule, FormsModule, LocationSelectorComponent],
  templateUrl: './marketing.html',
  styleUrl: './marketing.css'
})
export class MarketingComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  apiService = inject(ApiService);
  toastService = inject(ToastService);
  cdr = inject(ChangeDetectorRef);
  csvService = inject(CsvService);

  isSaving = false;
  activeTab: any = 'Campaigns';
  campaignTab: any = 'Promotions';

  /**
   * Campaigns hold four separately-granted things (promotions, cards,
   * memberships, packages). This checked a single `campaigns` key that roles
   * no longer store, so the tab was hidden from every non-superuser (R8).
   */
  get canReadCampaigns(): boolean {
    const m = this.permissions?.marketing || {};
    return this.isOwner || ['promotions', 'cards', 'memberships', 'packages']
      .some(sub => m[sub]?.read === true);
  }
  /** Holds `verb` on a Marketing submodule (the server enforces it too). */
  can(sub: string, verb: 'read' | 'create' | 'update' | 'delete'): boolean {
    return this.isOwner || this.permissions?.marketing?.[sub]?.[verb] === true;
  }
  /** Holds this Campaign sub-grant (promotions, cards, memberships or packages). */
  canRead(sub: string): boolean {
    return this.isOwner || this.permissions?.marketing?.[sub]?.read === true;
  }
  minDate: string = todayLocal();

  isOwner = false;
  hasGlobalAccess = false;
  permissions: any = {};
  centers: any[] = [];
  selectedCenterId: number | null = null;
  selectedFilterCenterId: number | null = null; // Used for WhatsApp filtering only

  // Data
  whatsappMessages: any[] = [];
  promotions: any[] = [];
  cards: any[] = [];
  memberships: any[] = [];
  packages: any[] = [];
  servicesList: any[] = [];

  /** A fresh, empty promotion form (also what the form returns to after a save). */
  private blankPromotion(): any {
    return {
      level: 'Organisation',
      center: null,
      name: '',
      start_date: '',
      end_date: '',
      description: '',
      members_only: false,
      max_usage_per_client: null,
      promo_type: 'Discount',
      // Promotional coupons: where the codes come from, and what to make.
      code_source: 'list',
      codes_text: '',
      quantity: null,
      code_prefix: '',
      config: {
        discount_type: 'Percentage',
        discount_value: null,
        target: 'Overall Bill',
        min_bill_amount: null,
        specific_service: '',
        flat_price: null,
        trigger_services: ['', ''],
        trigger_discount: null,
        cashback_min_bill: null,
        cashback_discount: null,
        cashback_validity_type: 'Days',
        cashback_validity_days: null,
        cashback_specific_date: '',
        brand: '',
        coupon_value_type: 'Flat',
        coupon_value: null,
        coupon_min_bill: null,
        coupon_max_discount: null
      }
    };
  }

  newPromotion: any = this.blankPromotion();

  usageStartDate = '';
  usageEndDate = '';
  usageData: any[] = [];
  hasRunUsageReport = false;
  selectedUsage: any = null;

  // Filters
  showExpired = false;
  showInactive = false;
  showOrgOnly = false;

  /**
   * The server's answer to a refused save, in words an operator can act on.
   * The screens used to show the raw JSON ({"config":{"cashback_discount":[...]}}).
   */
  errorText(err: any, fallback = 'Something went wrong.'): string {
    const body = err?.error;
    if (!body) return fallback;
    if (typeof body === 'string') return body.length < 300 ? body : fallback;
    const parts: string[] = [];
    const walk = (value: any, label = '') => {
      if (value === null || value === undefined) return;
      if (typeof value === 'string') {
        parts.push(label ? `${label}: ${value}` : value);
      } else if (Array.isArray(value)) {
        value.forEach(v => walk(v, label));
      } else if (typeof value === 'object') {
        Object.keys(value).forEach(k =>
          walk(value[k], ['error', 'detail', 'what_to_do', 'non_field_errors'].includes(k) ? '' : k.replace(/_/g, ' ')));
      }
    };
    walk(body);
    return parts.length ? parts.join(' ') : fallback;
  }

  fetchUsageData() {
    this.selectedUsage = null;
    this.apiService.getPromotionUsage(this.usageStartDate, this.usageEndDate, this.selectedFilterCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.usageData = data || [];
      this.hasRunUsageReport = true;
      this.cdr.detectChanges();
    });
  }

  exportExcel() {
    // 'Count' was purchases only, so the export could not answer the question
    // the screen exists for: are these perks being used, or only sold?
    const headers = ['Item (Type)', 'Type', 'Start Date', 'End Date', 'Applied to',
                     'Status', 'Sold', 'Used', 'Currently Holding', 'Revenue'];
    const rows = this.usageData.map(u => [
      u.name || '',
      u.type || 'Promotion',
      u.start_date || '',
      u.end_date || '',
      u.level === 'Organisation' ? 'Org Level' : 'Salon level',
      u.status || '',
      u.sold ?? u.count ?? 0,
      u.used ?? 0,
      u.holders ?? '',
      u.revenue || 0
    ]);
    this.csvService.exportToCsv('Marketing_Usage_Report.csv', headers, rows);
  }

  viewUsageDetails(usage: any) {
    this.selectedUsage = usage;
    this.cdr.detectChanges();
  }

  newCard: any = {
    level: 'Organisation',
    center: null,
    title: '',
    pre_tax_price: null,
    post_tax_price: null,
    value: null,
    benefit_percent: null,
    incentive: null,
    expiry_days: null,
    description: '',
    gst_percent: 18
  };

  newMembership: any = {
    level: 'Organisation',
    center: null,
    name: '',
    discount_percent: null,
    pre_tax_price: null,
    post_tax_price: null,
    value: null,
    incentive: null,
    expiry_days: null,
    description: '',
    is_vip: false,
    gst_percent: 18
  };

  /**
   * GST rates Indian tax law actually uses, for the rate selector.
   *
   * The rate used to be implied by the gap between Pre Tax Price and Post Tax
   * Price, and both fields rewrote each other through a hardcoded factor of
   * five percent - so typing in either one snapped the pair back to 5%, and 18%
   * was physically unreachable. Beauty and personal-care services under SAC
   * 9997 attract 18%. The rate is now its own field, `gst_percent`, and these
   * two helpers are the only place the arithmetic lives.
   */
  readonly gstRates = [0, 5, 12, 18, 28];

  /** Post-tax price from pre-tax, at the row's own stated rate. */
  private postFromPre(row: any): number | null {
    const pre = Number(row?.pre_tax_price);
    if (!Number.isFinite(pre) || pre <= 0) {
      return null;
    }
    const rate = Number(row?.gst_percent);
    const safeRate = Number.isFinite(rate) && rate >= 0 && rate <= 100 ? rate : 0;
    return Math.round(pre * (1 + safeRate / 100) * 100) / 100;
  }

  /** Pre-tax price worked back out of a tax-inclusive price. */
  private preFromPost(row: any): number | null {
    const post = Number(row?.post_tax_price);
    if (!Number.isFinite(post) || post <= 0) {
      return null;
    }
    const rate = Number(row?.gst_percent);
    const safeRate = Number.isFinite(rate) && rate >= 0 && rate <= 100 ? rate : 0;
    return Math.round((post / (1 + safeRate / 100)) * 100) / 100;
  }

  calculateCardPostTax() {
    if (this.newCard.pre_tax_price) {
      this.newCard.post_tax_price = this.postFromPre(this.newCard);
      this.calculateCardBenefit();
    }
  }

  calculateCardValue() {
    if (this.newCard.post_tax_price && this.newCard.benefit_percent != null) {
      const pt = Number(this.newCard.post_tax_price);
      const bp = Number(this.newCard.benefit_percent);
      this.newCard.value = Math.round(pt * (1 + bp / 100) * 100) / 100;
    }
  }

  calculateCardPreTax() {
    if (this.newCard.post_tax_price) {
      this.newCard.pre_tax_price = this.preFromPost(this.newCard);
      this.calculateCardBenefit();
    }
  }

  calculateCardBenefit() {
    if (this.newCard.post_tax_price && this.newCard.value) {
      const pt = Number(this.newCard.post_tax_price);
      const val = Number(this.newCard.value);
      if (pt > 0) {
        this.newCard.benefit_percent = Math.round(((val - pt) / pt) * 10000) / 100;
      }
    }
  }

  calculateMembershipBenefit() {
    if (this.newMembership.post_tax_price && this.newMembership.value) {
      const pt = Number(this.newMembership.post_tax_price);
      const val = Number(this.newMembership.value);
      if (pt > 0) {
        this.newMembership.discount_percent = Math.round(((val - pt) / pt) * 10000) / 100;
      }
    }
  }

  calculateMembershipValue() {
    if (this.newMembership.post_tax_price && this.newMembership.discount_percent != null) {
      const pt = Number(this.newMembership.post_tax_price);
      const bp = Number(this.newMembership.discount_percent);
      this.newMembership.value = Math.round(pt * (1 + bp / 100) * 100) / 100;
    }
  }

  calculateMembershipPostTax() {
    if (this.newMembership.pre_tax_price) {
      this.newMembership.post_tax_price = this.postFromPre(this.newMembership);
      this.calculateMembershipBenefit();
    }
  }

  calculateMembershipPreTax() {
    if (this.newMembership.post_tax_price) {
      this.newMembership.pre_tax_price = this.preFromPost(this.newMembership);
      this.calculateMembershipBenefit();
    }
  }

  newPackage: any = {
    level: 'Organisation',
    center: null,
    name: '',
    service_name: '',
    description: '',
    price: null,
    validity_days: null,
    gst_percent: null,
    // One row per service in the package: the service and how many sessions.
    rows: [{ service_name: '', pkgQty: null }]
  };

  /** A package's services as one line: "Haircut x3, Spa x2". */
  packageServices(p: any): string {
    const list = Array.isArray(p?.services_json) ? p.services_json : [];
    if (!list.length) return p?.service_name || '';
    return list.map((s: any) => `${s.name || s.service_name} x${s.pkgQty || 1}`).join(', ');
  }

  addPackageRow(target: any) {
    target.rows = [...(target.rows || []), { service_name: '', pkgQty: null }];
  }

  removePackageRow(target: any, index: number) {
    target.rows = (target.rows || []).filter((_: any, i: number) => i !== index);
    if (!target.rows.length) target.rows = [{ service_name: '', pkgQty: null }];
  }

  /** The rows turned into the server's list; null when a row is half filled. */
  private packageServicesPayload(rows: any[]): any[] | null {
    const out: any[] = [];
    for (const row of rows || []) {
      if (!row.service_name && !row.pkgQty) continue;          // an untouched row
      const svc = this.servicesList.find(s => s.name === row.service_name);
      const qty = Number(row.pkgQty);
      if (!svc || !qty || qty < 1) return null;
      out.push({ id: svc.id, service_id: svc.id, name: svc.name, service_name: svc.name,
        price: svc.price || svc.default_price || 0, pkgQty: qty });
    }
    return out;
  }

  ngOnInit() {
    let user: any = null;
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        user = JSON.parse(userStr);
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.permissions = user.permissions || {};
      } catch (e) {
        console.error('Failed to parse user from localStorage', e);
      }
    }

    if (this.canReadCampaigns) {
      this.activeTab = 'Campaigns';
      // Open the first campaign sub-tab this role holds, not always Promotions.
      const firstSub = [['Promotions', 'promotions'], ['Cards', 'cards'], ['Memberships', 'memberships'], ['Packages', 'packages']]
        .find(([, key]) => this.canRead(key));
      if (firstSub) this.campaignTab = firstSub[0];
    } else if (this.permissions.marketing?.whatsapp?.read) {
      this.activeTab = 'WhatsApp';
    } else if (this.permissions.marketing?.usage?.read) {
      this.activeTab = 'Usage';
    } else {
      this.activeTab = '';
    }

    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.centers = data || [];
      if (this.hasGlobalAccess) {
        this.selectedFilterCenterId = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true) ?? null;
      } else {
        this.selectedFilterCenterId = user?.center_id || null;
        if (this.centers.length > 0 && !this.centers.some(c => c.id == this.selectedFilterCenterId)) {
          this.selectedFilterCenterId = this.centers[0].id;
        }
      }
      this.selectedCenterId = this.selectedFilterCenterId;
      if (!this.hasGlobalAccess) {
        this.newPromotion.level = 'Center';
        this.newCard.level = 'Center';
        this.newMembership.level = 'Center';
        this.newPackage.level = 'Center';
      }
      this.loadData();
    });
  }

  setTab(tab: string) {
    this.activeTab = tab;
    this.loadData();
  }

  setCampaignTab(tab: string) {
    this.campaignTab = tab;
  }

  onFilterCenterChange() {
    this.selectedCenterId = this.selectedFilterCenterId;
    this.loadData();
  }

  loadData() {
    if (this.activeTab === 'WhatsApp') {
      const cid = this.selectedFilterCenterId ? this.selectedFilterCenterId : undefined;
      this.apiService.getWhatsAppMessages(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
        this.whatsappMessages = data;
        this.cdr.detectChanges();
      });
    } else {
      const cid = this.selectedFilterCenterId ? this.selectedFilterCenterId : undefined;
      // Only what this role may read: asking for the rest was refused.
      if (this.canRead('promotions')) {
        this.apiService.getPromotions(cid, true, true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          this.promotions = data;
          this.cdr.detectChanges();
        });
      }
      if (this.canRead('cards')) {
        this.apiService.getValueCards(cid, true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          this.cards = data;
          this.cdr.detectChanges();
        });
      }
      if (this.canRead('memberships')) {
        this.apiService.getMemberships(cid, true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          this.memberships = data;
          this.cdr.detectChanges();
        });
      }
      if (this.canRead('packages')) {
        this.apiService.getPackages(cid, true).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          this.packages = data;
          this.cdr.detectChanges();
        });
      }
      if (this.canReadCampaigns) {
        this.apiService.getServices().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
          this.servicesList = data || [];
          this.cdr.detectChanges();
        });
      }
    }
  }

  private getCreateCenterId(): number | null {
    return this.selectedCenterId ?? this.selectedFilterCenterId ?? null;
  }

  // --- Promotions ---
  private filterCampaigns(list: any[], hasExpiry: boolean = false) {
    return list.filter(item => {
      if (!this.showInactive && item.is_active === false) return false;
      if (this.showOrgOnly && item.level !== 'Organisation') return false;
      if (hasExpiry && !this.showExpired && item.end_date) {
        if (new Date(item.end_date) < new Date()) return false;
      }
      return true;
    });
  }

  get orgPromotions() { return this.filterCampaigns(this.promotions, true).filter((p: any) => p.level === 'Organisation'); }
  get centerPromotions() { return this.filterCampaigns(this.promotions, true).filter((p: any) => p.level === 'Center'); }

  addPromotion() {
    const payload = { ...this.newPromotion };
    if (payload.level === 'Center') {
      const centerId = this.getCreateCenterId();
      if (!centerId) { this.toastService.showError('Please select a center for this promotion'); return; }
      payload.center = centerId;
    } else {
      payload.center = null;
    }

    if (!payload.name || !payload.start_date || !payload.end_date) {
      this.toastService.showError('Please fill all required fields (Name, Start Date, End Date)');
      return;
    }

    // Lift model fields out of config JSON dict so the backend model serializer catches them
    if (payload.config) {
      payload.discount_type = payload.config.discount_type;
      payload.discount_value = payload.config.discount_value;
    }

    if (payload.promo_type === 'Coupon') {
      const problem = this.couponFormProblem(payload);
      if (problem) { this.toastService.showError(problem); return; }
      // A coupon promotion has no discount of its own, and the codes go with it.
      delete payload.discount_type;
      delete payload.discount_value;
      payload.max_usage_per_client = null;
      payload.config = { ...payload.config, brand: String(payload.config.brand).trim() };
      if (payload.code_source === 'list') { delete payload.quantity; delete payload.code_prefix; }
      else { delete payload.codes_text; }
    } else {
      delete payload.code_source; delete payload.codes_text; delete payload.quantity; delete payload.code_prefix;
    }

    this.isSaving = true;
    this.apiService.createPromotion(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.isSaving = false;
        this.toastService.showSuccess(payload.promo_type === 'Coupon'
          ? this.couponLoadMessage(res?.codes_load, 'Coupon promotion created')
          : 'Promotion created successfully');
        this.loadData();
        this.newPromotion = this.blankPromotion();
      },
      error: (err) => {
        this.isSaving = false;
        this.toastService.showError('Failed to create promotion: ' + this.errorText(err));
      }
    });
  }

  // --- Promotional coupons ---
  /** What is missing on the coupon form, in words; null when it can be saved. */
  private couponFormProblem(p: any): string | null {
    const c = p.config || {};
    if (!String(c.brand || '').trim()) return "Enter the brand's name (for example Blinkit).";
    if (!(Number(c.coupon_value) > 0)) return 'Enter what each coupon is worth.';
    if (c.coupon_value_type === 'Percentage' && Number(c.coupon_value) > 100) return 'A percentage cannot be more than 100.';
    if (p.code_source === 'list') {
      if (this.codeLineCount(p.codes_text) === 0) return "Paste the brand's codes (one on each line), or choose to make the codes here.";
      return null;
    }
    if (!(Number(p.quantity) >= 1)) return 'Enter how many codes to make.';
    if (Number(p.quantity) > 50000) return 'Make at most 50,000 codes at a time.';
    return null;
  }

  /** How many lines of a pasted list are codes (a header line does not count). */
  codeLineCount(text: string): number {
    return String(text || '').split(/\r?\n/)
      .filter(line => line.trim() && !/^"?codes?"?\s*([,;\t]\s*"?(amount|value)"?)?$/i.test(line.trim())).length;
  }

  private couponLoadMessage(load: any, lead: string): string {
    if (!load) return lead;
    const parts = [`${lead}: ${load.added} code(s) added`];
    if (load.already_held) parts.push(`${load.already_held} already held, left out`);
    if (load.repeated_in_list) parts.push(`${load.repeated_in_list} repeated in the list`);
    if (load.invalid) parts.push(`${load.invalid} not valid`);
    return parts.join(', ') + '.';
  }

  /** The chip text for a promotion's kind. */
  promoTypeLabel(p: any): string {
    switch (p.promo_type) {
      case 'Discount': return 'Discount';
      case 'FlatPrice': return 'Flat';
      case 'Trigger': return 'Trigger';
      case 'Coupon': return 'Coupon';
      default: return 'Cashback';
    }
  }

  /** The LIMIT column: a coupon shows how many of its codes are used. */
  promoLimitText(p: any): string {
    if (p.promo_type === 'Coupon') {
      const s = p.codes_summary || { used: 0, total: 0 };
      return `${s.used} of ${s.total} used`;
    }
    return p.max_usage_per_client ? (p.max_usage_per_client + ' uses') : ' Unlimited';
  }

  /** What a coupon is worth, as the list shows it (Rs 500 / 10% up to Rs 150). */
  couponWorth(p: any): string {
    const c = p?.config || {};
    const value = Number(c.coupon_value) || 0;
    if (c.coupon_value_type === 'Percentage') {
      const cap = Number(c.coupon_max_discount) || 0;
      return `${value}%${cap > 0 ? ' up to Rs ' + cap : ''}`;
    }
    return `Rs ${value}`;
  }

  /** The brand's list from a .csv or .txt file, put into the box (an Excel sheet: copy its column and paste). */
  onCodesFile(event: Event, target: 'new' | 'add') {
    const input = event.target as HTMLInputElement;
    const file = input.files && input.files[0];
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) {
      this.toastService.showError('That file is too big for a list of codes. Use a .csv or .txt file.');
      input.value = '';
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const text = String(reader.result || '');
      if (target === 'new') this.newPromotion.codes_text = text; else this.codesAddText = text;
      input.value = '';
      this.cdr.detectChanges();
    };
    reader.readAsText(file);
  }

  // --- The codes of a coupon promotion ---
  showCodesModal = false;
  codesPromo: any = null;
  codesData: any = null;           // { summary, count, page, page_size, results }
  codesStatus = '';                // '' (all), 'unused', 'used', 'void'
  codesQuery = '';
  codesPage = 1;
  codesLoading = false;
  codesMode: '' | 'add' | 'make' = '';
  codesAddText = '';
  codesMakeQty: number | null = null;
  codesMakePrefix = '';
  codesBusy = false;
  copiedCodeId: number | null = null;
  private codesQueryTimer: any = null;

  openCodes(promo: any) {
    this.codesPromo = promo;
    this.codesData = null;
    this.codesStatus = '';
    this.codesQuery = '';
    this.codesPage = 1;
    this.codesMode = '';
    this.codesAddText = '';
    this.codesMakeQty = null;
    this.codesMakePrefix = '';
    this.showCodesModal = true;
    this.loadCodes();
  }

  closeCodes() {
    this.showCodesModal = false;
    this.codesPromo = null;
    this.codesData = null;
    this.loadData();                 // the list shows "N of M used"
  }

  loadCodes() {
    if (!this.codesPromo) return;
    this.codesLoading = true;
    this.apiService.getCouponCodes(this.codesPromo.id, {
      status: this.codesStatus, q: this.codesQuery.trim(), page: this.codesPage, page_size: 50,
    }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any) => {
        this.codesLoading = false;
        this.codesData = data;
        this.codesPromo.codes_summary = data.summary;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.codesLoading = false;
        this.toastService.showError(this.errorText(err, 'Could not load the codes.'));
        this.cdr.detectChanges();
      },
    });
  }

  setCodesStatus(status: string) {
    this.codesStatus = status;
    this.codesPage = 1;
    this.loadCodes();
  }

  onCodesQueryChange() {
    clearTimeout(this.codesQueryTimer);
    this.codesQueryTimer = setTimeout(() => { this.codesPage = 1; this.loadCodes(); }, 300);
  }

  get codesPageCount(): number {
    const d = this.codesData;
    return d ? Math.max(1, Math.ceil(d.count / d.page_size)) : 1;
  }

  gotoCodesPage(page: number) {
    if (page < 1 || page > this.codesPageCount || page === this.codesPage) return;
    this.codesPage = page;
    this.loadCodes();
  }

  exportCodes() {
    if (!this.codesPromo) return;
    this.apiService.exportCouponCodes(this.codesPromo.id, { status: this.codesStatus, q: this.codesQuery.trim() })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (blob: Blob) => {
          const slug = String(this.codesPromo?.name || 'coupons').replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'coupons';
          const url = URL.createObjectURL(blob);
          const link = document.createElement('a');
          link.href = url;
          link.download = `coupon-codes-${slug}.csv`;
          link.click();
          URL.revokeObjectURL(url);
        },
        error: (err: any) => this.toastService.showError(this.errorText(err, 'Could not download the codes.')),
      });
  }

  submitAddCodes() {
    if (!this.codesPromo || this.codesBusy) return;
    if (this.codeLineCount(this.codesAddText) === 0) { this.toastService.showError("Paste the brand's codes (one on each line)."); return; }
    this.codesBusy = true;
    this.apiService.addCouponCodes(this.codesPromo.id, this.codesAddText).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.codesBusy = false;
        this.toastService.showSuccess(this.couponLoadMessage(res.load, 'Codes loaded'));
        this.codesAddText = '';
        this.codesMode = '';
        this.codesPage = 1;
        this.loadCodes();
      },
      error: (err: any) => {
        this.codesBusy = false;
        this.toastService.showError(this.errorText(err, 'Could not add the codes.'));
        this.cdr.detectChanges();
      },
    });
  }

  submitMakeCodes() {
    if (!this.codesPromo || this.codesBusy) return;
    const qty = Number(this.codesMakeQty);
    if (!(qty >= 1) || qty > 50000) { this.toastService.showError('Enter how many codes to make (1 to 50,000).'); return; }
    this.codesBusy = true;
    this.apiService.generateCouponCodes(this.codesPromo.id, qty, this.codesMakePrefix.trim()).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.codesBusy = false;
        this.toastService.showSuccess(`${res.load.added} new code(s) made.`);
        this.codesMakeQty = null;
        this.codesMode = '';
        this.codesPage = 1;
        this.loadCodes();
      },
      error: (err: any) => {
        this.codesBusy = false;
        this.toastService.showError(this.errorText(err, 'Could not make the codes.'));
        this.cdr.detectChanges();
      },
    });
  }

  async toggleCodeVoid(row: any) {
    if (!this.codesPromo || this.codesBusy || row.status === 'Used') return;
    const withdrawing = row.status !== 'Void';
    if (withdrawing && !await this.confirmDialog.ask(`Withdraw ${row.code}? It will not be accepted at the till.`)) return;
    this.codesBusy = true;
    this.apiService.voidCouponCode(this.codesPromo.id, row.id, withdrawing).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.codesBusy = false; this.loadCodes(); },
      error: (err: any) => {
        this.codesBusy = false;
        this.toastService.showError(this.errorText(err, 'Could not change that code.'));
        this.cdr.detectChanges();
      },
    });
  }

  copyCode(row: any) {
    const done = () => {
      this.copiedCodeId = row.id;
      this.cdr.detectChanges();
      setTimeout(() => { if (this.copiedCodeId === row.id) { this.copiedCodeId = null; this.cdr.detectChanges(); } }, 1200);
    };
    try { navigator.clipboard.writeText(row.code).then(done, () => undefined); } catch { /* clipboard not available */ }
  }

  // --- Cards ---
  get orgCards() { return this.filterCampaigns(this.cards).filter((p: any) => p.level === 'Organisation'); }
  get centerCards() { return this.filterCampaigns(this.cards).filter((p: any) => p.level === 'Center'); }

  addCard() {
    const payload = { ...this.newCard };
    if (!payload.title || !(Number(payload.post_tax_price) > 0) || !(Number(payload.value) > 0) || payload.expiry_days == null) {
      this.toastService.showError('Please fill all required fields');
      return;
    }
    if (payload.level === 'Center') {
      const centerId = this.getCreateCenterId();
      if (!centerId) { this.toastService.showError('Please select a center for this card'); return; }
      payload.center = centerId;
    } else {
      payload.center = null;
    }

    this.isSaving = true;
    this.apiService.createValueCard(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.toastService.showSuccess('Card created successfully');
        this.loadData();
        this.newCard = { level: 'Organisation', center: null, title: '', pre_tax_price: null, post_tax_price: null, value: null, benefit_percent: null, incentive: null, expiry_days: null, description: '', gst_percent: 18 };
      },
      error: (err) => { this.isSaving = false; this.toastService.showError('Failed to create card: ' + this.errorText(err)); }
    });
  }

  // --- Memberships ---
  get orgMemberships() { return this.filterCampaigns(this.memberships).filter((p: any) => p.level === 'Organisation'); }
  get centerMemberships() { return this.filterCampaigns(this.memberships).filter((p: any) => p.level === 'Center'); }

  addMembership() {
    const payload = { ...this.newMembership };
    if (!String(payload.name || '').trim() || payload.expiry_days == null || payload.expiry_days === ''
        || (!(Number(payload.pre_tax_price) > 0) && !(Number(payload.post_tax_price) > 0))) {
      this.toastService.showError('Please fill the name, a price and the validity (days).');
      return;
    }
    if (payload.level === 'Center') {
      const centerId = this.getCreateCenterId();
      if (!centerId) { this.toastService.showError('Please select a center for this membership'); return; }
      payload.center = centerId;
    } else {
      payload.center = null;
    }

    this.isSaving = true;
    this.apiService.createMembership(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.toastService.showSuccess('Membership created successfully');
        this.loadData();
        this.newMembership = { level: 'Organisation', center: null, name: '', discount_percent: null, pre_tax_price: null, post_tax_price: null, value: null, incentive: null, expiry_days: null, description: '', is_vip: false, gst_percent: 18 };
      },
      error: (err) => { this.isSaving = false; this.toastService.showError('Failed to create membership: ' + this.errorText(err)); }
    });
  }

  // --- Packages ---
  get orgPackages() { return this.filterCampaigns(this.packages).filter((p: any) => p.level === 'Organisation'); }
  get centerPackages() { return this.filterCampaigns(this.packages).filter((p: any) => p.level === 'Center'); }

  addPackage() {
    const payload = { ...this.newPackage };
    if (!String(payload.name || '').trim() || !(Number(payload.price) > 0) || !(Number(payload.validity_days) > 0)) {
      this.toastService.showError('Please fill the name, a price above 0 and the validity (days).');
      return;
    }
    if (payload.level === 'Center') {
      const centerId = this.getCreateCenterId();
      if (!centerId) { this.toastService.showError('Please select a center for this package'); return; }
      payload.center = centerId;
    } else {
      payload.center = null;
    }

    // Lift model fields out of config JSON dict so the backend model serializer catches them
    if (payload.config) {
      payload.discount_type = payload.config.discount_type;
      payload.discount_value = payload.config.discount_value;
    }

    const services = this.packageServicesPayload(payload.rows);
    if (services === null || services.length === 0) {
      this.toastService.showError('Choose a service and the number of sessions for every row.');
      return;
    }
    payload.services_json = services;
    payload.service_name = services[0].name;
    delete payload.rows;
    delete payload.pkgQty;
    if (payload.gst_percent === null || payload.gst_percent === '') delete payload.gst_percent;

    this.isSaving = true;
    this.apiService.createPackage(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.isSaving = false;
        this.toastService.showSuccess('Package created successfully');
        this.loadData();
        this.newPackage = { level: 'Organisation', center: null, name: '', service_name: '', description: '', price: null, validity_days: null, gst_percent: null, rows: [{ service_name: '', pkgQty: null }] };
      },
      error: (err) => { this.isSaving = false; this.toastService.showError('Failed to create package: ' + this.errorText(err)); }
    });
  }
  // --- Actions ---
  toggleStatus(item: any, type: string) {
    if (this.isSaving) return;
    this.isSaving = true;
    let obs: any;

    // Use dedicated toggle-status endpoints that bypass the is_active filter,
    // so a deactivated item can always be re-activated (no 404 on inactive items).
    if (type === 'promotion') obs = this.apiService.togglePromotion(item.id);
    else if (type === 'card') obs = this.apiService.toggleValueCard(item.id);
    else if (type === 'membership') obs = this.apiService.toggleMembership(item.id);
    else if (type === 'package') obs = this.apiService.togglePackage(item.id);

    if (obs) {
      obs.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.isSaving = false;
          // Optimistically update the UI state instantly
          item.is_active = res.is_active;
          const label = res.is_active ? 'Activated' : 'Deactivated';
          this.toastService.showSuccess(`${label} successfully`);
          // Still fetch in background to ensure sync
          this.loadData();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.toastService.showError('Failed to update status: ' + this.errorText(err));
        }
      });
    } else {
      this.isSaving = false;
    }
  }

  async deleteCampaign(item: any, type: string) {
    if (this.isSaving) return;
    if (!await this.confirmDialog.ask('Are you sure you want to delete this?')) return;
    this.isSaving = true;
    let obs: any;

    if (type === 'promotion') obs = this.apiService.deletePromotion(item.id);
    else if (type === 'card') obs = this.apiService.deleteValueCard(item.id);
    else if (type === 'membership') obs = this.apiService.deleteMembership(item.id);
    else if (type === 'package') obs = this.apiService.deletePackage(item.id);

    if (obs) {
      obs.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.toastService.showSuccess('Deleted successfully');
          this.loadData();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.toastService.showError(this.errorText(err, 'Failed to delete.'));
        }
      });
    } else {
      this.isSaving = false;
    }
  }

  // --- Edit Modal Logic ---
  showEditModal: boolean = false;
  editItemType: 'promotion' | 'card' | 'membership' | 'package' | null = null;
  editItemData: any = {};

  openEditModal(item: any, type: 'promotion' | 'card' | 'membership' | 'package') {
    this.editItemType = type;
    // deep copy the object so we don't mutate the list immediately
    this.editItemData = JSON.parse(JSON.stringify(item));

    // Ensure config exists for promotions
    if (type === 'promotion' && !this.editItemData.config) {
      this.editItemData.config = {};
    }

    // A package opens with one row per service it holds.
    if (type === 'package') {
      const list = Array.isArray(this.editItemData.services_json) ? this.editItemData.services_json : [];
      this.editItemData.rows = list.length
        ? list.map((s: any) => ({ service_name: s.name || s.service_name || '', pkgQty: s.pkgQty || 1 }))
        : [{ service_name: this.editItemData.service_name || '', pkgQty: null }];
    }

    this.showEditModal = true;
  }

  closeEditModal() {
    this.showEditModal = false;
    this.editItemType = null;
    this.editItemData = {};
  }

  saveEdit() {
    if (this.isSaving || !this.editItemType) return;
    this.isSaving = true;

    const payload = { ...this.editItemData };

    if (this.editItemType === 'promotion' && payload.config) {
      payload.discount_type = payload.config.discount_type;
      payload.discount_value = payload.config.discount_value;
    }

    if (this.editItemType === 'package') {
      const services = this.packageServicesPayload(payload.rows);
      if (services === null || services.length === 0) {
        this.isSaving = false;
        this.toastService.showError('Choose a service and the number of sessions for every row.');
        return;
      }
      payload.services_json = services;
      payload.service_name = services[0].name;
      delete payload.rows;
      delete payload.pkgQty;
      if (payload.gst_percent === '') payload.gst_percent = null;
    }

    let obs: any;
    if (this.editItemType === 'promotion') obs = this.apiService.updatePromotion(payload.id, payload);
    else if (this.editItemType === 'card') obs = this.apiService.updateValueCard(payload.id, payload);
    else if (this.editItemType === 'membership') obs = this.apiService.updateMembership(payload.id, payload);
    else if (this.editItemType === 'package') obs = this.apiService.updatePackage(payload.id, payload);

    if (obs) {
      obs.pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.toastService.showSuccess('Updated successfully');
          this.closeEditModal();
          this.loadData();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.toastService.showError('Failed to update: ' + this.errorText(err));
        }
      });
    }
  }

  calculateEditCardPostTax() {
    if (this.editItemData.pre_tax_price) {
      this.editItemData.post_tax_price = this.postFromPre(this.editItemData);
      this.calculateEditCardBenefit();
    }
  }

  calculateEditCardValue() {
    if (this.editItemData.post_tax_price && this.editItemData.benefit_percent != null) {
      const pt = Number(this.editItemData.post_tax_price);
      const bp = Number(this.editItemData.benefit_percent);
      this.editItemData.value = Math.round(pt * (1 + bp / 100) * 100) / 100;
    }
  }

  calculateEditCardPreTax() {
    if (this.editItemData.post_tax_price) {
      this.editItemData.pre_tax_price = this.preFromPost(this.editItemData);
      this.calculateEditCardBenefit();
    }
  }

  calculateEditCardBenefit() {
    if (this.editItemData.post_tax_price && this.editItemData.value) {
      const pt = Number(this.editItemData.post_tax_price);
      const val = Number(this.editItemData.value);
      if (pt > 0) {
        this.editItemData.benefit_percent = Math.round(((val - pt) / pt) * 10000) / 100;
      }
    }
  }

  calculateEditMembershipBenefit() {
    if (this.editItemData.post_tax_price && this.editItemData.value) {
      const pt = Number(this.editItemData.post_tax_price);
      const val = Number(this.editItemData.value);
      if (pt > 0) {
        this.editItemData.discount_percent = Math.round(((val - pt) / pt) * 10000) / 100;
      }
    }
  }

  calculateEditMembershipValue() {
    if (this.editItemData.post_tax_price && this.editItemData.discount_percent != null) {
      const pt = Number(this.editItemData.post_tax_price);
      const bp = Number(this.editItemData.discount_percent);
      this.editItemData.value = Math.round(pt * (1 + bp / 100) * 100) / 100;
    }
  }

  calculateEditMembershipPostTax() {
    if (this.editItemData.pre_tax_price) {
      this.editItemData.post_tax_price = this.postFromPre(this.editItemData);
      this.calculateEditMembershipBenefit();
    }
  }

  calculateEditMembershipPreTax() {
    if (this.editItemData.post_tax_price) {
      this.editItemData.pre_tax_price = this.preFromPost(this.editItemData);
      this.calculateEditMembershipBenefit();
    }
  }


  // --- WhatsApp Campaign Logic ---
  showNewCampaignModal: boolean = false;
  campaignMessage: string = '';
  campaignCenterId: any = 'all';
  /** What the server says a send would do right now (null while asking). */
  campaignPreview: any = null;
  isSending = false;
  private previewTimer: any = null;

  openNewCampaignModal() {
    this.campaignMessage = '';
    this.campaignCenterId = 'all';
    this.showNewCampaignModal = true;
    this.refreshCampaignPreview();
  }

  closeNewCampaignModal() {
    this.showNewCampaignModal = false;
    this.campaignPreview = null;
    clearTimeout(this.previewTimer);
  }

  /** Re-count the audience a moment after the operator stops typing or changes branch. */
  refreshCampaignPreview(delay = 0) {
    clearTimeout(this.previewTimer);
    this.campaignPreview = null;
    this.previewTimer = setTimeout(() => {
      this.apiService.previewWhatsAppCampaign(this.campaignCenterId, this.campaignMessage)
        .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
          next: (res: any) => { this.campaignPreview = res; this.cdr.detectChanges(); },
          error: () => { this.campaignPreview = null; this.cdr.detectChanges(); }
        });
    }, delay);
  }

  get campaignCanSend(): boolean {
    return !this.isSending && !!this.campaignMessage.trim() && this.campaignPreview?.eligible !== 0;
  }

  async sendCampaign(confirmRepeat = false) {
    if (this.isSending) return;
    if (!this.campaignMessage.trim()) {
      this.toastService.showError('Please enter a message to send.');
      return;
    }

    if (!confirmRepeat) {
      const p = this.campaignPreview;
      const who = p && typeof p.eligible === 'number'
        ? `${p.eligible} customer(s)` + (this.campaignCenterId === 'all' ? '' : ' in the selected branch')
        : (this.campaignCenterId === 'all' ? 'all clients' : 'clients in the selected branch');
      const note = p && p.gateway_configured === false
        ? '\n\nWhatsApp is not set up yet, so this will only be recorded - nothing will be transmitted.' : '';
      if (!await this.confirmDialog.ask(`Send Campaign?\nAre you sure you want to send this message to ${who}?${note}`)) return;
    }

    this.isSending = true;
    this.cdr.detectChanges();
    this.apiService.sendWhatsAppCampaign(this.campaignCenterId, this.campaignMessage, confirmRepeat)
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.isSending = false;
          // The server says exactly what happened. "Sent successfully" used to be
          // shown even when nothing had been transmitted.
          if (res.gateway_configured === false) this.toastService.show(res.message || 'Recorded, not transmitted.', 'info', 7000);
          else this.toastService.showSuccess(res.message || `${res.count || 0} message(s) queued.`);
          this.closeNewCampaignModal();
          this.loadData(); // Refresh list
        },
        error: async (err: any) => {
          this.isSending = false;
          this.cdr.detectChanges();
          // The same text went to the same people a moment ago: only on a deliberate yes.
          if (err.status === 409 && err.error?.duplicate) {
            if (await this.confirmDialog.ask(`${err.error.error}\n\nSend it again anyway?`)) {
              this.sendCampaign(true);
            }
            return;
          }
          this.toastService.showError(this.errorText(err, 'Failed to send campaign.'));
        }
      });
  }

  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
