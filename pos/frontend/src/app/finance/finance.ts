import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectionStrategy, ChangeDetectorRef, Component, DestroyRef, OnInit, inject } from '@angular/core';
import { CommonModule, DatePipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { ToastService } from '../services/toast.service';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { forkJoin } from 'rxjs';
import { toLocalDateString, todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';
import { formatAmount, toAmount } from '../services/money';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  selector: 'app-finance',
  imports: [CommonModule, FormsModule, LocationSelectorComponent],
  templateUrl: './finance.html',
  styleUrl: './finance.css'
})
export class FinanceComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  apiService = inject(ApiService);
  cdr = inject(ChangeDetectorRef);

  isOwner = false;
  hasGlobalAccess = false;
  permissions: any = {};
  centers: any[] = [];
  selectedFilterLocation: any = null;

  currentRange: string = ''; // Track active date pill

  // Tab State
  activeMainTab = 'single';
  activeMultiTab = 'register';
  activeSingleTab = 'register';
  activePettyCashTab = 'admin';
  activeClosingTab = 'admin';
  selectedAdminClosing: any = null;
  parseFloat = parseFloat;

  /**
   * Formatted expected cash for a SAVED day-closing row.
   *
   * Distinct from the `expectedCash` getter below, which computes the live
   * figure for the form being filled in. Naming this one `expectedCash` shadowed
   * that getter - a number used in arithmetic - and broke register closing.
   *
   * The template computed this inline as
   * `parseFloat(a) + parseFloat(b) - parseFloat(c)`, so any of the three fields
   * arriving null - which they are until the closing is completed - rendered the
   * literal text "NaN" in the Expected Cash row of a cash-reconciliation panel.
   */
  expectedCashFor(closing: any): string {
    if (!closing) {
      return formatAmount(0);
    }
    return formatAmount(
      toAmount(closing.opening_balance)
      + toAmount(closing.system_cash)
      - toAmount(closing.todays_expenses),
    );
  }
  editingPettyCash: any = null;
  targetMultiplier = 2.5;

  // Date filters
  startDate = '';
  endDate = '';
  closingStartDate = '';
  closingEndDate = '';

  // Per-report loading flags — prevents cross-tab isLoading contamination
  isLoading = false;
  isLoadingRegister = false;
  isLoadingMonthly = false;
  isLoadingDetailed = false;
  isLoadingRefunds = false;
  isLoadingProcurement = false;
  isLoadingMulti = false;
  isLoadingIncentives = false;
  isLoadingClosing = false;
  isSaving = false;

  // ---- Register Summary ----
  registerSummaryData: any = null;

  // ---- Monthly Sales ----
  monthlySalesData: any[] = [];

  // ---- Detailed Revenues ----
  detailedRevenuesData: any[] = [];

  // ---- Refunds ----
  refundsData: any = null;

  // ---- Procurement ----
  procurementData: any = null;
  // Tax Report, Service Drilldown and Staff Performance (Single Salon)
  taxReportData: any = null;
  serviceDrilldownData: any[] | null = null;
  staffPerformanceData: any[] | null = null;
  isLoadingTax = false;
  isLoadingServiceDrilldown = false;
  isLoadingStaffPerformance = false;

  // ---- Multi Salon ----
  multiSalonData: any[] = [];
  multiBalancesData: any[] = [];
  showServiceBalances = true;
  showCardBalances = true;
  showAdvanceBalances = true;
  multiStartDate = '';
  multiEndDate = '';

  // ---- Incentives ----
  incentivesData: any[] = [];
  /** Centres picked for the Incentives report; empty means every centre this user may see. */
  /** Locations ticked in the dropdown when more than one (empty otherwise). */
  selectedFilterLocations: number[] = [];
  incentiveStartDate = '';
  incentiveEndDate = '';
  incentivePercent = 5;
  incentiveFrequency = 'monthly';
  incentiveSearchQuery = '';
  selectedStaffBreakdown: any = null;
  showStaffBreakdownModal = false;
  incentiveConfigs: any[] = [];
  showIncentiveConfigModal = false;
  currentIncentiveConfig: any = null;

  // ---- Manage Rules (Incentive Structure) ----
  manageViewMode: 'monthly' | 'daily' = 'monthly';
  manageDailySubTab: 'business_slabs' | 'service_addons_targets' = 'business_slabs';
  incentiveRules: any[] = [];
  manageCategoryFilter = 'all';
  manageFrequencyFilter = 'all';
  manageCenterFilter: any = null;
  manageSearchQuery = '';
  showRuleModal = false;
  showDuplicateModal = false;
  selectedRuleForDuplicate: any = null;
  duplicateTargetCenterId: any = null;
  currentRule: any = {
    name: '',
    category: 'services',
    frequency: 'monthly',
    center: null,
    applicable_role: 'all',
    rule_type: 'multiple',
    flat_percent: 0,
    flat_amount: 0,
    tiers: [],
    slabs: [],
    effective_from: todayLocal(),
    effective_to: null,
    description: '',
    is_active: true
  };

  loadIncentiveRules() {
    this.isLoading = true;
    // The category pills belong to the monthly view only; one picked there must not hide the daily rules.
    const cat = this.manageViewMode === 'monthly' && this.manageCategoryFilter !== 'all' ? this.manageCategoryFilter : undefined;
    const freq = this.manageViewMode === 'daily' ? 'daily' : (this.manageFrequencyFilter !== 'all' ? this.manageFrequencyFilter : undefined);
    const cid = this.manageCenterFilter ? this.manageCenterFilter : undefined;
    this.apiService.getIncentiveRules(cid, cat, freq).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.incentiveRules = (res || []).map((rule: any) => this.screenRuleType(rule));
        this.isLoading = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.isLoading = false;
        this.cdr.detectChanges();
      }
    });
  }


  getFilteredRules(): any[] {
    if (!this.incentiveRules) return [];
    return this.incentiveRules.filter(r => {
      if (this.manageViewMode === 'monthly') {
        if (r.category === 'daily_business' || r.category === 'service_target') return false;
        if (r.frequency === 'daily') return false;
      } else if (this.manageViewMode === 'daily') {
        if (this.manageDailySubTab === 'business_slabs') {
          if (r.category !== 'daily_business') return false;
        } else {
          if (r.category !== 'service_addon' && r.category !== 'service_target') return false;
        }
      }

      if (this.manageViewMode === 'monthly' && this.manageCategoryFilter !== 'all' && r.category !== this.manageCategoryFilter) return false;
      if (this.manageCenterFilter && r.center !== this.manageCenterFilter) return false;
      if (this.manageSearchQuery?.trim()) {
        const q = this.manageSearchQuery.toLowerCase().trim();
        const matchName = r.name?.toLowerCase().includes(q);
        const matchRole = r.applicable_role?.toLowerCase().includes(q);
        const matchCenter = r.center_name?.toLowerCase().includes(q);
        if (!matchName && !matchRole && !matchCenter) return false;
      }
      return true;
    });
  }

  /**
   * Daily Business Slab rules for one card of the screen. 'lhds_uhds' and 'mhds_beauty' are the
   * two role cards; 'other' is every rule for any other audience (Everyone, Staff, Managers,
   * Pedicurists...), which used to be listed nowhere, so it could be neither seen, edited nor deleted.
   */
  getDailyBusinessRules(group: 'lhds_uhds' | 'mhds_beauty' | 'other'): any[] {
    const named = ['lhds_uhds', 'mhds_beauty'];
    return this.getFilteredRules().filter(r =>
      r.category === 'daily_business'
      && (group === 'other' ? !named.includes(r.applicable_role) : r.applicable_role === group));
  }

  getServiceAddonRules(): any[] {
    return this.getFilteredRules().filter(r => r.category === 'service_addon');
  }

  getServiceTargetRules(): any[] {
    return this.getFilteredRules().filter(r => r.category === 'service_target');
  }

  /** Who a rule is for, in the words of the Who gets it? drop-down. */
  roleLabel(role: string): string {
    const found = this.ruleRoleOptions.find(r => r.value === role);
    return found ? found.label : (role || 'Everyone');
  }

  /** Where a rule stands today: Active, Inactive, Not started or Ended. */
  ruleStatus(rule: any): string {
    if (!rule.is_active) return 'Inactive';
    const today = todayLocal();
    if (rule.effective_from && rule.effective_from > today) return 'Starts ' + rule.effective_from;
    if (rule.effective_to && rule.effective_to < today) return 'Ended ' + rule.effective_to;
    return 'Active';
  }

  openCreateRuleModal(category: string = 'services') {
    const today = todayLocal();
    const freq = this.manageViewMode === 'daily' ? 'daily' : 'monthly';
    
    let defaultRuleType = 'multiple';
    let defaultTiers: any[] = [];
    let defaultSlabs: any[] = [];
    let role = 'all';

    if (category === 'daily_business') {
      defaultRuleType = 'slab';
      role = 'lhds_uhds';
      defaultTiers = [
        { min_amount: 8000, bonus_type: 'flat', bonus_amount: 350, bonus_percent: 0 },
        { min_amount: 10000, bonus_type: 'flat', bonus_amount: 450, bonus_percent: 0 },
        { min_amount: 15000, bonus_type: 'flat', bonus_amount: 700, bonus_percent: 0 },
        { min_amount: 20000, bonus_type: 'percentage', bonus_amount: 0, bonus_percent: 5.0 }
      ];
    } else if (category === 'service_addon') {
      defaultRuleType = 'slab';
      role = 'pedicurist_k_ambassador';
      defaultTiers = [
        { service_name: 'Fusio Dose / Scrub', match_keyword: 'fusio', worth_amount: 0, incentive_amount: 100 },
        { service_name: 'Express Ritual (Fusio Dose +)', match_keyword: 'express ritual', worth_amount: 3600, incentive_amount: 150 },
        { service_name: 'Experience Ritual', match_keyword: 'experience ritual', worth_amount: 4500, incentive_amount: 200 },
        { service_name: 'K Premiere Ritual', match_keyword: 'premiere ritual', worth_amount: 5500, incentive_amount: 250 },
        { service_name: 'Chronologist', match_keyword: 'chronologist', worth_amount: 6000, incentive_amount: 300 },
        { service_name: 'Be Spoke Signature Ritual', match_keyword: 'be spoke', worth_amount: 7000, incentive_amount: 400 },
        { service_name: 'Alga Exquis Mani/Pedi', match_keyword: 'alga exquis', worth_amount: 2500, incentive_amount: 200 },
        { service_name: 'Algae Mani/Pedi', match_keyword: 'algae', worth_amount: 1800, incentive_amount: 150 },
        { service_name: 'Footlogix / Alga Fondu Mani/Pedi', match_keyword: 'footlogix', worth_amount: 0, incentive_amount: 50 },
        { service_name: 'Absolute Repair Molecular', match_keyword: 'absolute repair', worth_amount: 2500, incentive_amount: 100 }
      ];
    } else if (category === 'service_target') {
      defaultRuleType = 'target';
      role = 'pedicurist_k_ambassador';
      defaultTiers = [
        { service_name: 'Rituals Target', match_keyword: 'ritual', target_count: 5, reward_amount: 500 },
        { service_name: 'Mani / Pedi Target', match_keyword: 'mani', target_count: 8, reward_amount: 400 }
      ];
    } else if (category === 'value_cards') {
      defaultRuleType = 'slabs';
      defaultSlabs = [
        { name: 'Elite', min_amount: 11000, max_amount: 14000, incentive_amount: 200 },
        { name: 'Luxe', min_amount: 21000, max_amount: 30000, incentive_amount: 400 },
        { name: 'Prestige', min_amount: 51000, max_amount: 80000, incentive_amount: 600 },
        { name: 'Infinity', min_amount: 111000, max_amount: 180000, incentive_amount: 800 }
      ];
    } else {
      defaultRuleType = 'multiple';
      defaultTiers = [
        { min_multiple: 5.0, max_multiple: 5.99, incentive_percent: 3.0 },
        { min_multiple: 6.0, max_multiple: 6.99, incentive_percent: 6.0 },
        { min_multiple: 7.0, max_multiple: null, incentive_percent: 7.0 }
      ];
    }

    this.currentRule = {
      name: '',
      category: category,
      frequency: freq,
      center: this.getCenterId() || null,
      applicable_role: role,
      rule_type: defaultRuleType,
      flat_percent: 5,
      flat_amount: 200,
      tiers: defaultTiers,
      slabs: defaultSlabs,
      effective_from: today,
      effective_to: null,
      description: '',
      is_active: true
    };
    if (this.isRowCategory(category)) this.currentRule.slabs = this.slabsFromTiers(category, defaultTiers);
    this.showRuleModal = true;
  }

  openCreateDailyBusinessModal(role: string = 'lhds_uhds') {
    this.openCreateRuleModal('daily_business');
    this.currentRule.applicable_role = role;
    if (role === 'lhds_uhds') {
      this.currentRule.name = 'LHDS / UHDS Daily Sales Slabs';
      this.currentRule.tiers = [
        { min_amount: 8000, bonus_type: 'flat', bonus_amount: 350, bonus_percent: 0 },
        { min_amount: 10000, bonus_type: 'flat', bonus_amount: 450, bonus_percent: 0 },
        { min_amount: 15000, bonus_type: 'flat', bonus_amount: 700, bonus_percent: 0 },
        { min_amount: 20000, bonus_type: 'percentage', bonus_amount: 0, bonus_percent: 5.0 }
      ];
    } else {
      this.currentRule.name = 'MHDS / Beauty Daily Sales Slabs';
      this.currentRule.tiers = [
        { min_amount: 6000, bonus_type: 'flat', bonus_amount: 200, bonus_percent: 0 },
        { min_amount: 8000, bonus_type: 'flat', bonus_amount: 350, bonus_percent: 0 },
        { min_amount: 10000, bonus_type: 'flat', bonus_amount: 450, bonus_percent: 0 },
        { min_amount: 15000, bonus_type: 'percentage', bonus_amount: 0, bonus_percent: 5.0 }
      ];
    }
    this.currentRule.slabs = this.slabsFromTiers('daily_business', this.currentRule.tiers);
  }

  openEditRuleModal(rule: any) {
    this.currentRule = JSON.parse(JSON.stringify(rule));
    if (!this.currentRule.tiers) this.currentRule.tiers = [];
    if (!this.currentRule.slabs) this.currentRule.slabs = [];
    
    // Reverse-map tiers to slabs for the UI builder
    if (this.currentRule.tiers.length > 0 && this.isRowCategory(this.currentRule.category)) {
      this.currentRule.slabs = this.slabsFromTiers(this.currentRule.category, this.currentRule.tiers);
    }

    this.showRuleModal = true;
  }

  /**
   * The server stores a rule's type as slab / percentage / flat; the screen's own switches speak
   * slabs / flat_percentage / flat_amount. Without this a saved Value Card, percentage or flat rule
   * showed an empty preview and an edit form with nothing selected. (Daily slabs, add-ons and
   * targets keep the stored value: they are told apart by category, not by type.)
   */
  private screenRuleType(rule: any): any {
    if (this.isRowCategory(rule.category)) return rule;
    const names: any = { slab: 'slabs', percentage: 'flat_percentage', flat: 'flat_amount' };
    return names[rule.rule_type] ? { ...rule, rule_type: names[rule.rule_type] } : rule;
  }

  /** Daily slabs, add-ons and targets are edited as rows (`slabs`) and paid from `tiers`. */
  private isRowCategory(category: string): boolean {
    return ['daily_business', 'service_addon', 'service_target'].includes(category);
  }

  /** The editing rows for tiers in the shape the incentive engine reads. */
  private slabsFromTiers(category: string, tiers: any[]): any[] {
    if (category === 'daily_business') {
      return tiers.map((t: any) => ({
        min_amount: t.min_amount,
        type: t.bonus_type === 'percentage' ? 'percent' : 'flat',
        incentive_amount: t.bonus_type === 'percentage' ? t.bonus_percent : t.bonus_amount
      }));
    }
    if (category === 'service_addon') {
      return tiers.map((t: any) => ({
        name: t.service_name, keyword: t.match_keyword, worth: t.worth_amount, incentive_amount: t.incentive_amount
      }));
    }
    if (category === 'service_target') {
      return tiers.map((t: any) => ({
        name: t.service_name, keyword: t.match_keyword, min_amount: t.target_count, incentive_amount: t.reward_amount
      }));
    }
    return [];
  }

  openDuplicateRuleModal(rule: any) {
    this.selectedRuleForDuplicate = rule;
    this.duplicateTargetCenterId = null;
    this.showDuplicateModal = true;
  }

  confirmDuplicateRule() {
    if (!this.selectedRuleForDuplicate) return;
    this.apiService.duplicateIncentiveRule(this.selectedRuleForDuplicate.id, this.duplicateTargetCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.showDuplicateModal = false;
        this.selectedRuleForDuplicate = null;
        this.loadIncentiveRules();
        alert('Rule duplicated successfully with clean date-isolation!');
      },
      error: (err) => alert(err.error?.detail || 'Error duplicating rule')
    });
  }

  saveRule() {
    if (!this.currentRule.name?.trim()) {
      alert('Please enter a descriptive Rule Name.');
      return;
    }
    if (!this.currentRule.effective_from) {
      alert('Please select an Effective From date.');
      return;
    }
    if (this.currentRule.effective_to && this.currentRule.effective_from > this.currentRule.effective_to) {
      alert('Effective To date cannot be earlier than Effective From date.');
      return;
    }

    // These three are paid from their rows alone; a rule with none would store the previous
    // category's rows (or nothing) and quietly pay nothing.
    const rowNames: any = { daily_business: 'a daily slab', service_addon: 'a service add-on', service_target: 'a volume target' };
    if (rowNames[this.currentRule.category] && !(this.currentRule.slabs?.length > 0)) {
      alert(`Add at least ${rowNames[this.currentRule.category]} before saving.`);
      return;
    }

    const payload = { ...this.currentRule };
    // Synchronize tiers/slabs based on rule_type or category
    if (payload.rule_type === 'slabs' && payload.slabs?.length > 0) {
      payload.tiers = payload.slabs;
    } else if (payload.category === 'daily_business' && payload.slabs?.length > 0) {
      // Map inline builder slabs → backend tiers format
      payload.tiers = payload.slabs.map((s: any) => ({
        min_amount: s.min_amount || 0,
        bonus_type: s.type === 'percent' ? 'percentage' : 'flat',
        bonus_amount: s.type !== 'percent' ? (s.incentive_amount || 0) : 0,
        bonus_percent: s.type === 'percent' ? (s.incentive_amount || 0) : 0
      }));
    } else if (payload.category === 'service_addon' && payload.slabs?.length > 0) {
      // Map inline builder slabs → backend tiers format
      payload.tiers = payload.slabs.map((s: any) => ({
        service_name: s.name || '',
        match_keyword: s.keyword || '',
        worth_amount: s.worth || 0,
        incentive_amount: s.incentive_amount || 0
      }));
    } else if (payload.category === 'service_target' && payload.slabs?.length > 0) {
      // Map inline builder slabs → backend tiers format
      payload.tiers = payload.slabs.map((s: any) => ({
        service_name: s.name || '',
        match_keyword: s.keyword || '',
        target_count: s.min_amount || 1,
        reward_amount: s.incentive_amount || 0
      }));
    }

    if (payload.id) {
      this.apiService.updateIncentiveRule(payload.id, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.showRuleModal = false;
          this.loadIncentiveRules();
          alert('Incentive Rule updated successfully!');
        },
        error: (err) => alert(err.error?.detail || JSON.stringify(err.error) || 'Error updating rule')
      });
    } else {
      this.apiService.createIncentiveRule(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.showRuleModal = false;
          this.loadIncentiveRules();
          alert('Incentive Rule created successfully!');
        },
        error: (err) => alert(err.error?.detail || JSON.stringify(err.error) || 'Error creating rule')
      });
    }
  }

  toggleRuleStatus(rule: any) {
    const updated = { ...rule, is_active: !rule.is_active };
    this.apiService.updateIncentiveRule(rule.id, updated).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        rule.is_active = !rule.is_active;
        this.cdr.detectChanges();
      },
      error: (err) => alert(err.error?.detail || 'Failed to toggle status')
    });
  }

  async deleteRule(id: number) {
    if (!await this.confirmDialog.ask('Are you sure you want to delete this rule? Historical calculations prior to today will remain unaffected.')) return;
    this.apiService.deleteIncentiveRule(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => this.loadIncentiveRules(),
      error: (err) => alert(err.error?.detail || 'Error deleting rule')
    });
  }

  addRuleTier() {
    if (!this.currentRule.tiers) this.currentRule.tiers = [];
    if (this.currentRule.category === 'daily_business') {
      this.currentRule.tiers.push({ min_amount: 0, bonus_type: 'flat', bonus_amount: 0, bonus_percent: 0 });
    } else if (this.currentRule.category === 'service_addon') {
      this.currentRule.tiers.push({ service_name: '', match_keyword: '', worth_amount: 0, incentive_amount: 0 });
    } else if (this.currentRule.category === 'service_target') {
      this.currentRule.tiers.push({ service_name: '', match_keyword: '', target_count: 1, reward_amount: 0 });
    } else {
      this.currentRule.tiers.push({ min_multiple: 0, max_multiple: null, incentive_percent: 0 });
    }
  }

  removeRuleTier(index: number) {
    this.currentRule.tiers.splice(index, 1);
  }

  addRuleSlab() {
    if (!this.currentRule.slabs) this.currentRule.slabs = [];
    this.currentRule.slabs.push({ name: '', min_amount: 0, max_amount: 0, incentive_amount: 0 });
  }

  removeRuleSlab(index: number) {
    this.currentRule.slabs.splice(index, 1);
  }

  // Daily Business Slabs builder (used by inline modal table for category=daily_business)
  addDailyBusinessSlab() {
    if (!this.currentRule.slabs) this.currentRule.slabs = [];
    this.currentRule.slabs.push({ min_amount: 0, type: 'flat', incentive_amount: 0 });
  }

  // Service Add-on builder (used by inline modal table for category=service_addon)
  addServiceAddonItem() {
    if (!this.currentRule.slabs) this.currentRule.slabs = [];
    this.currentRule.slabs.push({ name: '', keyword: '', worth: 0, incentive_amount: 0 });
  }

  // Service Volume Target builder (used by inline modal table for category=service_target)
  addServiceTargetItem() {
    if (!this.currentRule.slabs) this.currentRule.slabs = [];
    this.currentRule.slabs.push({ name: '', keyword: '', min_amount: 1, incentive_amount: 0 });
  }

  /** Who a rule can be for; the values are the ones the incentive engine understands. */
  ruleRoleOptions = [
    { value: 'all', label: 'Everyone (managers and staff)' },
    { value: 'staff', label: 'Staff / stylists' },
    { value: 'manager', label: 'Managers only' },
    { value: 'lhds_uhds', label: 'LHDS / UHDS (hair designers)' },
    { value: 'mhds_beauty', label: 'MHDS / Beauty / Therapists' },
    { value: 'pedicurist_k_ambassador', label: 'Pedicurists & K Ambassadors' },
  ];

  isKnownRuleRole(value: string): boolean {
    return !value || this.ruleRoleOptions.some(r => r.value === value);
  }

  onCategoryChangeInModal() {
    // How often it runs follows from what it is for, so it is not asked.
    this.currentRule.frequency =
      ['daily_business', 'service_addon', 'service_target'].includes(this.currentRule.category) ? 'daily' : 'monthly';
    if (this.currentRule.category === 'value_cards') {
      this.currentRule.rule_type = 'slabs';
      this.currentRule.slabs = [
        { name: 'Elite', min_amount: 11000, max_amount: 14000, incentive_amount: 200 },
        { name: 'Luxe', min_amount: 21000, max_amount: 30000, incentive_amount: 400 },
        { name: 'Prestige', min_amount: 51000, max_amount: 80000, incentive_amount: 600 },
        { name: 'Infinity', min_amount: 111000, max_amount: 180000, incentive_amount: 800 }
      ];
    } else if (this.currentRule.category === 'daily_business') {
      this.currentRule.rule_type = 'slab';
      this.currentRule.frequency = 'daily';
      // The rows of the previous category mean nothing here, so they are replaced, not kept.
      this.currentRule.tiers = [
        { min_amount: 8000, bonus_type: 'flat', bonus_amount: 350, bonus_percent: 0 },
        { min_amount: 10000, bonus_type: 'flat', bonus_amount: 450, bonus_percent: 0 },
        { min_amount: 15000, bonus_type: 'flat', bonus_amount: 700, bonus_percent: 0 },
        { min_amount: 20000, bonus_type: 'percentage', bonus_amount: 0, bonus_percent: 5.0 }
      ];
      this.currentRule.slabs = this.slabsFromTiers('daily_business', this.currentRule.tiers);
    } else if (this.currentRule.category === 'service_addon') {
      this.currentRule.rule_type = 'slab';
      this.currentRule.frequency = 'daily';
      this.currentRule.tiers = [
        { service_name: 'Fusio Dose / Scrub', match_keyword: 'fusio', worth_amount: 0, incentive_amount: 100 },
        { service_name: 'Express Ritual (Fusio Dose +)', match_keyword: 'express ritual', worth_amount: 3600, incentive_amount: 150 }
      ];
      this.currentRule.slabs = this.slabsFromTiers('service_addon', this.currentRule.tiers);
    } else if (this.currentRule.category === 'service_target') {
      this.currentRule.rule_type = 'target';
      this.currentRule.frequency = 'daily';
      this.currentRule.tiers = [
        { service_name: 'Rituals Target', match_keyword: 'ritual', target_count: 5, reward_amount: 500 }
      ];
      this.currentRule.slabs = this.slabsFromTiers('service_target', this.currentRule.tiers);
    } else if (this.currentRule.category === 'services' || this.currentRule.category === 'products'
               || this.currentRule.category === 'memberships' || this.currentRule.category === 'packages') {
      this.currentRule.rule_type = 'multiple';
      this.currentRule.slabs = [];
      this.currentRule.tiers = [
        { min_multiple: 5.0, max_multiple: 5.99, incentive_percent: 3.0 },
        { min_multiple: 6.0, max_multiple: 6.99, incentive_percent: 6.0 },
        { min_multiple: 7.0, max_multiple: null, incentive_percent: 7.0 }
      ];
    }
  }

  getCategoryName(cat: string): string {
    const map: any = {
      'services': 'Services',
      'products': 'Products',
      'value_cards': 'Value Cards',
      'memberships': 'Memberships',
      'packages': 'Packages',
      'daily_business': 'Daily Business Slabs',
      'service_addon': 'Service Add-on Bonuses',
      'service_target': 'Service Volume Targets'
    };
    return map[cat] || cat;
  }

  getCategoryIcon(cat: string): string {
    const map: any = {
      'services': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 4L8.12 15.88M14.47 14.48L20 20M8.12 8.12L12 12"/></svg>',
      'products': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.69l5.66 5.66a8 8 0 1 1-11.31 0z"/></svg>',
      'value_cards': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 4H3a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h18a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zM1 10h22"/></svg>',
      'memberships': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 9a3 3 0 0 1 0 6v2a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-2a3 3 0 0 1 0-6V7a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v2z"/></svg>',
      'packages': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M16.5 9.4l-9-5.19M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16zM3.27 6.96L12 12.01l8.73-5.05M12 22.08V12"/></svg>',
      'daily_business': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/></svg>',
      'service_addon': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>',
      'service_target': '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8z M12 12h.01"/></svg>'
    };
    return map[cat] || '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z"/></svg>';
  }
  pettyCashForm: any = { description: '', amount: '', voucher_number: '', comments: '', category: null };
  pettyCategories: any[] = [];

  // The picture of the bill behind a new spend (compulsory); on an edit the saved one stays unless a new one is picked.
  pettyBillFile: File | null = null;
  pettyBillPreview = '';
  private pettyBillInput: HTMLInputElement | null = null;

  onPettyBill(event: Event) {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] || null;
    if (!file) return;
    if (!/\.(jpe?g|png|webp)$/i.test(file.name)) {
      alert('The bill must be a picture (jpg, png or webp).');
      input.value = '';
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      alert('The picture is too big; keep it under 8 MB.');
      input.value = '';
      return;
    }
    this.pettyBillInput = input;
    this.clearPettyBill(false);
    this.pettyBillFile = file;
    this.pettyBillPreview = URL.createObjectURL(file);
  }

  clearPettyBill(resetInput = true) {
    if (this.pettyBillPreview) URL.revokeObjectURL(this.pettyBillPreview);
    this.pettyBillFile = null;
    this.pettyBillPreview = '';
    if (resetInput && this.pettyBillInput) this.pettyBillInput.value = '';
  }

  /** Where a saved bill picture can be opened. */
  billLink(log: any): string {
    return log?.bill_image_url ? this.apiService.baseUrl.replace(/\/$/, '') + log.bill_image_url : '';
  }

  loadPettyCategories() {
    // Categories belong to Petty Cash; any other finance role is refused by the
    // server, so do not ask.
    if (!this.isOwner && this.permissions.finance?.pettycash?.read !== true) return;
    this.apiService.get('finance/api/petty-cash-categories/', undefined, { headers: { 'X-Background-Request': '1' } }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (rows: any) => { this.pettyCategories = (Array.isArray(rows) ? rows : rows?.results || []).filter((c: any) => c.is_active); this.cdr.detectChanges(); },
      error: () => { /* categories are optional */ },
    });
  }

  // The server's refusal in words: a {detail} or the first field error.
  private pettyError(err: any, fallback: string): string {
    const body = err?.error;
    if (body?.detail) return body.detail;
    const first = body && typeof body === 'object' ? Object.values(body)[0] : null;
    return (Array.isArray(first) ? first[0] : first) || fallback;
  }
  pettyCashLogs: any[] = [];

  activeShift: any = null;
  shiftFloat: number = 0;
  shiftActualCash: number = 0;

  // ---- Daily Closing ----
  closingDate = todayLocal();
  noClosingForDate = true;
  closingData: any = {
    opening_balance: 0,
    days_collection: 0,
    todays_expenses: 0,
    cash_in_hand: '',
    cash_deposit: '',
    credit_card: '',
    upi: '',
    paytm: '',
    bharat_pe: '',
    cheque_netbanking: '',
    google_pay: '',
    phone_pe: '',
    nearbuy: '',
    gift_card: '',
    other: '',
  };
  closingHistory: any[] = [];
  closingPaymentSummary: any = null;

  ngOnInit() {
    const userStr = localStorage.getItem('user');
    if (userStr) {
      try {
        const user = JSON.parse(userStr);
        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.permissions = user.permissions || {};
      } catch (e) { }
    }
    this.loadPettyCategories();

    if (this.hasSingleSalonAccess) {
      this.activeMainTab = 'single';
      if (this.isOwner || this.permissions.finance?.register_summary?.read) {
        this.activeSingleTab = 'register';
      } else if (this.permissions.finance?.monthly_sales?.read) {
        this.activeSingleTab = 'monthly';
      } else if (this.permissions.finance?.detailed_revenues?.read) {
        this.activeSingleTab = 'detailed';
      } else if (this.permissions.finance?.refunds?.read) {
        this.activeSingleTab = 'refunds';
      } else if (this.permissions.finance?.procurement?.read) {
        this.activeSingleTab = 'procurement';
      } else if (this.permissions.finance?.tax_report?.read) {
        this.activeSingleTab = 'tax';
      } else if (this.permissions.finance?.service_drilldown?.read) {
        this.activeSingleTab = 'services';
      } else if (this.permissions.finance?.staff_performance?.read) {
        this.activeSingleTab = 'staffperf';
      }
    } else if (this.permissions.finance?.multi_salon?.read) {
      this.activeMainTab = 'multi';
    } else if (this.permissions.finance?.incentives?.read) {
      this.activeMainTab = 'incentives';
    } else if (this.permissions.finance?.pettycash?.read || this.permissions.finance?.shifts?.read) {
      this.activeMainTab = 'pettycash';
    } else if (this.permissions.finance?.manage?.read) {
      this.activeMainTab = 'manage';
    } else {
      this.activeMainTab = '';
    }

    // Default to this month (more relevant than past 30 days)
    const today = new Date();
    const tzOffset = today.getTimezoneOffset() * 60000;
    const localToday = new Date(today.getTime() - tzOffset);
    const y = today.getFullYear();
    const m = today.getMonth();
    const thisMonthStart = `${y}-${String(m + 1).padStart(2, '0')}-01`;
    const todayStr = toLocalDateString(localToday);
    
    this.startDate = thisMonthStart;
    this.endDate = todayStr;
    this.multiStartDate = thisMonthStart;
    this.multiEndDate = todayStr;
    this.incentiveStartDate = thisMonthStart;
    this.incentiveEndDate = todayStr;
    this.closingStartDate = thisMonthStart;
    this.closingEndDate = todayStr;
    this.currentRange = 'thisMonth'; // Set default fast-pill to "This Month"

    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.centers = Array.isArray(data) ? data : (data.results || []);
      if (!this.hasGlobalAccess) {
        const userStr2 = localStorage.getItem('user');
        if (userStr2) {
          try {
            const user = JSON.parse(userStr2);
            this.selectedFilterLocation = user?.center_id || null;
          } catch (e) { }
        }
        if (this.centers.length > 0 && !this.centers.some((c: any) => c.id == this.selectedFilterLocation)) {
          this.selectedFilterLocation = this.centers[0].id;
        }
      } else {
        const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
        if (remembered !== undefined) this.selectedFilterLocation = remembered;
      }
      // Auto-load data for the active tab once centers are known
      if (this.activeMainTab === 'single') {
        this.setSingleTab(this.activeSingleTab);
      } else if (this.activeMainTab === 'pettycash') {
        this.loadPettyCash();
        this.loadClosingHistory();
        this.checkActiveShift();
        this.loadTodayClosingData();
      } else if (this.activeMainTab === 'multi') {
        this.runMultiSalon();
      } else if (this.activeMainTab === 'incentives') {
        this.loadIncentives();
      } else if (this.activeMainTab === 'manage') {
        this.loadIncentiveRules();
      }
      // else: this role holds no submodule that has a screen here, so there is
      // nothing to load (it used to ask for petty cash and be refused).
      this.cdr.detectChanges();
    });
  }

  /** Holds any report that lives under Single Salon. */
  get hasSingleSalonAccess(): boolean {
    const f = this.permissions.finance || {};
    return this.isOwner || ['register_summary', 'monthly_sales', 'detailed_revenues', 'refunds',
      'procurement', 'tax_report', 'service_drilldown', 'staff_performance'].some(k => f[k]?.read === true);
  }

  /** True when the role has no Finance submodule that opens a screen. */
  get noFinanceScreen(): boolean {
    return this.activeMainTab === '';
  }

  onLocationChange() {
    this.refreshCurrentTab();
  }

  setDateRange(range: string) {
    this.currentRange = range;
    const today = new Date();
    const y = today.getFullYear();
    const m = today.getMonth();
    const d = today.getDate();

    let start = '';
    let end = '';

    if (range === 'thisMonth') {
      start = `${y}-${String(m + 1).padStart(2, '0')}-01`;
      end = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'past3Months') {
      const past = new Date(y, m - 2, 1);
      start = `${past.getFullYear()}-${String(past.getMonth() + 1).padStart(2, '0')}-01`;
      end = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    } else if (range === 'past6Months') {
      const past = new Date(y, m - 5, 1);
      start = `${past.getFullYear()}-${String(past.getMonth() + 1).padStart(2, '0')}-01`;
      end = `${y}-${String(m + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
    }

    this.startDate = start;
    this.endDate = end;
    this.multiStartDate = start;
    this.multiEndDate = end;
    this.incentiveStartDate = start;
    this.incentiveEndDate = end;
    this.closingStartDate = start;
    this.closingEndDate = end;

    this.refreshCurrentTab();
  }

  refreshCurrentTab() {
    // Reload data smoothly for the active tab and location
    if (this.activeMainTab === 'single') {
      this.setSingleTab(this.activeSingleTab);
    } else if (this.activeMainTab === 'pettycash') {
      this.loadPettyCash();
      this.loadClosingHistory();
      this.loadTodayClosingData();
    } else if (this.activeMainTab === 'multi') {
      this.runMultiSalon();
    } else if (this.activeMainTab === 'incentives') {
      this.loadIncentives();
    } else if (this.activeMainTab === 'manage') {
      this.loadIncentiveRules();
    }
  }

  onApplyFilter() {
    this.closingStartDate = this.startDate;
    this.closingEndDate = this.endDate;
    this.multiStartDate = this.startDate;
    this.multiEndDate = this.endDate;
    this.incentiveStartDate = this.startDate;
    this.incentiveEndDate = this.endDate;

    this.refreshCurrentTab();
  }

  setMainTab(tab: string) {
    this.activeMainTab = tab;
    if (tab === 'single') {
      this.setSingleTab(this.activeSingleTab);
    } else if (tab === 'pettycash') {
      this.loadPettyCash();
      this.loadClosingHistory();
      this.loadTodayClosingData();
      this.checkActiveShift();
    } else if (tab === 'multi') {
      this.runMultiSalon();
    } else if (tab === 'incentives') {
      this.resetIncentiveDates();
      this.loadIncentives();
    } else if (tab === 'manage') {
      this.loadIncentiveRules();
    }
  }

  setClosingTab(tab: string) {
    this.activeClosingTab = tab;
    if (tab === 'admin') {
      this.loadClosingHistory();
    } else if (tab === 'manager') {
      this.loadPettyCash();
      this.loadTodayClosingData();
      this.checkActiveShift();
    }
  }

  selectAdminClosing(row: any) {
    this.selectedAdminClosing = row;
  }

  exportClosingHistory() {
    alert("Export functionality coming soon");
  }

  setSingleTab(tab: string) {
    this.activeSingleTab = tab;
    if (tab === 'register') this.runRegisterSummary();
    else if (tab === 'monthly') this.runMonthlySales();
    else if (tab === 'detailed') this.runDetailedRevenues();
    else if (tab === 'refunds') this.runRefunds();
    else if (tab === 'procurement') this.runProcurement();
    else if (tab === 'tax') this.runTaxReport();
    else if (tab === 'services') this.runServiceDrilldown();
    else if (tab === 'staffperf') this.runStaffPerformance();
  }

  onDateChange() {
    // Only fire if both dates are fully entered (YYYY-MM-DD = 10 chars)
    if (!this.startDate || !this.endDate || this.startDate.length < 10 || this.endDate.length < 10) return;
    // Sanity check: start must not be after end
    if (this.startDate > this.endDate) return;

    this.multiStartDate = this.startDate;
    this.multiEndDate = this.endDate;
    this.incentiveStartDate = this.startDate;
    this.incentiveEndDate = this.endDate;
    this.closingStartDate = this.startDate;
    this.closingEndDate = this.endDate;

    this.refreshCurrentTab();
  }

  clearSingleTabData() {
    this.registerSummaryData = null;
    this.monthlySalesData = [];
    this.detailedRevenuesData = [];
    this.refundsData = null;
    this.procurementData = null;
    this.taxReportData = null;
    this.serviceDrilldownData = null;
    this.staffPerformanceData = null;
  }

  setMultiTab(tab: string) {
    this.activeMultiTab = tab;
    this.runMultiSalon();
  }

  setPettyCashTab(tab: string) {
    this.activePettyCashTab = tab;
    if (tab === 'admin') this.loadClosingHistory();
    if (this.activeMainTab === 'pettycash') {
      this.loadPettyCash();
      this.checkActiveShift();
      this.loadTodayClosingData();
    }
  }

  exportExcel() {
    if (this.exportCurrentReportCsv()) return;
    let url = '';
    const centerId = this.getCenterId();
    
    if (this.activeMainTab === 'multi') {
      url = `${this.apiService.baseUrl}/finance/api/export_multi_salon/?start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
    } else if (this.activeMainTab === 'single') {
      const base = `${this.apiService.baseUrl}/finance/api`;
      const dateParams = `start_date=${this.startDate}&end_date=${this.endDate}`;
      let cIdParam = centerId ? `&center_id=${centerId}` : '';
      
      if (this.activeSingleTab === 'register') {
        url = `${base}/register_summary/?export=true&${dateParams}${cIdParam}`;
      } else if (this.activeSingleTab === 'monthly') {
        url = `${base}/monthly_sales/?export=true&${dateParams}${cIdParam}`;
      } else if (this.activeSingleTab === 'detailed') {
        url = `${base}/detailed_revenues/?export=true&${dateParams}${cIdParam}`;
      } else if (this.activeSingleTab === 'refunds') {
        url = `${base}/refunds/?export=true&${dateParams}${cIdParam}`;
      } else if (this.activeSingleTab === 'procurement') {
        url = `${base}/procurement/?export=true&${dateParams}${cIdParam}`;
      } else {
        url = `${base}/export/?start_date=${this.startDate}&end_date=${this.endDate}&type=detailed${cIdParam}`;
      }
    }
    
    if (!url) return;
    
    const token = localStorage.getItem('token');
    fetch(url, { headers: { 'Authorization': `Token ${token}` } })
      .then(res => {
        if (!res.ok) throw new Error('Network response was not ok');
        return res.blob();
      })
      .then(blob => {
        const a = document.createElement('a');
        a.href = window.URL.createObjectURL(blob);
        a.download = `finance_export_${Date.now()}.xlsx`;
        document.body.appendChild(a);
        a.click();
        a.remove();
      })
      .catch(err => {
        console.error("Export error:", err);
        alert('Failed to export Excel.');
      });
  }

  loadRegisterSummary() {
  }

  getCenterId(): number | undefined {
    const v = this.selectedFilterLocation;
    if (!v || v === 'null' || v === null || v === 'undefined' || v === undefined) return undefined;
    return typeof v === 'number' ? v : parseInt(v, 10);
  }

  // ---- Register Summary ----
  runRegisterSummary() {
    this.isLoadingRegister = true;
    this.cdr.detectChanges();
    this.apiService.getRegisterSummary(this.getCenterId(), this.startDate, this.endDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.registerSummaryData = res;
        this.isLoadingRegister = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        console.error('[Finance] Register summary error:', err);
        this.isLoadingRegister = false;
        this.cdr.detectChanges();
      }
    });
  }

  // ---- Monthly Sales ----
  runMonthlySales() {
    this.isLoadingMonthly = true;
    this.cdr.detectChanges();
    this.apiService.getMonthlySales(this.getCenterId(), this.startDate, this.endDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.monthlySalesData = res || [];
        this.isLoadingMonthly = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.isLoadingMonthly = false;
        this.cdr.detectChanges();
      }
    });
  }

  // ---- Detailed Revenues ----
  detailedRevenuesPage = 1;
  detailedRevenuesPageSize = 100;
  detailedRevenuesTotal = 0;

  runDetailedRevenues() {
    this.isLoadingDetailed = true;
    this.cdr.detectChanges();
    this.apiService.getDetailedRevenues(this.getCenterId(), this.startDate, this.endDate, this.detailedRevenuesPage, this.detailedRevenuesPageSize).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.detailedRevenuesData = res.results || res || [];
        this.detailedRevenuesTotal = res.count || 0;
        this.isLoadingDetailed = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.isLoadingDetailed = false;
        this.cdr.detectChanges();
      }
    });
  }

  // ---- Refunds ----
  runRefunds() {
    this.isLoadingRefunds = true;
    this.cdr.detectChanges();
    this.apiService.getFinanceRefunds(this.getCenterId(), this.startDate, this.endDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.refundsData = res;
        this.isLoadingRefunds = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.isLoadingRefunds = false;
        this.cdr.detectChanges();
      }
    });
  }

  // ---- Procurement ----
  runProcurement() {
    this.isLoadingProcurement = true;
    this.cdr.detectChanges();
    this.apiService.getProcurementReport(this.getCenterId(), this.startDate, this.endDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.procurementData = res;
        this.isLoadingProcurement = false;
        this.cdr.detectChanges();
      },
      error: () => { 
        this.isLoadingProcurement = false;
        this.cdr.detectChanges();
      }
    });
  }

  // ---- Tax Report, Service Drilldown, Staff Performance ----
  private reportParams(): Record<string, any> {
    const params: Record<string, any> = { start_date: this.startDate, end_date: this.endDate };
    const cid = this.getCenterId();
    if (cid) params['center_id'] = cid;
    return params;
  }

  runTaxReport() {
    this.isLoadingTax = true;
    this.cdr.detectChanges();
    this.apiService.get('finance/api/reports/tax/', this.reportParams()).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => { this.taxReportData = res; this.isLoadingTax = false; this.cdr.detectChanges(); },
      error: () => { this.isLoadingTax = false; this.cdr.detectChanges(); }
    });
  }

  runServiceDrilldown() {
    this.isLoadingServiceDrilldown = true;
    this.cdr.detectChanges();
    this.apiService.get('finance/api/reports/services/', this.reportParams()).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => { this.serviceDrilldownData = Array.isArray(res) ? res : (res?.results || []); this.isLoadingServiceDrilldown = false; this.cdr.detectChanges(); },
      error: () => { this.isLoadingServiceDrilldown = false; this.cdr.detectChanges(); }
    });
  }

  runStaffPerformance() {
    this.isLoadingStaffPerformance = true;
    this.cdr.detectChanges();
    this.apiService.get('finance/api/reports/staff-performance/', this.reportParams()).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => { this.staffPerformanceData = Array.isArray(res) ? res : (res?.results || []); this.isLoadingStaffPerformance = false; this.cdr.detectChanges(); },
      error: () => { this.isLoadingStaffPerformance = false; this.cdr.detectChanges(); }
    });
  }

  /** Revenue, count and tax columns of the drilldown, summed for the footer. */
  sumOf(rows: any[] | null, key: string): number {
    return (rows || []).reduce((total, row) => total + (Number(row?.[key]) || 0), 0);
  }

  /** These three reports have no server-side Excel; the screen's rows download as a CSV instead. */
  private downloadCsv(name: string, headers: string[], rows: any[][]) {
    const cell = (v: any) => {
      let text = v === null || v === undefined ? '' : String(v);
      // A cell that starts with = + - @ is read as a formula by a spreadsheet.
      if (/^[=+\-@]/.test(text) && isNaN(Number(text))) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const csv = [headers, ...rows].map(r => r.map(cell).join(',')).join('\r\n');
    const blob = new Blob(['﻿' + csv], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = window.URL.createObjectURL(blob);
    a.download = `${name}_${this.startDate}_${this.endDate}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  exportCurrentReportCsv(): boolean {
    if (this.activeMainTab !== 'single') return false;
    if (this.activeSingleTab === 'tax' && this.taxReportData) {
      this.downloadCsv('tax_report',
        ['Bill No.', 'Date/Time', 'Client', 'Taxable value', 'CGST', 'SGST', 'Total tax', 'Bill total'],
        (this.taxReportData.invoices || []).map((i: any) =>
          [i.invoice_number, i.created_at, i.client_name, i.subtotal, i.cgst_amount, i.sgst_amount, i.tax_amount, i.total_amount]));
      return true;
    }
    if (this.activeSingleTab === 'services' && this.serviceDrilldownData) {
      this.downloadCsv('service_drilldown', ['Item', 'Type', 'Count', 'Revenue', 'Tax', 'Average price'],
        this.serviceDrilldownData.map(r => [r.name, r.type, r.count, r.revenue, r.tax, r.avg_price]));
      return true;
    }
    if (this.activeSingleTab === 'staffperf' && this.staffPerformanceData) {
      this.downloadCsv('staff_performance', ['Staff', 'Designation', 'Centre', 'Services done', 'Revenue generated', 'Tax collected'],
        this.staffPerformanceData.map(r => [r.staff_name, r.designation, r.center, r.services_done, r.revenue_generated, r.tax_collected]));
      return true;
    }
    return false;
  }

  exportPdfMulti() {
    // Not implemented
  }

  getTargetColor(percentage: number | string): string {
    const p = typeof percentage === 'string' ? parseFloat(percentage) : percentage;
    if (isNaN(p)) return '#ef4444';
    if (p >= 100) return '#10b981'; // Emerald Green
    if (p >= 80) return '#0ea5e9'; // Light Blue
    if (p >= 65) return '#b45309'; // Brown/Gold
    if (p >= 50) return '#f97316'; // Orange
    return '#ef4444'; // Red
  }

  getTrendColor(current: number | string, previous: number | string | undefined, invert: boolean = false): string {
    if (previous === undefined || previous === null || previous === '') return '';
    const curr = typeof current === 'string' ? parseFloat(current.replace(/,/g, '')) : current;
    const prev = typeof previous === 'string' ? parseFloat(previous.replace(/,/g, '')) : previous;
    if (isNaN(curr) || isNaN(prev)) return '';
    
    if (curr > prev) return invert ? '#ef4444' : '#10b981'; // Green (or Red if inverted)
    if (curr < prev) return invert ? '#10b981' : '#ef4444'; // Red (or Green if inverted)
    return ''; // Equal
  }

  // ---- Incentives (Upgraded Dashboard & Engine) ----
  formatDateLocal(d: Date): string {
    const year = d.getFullYear();
    const month = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
  }

  resetIncentiveDates() {
    const now = new Date();
    if (this.incentiveFrequency === 'monthly') {
      const year = now.getFullYear();
      const month = now.getMonth();
      const firstDay = new Date(year, month, 1);
      const lastDay = new Date(year, month + 1, 0);
      this.incentiveStartDate = this.formatDateLocal(firstDay);
      this.incentiveEndDate = this.formatDateLocal(lastDay);
    } else {
      const todayStr = this.formatDateLocal(now);
      this.incentiveStartDate = todayStr;
      this.incentiveEndDate = todayStr;
    }
  }

  loadIncentives() {
    if (!this.incentiveStartDate || !this.incentiveEndDate) {
      this.resetIncentiveDates();
    }
    this.isLoadingIncentives = true;
    this.apiService.getIncentiveReport(
      this.incentiveStartDate,
      this.incentiveEndDate,
      this.selectedFilterLocation ? Number(this.selectedFilterLocation) : undefined,
      this.incentiveFrequency
    ).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: data => {
        this.incentivesData = data || [];
        this.isLoadingIncentives = false;
        this.cdr.detectChanges();
        this.loadIncentiveFinanceFigures();
      },
      error: () => {
        this.isLoadingIncentives = false;
        this.cdr.detectChanges();
      }
    });
  }

  /** The location(s) the dropdown has chosen, for the headline tiles. */
  get incentiveCentreLabel(): string {
    if (this.selectedFilterLocations.length > 1) return `${this.selectedFilterLocations.length} centres`;
    return this.selectedFilterLocation ? this.selectedCenterName : 'All centres';
  }

  /** Finance's own figures for the same period and locations, to set beside the tiles. */
  incentiveFinance: { services: number; products: number } | null = null;

  private loadIncentiveFinanceFigures() {
    this.incentiveFinance = null;
    this.apiService.getRegisterSummary(
      this.selectedFilterLocation ? Number(this.selectedFilterLocation) : undefined,
      this.incentiveStartDate, this.incentiveEndDate,
    ).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: res => {
        this.incentiveFinance = {
          services: parseFloat(res?.revenues?.services?.amount) || 0,
          products: parseFloat(res?.revenues?.products?.amount) || 0,
        };
        this.cdr.detectChanges();
      },
      error: () => { /* the tiles simply stay without the comparison */ },
    });
  }

  /** Services Finance counted but no listed staff member earns on: done by inactive staff or staff outside the chosen locations. */
  getServicesOutsideThisList(): number {
    if (!this.incentiveFinance) return 0;
    const gap = this.incentiveFinance.services - this.getTotalServicesBilled();
    return gap > 0.004 ? Math.round(gap * 100) / 100 : 0;
  }
  getProductsOutsideThisList(): number {
    if (!this.incentiveFinance) return 0;
    const gap = this.incentiveFinance.products - this.getTotalProductsBilled();
    return gap > 0.004 ? Math.round(gap * 100) / 100 : 0;
  }

  /** What Finance reports for these staff's services, before the part paid from cards/advances comes off. */
  getTotalServicesBilled(): number {
    return (this.incentivesData || []).reduce((s, i) => s + (parseFloat(i.services_billed) || 0), 0);
  }
  getTotalServicesFromCards(): number {
    return (this.incentivesData || []).reduce((s, i) => s + (parseFloat(i.services_from_cards) || 0), 0);
  }
  getTotalProductsBilled(): number {
    return (this.incentivesData || []).reduce((s, i) => s + (parseFloat(i.products_billed) || 0), 0);
  }
  getTotalProductsFromCards(): number {
    return (this.incentivesData || []).reduce((s, i) => s + (parseFloat(i.products_from_cards) || 0), 0);
  }

  getTotalServicesRevenue(): number {
    return (this.incentivesData || []).reduce((acc, r) => acc + (parseFloat(r.services_revenue) || 0), 0);
  }
  getTotalProductsRevenue(): number {
    return (this.incentivesData || []).reduce((acc, r) => acc + (parseFloat(r.products_revenue) || 0), 0);
  }
  getTotalCardsRevenue(): number {
    return (this.incentivesData || []).reduce((acc, r) => acc + (parseFloat(r.cards_revenue) || 0), 0);
  }

  onIncentiveFrequencyChange(freq: string) {
    this.incentiveFrequency = freq;
    this.resetIncentiveDates();
    this.loadIncentives();
  }

  getFilteredIncentives(): any[] {
    if (!this.incentivesData) return [];
    if (!this.incentiveSearchQuery?.trim()) return this.incentivesData;
    const q = this.incentiveSearchQuery.toLowerCase().trim();
    return this.incentivesData.filter(item =>
      (item.staff_name && item.staff_name.toLowerCase().includes(q)) ||
      (item.role && item.role.toLowerCase().includes(q)) ||
      (item.center && item.center.toLowerCase().includes(q))
    );
  }

  openStaffBreakdown(staff: any) {
    this.selectedStaffBreakdown = staff;
    this.showStaffBreakdownModal = true;
  }

  closeStaffBreakdown() {
    this.selectedStaffBreakdown = null;
    this.showStaffBreakdownModal = false;
  }

  getTotalSalesRevenue(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.revenue) || 0), 0);
  }

  getTotalIncentivePayout(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.incentive_amount) || 0), 0);
  }

  getTotalCardsSold(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (curr.cards_count || (curr.card_slabs?.length || 0)), 0);
  }

  /** The bill lines on which this person earned a service add-on (a daily incentive). */
  addonLines(staff: any): any[] {
    return (staff?.items || []).filter((line: any) => (parseFloat(line?.addon_amount) || 0) > 0);
  }

  /** The sum of one incentive column over every staff row of the report. */
  sumIncentive(key: string): number {
    return (this.incentivesData || []).reduce((acc, curr) => acc + (parseFloat(curr?.[key]) || 0), 0);
  }

  /** Daily incentive (daily slab + add-ons + targets) over every row: what the Daily view pays. */
  getTotalDailyIncentive(): number {
    return this.sumIncentive('daily_incentive');
  }

  getTotalCardsIncentive(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.cards_incentive) || 0), 0);
  }

  getTotalProductIncentive(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.products_incentive) || 0), 0);
  }

  getTotalServiceIncentive(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.services_incentive) || 0), 0);
  }

  // Daily-mode specific totals
  getTotalDailyBonus(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.daily_bonus) || 0), 0);
  }

  getTotalAddonIncentive(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.service_addon_incentive) || 0), 0);
  }

  getTotalTargetIncentive(): number {
    if (!this.incentivesData) return 0;
    return this.incentivesData.reduce((acc, curr) => acc + (parseFloat(curr.service_target_incentive) || 0), 0);
  }

  exportIncentives() {
    if (!this.incentiveStartDate || !this.incentiveEndDate) return;
    let url = `${this.apiService.baseUrl}/finance/api/reports/incentive-calculation/?export=true&start_date=${this.incentiveStartDate}&end_date=${this.incentiveEndDate}&frequency=${this.incentiveFrequency}`;
    if (this.selectedFilterLocations.length > 1) url += `&center_ids=${this.selectedFilterLocations.join(',')}`;
    else if (this.selectedFilterLocation) url += `&center_id=${this.selectedFilterLocation}`;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.blob())
        .then(blob => {
          const a = document.createElement('a');
          a.href = window.URL.createObjectURL(blob);
          a.download = `incentive_report_${this.incentiveFrequency}_${this.incentiveStartDate}_to_${this.incentiveEndDate}.xlsx`;
          a.click();
        });
    } else {
      window.open(url);
    }
  }

  getCenterName(centerId: any): string {
    if (!centerId) return 'Organisation-Wide (All Centers)';
    const found = this.centers.find(c => c.id === centerId);
    return found ? (found.display_name || found.center_name) : `Center #${centerId}`;
  }

  // ---- Multi Salon ----
  loadMultiSalonData() {
    this.isLoadingMulti = true;
    if (this.centers.length === 0) {
      this.isLoadingMulti = false;
      return;
    }

    const cid = this.getCenterId();
    const centersToLoad = cid ? this.centers.filter(c => c.id === cid) : this.centers;

    const observables = centersToLoad.map(center =>
      this.apiService.getRegisterSummary(center.id, this.multiStartDate, this.multiEndDate)
    );

    forkJoin(observables).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: results => {
        let mapped = (results as any[]).map((res: any, index) => ({
          center_name: centersToLoad[index].display_name || centersToLoad[index].center_name,
          ...res
        }));
        mapped.sort((a, b) => {
          const valA = a.categories?.target_achieved_percentage || 0;
          const valB = b.categories?.target_achieved_percentage || 0;
          return valA - valB;
        });
        this.multiSalonData = mapped;
        this.isLoadingMulti = false;
        this.cdr.detectChanges();
      },
      error: () => {
        this.isLoadingMulti = false;
        this.cdr.detectChanges();
      }
    });
  }

  
  loadMultiBalances() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/balances/`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiBalancesData = data || [];
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  get filteredBalances() {
    return this.multiBalancesData.filter(b => {
      if (b.type === 'Package' && !this.showServiceBalances) return false;
      if (b.type === 'Value Card' && !this.showCardBalances) return false;
      if (b.type === 'Advance' && !this.showAdvanceBalances) return false;
      return true;
    });
  }

  
  downloadMultiSales(itemType: string) {
    const cid = this.getCenterId();
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/sales_export/?item_type=${itemType}&start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
    if (cid) url += `&center_id=${cid}`;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.blob())
        .then(blob => {
          const downloadUrl = window.URL.createObjectURL(blob);
          const a = document.createElement('a');
          a.href = downloadUrl;
          a.download = `${itemType}_sales_report.xlsx`;
          document.body.appendChild(a);
          a.click();
          a.remove();
        })
        .catch(err => console.error(err));
    }
  }

  
  multiCategoriesData: any = null;

  loadMultiCategories() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/categories/?start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiCategoriesData = data;
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  
  serviceSearchTerm = '';
  productSearchTerm = '';
  multiServiceDrilldownData: any[] = [];
  multiProductDrilldownData: any[] = [];

  loadMultiServiceDrilldown() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/drilldown/services/?search=${this.serviceSearchTerm}&start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiServiceDrilldownData = data || [];
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  loadMultiProductDrilldown() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/drilldown/products/?search=${this.productSearchTerm}&start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiProductDrilldownData = data || [];
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  
  multiPettyCashLogs: any[] = [];
  multiPettyCashSummary: any[] = [];
  multiPettyCashTotal: number = 0;
  multiPettyCashCenterId: number = 0;

  loadMultiPettyCash() {
    const cid = this.getCenterId();
    this.multiPettyCashCenterId = cid || (this.centers && this.centers.length > 0 ? this.centers[0].id : 0);
    if (!this.multiPettyCashCenterId) {
      return;
    }
    
    this.isLoadingMulti = true;
    this.apiService.getPettyCashEntries(this.multiPettyCashCenterId, this.multiStartDate, this.multiEndDate)
      .pipe(takeUntilDestroyed(this.destroyRef))
      .subscribe({
        next: res => {
          this.multiPettyCashLogs = res || [];
          const summaryMap: any = {};
          this.multiPettyCashTotal = 0;
          for (let log of this.multiPettyCashLogs) {
            const cat = log.description || 'Miscellaneous';
            const amount = parseFloat(log.amount || 0);
            if (amount > 0) {
              summaryMap[cat] = (summaryMap[cat] || 0) + amount;
              this.multiPettyCashTotal += amount;
            }
          }
          this.multiPettyCashSummary = Object.keys(summaryMap).map(k => ({ category: k, amount: summaryMap[k] }));
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        },
        error: err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        }
      });
  }

  exportMultiPettyCash() {
    import('xlsx').then(xlsx => {
      const data = this.multiPettyCashLogs.map(log => {
        return {
          Date: new Date(log.date).toLocaleString(),
          'Recorded by': log.user_name || 'Unknown',
          Description: log.description || '-',
          'Amount (Out)': parseFloat(log.amount || 0),
          Voucher: log.voucher_number || '-',
          Comments: log.comments || '-'
        };
      });
      const ws = xlsx.utils.json_to_sheet(data);
      const wb = xlsx.utils.book_new();
      xlsx.utils.book_append_sheet(wb, ws, 'Petty Cash');
      xlsx.writeFile(wb, `PettyCash_${this.multiPettyCashCenterId}_${this.multiStartDate}_${this.multiEndDate}.xlsx`);
    });
  }

  
  multiClientsData: any[] = [];
  visibleClientsCount: number = 100;
  selectedMultiClient: any = null;

  loadMultiClients() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/clients/?start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiClientsData = data || [];
          this.visibleClientsCount = 100;
          this.selectedMultiClient = null;
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  exportMultiClients() {
    import('xlsx').then(xlsx => {
      const data = this.multiClientsData.map(c => ({
        'Client Name': c.name,
        'Phone': c.phone,
        'Member?': c.is_member,
        'Gender': c.gender,
        'Visits': c.visits,
        'Average Spend': c.avg_spend,
        'Total Spend': c.total_spend,
        'Last Visit': c.last_visit ? new Date(c.last_visit).toLocaleString() : '-',
        'Serv. Balance': c.serv_balance || '-',
        'Card Balance': c.card_balance || '-',
        'Advance': c.advance || '-'
      }));
      const ws = xlsx.utils.json_to_sheet(data);
      const wb = xlsx.utils.book_new();
      xlsx.utils.book_append_sheet(wb, ws, 'Clients');
      xlsx.writeFile(wb, `Clients_${this.multiStartDate}_${this.multiEndDate}.xlsx`);
    });
  }

  selectMultiClient(client: any) {
    this.selectedMultiClient = client;
  }

  
  multiStaffData: any[] = [];

  loadMultiStaff() {
    this.isLoadingMulti = true;
    let url = `${this.apiService.baseUrl}/finance/api/reports/multi_salon/staff/?start_date=${this.multiStartDate}&end_date=${this.multiEndDate}`;
      const cid = this.getCenterId();
      if (cid) url += (url.includes('?') ? '&' : '?') + 'center_id=' + cid;
    const token = localStorage.getItem('token');
    if (token) {
      fetch(url, { headers: { 'Authorization': `Token ${token}` } })
        .then(res => res.json())
        .then(data => {
          this.multiStaffData = data || [];
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        })
        .catch(err => {
          console.error(err);
          this.isLoadingMulti = false;
          this.cdr.detectChanges();
        });
    }
  }

  exportMultiStaff() {
    import('xlsx').then(xlsx => {
      const data = this.multiStaffData.map(s => ({
        'Staff': s.staff_name,
        'Salon': s.salon,
        'Revenue': s.revenue,
        'Services': s.services,
        'Service Red.': s.service_red,
        'Value Card Red.': s.value_card_red,
        'Products': s.products,
        'Packages': s.packages,
        'Memberships': s.memberships,
        'Gift Cards': s.gift_cards,
        'Cards': s.cards
      }));
      const ws = xlsx.utils.json_to_sheet(data);
      const wb = xlsx.utils.book_new();
      xlsx.utils.book_append_sheet(wb, ws, 'Staff Performance');
      xlsx.writeFile(wb, `Staff_Performance_${this.multiStartDate}_${this.multiEndDate}.xlsx`);
    });
  }

  runMultiSalon() {
    if (this.activeMultiTab === 'register') this.loadMultiSalonData();
    else if (this.activeMultiTab === 'balances') this.loadMultiBalances();
    else if (this.activeMultiTab === 'categories') this.loadMultiCategories();
    else if (this.activeMultiTab === 'service_drilldown') this.loadMultiServiceDrilldown();
    else if (this.activeMultiTab === 'product_drilldown') this.loadMultiProductDrilldown();
    else if (this.activeMultiTab === 'petty_cash') this.loadMultiPettyCash();
    else if (this.activeMultiTab === 'clients') this.loadMultiClients();
    else if (this.activeMultiTab === 'staff') this.loadMultiStaff();
  }

  // ---- Petty Cash ----
  loadPettyCash() {
    const cid = this.getCenterId();
    if (!cid && !this.hasGlobalAccess) return;
    this.apiService.getPettyCashEntries(cid, this.closingDate, this.closingDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(res => {
      this.pettyCashLogs = res || [];
      if (this.closingData) {
        this.closingData.todays_expenses = this.todaysExpenses;
      }
      this.cdr.detectChanges();
    });
  }

  submitPettyCash() {
    if (this.isSaving) return;
    const cid = this.getCenterId();
    if (!cid) return alert('Select a location first');
    if (!this.pettyCashForm.comments?.trim()) return alert('Comments are compulsory');
    if (!this.pettyCashForm.description?.trim()) return alert('Description is required');
    if (!this.pettyCashForm.amount) return alert('Amount is required');
    if (!this.pettyCashForm.category) return alert('Pick a category');
    if (!this.editingPettyCash && !this.pettyBillFile) return alert('Attach a picture of the bill or slip');

    this.isSaving = true;
    // Multipart, because of the picture. The date is set by the server when the spend is recorded.
    const data = new FormData();
    data.append('center', String(cid));
    for (const key of ['description', 'amount', 'voucher_number', 'comments']) data.append(key, this.pettyCashForm[key] ?? '');
    data.append('category', String(this.pettyCashForm.category));
    if (this.pettyBillFile) data.append('bill_image', this.pettyBillFile);

    if (this.editingPettyCash) {
      this.apiService.updatePettyCashEntry(this.editingPettyCash.id, data).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.pettyCashForm = { description: '', amount: '', voucher_number: '', comments: '', category: null };
          this.clearPettyBill();
          this.editingPettyCash = null;
          this.isSaving = false;
          alert('Petty cash entry updated successfully!');
          this.loadPettyCash();
        },
        error: (err) => {
          this.isSaving = false;
          alert(this.pettyError(err, 'Failed to update petty cash entry. Please try again.'));
          this.cdr.detectChanges();
        }
      });
    } else {
      this.apiService.createPettyCashEntry(data).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.pettyCashForm = { description: '', amount: '', voucher_number: '', comments: '', category: null };
          this.clearPettyBill();
          this.isSaving = false;
          alert('Petty cash entry added successfully!');
          this.loadPettyCash();
        },
        error: (err) => {
            this.isSaving = false;
            alert(this.pettyError(err, 'Failed to add petty cash entry. Please try again.'));
            this.cdr.detectChanges();
        }
      });
    }
  }

  editPettyCash(log: any) {
    this.editingPettyCash = log;
    this.clearPettyBill();
    this.pettyCashForm = {
      description: log.description,
      amount: log.amount,
      voucher_number: log.voucher_number || '',
      comments: log.comments || '',
      category: log.category ?? null,
    };
    // Scroll to top of form
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  get todaysExpenses(): number {
    return this.pettyCashLogs
      .filter(l => l.date && l.date.startsWith(this.closingDate))
      .reduce((sum, l) => sum + parseFloat(l.amount || 0), 0);
  }

  // ---- Daily Closing ----  // Admin View petty cash logs
  adminPettyCashLogs: any[] = [];

  loadClosingHistory() {
    const cid = this.getCenterId();
    if (!cid && !this.hasGlobalAccess) return;
    // Pass the current date range so the admin view is filtered correctly
    this.apiService.getDailyClosings(cid, undefined, this.closingStartDate, this.closingEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.closingHistory = data || [];
      this.cdr.detectChanges();
    });

    // Also load petty cash for the admin view using these dates
    this.apiService.getPettyCashEntries(cid, this.closingStartDate, this.closingEndDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.adminPettyCashLogs = data || [];
      this.cdr.detectChanges();
    });
  }

  loadTodayClosingData() {
    const cid = this.getCenterId();
    if (!cid) return;
    
    // Fetch opening balance from previous closing
    this.apiService.getDailyClosingOpeningBalance(cid, this.closingDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(res => {
      if (res && res.opening_balance !== undefined) {
        if (!this.activeShift || this.closingDate !== this.todayDate) {
           this.closingData.opening_balance = res.opening_balance;
        }
      }
      this.cdr.detectChanges();
    });

    // Check if a closing already exists for this date
    this.apiService.getDailyClosings(cid, this.closingDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(existing => {
      if (existing && existing.length > 0) {
        this.noClosingForDate = false;
        const saved = existing[0];
        this.closingData = { ...this.closingData, ...saved };
      } else {
        this.noClosingForDate = true;
      }
      this.cdr.detectChanges();
    });

    // Load today's cash collection from billing
    this.apiService.getRegisterSummary(cid, this.closingDate, this.closingDate).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(res => {
      const cash = res?.payment_methods?.cash?.amount || 0;
      this.closingData.days_collection = cash;
      this.closingData.todays_expenses = this.todaysExpenses;
      this.closingPaymentSummary = res?.payment_methods;
      this.cdr.detectChanges();
    });
  }

  get expectedCash(): number {
    return (parseFloat(this.closingData.opening_balance || 0) +
      parseFloat(this.closingData.days_collection || 0) -
      parseFloat(this.closingData.todays_expenses || 0));
  }

  get cashDifference(): number {
    const cashInHand = parseFloat(this.closingData.cash_in_hand || 0);
    return cashInHand - this.expectedCash;
  }

  get closingBalance(): number {
    return this.expectedCash - parseFloat(this.closingData.cash_deposit || 0);
  }

  onClosingDateChange() {
    this.noClosingForDate = true;
    this.loadPettyCash();
    this.loadTodayClosingData();
  }

  // ---- SHIFT MANAGEMENT ----
  checkActiveShift() {
    const cid = this.getCenterId();
    if (!cid) return;
    this.apiService.getShifts(cid, 'Open').pipe(takeUntilDestroyed(this.destroyRef)).subscribe(res => {
      if (res && res.length > 0) {
        this.activeShift = res[0];
        this.closingData.opening_balance = this.activeShift.starting_float;
      } else {
        this.activeShift = null;
      }
      this.cdr.detectChanges();
    });
  }

  openShift() {
    if (this.isSaving) return;
    const cid = this.getCenterId();
    if (!cid) return alert('Select a location first');
    if (this.shiftFloat < 0) return alert('Float cannot be negative');

    this.isSaving = true;
    this.apiService.openShift(cid, this.shiftFloat).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        alert('Register Opened Successfully!');
        this.isSaving = false;
        this.checkActiveShift();
      },
      error: () => this.isSaving = false
    });
  }

  closeShift() {
    if (!this.activeShift || this.isSaving) return;
    this.isSaving = true;
    this.apiService.closeShift(this.activeShift.id, this.shiftActualCash, this.expectedCash).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        alert(`Register Closed! Variance: Rs. ${this.shiftActualCash - this.expectedCash}`);
        // Save the daily closing as well for historical reports
        this.submitClosing();
        this.checkActiveShift();
        this.isSaving = false;
      },
      error: () => this.isSaving = false
    });
  }

  submitClosing() {
    if (this.isSaving && !this.activeShift) return; // If called from closeShift, isSaving is already true. Otherwise, check.
    if (!this.activeShift) this.isSaving = true;

    const cid = this.getCenterId();
    if (!cid) {
      if (!this.activeShift) this.isSaving = false;
      return alert('Select a location first');
    }
    const data = {
      center: cid,
      date: this.closingDate,
      opening_balance: this.closingData.opening_balance || 0,
      cash_in_hand: this.closingData.cash_in_hand || 0,
      todays_expenses: this.todaysExpenses,
      cash_deposit: this.closingData.cash_deposit || 0,
      closing_balance: this.closingBalance,
      credit_card: this.closingData.credit_card || 0,
      upi: this.closingData.upi || 0,
      paytm: this.closingData.paytm || 0,
      bharat_pe: this.closingData.bharat_pe || 0,
      cheque_netbanking: this.closingData.cheque_netbanking || 0,
      google_pay: this.closingData.google_pay || 0,
      phone_pe: this.closingData.phone_pe || 0,
      nearbuy: this.closingData.nearbuy || 0,
      gift_card: this.closingData.gift_card || 0,
      other: this.closingData.other || 0,
    };
    this.apiService.submitDailyClosing(data).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.noClosingForDate = false;
        alert('Closing submitted successfully!');
        if (!this.activeShift) this.isSaving = false;
        this.loadClosingHistory();
      },
      error: (err) => {
        const msg = err?.error?.detail || err?.error?.non_field_errors?.[0] || 'Failed to submit closing.';
        alert(msg);
        if (!this.activeShift) this.isSaving = false;
      }
    });
  }

  formatCurrency(val: number): string {
    return new Intl.NumberFormat('en-IN').format(val || 0);
  }

  get totalIncentiveAmount(): number {
    return this.incentivesData.reduce((sum, i) => sum + (parseFloat(i.incentive_amount) || 0), 0);
  }

  get selectedCenterName(): string {
    const cid = this.getCenterId();
    if (!cid) return 'All Locations';
    const c = this.centers.find(x => x.id === cid);
    return c ? (c.display_name || c.center_name) : 'Selected Center';
  }

  get adminTotals() {
    let totals = {
      opening: 0, system: 0, expenses: 0, cash_in_hand: 0, diff: 0, deposit: 0, closing: 0
    };
    if (this.closingHistory && this.closingHistory.length > 0) {
      for (let row of this.closingHistory) {
        totals.opening += parseFloat(row.opening_balance || 0);
        totals.system += parseFloat(row.system_cash || 0);
        totals.expenses += parseFloat(row.todays_expenses || 0);
        totals.cash_in_hand += parseFloat(row.cash_in_hand || 0);
        totals.diff += parseFloat(row.difference || 0);
        totals.deposit += parseFloat(row.cash_deposit || 0);
        totals.closing += parseFloat(row.closing_balance || 0);
      }
    }
    return totals;
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
