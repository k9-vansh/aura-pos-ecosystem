import { SoundService } from '../services/sound.service';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { ChangeDetectorRef, Component, DestroyRef, ElementRef, HostListener, OnInit, OnDestroy, ViewChild, inject, ChangeDetectionStrategy } from '@angular/core';
import { isRedemptionLine, money, priceCart, rupees, toAmount } from '../services/money';
import { capWalletRows, giftCardProblem, rebalancePayments } from '../services/payment-split';

import { ActivatedRoute } from '@angular/router';

import { CommonModule } from '@angular/common';

import { FormsModule } from '@angular/forms';
import { ClientSearchComponent } from './components/client-search/client-search';
import { BillingLandingComponent } from './components/landing/billing-landing.component';
import { HeldCartsModalComponent } from './components/held-carts-modal/held-carts-modal';
import { PaymentModalComponent } from './components/payment-modal/payment-modal';
import { LocationSelectorComponent } from '../components/location-selector/location-selector';
import { ApprovalWaitComponent } from '../components/approval-wait/approval-wait.component';
import { PinCheckComponent } from '../components/pin-check/pin-check.component';

import { ToastService } from '../services/toast.service';
import { ApiService } from '../services/api';
import { ConfirmService } from '../services/confirm.service';
import { todayLocal } from '../services/date-util';
import { LocationMemory } from '../services/location-memory';



import { ViewEncapsulation } from '@angular/core';

@Component({
  changeDetection: ChangeDetectionStrategy.OnPush,
  encapsulation: ViewEncapsulation.None,
  selector: 'app-billing',
  standalone: true,
  imports: [CommonModule, FormsModule, ClientSearchComponent, BillingLandingComponent, HeldCartsModalComponent, PaymentModalComponent, LocationSelectorComponent, ApprovalWaitComponent, PinCheckComponent],
  templateUrl: './billing.html',
  styleUrls: ['./billing.css']
})

export class BillingComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  toastService = inject(ToastService);
  private sound = inject(SoundService);
  @ViewChild('clientSearchInput') clientSearchInput!: ElementRef;
  @ViewChild('serviceSearchInput') serviceSearchInput!: ElementRef;
  // Toast Notifications
  toastMessage: string = '';
  toastType: 'success' | 'error' | 'info' = 'info';
  toastVisible: boolean = false;

  // Success & Print Flow
  showSuccessModal: boolean = false;
  completedInvoiceData: any = null;

  /**
   * Print on the paper set for the branch (Admin > Settings, "Receipt
   * paper"): Auto lets the printer's own paper decide; 80 mm, 58 mm and A4
   * fix it (#51, F21).
   */
  printInvoice() {
    const size = this.completedInvoiceData?.receipt_settings?.size || 'auto';
    const page = ({ '80mm': '80mm auto', '58mm': '58mm auto', 'a4': 'A4' } as any)[size] || 'auto';
    let el = document.getElementById('receipt-page-size') as HTMLStyleElement | null;
    if (!el) {
      el = document.createElement('style');
      el.id = 'receipt-page-size';
      document.head.appendChild(el);
    }
    el.textContent = `@media print { @page { size: ${page}; margin: ${size === 'a4' ? '10mm' : '0'}; } }`;
    this.sound.play('print');
    window.print();
  }

  closeSuccessModal() {
    this.showSuccessModal = false;
    this.completedInvoiceData = null;
  }

  toastTimeout: any;

  showToast(msg: string, type: 'success' | 'error' | 'info' = 'info') {
    this.toastService.show(msg, type);
  }


  apiService = inject(ApiService);

  cdr = inject(ChangeDetectorRef);

  route = inject(ActivatedRoute);



  viewMode: 'landing' | 'new-invoice' = 'landing';



  centers: any[] = [];

  selectedCenterId: number | null = null;



  // Landing view data

  globalInvoices: any[] = [];

  appointments: any[] = [];
  selectedAppointmentId: any = null;

  staffActivity: any[] = [];



  get selectedCenterName(): string {

    if (!this.selectedCenterId || !this.centers) return 'All Centers';

    const center = this.centers.find(c => c.id === this.selectedCenterId);

    return center ? center.display_name : 'All Centers';

  }

  get receiptCenter(): any {
    if (!this.completedInvoiceData || !this.centers) return null;
    const centerId = this.completedInvoiceData.center || this.selectedCenterId;
    return this.centers.find(c => c.id === centerId) || null;
  }

  // New Invoice view data

  searchPhone: string = '';

  clients: any[] = [];

  client: any = null;

  clientInvoices: any[] = [];

  clientServiceHistory: any[] = [];

  clientAdvances: any[] = [];

  clientAdvanceBalance: number = 0;
  clientCashbackBalance: number = 0;

  clientHistory: any[] = [];

  useAdvancePayment: boolean = false;



  advances: any[] = [];



  services: any[] = [];

  products: any[] = [];

  memberships: any[] = [];

  packages: any[] = [];

  cards: any[] = [];

  staffMembers: any[] = [];
  activityFeed: any[] = [];


  currentInvoiceId: number | null = null;
  currentInvoiceStatus: string = 'draft';

  promotions: any[] = [];

  selectedPromotion: any = null;

  configStaffIds: number[] = [];



  showCheckoutModal: boolean = false;

  // Advanced POS States
  heldCarts: any[] = [];
  showHeldCartsModal: boolean = false;
  barcodeBuffer: string = '';
  lastBarcodeKeystrokeTime: number = 0;

  checkoutPayments: any[] = [];

  checkoutRemaining: number = 0;

  // ─── Server quote (Phase 6) ─────────────────────────────────────────────
  // The server prices the bill - offer, value card, tax, rounding - and the
  // till shows that result. `priceCart` still gives the instant figure while
  // the quote is on its way, so the screen never waits.
  serverQuote: any = null;
  quoteStale = true;
  private quoteTimer: any = null;
  private quoteSeq = 0;
  /** The branch the lines on this bill were picked at: their staff, prices and stock belong to it. */
  private cartCenterId: number | null = null;
  private lastQuoteError = '';
  // ─── Approvals (Phase 5) ─────────────────────────────────────────────────
  // A price change or a discount above the free limit waits for an approver.
  // The server prices the bill and decides what needs approval; the till only
  // asks, waits, and finishes the bill with the approved requests.
  approvalIds: number[] = [];
  approvalWait: any[] | null = null;
  /** The client's value card covering this bill's services (F5), or null. */
  selectedValueCardId: number | null = null;
  /** The ways this branch is paid, from Admin > Settings. */
  branchPaymentMethods: string[] = ['Cash', 'UPI', 'Card', 'Cheque'];





  cart: any[] = [];

  // ── Service recipes ───────────────────────────────────────────────────
  // The services whose recipe the POS owns, at this branch: the pick-one groups (Colour,
  // Shampoo) with the stock of each option, and the products that can be added as an extra.
  // The person billing picks per client; the server checks every choice again.
  recipes: Record<string, any> = {};
  recipeExtras: any[] = [];
  recipeEditIndex: number | null = null;
  recipeExtraCode = '';
  recipeExtraQty: number | null = null;

  loadRecipes() {
    const cid = this.selectedCenterId ?? undefined;
    if (!cid) { this.recipes = {}; this.recipeExtras = []; return; }
    this.apiService.get('services/api/master/till-recipes/', { center_id: cid }, { headers: { 'X-Background-Request': '1' } })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (r: any) => {
          this.recipes = r?.recipes || {};
          this.recipeExtras = r?.extras || [];
          this.ensureRecipeDefaults();
          this.cdr.detectChanges();
        },
        error: () => { /* a role that cannot read services simply bills without choices */ },
      });
  }

  recipeOf(line: any): any {
    return line?.content_type === 'services.servicemaster' ? (this.recipes[String(line.object_id)] || null) : null;
  }

  /** Fill every group that has a default and no choice yet. Safe to run any number of times. */
  ensureRecipeDefaults() {
    for (const line of this.cart) {
      const recipe = this.recipeOf(line);
      if (!recipe) continue;
      const sel = line.recipe_choices || (line.recipe_choices = { choices: {}, extras: [] });
      sel.choices = sel.choices || {};
      sel.extras = sel.extras || [];
      for (const g of recipe.groups) {
        if (sel.choices[g.id]?.product_code) continue;
        const d = g.options.find((o: any) => o.is_default && !this.outOfStock(line, o));
        if (d) sel.choices[g.id] = { product_code: d.product_code };
      }
    }
  }

  chosenOption(line: any, g: any): any {
    const code = line?.recipe_choices?.choices?.[g.id]?.product_code;
    return code ? (g.options.find((o: any) => o.product_code === code) || null) : null;
  }

  usedQty(line: any, g: any, o: any): number {
    const typed = Number(line?.recipe_choices?.choices?.[g.id]?.quantity);
    return typed > 0 ? typed : Number(o.quantity) * Number(line.quantity || 1);
  }

  private outOfStock(line: any, o: any): boolean {
    return Number(o.stock) < Number(o.quantity) * Number(line.quantity || 1);
  }

  optionBlocked(line: any, g: any, o: any): boolean {
    return this.chosenOption(line, g)?.product_code !== o.product_code && this.outOfStock(line, o);
  }

  /** The required groups of a line with nothing chosen. */
  missingChoices(line: any): string[] {
    const recipe = this.recipeOf(line);
    if (!recipe) return [];
    return recipe.groups.filter((g: any) => g.required && !this.chosenOption(line, g)).map((g: any) => g.name);
  }

  /** The first line that still needs a required choice, or null. */
  recipeProblem(): { index: number; names: string[] } | null {
    this.ensureRecipeDefaults();
    for (let i = 0; i < this.cart.length; i++) {
      const names = this.missingChoices(this.cart[i]);
      if (names.length) return { index: i, names };
    }
    return null;
  }

  pickRecipeOption(line: any, g: any, o: any) {
    if (this.optionBlocked(line, g, o)) return;
    const sel = line.recipe_choices;
    const same = sel.choices[g.id]?.product_code === o.product_code;
    if (same && !g.required) { delete sel.choices[g.id]; }          // tapping the chosen one of an optional group clears it
    else if (!same) { sel.choices[g.id] = { product_code: o.product_code }; }
    this.refreshQuote();
    this.cdr.detectChanges();
  }

  setUsedQty(line: any, g: any, value: any) {
    const choice = line.recipe_choices?.choices?.[g.id];
    if (!choice) return;
    const n = Number(value);
    if (value === '' || value === null || !(n > 0)) delete choice.quantity;
    else choice.quantity = String(n);
    this.refreshQuote();
  }

  addRecipeExtra(line: any) {
    const code = this.recipeExtraCode;
    const qty = Number(this.recipeExtraQty);
    if (!code || !(qty > 0)) { this.showToast('Choose the product and how much was used.', 'error'); return; }
    const extra = this.recipeExtras.find((e: any) => e.product_code === code);
    if (extra && Number(extra.stock) < qty) { this.showToast(`Only ${extra.stock} ${extra.unit} of ${extra.name} left.`, 'error'); return; }
    line.recipe_choices.extras.push({ product_code: code, quantity: String(qty) });
    this.recipeExtraCode = '';
    this.recipeExtraQty = null;
    this.refreshQuote();
    this.cdr.detectChanges();
  }

  removeRecipeExtra(line: any, index: number) {
    line.recipe_choices.extras.splice(index, 1);
    this.refreshQuote();
  }

  extraName(code: string): string {
    return this.recipeExtras.find((e: any) => e.product_code === code)?.name || code;
  }

  /** A typed quantity belongs to the quantity it was typed for; a new quantity starts again from the recipe. */
  private clearRecipeQuantities(line: any) {
    const choices = line?.recipe_choices?.choices;
    if (!choices) return;
    Object.values(choices).forEach((c: any) => { delete c.quantity; });
  }

  openRecipe(index: number) { this.recipeEditIndex = index; this.recipeExtraCode = ''; this.recipeExtraQty = null; this.loadRecipes(); }
  closeRecipe() { this.recipeEditIndex = null; this.cdr.detectChanges(); }

  /** A press on the dark backdrop closes the recipe dialog; a press inside it must keep its default (a click into a box). A template `a && b()` returns false for a press inside and blocks that. */
  onRecipeBackdropPress(event: Event) {
    if (event.target === event.currentTarget) this.closeRecipe();
  }

  activeTab: 'search' | 'services' | 'products' | 'cards' | 'packages' | 'memberships' | 'advance' = 'search';

  serviceSearchTerm: string = '';
  productSearchTerm: string = '';

  toggleServiceDropdown(event?: Event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    this.selectedItemForConfig = null;
    this.configType = null;
    if (this.activeTab === 'services') {
      this.activeTab = 'search';
    } else {
      this.activeTab = 'services';
      this.productSearchTerm = '';
    }
    this.cdr.detectChanges();
  }

  toggleProductDropdown(event?: Event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    this.selectedItemForConfig = null;
    this.configType = null;
    if (this.activeTab === 'products') {
      this.activeTab = 'search';
    } else {
      this.activeTab = 'products';
      this.serviceSearchTerm = '';
    }
    this.cdr.detectChanges();
  }



  // Detailed Configuration State

  selectedItemForConfig: any = null;

  configType: 'service' | 'product' | 'card' | 'advance' | 'package' | 'membership' | null = null;

  managerDiscountPercent: number = 0;

  managerDiscountAmount: number = 0;

  customBasePrice: number | null = null;

  finalConfigPrice: number = 0;

  configStaffId: any = null;
  configStaffAllocations: any = {};

  advanceAmount: number = 0;
  advanceGstPercent: number = 0;
  advanceGstAmount: number = 0;
  advanceDescription: string = '';

  onAdvanceAmountChange() {
    this.onAdvanceGstPercentChange();
  }

  onAdvanceGstPercentChange() {
    if (this.advanceGstPercent > 0) {
      this.advanceGstAmount = (this.advanceAmount * this.advanceGstPercent) / 100;
    } else {
      this.advanceGstAmount = 0;
    }
  }


  // Taxes and Discounts

  invoiceDiscount: number = 0;

  invoiceCGST: number = 0;

  invoiceSGST: number = 0;



  // Package Config

  packageSearchTerm: string = '';

  packageSelectedServices: any[] = [];



  isOwner = false;
  hasGlobalAccess = false;

  permissions: any = {};



  ngOnInit(): void {

    this.route.queryParams.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(params => {

      if (params['appointment_id']) {

        this.loadAppointmentIntoBilling(params['appointment_id']);

      }

    });

    const userStr = localStorage.getItem('user');

    if (userStr) {

      try {

        const user = JSON.parse(userStr);

        this.isOwner = user.is_superuser === true;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
        this.permissions = user.permissions || {};

      } catch (e) { }

    }

    this.loadCenters();

  }



  loadAppointmentIntoBilling(appointmentId: string | number) {

    this.apiService.get('appointments/api/appointments/' + appointmentId + '/').pipe(takeUntilDestroyed(this.destroyRef)).subscribe((appt: any) => {

      if (!appt) return;
      this.selectedAppointmentId = appointmentId;



      if (appt.center && this.selectedCenterId !== appt.center) {

        this.selectedCenterId = appt.center;

        this.loadMasters();

      }



      this.apiService.getClients(appt.client_phone).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({

        next: (clients: any[]) => {

          this.openNewInvoice();

          const existingClient = (clients || []).find(c => c.phone === appt.client_phone);

          if (existingClient) {

            this.selectClient(existingClient);

          } else {

            this.client = {

              name: appt.client_name,

              phone: appt.client_phone,

            };

            this.searchPhone = appt.client_phone;

          }



          if (appt.services && appt.services.length > 0) {

            let attempts = 0;

            const populateCart = () => {

              if (this.services.length === 0 && attempts < 10) {

                attempts++;

                setTimeout(populateCart, 300);

                return;

              }

              appt.services.forEach((s: any) => {
                // By the catalogue link; the name only for old free-text lines.
                const matchedService = (s.service ? this.services.find(ms => ms.id === s.service) : null)
                  || this.services.find(ms => ms.name === s.service_name);
                const objId = matchedService ? matchedService.id : (s.service || null);
                const taxPercentage = matchedService ? (matchedService.tax_percentage || 0) : 0;

                this.cart.push({
                  content_type: 'services.servicemaster',
                  object_id: objId,
                  description: s.service_name,
                  unit_price: Number(s.price),
                  discount: 0,
                  quantity: 1,
                  tax_percentage: taxPercentage,
                  staff_members: s.staff ? [Number(s.staff)] : []
                });
              });
              this.ensureRecipeDefaults();

              this.cdr.detectChanges();

            };

            populateCart();

          } else {

            this.cdr.detectChanges();

          }

        },

        error: (err: any) => {

          this.client = {

            name: appt.client_name,

            phone: appt.client_phone,

          };

          this.searchPhone = appt.client_phone;

          this.openNewInvoice();

          this.cdr.detectChanges();

        }

      });

    }, (err: any) => {

      console.error("Failed to load appointment", err);

    });

  }



  loadCenters() {

    this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {

      this.centers = Array.isArray(data) ? data : (data.results || []);

      if (this.centers.length && !this.selectedCenterId) {

        this.selectedCenterId = this.locationMemory.pick(this.centers, this.hasGlobalAccess, false) ?? this.centers[0].id;

      }

      this.loadLandingData();

      this.loadMasters();

      this.cdr.detectChanges();

    }, (err: any) => {

      console.error('Failed to load centers', err);

    });

  }



  /**
   * Another branch was picked. A bill in progress belongs to the branch it was started at (its staff, prices and stock
   * are that branch's), and the server refuses it anywhere else - the till used to carry on and ask for the whole
   * amount in cash. So the person is asked: clear the bill and work at the new branch, or stay where they were.
   */
  async onCenterChange() {
    const was = this.cartCenterId;
    if (this.cart.length > 0 && was && this.selectedCenterId !== was) {
      const name = (id: number | null) => {
        const c = this.centers.find((x: any) => x.id === id);
        return c?.display_name || c?.center_name || 'the other branch';
      };
      const proceed = await this.confirmDialog.ask(
        `This bill was started at ${name(was)}. Its items, staff and prices belong to that branch, so it has to be `
        + `cleared to bill at ${name(this.selectedCenterId)}. Clear it?`);
      if (!proceed) {
        this.selectedCenterId = was;
        this.cdr.detectChanges();
        return;
      }
      this.clearBillForNewCenter();
    }

    this.loadLandingData();

    this.loadMasters();

  }

  private clearBillForNewCenter() {
    this.resetConfig();
    this.cart = [];
    this.cartCenterId = null;
    this.invoiceDiscount = 0;
    this.invoiceCGST = 0;
    this.invoiceSGST = 0;
    this.selectedValueCardId = null;
    this.selectedPromotion = null;
    this.selectedAppointmentId = null;
    this.approvalIds = [];
    this.useAdvancePayment = false;
    this.currentInvoiceId = null;
    this.currentInvoiceStatus = 'draft';
    this.serverQuote = null;
    this.pinToken = null;                       // a pass is for one client at one branch
    this.clearDraft();
    this.showToast('Bill cleared. Add the items again at this branch.', 'info');
    this.cdr.detectChanges();
  }



  setViewMode(mode: 'landing' | 'new-invoice') {
    this.viewMode = mode;
    if (mode === 'landing') {
      this.loadLandingData();
    }
  }

  loadLandingData() {
    const now = new Date();
    const today = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');

    // 1. Fetch Global Invoices (Only Open/Draft Invoices)
    this.apiService.getInvoices(undefined, this.selectedCenterId || undefined, 'draft').pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
      this.globalInvoices = (d || []).filter(inv => inv.status === 'draft');
      this.cdr.detectChanges();
    });

    // Cards for modules this role does not hold stay empty instead of asking
    // the server for data it will refuse.
    const p = this.permissions || {};
    const canSeeAppointments = this.isOwner
      || Object.values(p.appointments || p.appointment || {}).some((s: any) => s?.read === true);
    const canSeeStaffActivity = this.isOwner
      || p.billing?.new_invoice?.read === true || p.staff?.reports?.read === true;

  // 2. Fetch Appointments for today
    if (canSeeAppointments) this.apiService.getAppointments(this.selectedCenterId || undefined, today).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
      this.appointments = d || [];
      this.cdr.detectChanges();
    });

    // 3. Fetch Staff Activity for today (using StaffRevenueReport)
    if (canSeeStaffActivity) this.apiService.getStaffRevenueReport(this.selectedCenterId || undefined, today, today).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any) => {

      this.staffActivity = (d?.breakdown || []).map((b: any) => ({
         staff_name: b.staff_name,
         amount: b.revenue,
         count: b.services
      }));
      this.activityFeed = d?.activity_feed || [];

      this.cdr.detectChanges();

    });

  }

  async deleteDraftInvoice(inv: any) {
    if (!await this.confirmDialog.ask("Are you sure you want to permanently delete this draft invoice?")) return;
    this.apiService.deleteInvoice(inv.id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.showToast('Draft invoice deleted successfully.', 'success');
        this.globalInvoices = this.globalInvoices.filter(x => x.id !== inv.id);
        if (this.currentInvoiceId === inv.id) {
           this.discardInvoice();
        }
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.showToast('Failed to delete draft invoice.', 'error');
        console.error(err);
      }
    });
  }



  loadMasters() {

    const cid = this.selectedCenterId ?? undefined;

    // A service switched off at this branch (Services > branch override) is not sold here.
    this.apiService.getServices(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => this.services = (d || []).filter(s => s.center_override?.is_active !== false));

    this.apiService.getProducts(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
      this.products = (d || []).filter(p => p.is_active !== false && p.category && p.category.toLowerCase().includes('retail'));
    });

    this.apiService.getMemberships(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
      this.memberships = (d || []).map(m => ({ ...m, price: m.pre_tax_price || m.post_tax_price || 0 }));
    });

    this.apiService.getPackages(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => this.packages = d || []);

    this.apiService.getValueCards(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {
      this.cards = (d || []).map(c => ({ ...c, price: c.pre_tax_price || c.post_tax_price || 0 }));
    });

    this.apiService.getStaffMembers(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {

      this.staffMembers = (d || []).filter((s: any) => s.is_active !== false);

      this.cdr.detectChanges();

    });

    this.loadRecipes();

    // Only the offers usable at this branch today (B5.6).
    const promoUrl = cid ? `marketing/api/promotions/?usable_at=${cid}` : 'marketing/api/promotions/';
    this.apiService.get(promoUrl).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => this.promotions = d || []);



  }



  loadClients() {

    this.apiService.getClients('').pipe(takeUntilDestroyed(this.destroyRef)).subscribe((d: any[]) => {

      this.clients = d || [];

      this.cdr.detectChanges();

    }, (err: any) => {

      console.error('Failed to load clients', err);

    });

  }



  // --- View Mode Toggles ---

  openNewInvoice() {
    this.cart = [];
    this.client = null;
    this.selectedValueCardId = null;
    this.serverQuote = null;
    this.resetConfig();
    this.searchPhone = '';
    this.clientHistory = [];
    this.invoiceCGST = 0;
    this.invoiceSGST = 0;
    this.invoiceDiscount = 0;
    this.clearDraft();
    this.setViewMode('new-invoice');
  }



  discardInvoice() {
    if (this.currentInvoiceId && this.currentInvoiceStatus === 'draft') {
      this.apiService.delete(`billing/invoices/${this.currentInvoiceId}/`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.finalizeDiscard();
        },
        error: (err) => {
          console.error('Failed to delete draft', err);
          this.finalizeDiscard();
        }
      });
    } else {
      this.finalizeDiscard();
    }
  }

  finalizeDiscard() {
    this.isSaving = false;
    this.setViewMode('landing');
    this.cart = [];
    this.client = null;
    this.searchPhone = '';
    this.currentInvoiceId = null;
    this.currentInvoiceStatus = 'draft';
    this.invoiceDiscount = 0;
    this.selectedValueCardId = null;
    this.serverQuote = null;
    this.clearDraft();

    this.invoiceCGST = 0;

    this.invoiceSGST = 0;

    this.useAdvancePayment = false;

    this.loadLandingData();
    this.cdr.detectChanges();
  }



  // --- Client Select ---

  selectClient(client: any) {
    if (client.is_blacklisted) {
      this.showToast('This client is blacklisted and cannot be billed.', 'error');
      return;
    }
    this.client = client;
    this.searchPhone = client.phone;
    this.clients = [];
    this.selectedValueCardId = null;
    this.pinToken = null;                       // a pass is for one client

    if (client.is_blacklisted) {
      this.showToast(`WARNING: Client is blacklisted! Reason: ${client.blacklist_reason || 'No reason provided.'}`, 'error');
    }

    this.loadClientHistory(client.id);

    this.loadClientAdvances(client.id);



    // Inject Active Memberships into Promotions
    // First, remove any previously injected memberships
    this.promotions = this.promotions.filter((p: any) => !p.id || !p.id.toString().startsWith('m_'));
    if (this.selectedPromotion && this.selectedPromotion.id && this.selectedPromotion.id.toString().startsWith('m_')) {
      this.selectedPromotion = null;
      this.applyPromotion();
    }

    if (this.client && this.client.active_memberships) {
      const todayStr = todayLocal();
      // A membership is used only at the branch that sold it (#36).
      this.client.active_memberships = this.client.active_memberships.filter((am: any) =>
        (!am.expiry_date || am.expiry_date >= todayStr) && this.soldHere(am));
    }

    if (this.client.active_memberships && this.client.active_memberships.length > 0) {
        this.client.active_memberships.forEach((am: any) => {
          if (am.membership_detail) {
          const exists = this.promotions.find((p: any) => p.id === 'm_' + am.id);
          if (!exists) {
            this.promotions.push({
              id: 'm_' + am.id,
              name: '👑 ' + am.membership_detail.name,
              discount_percent: am.membership_detail.discount_percent,
              discount_type: 'Percentage'
            });
          }
        }
      });

      const firstAm = this.client.active_memberships[0];
      const promoId = 'm_' + firstAm.id;
      const promo = this.promotions.find((p: any) => p.id === promoId);
      if (promo) {
        this.selectPromotionWithFeedback(promo, true);
      }
    }



    // Inject Active Packages into Services (Redeem for Rs. 0)
    this.services = this.services.filter(s => !(s.name && s.name.startsWith('🎁 [Redeem]')));
    if (this.client.active_packages && this.client.active_packages.length > 0) {
      this.client.active_packages.forEach((ap: any) => {
        if (ap.package_detail && ap.services_remaining) {
          Object.keys(ap.services_remaining).forEach(svcId => {
            const remaining = ap.services_remaining[svcId];
            if (remaining > 0) {
              const originalSvc = this.services.find(s => s.id === Number(svcId) && s.name && !s.name.includes('🎁'));
              if (originalSvc) {
                const redeemSvc = { ...originalSvc };
                redeemSvc.name = `🎁 [Redeem] ${originalSvc.name} (${remaining} left)`;
                redeemSvc.price = 0;
                redeemSvc.default_price = 0;
                redeemSvc.center_override = null;
                this.services.unshift(redeemSvc);
              }
            }
          });
        }
      });
    }
    this.cdr.detectChanges();
    this.saveDraft();
  }



  /** True when a perk sold at another branch must not be offered here (#32, #36). */
  private soldHere(perk: any): boolean {
    return !perk?.sold_at || !this.selectedCenterId || Number(perk.sold_at) === Number(this.selectedCenterId);
  }

  /** The client's value cards that can pay at this branch today. */
  get usableValueCards(): any[] {
    const today = todayLocal();
    return (this.client?.active_value_cards || []).filter((vc: any) =>
      Number(vc.balance) > 0 && (!vc.expiry_date || vc.expiry_date >= today) && this.soldHere(vc));
  }

  get selectedValueCard(): any {
    return this.usableValueCards.find((vc: any) => vc.id === this.selectedValueCardId) || null;
  }

  /**
   * Let a value card pay for the services on this bill (F5).
   *
   * The card's face value buys services at its pre-tax/face ratio, with the
   * card's GST; the server works the split out and the quote shows it. The
   * old button turned the card's benefit into a bill discount and debited
   * the card a scaled amount, which the cancel then gave back unscaled.
   * A card bill takes no other offer (D4, D4b).
   */
  toggleValueCard(vc: any) {
    if (this.selectedValueCardId === vc?.id) {
      this.selectedValueCardId = null;
      this.showToast('Value card removed from the bill.', 'info');
    } else {
      this.selectedPromotion = null;
      this.invoiceDiscount = 0;
      this.selectedValueCardId = vc.id;
      this.showToast(`${vc.value_card_detail?.title || 'Value card'} will pay for the services.`, 'success');
      this.askPinEarly(() => {
        if (this.selectedValueCardId === vc.id) this.selectedValueCardId = null;
        this.calcTaxes();
      });
    }
    this.calcTaxes();
  }

  /** The bill as the server prices it: lines, offer and value card. */
  private buildBillPayload(onHold: boolean): any {
    const promoId = (this.selectedPromotion && !String(this.selectedPromotion.id).startsWith('m_'))
      ? this.selectedPromotion.id : null;
    const membershipId = (this.selectedPromotion && String(this.selectedPromotion.id).startsWith('m_'))
      ? parseInt(String(this.selectedPromotion.id).replace('m_', ''), 10) : null;
    const offerOrCard = !!(promoId || membershipId || this.selectedValueCardId);
    return {
      client: this.client?.is_anonymous ? null : this.client?.id,
      center: this.selectedCenterId,
      notes: this.selectedPromotion ? `Promo Applied: ${this.selectedPromotion.name}` : '',
      // The server works an offer's discount out itself; only a bill
      // discount with no offer is sent as a figure.
      discount: offerOrCard ? '0.00' : Number(this.invoiceDiscount || 0).toFixed(2),
      // paid_amount and the status are derived server-side from the payment
      // rows; `status` here only says whether to hold or complete the bill.
      status: onHold ? 'draft' : 'paid',
      promo_id: promoId,
      membership_id: membershipId,
      value_card_id: this.selectedValueCardId,
      approval_ids: this.approvalIds,
      appointment: this.selectedAppointmentId,
      items: this.cart.map((c: any) => {
        return {
          content_type: c.content_type,
          object_id: c.object_id,
          description: c.description,
          // toAmount, not Number: `Number(undefined)` is NaN, and
          // `NaN.toFixed(2)` is the string "NaN", which the server then had to
          // coerce back to zero.
          unit_price: toAmount(c.unit_price).toFixed(2),
          discount: toAmount(c.discount).toFixed(2),
          manager_discount: toAmount(c.manager_discount).toFixed(2),
          quantity: c.quantity,
          tax_percentage: toAmount(c.tax_percentage).toFixed(2),
          // State the redemption explicitly. The server falls back to looking
          // for a "🎁 [Redeem]" marker in the description, which meant that if
          // anyone edited the line description the package silently stopped
          // being depleted - the customer got the service for free and kept
          // their entitlement.
          is_redemption: isRedemptionLine(c),
          staff: c.staff,
          staff_members: c.staff_members,
          // The contribution % per staff member (#49). It was collected on
          // screen but never sent, so commission was always split evenly.
          staff_allocations: c.staff_allocations || {},
          custom_package_services: c.custom_package_services,
          // What was chosen from the service's recipe (colour, shampoo, extras).
          recipe_choices: this.recipeOf(c) ? c.recipe_choices : undefined
        };
      })
    };
  }

  /** Ask the server for the priced bill, a moment after the cart settles. */
  refreshQuote() {
    this.quoteStale = true;
    if (this.quoteTimer) clearTimeout(this.quoteTimer);
    if (!this.cart || this.cart.length === 0 || !this.selectedCenterId) {
      this.serverQuote = null;
      if (!this.cart || this.cart.length === 0) this.cartCenterId = null;
      return;
    }
    if (!this.cartCenterId) this.cartCenterId = this.selectedCenterId;
    this.quoteTimer = setTimeout(() => {
      const seq = ++this.quoteSeq;
      this.apiService.post('billing/invoices/quote/', this.buildBillPayload(true),
        { headers: { 'X-Background-Request': '1' } })
        .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
          next: (q: any) => {
            if (seq !== this.quoteSeq) return;     // a newer cart is on its way
            this.serverQuote = q;
            this.quoteStale = false;
            this.lastQuoteError = '';
            if (Array.isArray(q?.payment_methods) && q.payment_methods.length) {
              this.branchPaymentMethods = q.payment_methods;
            }
            if (this.selectedPromotion || this.selectedValueCardId) {
              this.invoiceDiscount = Number(q.discount) || 0;
            }
            this.invoiceCGST = Number(q.cgst) || 0;
            this.invoiceSGST = Number(q.sgst) || 0;
            if (this.showCheckoutModal) {
              const rows = this.checkoutPayments;
              if (rows.length === 1 && !rows[0].touched) rows[0].amount = this.payableNow;
              this.calcCheckoutRemaining();
            }
            this.cdr.detectChanges();
          },
          error: (err: any) => {
            if (seq !== this.quoteSeq) return;
            this.serverQuote = null;
            const data = err?.error || {};
            // An offer or card the server will not take is dropped, with its reason.
            if (data.promo_id || data.membership_id) {
              this.selectedPromotion = null;
              this.invoiceDiscount = 0;
              this.showToast(this.extractApiError(err, 'This offer cannot be used.'), 'error');
              this.calcTaxes();
            } else if (data.value_card_id) {
              this.selectedValueCardId = null;
              this.showToast(this.extractApiError(err, 'This value card cannot be used.'), 'error');
              this.calcTaxes();
            } else if (err?.status === 400 && this.cart.length > 0) {
              // Any other refusal (a staff member of another branch, a product with no stock) used to be swallowed:
              // the till then showed the whole bill in cash and the card or offer silently did nothing.
              const why = this.extractApiError(err, '');
              if (why && why !== this.lastQuoteError) {
                this.lastQuoteError = why;
                this.showToast(`The bill cannot be priced: ${why}`, 'error');
              }
            }
            this.cdr.detectChanges();
          }
        });
    }, 250);
  }

  /** The server's figures once they match the cart on screen. */
  get liveQuote(): any {
    return this.quoteStale ? null : this.serverQuote;
  }

  /** What the value card settles on this bill, when one is used. */
  get cardSettled(): number {
    return this.liveQuote?.value_card ? Number(this.liveQuote.value_card.settled) : 0;
  }

  /** What is paid at the till now: the bill less the value card's part. */
  get payableNow(): number {
    return this.liveQuote ? Number(this.liveQuote.payable) : this.finalTotalAmount;
  }

  loadClientHistory(clientId: number) {
    this.apiService.get(`billing/invoices/?client_id=${clientId}`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.clientInvoices = data;
      this.buildClientHistory();
      this.cdr.detectChanges();
    });
  }

  loadClientAdvances(clientId: number) {
    this.apiService.get(`billing/advances/?client_id=${clientId}`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((data: any) => {
      this.clientAdvances = data;
      this.clientAdvanceBalance = this.client?.advance_balance || 0;
      this.clientCashbackBalance = this.client?.cashback_balance || 0;
      this.buildClientHistory();
      this.cdr.detectChanges();
    });
  }



  buildClientHistory() {

    const history = [];

    for (const inv of this.clientInvoices) {

      history.push({

        type: 'Invoice',

        center: inv.client?.center_detail?.display_name || 'All Centers',

        date: new Date(inv.created_at),

        ref: inv.id,

        amount: inv.total_amount,

        status: inv.status

      });

    }

    for (const adv of this.clientAdvances) {

      history.push({

        type: 'Advance',

        center: adv.client?.center_detail?.display_name || 'All Centers',

        date: new Date(adv.created_at),

        ref: adv.id,

        amount: adv.amount

      });

    }

    history.sort((a, b) => b.date.getTime() - a.date.getTime());

    this.clientHistory = history;

    // Auto-detect membership based on true active status
    if (this.client && this.client.active_memberships && this.client.active_memberships.length > 0) {
      const am = this.client.active_memberships[0];
      const promoId = 'm_' + am.id;
      const promo = this.promotions.find((p: any) => p.id === promoId);
      if (promo && (!this.selectedPromotion || this.selectedPromotion.id !== promoId)) {
        this.selectPromotionWithFeedback(promo, true);
      }
    }



  }



  setAnonymousClient() {
    this.client = {
      id: null,
      first_name: 'Walk-in',
      last_name: 'Client',
      phone: '',
      email: '',
      gender: 'other',
      is_anonymous: true
    };
    this.searchPhone = '';

    // Switch to search tab if currently on a restricted tab
    if (['cards', 'packages', 'memberships', 'advance'].includes(this.activeTab)) {
      this.setActiveTab('search');
    }
  }

  newClient() {

    let defaultCenter = this.selectedCenterId;

    if (!defaultCenter && this.centers && this.centers.length > 0) {

      defaultCenter = this.centers[0].id;

    }

    this.client = {

      phone: this.searchPhone || '',

      app_pin: '',

      first_name: '',

      last_name: '',

      email: '',

      birthday: '',

      gst_number: '',
      address: '',
      pin_code: '',

      notes: '',

      dnd_status: 'NOT ON DND',

      center: defaultCenter,

      gender: 'female'
    };

    // Reset client-specific data
    this.promotions = this.promotions.filter((p: any) => !p.id || !p.id.toString().startsWith('m_'));
    if (this.selectedPromotion && this.selectedPromotion.id && this.selectedPromotion.id.toString().startsWith('m_')) {
      this.selectedPromotion = null;
      this.applyPromotion();
    }
    this.clientAdvanceBalance = 0;
    this.clientCashbackBalance = 0;

  }



  saveClient() {
    if (!this.client || this.isSaving) {
      return;
    }

    if (!this.client.first_name || !this.client.phone) {
      this.showToast('First Name and Phone Number are required to save a client.', 'error');
      return;
    }

    this.isSaving = true;
    if (this.client.id) {
      this.apiService.updateClient(this.client.id, this.client).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.isSaving = false;
          this.showToast('Client updated', 'success');
          this.cdr.detectChanges();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.showToast('Failed to update client.', 'error');
          console.error(err);
          this.cdr.detectChanges();
        }
      });
    } else {
      this.apiService.createClient(this.client).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.isSaving = false;
          this.client = res;
          this.showToast('Client created', 'success');
          this.cdr.detectChanges();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.showToast('Failed to create client. Please check details.', 'error');
          console.error(err);
          this.cdr.detectChanges();
        }
      });
    }
  }



  // --- UI Tabs ---

  customPackageObj = { name: 'Custom Package', isCustom: true };



  closeConfig() {
    this.selectedItemForConfig = null;
    this.configType = null;
    this.cdr.detectChanges();
  }

  setActiveTab(tab: 'search' | 'services' | 'products' | 'cards' | 'packages' | 'memberships' | 'advance') {
    this.activeTab = tab;

    if (tab === 'advance') {
      this.configType = 'advance';
      this.selectedItemForConfig = { isAdvance: true };
      this.advanceAmount = 0;
      this.advanceGstPercent = 0;
      this.advanceGstAmount = 0;
      this.advanceDescription = '';
      this.onConfigSelectionChange();
    } else {
      this.selectedItemForConfig = null;
      this.configType = null;
    }

    this.cdr.detectChanges();
  }




  onConfigSelectionChange() {
    if (this.selectedItemForConfig) {
      this.finalConfigPrice = this.getEffectivePrice(this.selectedItemForConfig);
    } else {
      this.finalConfigPrice = 0;
    }
  }

  onPackageSelectionChange() {


    this.packageSelectedServices = [];

    this.finalConfigPrice = 0;

    this.packageSearchTerm = '';



    if (this.selectedItemForConfig && this.selectedItemForConfig.id) {

      this.finalConfigPrice = this.selectedItemForConfig.price || 0;

      if (this.selectedItemForConfig.services_json && Array.isArray(this.selectedItemForConfig.services_json)) {
        this.packageSelectedServices = this.selectedItemForConfig.services_json.map((s: any) => {
          const svcId = s.service_id || s.id;
          let svcPrice = s.price || 0;
          if (!svcPrice && svcId) {
            const matchSvc = this.services.find(svc => svc.id === Number(svcId));
            if (matchSvc) svcPrice = matchSvc.price || matchSvc.default_price || 0;
          }
          return {
            id: svcId,
            name: s.service_name || s.name,
            price: svcPrice,
            pkgQty: s.quantity || s.pkgQty || 1
          };
        });
      }

    }

  }



  selectedServiceCategory: string = 'ALL';
  selectedProductCategory: string = 'ALL';

  get serviceCategories(): { name: string, count: number }[] {
    const counts: { [cat: string]: number } = {};
    let total = 0;
    (this.services || []).forEach(s => {
      const cat = (s.category || 'General').trim();
      counts[cat] = (counts[cat] || 0) + 1;
      total++;
    });
    const list = Object.keys(counts).sort().map(cat => ({ name: cat, count: counts[cat] }));
    return [{ name: 'ALL', count: total }, ...list];
  }

  get productCategories(): { name: string, count: number }[] {
    const counts: { [cat: string]: number } = {};
    let total = 0;
    (this.products || []).forEach(p => {
      const cat = (p.category || 'General').trim();
      counts[cat] = (counts[cat] || 0) + 1;
      total++;
    });
    const list = Object.keys(counts).sort().map(cat => ({ name: cat, count: counts[cat] }));
    return [{ name: 'ALL', count: total }, ...list];
  }

  selectServiceCategory(cat: string) {
    this.selectedServiceCategory = cat;
  }

  selectProductCategory(cat: string) {
    this.selectedProductCategory = cat;
  }

  get groupedServices(): { category: string, items: any[] }[] {
    const search = (this.serviceSearchTerm || '').toLowerCase().trim();
    let list = this.services || [];
    
    if (this.selectedServiceCategory && this.selectedServiceCategory !== 'ALL') {
      list = list.filter(s => (s.category || 'General').trim().toLowerCase() === this.selectedServiceCategory.toLowerCase());
    }
    if (search) {
      list = list.filter(s => s.name?.toLowerCase().includes(search) || (s.category || '').toLowerCase().includes(search));
    }

    const groups: { [cat: string]: any[] } = {};
    list.forEach(s => {
      const cat = (s.category || 'General').trim();
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(s);
    });

    return Object.keys(groups).sort().map(cat => ({
      category: cat,
      items: groups[cat]
    }));
  }

  get groupedProducts(): { category: string, items: any[] }[] {
    const search = (this.productSearchTerm || '').toLowerCase().trim();
    let list = this.products || [];
    
    if (this.selectedProductCategory && this.selectedProductCategory !== 'ALL') {
      list = list.filter(p => (p.category || 'General').trim().toLowerCase() === this.selectedProductCategory.toLowerCase());
    }
    if (search) {
      list = list.filter(p => p.name?.toLowerCase().includes(search) || (p.category || '').toLowerCase().includes(search));
    }

    const groups: { [cat: string]: any[] } = {};
    list.forEach(p => {
      const cat = (p.category || 'General').trim();
      if (!groups[cat]) groups[cat] = [];
      groups[cat].push(p);
    });

    return Object.keys(groups).sort().map(cat => ({
      category: cat,
      items: groups[cat]
    }));
  }

  getCategoryIcon(cat: string): string {
    const c = (cat || '').toLowerCase();
    if (c.includes('hair') || c.includes('cut')) return 'fa-scissors';
    if (c.includes('skin') || c.includes('facial') || c.includes('spa')) return 'fa-spa';
    if (c.includes('nail') || c.includes('mani') || c.includes('pedi')) return 'fa-hand-sparkles';
    if (c.includes('makeup') || c.includes('beauty')) return 'fa-magic';
    if (c.includes('massage') || c.includes('body')) return 'fa-heart';
    if (c.includes('color') || c.includes('dye')) return 'fa-paint-brush';
    if (c.includes('wash') || c.includes('shampoo')) return 'fa-tint';
    return 'fa-tags';
  }

  get filteredServices() {
    const search = (this.serviceSearchTerm || '').toLowerCase().trim();
    if (!search) {
      if (this.activeTab === 'services') return this.services;
      return [];
    }
    return this.services.filter((item: any) => item.name?.toLowerCase().includes(search));
  }

  get filteredProducts() {
    const search = (this.productSearchTerm || '').toLowerCase().trim();
    if (!search) {
      if (this.activeTab === 'products') return this.products;
      return [];
    }
    return this.products.filter((item: any) => item.name?.toLowerCase().includes(search));
  }



  get filteredMemberships() {

    return this.memberships.filter(m => m.is_active !== false);

  }



  get filteredPackages() {

    return this.packages.filter(p => p.is_active !== false);

  }



  get filteredCards() {

    return this.cards.filter(c => c.is_active !== false);

  }



  get filteredPromotions() {

    return this.promotions.filter(p => p.is_active !== false);

  }





  getEffectivePrice(item: any): number {
    if (!item) return 0;

    const parseNum = (val: any) => {
      if (val === undefined || val === null || val === '') return undefined;
      if (typeof val === 'string') {
        const parsed = Number(val.replace(/,/g, ''));
        return isNaN(parsed) ? undefined : parsed;
      }
      const parsed = Number(val);
      return isNaN(parsed) ? undefined : parsed;
    };

    const overridePrice = parseNum(item?.center_override?.price);
    if (overridePrice !== undefined) return overridePrice;

    // Pre-tax BEFORE post-tax.
    //
    // Memberships and value cards have no `price` column, so this used to land
    // on `post_tax_price` - the tax-INCLUSIVE figure - while the server prices
    // the same line from `pre_tax_price` and then adds GST. The two only agreed
    // when there was no discount: with Rs 100 off a 1000/1050 membership the
    // till showed Rs 950 and the server stored Rs 945, so Rs 950 was collected
    // against a Rs 945 tax invoice. A 100% discount was rejected outright,
    // because the till sent a discount of 1050 against a gross of 1000.
    const candidates = [
      item?.mrp,
      item?.price,
      item?.pre_tax_price,
      item?.post_tax_price,
      item?.default_price,
      item?.selling_price,
      item?.price_amount,
      item?.value
    ];

    for (let c of candidates) {
      const num = parseNum(c);
      if (num !== undefined && num !== null) return num;
    }

    return 0;
  }

  getItemTaxAmount(item: any): number {
    const taxPct = Number(item.tax_percentage) || 0;
    if (taxPct <= 0) return 0;

    // total_price includes discount: ((unit_price * qty) - discount)
    const qty = Number(item.quantity) || 1;
    const unitPrice = Number(item.unit_price) || 0;
    const discount = Number(item.discount) || 0;

    let total = (unitPrice * qty) - discount;
    if (total < 0) total = 0;

    // Exclusive tax: calculate tax on top of the base total
    return total * (taxPct / 100);
  }







  isStaffSelected(id: any): boolean {
    return this.configStaffIds.includes(Number(id));
  }

  toggleStaffSelection(id: any) {
    const numId = Number(id);
    if (this.configStaffIds.includes(numId)) {
      this.configStaffIds = this.configStaffIds.filter(x => x !== numId);
      delete this.configStaffAllocations[numId];
    } else {
      this.configStaffIds = [...this.configStaffIds, numId];
    }
    
    // Auto-distribute percentage equally by default
    if (this.configStaffIds.length > 0) {
      const splitPercent = 100 / this.configStaffIds.length;
      this.configStaffIds.forEach(sid => {
        this.configStaffAllocations[sid] = splitPercent;
      });
    }

    this.cdr.detectChanges();
  }





  promoAppliedAnim = false;
  discountRowAnim = false;
  promoBadgePop = false;
  private promoAnimTimer: any;

  triggerPromoAnimation() {
    this.promoAppliedAnim = true;
    this.discountRowAnim = true;
    this.promoBadgePop = true;
    if (this.promoAnimTimer) clearTimeout(this.promoAnimTimer);
    this.promoAnimTimer = setTimeout(() => {
      this.promoAppliedAnim = false;
      this.discountRowAnim = false;
      this.promoBadgePop = false;
      this.cdr.detectChanges();
    }, 1200);
  }

  get isMembershipSelected(): boolean {
    return !!(this.selectedPromotion?.id && this.selectedPromotion.id.toString().startsWith('m_'));
  }

  get selectedPromoDisplayName(): string {
    if (!this.selectedPromotion) return '';
    return (this.selectedPromotion.name || '').replace('👑 ', '').trim();
  }

  get selectedPromoBenefitText(): string {
    if (!this.selectedPromotion) return '';
    if (this.isMembershipSelected) {
      return `${this.selectedPromotion.discount_percent}% OFF Services`;
    }
    const p = this.selectedPromotion;
    const cfg = p.config || {};
    if (p.promo_type === 'FlatPrice') {
      return `Flat Rs. ${cfg.flat_price || 0}`;
    }
    const dType = p.discount_type || cfg.discount_type;
    const dVal = p.discount_value || cfg.discount_value;
    if (dType === 'Percentage') {
      return `${dVal}% OFF`;
    } else if (dVal) {
      return `Rs. ${dVal} OFF`;
    }
    if (p.discount_percent) {
      return `${p.discount_percent}% OFF`;
    }
    return 'Offer Applied';
  }

  onPromoChange() {
    this.selectPromotionWithFeedback(this.selectedPromotion, false);
  }

  clearPromotion() {
    this.selectedPromotion = null;
    this.selectPromotionWithFeedback(null, false);
  }

  selectPromotionWithFeedback(promo: any, isAutoSelect: boolean = false) {
    if (promo && this.selectedValueCardId) {
      if (isAutoSelect) {
        return;                       // the chosen value card stays
      }
      this.selectedValueCardId = null;
      this.showToast('A bill paid with a value card takes no other offer; the card was removed.', 'info');
    }
    this.selectedPromotion = promo;
    this.applyPromotion();

    if (promo) {
      this.triggerPromoAnimation();
      const isMembership = promo.id && promo.id.toString().startsWith('m_');
      const cleanName = (promo.name || '').replace('👑 ', '').trim();

      if (isAutoSelect) {
        if (isMembership) {
          this.showToast(`👑 Active Membership "${cleanName}" auto-applied (${promo.discount_percent}% off on services)!`, 'success');
        } else {
          this.showToast(`✨ Promotion "${cleanName}" auto-applied!`, 'success');
        }
      } else {
        if (isMembership) {
          this.showToast(`👑 Membership Applied: "${cleanName}" (${promo.discount_percent}% off on services)`, 'success');
        } else {
          this.showToast(`✨ Promotion Applied: "${cleanName}"`, 'success');
        }
      }
    } else {
      if (!isAutoSelect) {
        this.showToast('Promotion / Membership removed', 'info');
      }
    }
    this.cdr.detectChanges();
  }

  applyPromotion() {
    if (!this.selectedPromotion) {
      this.invoiceDiscount = 0;
      this.calcTaxes();
      return;
    }

    let discount = 0;

    let preTaxSubtotal = 0;
    for (const it of this.cart) {
      // Offers discount services only (#30, D1). The server's quote has the
      // final figure; this is the instant one.
      if (it.content_type !== 'services.servicemaster' || isRedemptionLine(it)) continue;

      const qty = Number(it.quantity) || 1;
      const unitPrice = Number(it.unit_price) || 0;
      preTaxSubtotal += (unitPrice * qty);
    }

    const promo = this.selectedPromotion;
    const config = promo.config || {};

    // Support legacy/root properties for Memberships
    if (promo.id && promo.id.toString().startsWith('m_')) {
      const dPercent = Number(promo.discount_percent) || 0;
      discount = (preTaxSubtotal * dPercent) / 100;
    }
    else if (promo.promo_type === 'Trigger' && config.trigger_services?.length === 2) {
      const svc1 = config.trigger_services[0];
      const svc2 = config.trigger_services[1];
      const triggerDiscountPct = Number(config.trigger_discount) || 0;

      const hasSvc1 = this.cart.some(c => c.description === svc1 || c.name === svc1);
      const hasSvc2 = this.cart.some(c => c.description === svc2 || c.name === svc2);

      // Both services on the bill: the % comes off the services (F16, D1).
      if (hasSvc1 && hasSvc2) {
        discount = (preTaxSubtotal * triggerDiscountPct) / 100;
      }
    }
    else if (promo.promo_type === 'FlatPrice') {
      const flatPrice = Number(config.flat_price) || 0;
      if (config.target === 'Specific Service' && config.specific_service) {
        const targetSvc = this.cart.find(c => c.description === config.specific_service || c.name === config.specific_service);
        if (targetSvc && targetSvc.unit_price > flatPrice) {
           discount = (targetSvc.unit_price - flatPrice) * (Number(targetSvc.quantity) || 1);
        }
      } else {
        // Overall Bill Flat Price (acts as a flat discount amount off the total)
        if (preTaxSubtotal > flatPrice) {
          discount = flatPrice;
        } else {
          discount = preTaxSubtotal; // don't discount more than the bill itself
        }
      }
    }
    else if (promo.promo_type === 'Discount') {
      // The DB uses promo.discount_type and promo.discount_value (if set)
      // but they can also be in config.discount_type / config.discount_value
      const dType = promo.discount_type || config.discount_type;
      const dValue = Number(promo.discount_value || config.discount_value) || 0;

      if (config.target === 'Specific Service' && config.specific_service) {
        const targetSvc = this.cart.find(c => c.description === config.specific_service || c.name === config.specific_service);
        if (targetSvc) {
          const itemTotal = targetSvc.unit_price * (Number(targetSvc.quantity) || 1);
          if (dType === 'Percentage') {
             discount = (itemTotal * dValue) / 100;
          } else {
             discount = dValue;
          }
        }
      } else {
        // Overall Bill Discount
        if (dType === 'Percentage') {
          discount = (preTaxSubtotal * dValue) / 100;
        } else {
          discount = dValue;
        }
      }
    }

    // Cap discount at subtotal to avoid negative totals
    if (discount > preTaxSubtotal) {
      discount = preTaxSubtotal;
    }

    if (this.invoiceDiscount !== discount && discount > 0) {
      this.triggerPromoAnimation();
    }

    this.invoiceDiscount = discount;
    this.calcTaxes();
  }



  openCheckoutModal() {
    if (!this.client || (!this.client.id && !this.client.is_anonymous)) {
      this.showToast('Please select or create a client first.', 'error');
      return;
    }

    if (this.client.is_blacklisted) {
      this.showToast('This client is blacklisted and cannot be billed.', 'error');
      return;
    }
    const missingStaff = this.cart.some((c: any) => !c.staff_members || c.staff_members.length === 0);
    if (missingStaff) {
      this.showToast('Please select at least one staff member for each item before finalizing.', 'error');
      return;
    }
    this.applyPromotion(); // Re-apply to ensure exact math

    if (this.client.is_anonymous) {
      this.clientAdvanceBalance = 0;
      this.clientCashbackBalance = 0;
      this.checkoutPayments = [{ method: 'Cash', amount: this.payableNow }];
      this.showCheckoutModal = true;
      this.calcCheckoutRemaining();
      this.cdr.detectChanges();
      return;
    }

    // Refresh advance balance from server before showing checkout
    this.apiService.get(`clients/api/clients/${this.client.id}/`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (c: any) => {
        if (c && c.advance_balance !== undefined) {
          this.clientAdvanceBalance = c.advance_balance;
          this.clientCashbackBalance = c.cashback_balance;
          this.client.advance_balance = c.advance_balance;
        }
        this.checkoutPayments = [{ method: 'Cash', amount: this.payableNow }];
        this.showCheckoutModal = true;
        this.calcCheckoutRemaining();
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.showToast('Failed to sync client balances.', 'error');
        // still open it, just assume 0
        this.checkoutPayments = [{ method: 'Cash', amount: this.payableNow }];
        this.showCheckoutModal = true;
        this.calcCheckoutRemaining();
        this.cdr.detectChanges();
      }
    });
  }



  addPaymentRow() {
    this.calcCheckoutRemaining();
    const remaining = this.checkoutRemaining > 0 ? this.checkoutRemaining : 0;
    this.checkoutPayments.push({ method: 'UPI', amount: remaining });
    this.calcCheckoutRemaining();
  }

  removePaymentRow(index: number) {

    this.checkoutPayments.splice(index, 1);

    this.calcCheckoutRemaining();

  }

  isPaymentMethodUsed(method: string, currentIndex: number): boolean {
    return this.checkoutPayments.some((p, i) => i !== currentIndex && p.method === method);
  }



  /**
   * An amount was typed on one payment row: the last row nobody has typed in yet takes up the rest of the
   * bill. Cash 10,000 on a 15,750 bill makes the UPI row 5,750 by itself; the row the person types in is
   * never changed under them, and a row they have typed is never touched.
   */
  rebalancePayments() {
    this.capWallets(false);
    rebalancePayments(this.checkoutPayments, this.payableNow);
    this.calcCheckoutRemaining();
  }

  /**
   * A wallet row (advance, cashback) is held to the balance it draws on, so the till never shows a payment the
   * client cannot make. What is cut off goes onto the next free method (cash, UPI...), so the bill stays covered:
   * Cashback on a 1,050 bill with 240.50 in the wallet becomes 240.50 cashback + 809.50 cash.
   */
  private capWallets(say: boolean) {
    const cuts = capWalletRows(this.checkoutPayments, this.clientAdvanceBalance, this.clientCashbackBalance);
    if (!cuts.length) return;
    rebalancePayments(this.checkoutPayments, this.payableNow);
    this.calcCheckoutRemaining();
    let moved = '';
    if (this.checkoutRemaining > 0.004) {
      const used = this.checkoutPayments.map((p: any) => p.method);
      const next = (this.branchPaymentMethods || []).map(m => (m === 'Credit Card' ? 'Card' : m))
        .find(m => !used.includes(m)) || 'Cash';
      this.checkoutPayments.push({ method: next, amount: this.checkoutRemaining });
      moved = next;
      this.calcCheckoutRemaining();
    }
    this.checkoutPayments = [...this.checkoutPayments];
    if (say) {
      const first = cuts[0].row['method'] === 'Advance' ? 'The advance' : 'The cashback wallet';
      const have = Number(cuts[0].row['amount']).toFixed(2);
      this.showToast(`${first} has only ₹${have}.${moved ? ` The rest is put on ${moved}.` : ''}`, 'info');
    }
    this.cdr.detectChanges();
  }

  calcCheckoutRemaining() {
    let sum = 0;
    this.checkoutPayments.forEach(p => sum += Number(p.amount || 0));
    // Rounded to paise: a float remainder of 1e-13 blocked "Complete".
    this.checkoutRemaining = Math.round((this.payableNow - sum) * 100) / 100;
  }

  get canCompleteSale(): boolean {
    if (this.isSaving) return false;
    if (this.client?.is_blacklisted) return false;
    // The bill is paid in full and exactly (#37, #38): extra cash goes in
    // "Cash received", and the change is shown there.
    if (this.checkoutRemaining !== 0) return false;
    if (this.quoteStale) return false;
    for (const p of this.checkoutPayments) {
      if (p.tendered !== undefined && p.tendered !== null && p.tendered !== ''
          && Number(p.tendered) < Number(p.amount || 0)) return false;
    }

    // Validate wallet balances so the button is blocked before hitting the backend
    let advanceUsed = 0;
    let cashbackUsed = 0;
    for (const p of this.checkoutPayments) {
      if (p.method === 'Advance') advanceUsed += Number(p.amount || 0);
      if (p.method === 'Cashback Wallet') cashbackUsed += Number(p.amount || 0);
    }
    if (advanceUsed > 0 && advanceUsed > (this.clientAdvanceBalance || 0)) return false;
    if (cashbackUsed > 0 && cashbackUsed > (this.clientCashbackBalance || 0)) return false;

    // A brand's coupon must have been checked, and cannot pay more than it is worth on this bill.
    if (this.checkoutPayments.some(p => giftCardProblem(p) !== null)) return false;

    return true;
  }



  closeCheckoutModal() {

    this.showCheckoutModal = false;

  }



  // --- Cart & Invoice Logic ---

  openConfig(item: any, type: 'service' | 'product' | 'card' | 'advance' | 'package' | 'membership') {
    if (type === 'product' && item.current_stock !== undefined && item.current_stock <= 0) {
      this.showToast('This product is out of stock and cannot be selected.', 'error');
      return;
    }
    this.selectedItemForConfig = item;
    this.configType = type;
    this.resetConfig();
    if (type === 'package') {
      this.onPackageSelectionChange();
    } else {
      this.onConfigSelectionChange();
    }
    this.cdr.detectChanges();
  }

  async deleteHeldCart(hc: any) {
    if (!await this.confirmDialog.ask("Are you sure you want to delete this held cart?")) return;
    this.heldCarts = this.heldCarts.filter((c: any) => c.id !== hc.id);
    localStorage.setItem('held_carts', JSON.stringify(this.heldCarts));
    if (this.heldCarts.length === 0) {
      this.showHeldCartsModal = false;
    }
    this.cdr.detectChanges();
  }

  resetConfig() {
    this.managerDiscountPercent = 0;
    this.managerDiscountAmount = 0;
    this.configStaffIds = [];
    this.configStaffAllocations = {};
    if (this.selectedItemForConfig) {
      this.customBasePrice = this.getEffectivePrice(this.selectedItemForConfig);
      this.finalConfigPrice = this.customBasePrice;
      if (this.selectedItemForConfig.services_json && Array.isArray(this.selectedItemForConfig.services_json)) {
        this.packageSelectedServices = this.selectedItemForConfig.services_json.map((s: any) => {
          const svcId = s.service_id || s.id;
          let svcPrice = s.price || 0;
          if (!svcPrice && svcId) {
            const matchSvc = this.services.find(svc => svc.id === Number(svcId));
            if (matchSvc) svcPrice = matchSvc.price || matchSvc.default_price || 0;
          }
          return {
            id: svcId,
            name: s.service_name || s.name,
            price: svcPrice,
            pkgQty: s.quantity || s.pkgQty || 1
          };
        });
      } else {
        this.packageSelectedServices = [];
      }
    }
  }

  recalcConfigPrice() {

    if (!this.selectedItemForConfig) return;

    const base = this.customBasePrice !== null ? this.customBasePrice : this.getEffectivePrice(this.selectedItemForConfig);

    const afterPct = base - (base * (this.managerDiscountPercent / 100));
    this.finalConfigPrice = Math.max(0, afterPct - this.managerDiscountAmount);

    // Auto-rebalance percentage allocations if they are using equal splits, or reset them
    if (this.configStaffIds.length > 0) {
      const splitPercent = 100 / this.configStaffIds.length;
      this.configStaffIds.forEach(sid => {
        this.configStaffAllocations[sid] = splitPercent;
      });
    }
  }



  confirmAddToCart() {
    if (this.configType !== 'advance' && this.configStaffIds.length === 0) {
      this.showToast('Please select at least one staff member before adding to cart.', 'error');
      return;
    }

    if (this.configStaffIds.length > 0 && this.configType !== 'advance') {
      let sum = 0;
      for (const sid of this.configStaffIds) {
        sum += Number(this.configStaffAllocations[sid]) || 0;
      }
      // Check if sum equals 100% (allowing small float rounding difference like 33.33*3)
      if (Math.abs(sum - 100) > 0.1) {
        this.showToast(`Staff allocation percentages (currently ${sum.toFixed(1)}%) must equal exactly 100%.`, 'error');
        return;
      }
    }

    if (this.selectedItemForConfig?.name && this.selectedItemForConfig.name.includes('🎁 [Redeem]')) {
      const match = this.selectedItemForConfig.name.match(/\((\d+)\s+left\)/);
      if (match && match[1]) {
        const maxAllowed = parseInt(match[1], 10);
        const existingCount = this.cart.filter(c => c.description === this.selectedItemForConfig.name)
          .reduce((sum, c) => sum + c.quantity, 0);
        if (existingCount + 1 > maxAllowed) {
          this.showToast(`You can only redeem up to ${maxAllowed} of this service.`, 'error');
          return;
        }
      }
    }

    if (this.configType === 'product' && this.selectedItemForConfig?.current_stock !== undefined) {
      const stock = this.selectedItemForConfig.current_stock;
      const existingCount = this.cart.filter(c => c.object_id === this.selectedItemForConfig.id && c.content_type === 'inventory.product')
        .reduce((sum, c) => sum + c.quantity, 0);
      if (existingCount + 1 > stock) {
        this.showToast(`Only ${stock} left in stock.`, 'error');
        return;
      }
    }

    if (this.configType === 'advance') {
      const entry = {
        content_type: 'billing.advancepayment',
        object_id: null,
        description: this.advanceDescription || 'Advance Payment',
        unit_price: this.advanceAmount || 0,
        tax_amount: this.advanceGstAmount || 0,
        tax_percentage: this.advanceAmount ? (this.advanceGstAmount / this.advanceAmount) * 100 : 0,
        discount: 0,
        manager_discount: 0,
        quantity: 1,
        staff: this.configStaffIds.length > 0 ? this.configStaffIds[0] : null,
        staff_members: this.configStaffIds,
        staff_allocations: { ...this.configStaffAllocations }
      };

      this.cart.push(entry);

    } else if (this.selectedItemForConfig) {

      // Map frontend configType to backend content_type

      let ct = 'services.servicemaster';

      if (this.configType === 'product') ct = 'inventory.product';

      else if (this.configType === 'card') ct = 'marketing.valuecard';

      else if (this.configType === 'package') ct = 'marketing.package';

      else if (this.configType === 'membership') ct = 'marketing.membership';



      let basePrice = this.customBasePrice !== null ? this.customBasePrice : this.getEffectivePrice(this.selectedItemForConfig);
      let discount = basePrice - this.finalConfigPrice;

      // Handle custom packages or when package price is increased
      if (this.configType === 'package') {
        if (!this.selectedItemForConfig.id || discount < 0) {
          basePrice = this.finalConfigPrice;
          discount = 0;
        }
      }

      const entry = {
        content_type: ct,
        object_id: this.selectedItemForConfig.id,
        description: this.selectedItemForConfig.name || this.selectedItemForConfig.title || 'Item',
        unit_price: basePrice,
        discount: discount > 0 ? discount : 0,
        manager_discount: discount > 0 ? discount : 0,
        quantity: 1,
        // Every content type carries its own rate. This used to hardcode 0 for
        // anything that was not a service or a product, so memberships, value
        // cards and packages went onto the bill tax-free on screen while the
        // server taxed them - and the till's total disagreed with the invoice.
        tax_percentage: this.lineTaxPercent(ct, this.selectedItemForConfig),

        staff_members: [...this.configStaffIds],
        staff_allocations: { ...this.configStaffAllocations },
        custom_package_services: (this.configType === 'package' && !this.selectedItemForConfig.id) ? [...this.packageSelectedServices] : undefined,
        current_stock: this.configType === 'product' ? this.selectedItemForConfig.current_stock : undefined

      };

      this.cart.push(entry);
      this.ensureRecipeDefaults();
      // A service with a recipe asks what the client chose, straight away.
      const added = this.cart.length - 1;
      if (this.recipeOf(entry)) this.openRecipe(added);

    }

    this.packageSelectedServices = [];



    // Reset state after adding

    this.selectedItemForConfig = null;
    this.configType = null;
    this.activeTab = 'search';
    this.serviceSearchTerm = '';
    this.productSearchTerm = '';

    this.advanceAmount = 0;
    this.advanceGstPercent = 0;
    this.advanceGstAmount = 0;
    this.advanceDescription = '';
    this.applyPromotion();
    this.calcTaxes();

  }



  get packageFilteredServices() {

    const term = this.packageSearchTerm.toLowerCase();

    if (!term) return [];

    return this.services.filter(s => s.name?.toLowerCase().includes(term));

  }



  addServiceToPackage(service: any) {

    this.packageSelectedServices.push({ ...service, pkgQty: 1 });

    this.packageSearchTerm = '';

  }



  removeServiceFromPackage(index: number) {

    this.packageSelectedServices.splice(index, 1);

  }



  getStaffName(staffId: number | string): string {

    const st = this.staffMembers.find((s: any) => s.id == staffId);

    return st ? `${st.first_name} ${st.last_name || ''}`.trim() : 'Unknown Staff';

  }

  // Helper to get today's date for template comparisons
  get today(): Date {
    return new Date();
  }

  // Returns service IDs from a ClientPackage's services_remaining map (for *ngFor)
  getPackageServiceKeys(ap: any): string[] {
    if (!ap || !ap.services_remaining) return [];
    return Object.keys(ap.services_remaining);
  }

  // Look up a service name by its numeric ID
  getServiceName(svcId: number): string {
    const svc = this.services.find((s: any) => s.id === svcId);
    return svc ? svc.name : `Service #${svcId}`;
  }

  // Safely check if an expiry date string is in the past
  isExpired(expiryDate: string | null | undefined): boolean {
    if (!expiryDate) return false;
    return new Date(expiryDate) < new Date();
  }

  addRedeemableServiceToCart(svcId: string, ap: any) {
    const remaining = ap.services_remaining[svcId];
    if (remaining <= 0) return;

    const originalSvc = this.services.find(s => s.id === Number(svcId) && s.name && !s.name.includes('🎁'));
    if (!originalSvc) {
      this.showToast('Service not found in master list.', 'error');
      return;
    }

    // Dynamically create the redeem service config to avoid relying on array injection state
    const redeemSvc = { ...originalSvc };
    redeemSvc.name = `🎁 [Redeem] ${originalSvc.name} (${remaining} left)`;
    redeemSvc.price = 0;
    redeemSvc.default_price = 0;
    redeemSvc.center_override = null;

    this.openConfig(redeemSvc, 'service');
  }

  removeFromCart(i: number) {

    this.cart.splice(i, 1);
    this.applyPromotion();
    this.calcTaxes();

  }

  updateQuantity(index: number, change: number) {
    if (this.currentInvoiceStatus !== 'draft') return;
    const item = this.cart[index];

    if (change > 0 && item.description && item.description.includes('🎁 [Redeem]')) {
      const match = item.description.match(/\((\d+)\s+left\)/);
      if (match && match[1]) {
        const maxAllowed = parseInt(match[1], 10);
        if (item.quantity + change > maxAllowed) {
          this.showToast(`You can only redeem up to ${maxAllowed} of this service.`, 'error');
          return;
        }
      }
    }

    if (change > 0 && item.content_type === 'inventory.product') {
      if (item.current_stock !== undefined) {
        const existingCount = this.cart.filter(c => c.object_id === item.object_id && c.content_type === 'inventory.product')
          .reduce((sum, c) => sum + c.quantity, 0);
        if (existingCount + change > item.current_stock) {
          this.showToast(`Only ${item.current_stock} left in stock.`, 'error');
          return;
        }
      }
    }

    if (item.quantity + change >= 1) {
      item.quantity += change;
      this.clearRecipeQuantities(item);
      this.applyPromotion();
      this.calcTaxes();
    }
  }

  onQuantityDirectChange(index: number) {
    if (this.currentInvoiceStatus !== 'draft') return;
    const item = this.cart[index];
    if (!item) return;

    let parsed = parseInt(item.quantity, 10);
    if (isNaN(parsed) || parsed < 1) {
      parsed = 1;
    }
    if (parsed > 9999) {
      parsed = 9999;
    }
    item.quantity = parsed;

    // Redeem validation
    if (item.description && item.description.includes('🎁 [Redeem]')) {
      const match = item.description.match(/\((\d+)\s+left\)/);
      if (match && match[1]) {
        const maxAllowed = parseInt(match[1], 10);
        if (item.quantity > maxAllowed) {
          item.quantity = maxAllowed;
          this.showToast(`You can only redeem up to ${maxAllowed} of this service.`, 'error');
        }
      }
    }

    // Product stock validation
    if (item.content_type === 'inventory.product' && item.current_stock !== undefined) {
      const otherItemsCount = this.cart
        .filter((c, idx) => idx !== index && c.object_id === item.object_id && c.content_type === 'inventory.product')
        .reduce((sum, c) => sum + Number(c.quantity || 0), 0);
      if (otherItemsCount + item.quantity > item.current_stock) {
        item.quantity = Math.max(1, item.current_stock - otherItemsCount);
        this.showToast(`Only ${item.current_stock} left in stock.`, 'error');
      }
    }

    this.clearRecipeQuantities(item);
    this.applyPromotion();
    this.calcTaxes();
  }

  extractApiError(err: any, defaultMsg: string = 'Operation failed'): string {
    if (!err) return defaultMsg;
    if (typeof err === 'string') return err;
    const errorData = err.error || err;
    if (typeof errorData === 'string') return errorData;
    if (errorData.detail) return errorData.detail;
    if (errorData.non_field_errors && Array.isArray(errorData.non_field_errors) && errorData.non_field_errors.length > 0) {
      return errorData.non_field_errors.join(', ');
    }
    if (Array.isArray(errorData) && errorData.length > 0) {
      return errorData.map((e: any) => typeof e === 'string' ? e : JSON.stringify(e)).join(', ');
    }
    if (typeof errorData === 'object') {
      const messages: string[] = [];
      for (const [key, val] of Object.entries(errorData)) {
        if (Array.isArray(val)) {
          messages.push(`${key}: ${(val as any[]).join(', ')}`);
        } else if (typeof val === 'string') {
          messages.push(`${key}: ${val}`);
        } else if (typeof val === 'object' && val !== null) {
          messages.push(`${key}: ${JSON.stringify(val)}`);
        }
      }
      if (messages.length > 0) return messages.join(' | ');
    }
    return err.message || defaultMsg;
  }

  /**
   * Sum of the line values, matching the server.
   *
   * The server stores each `InvoiceItem.total_price` rounded to paise and sums
   * those. This summed raw floats, and it also counted a package redemption at
   * its catalog price instead of zero.
   */
  get subtotalAmount(): number {
    return this.pricedCart.subtotal;
  }

  /**
   * The single source of the header amounts shown at the till.
   *
   * Everything on screen now comes from `priceCart`, which reproduces
   * `InvoiceSerializer._price_invoice` step for step - including where it
   * rounds. Before this, the till used its own arithmetic and could show a
   * total a rupee away from the invoice the server went on to store.
   */
  /**
   * The GST rate for a cart line, by content type.
   *
   * Services carry `tax_percentage`, products `gst_percent`, and memberships and
   * value cards now carry `gst_percent` too (it used to be inferred from their
   * pre/post price pair). Packages take the organisation-wide
   * PACKAGE_GST_PERCENT decision, which the server applies, so the till reads
   * whatever the API reports for the row and falls back to 0.
   */
  lineTaxPercent(contentType: string, item: any): number {
    if (!item) {
      return 0;
    }
    if (contentType === 'services.servicemaster') {
      return toAmount(item.tax_percentage);
    }
    if (contentType === 'inventory.product') {
      // The MRP includes GST (F22): priceCart takes the GST out of the MRP
      // rather than adding it on top, so the customer pays the MRP.
      return toAmount(item.gst_percent);
    }
    // marketing.membership / marketing.valuecard / marketing.package
    return toAmount(item.gst_percent ?? item.tax_percentage);
  }

  get pricedCart() {
    return priceCart(this.cart || [], this.invoiceDiscount, this.gstEnabled);
  }

  /** `Center.gst_enabled` for the branch being billed. */
  get gstEnabled(): boolean {
    const center = (this.centers || []).find(
      (c: any) => c.id === this.selectedCenterId);
    // Default to charging GST when the branch is not loaded yet: that matches
    // the server default and is the safe direction.
    return center ? center.gst_enabled !== false : true;
  }



  get totalItemDiscount() {

    return this.cart.reduce((s: any, c: any) => s + Number(c.discount || 0), 0) + Number(this.invoiceDiscount || 0)
      + this.cardBenefit;

  }

  /** The value card's benefit on the covered services (face less taxable). */
  get cardBenefit(): number {
    const lines = this.liveQuote?.value_card ? (this.liveQuote.lines || []) : [];
    return lines.reduce((s: number, l: any) => s + Number(l.card_benefit || 0), 0);
  }



  get finalTotalAmount(): number {
    // The server's total once its quote matches the cart; `priceCart`'s
    // identical arithmetic until then. A value card's split is known only to
    // the server.
    if (this.liveQuote) {
      return Number(this.liveQuote.total_amount);
    }
    return this.pricedCart.total;
  }

  @HostListener('window:keydown', ['$event'])
  handleKeyboardEvent(event: KeyboardEvent) {
    if (this.viewMode !== 'new-invoice') return;

    const currentTime = new Date().getTime();

    // Barcode Scanner logic (detect rapid typing ending in Enter)
    if (event.key.length === 1 && !event.ctrlKey && !event.altKey) {
      if (currentTime - this.lastBarcodeKeystrokeTime > 50) {
        this.barcodeBuffer = ''; // reset if typing slowly
      }
      this.barcodeBuffer += event.key;
      this.lastBarcodeKeystrokeTime = currentTime;
    } else if (event.key === 'Enter') {
      if (this.barcodeBuffer.length >= 3 && currentTime - this.lastBarcodeKeystrokeTime <= 50) {
        this.processBarcode(this.barcodeBuffer);
        this.barcodeBuffer = '';
        event.preventDefault();
        return;
      }
      this.barcodeBuffer = '';
    }

    if (event.key === 'F2') {
      event.preventDefault();
      if (this.clientSearchInput) this.clientSearchInput.nativeElement.focus();
    } else if (event.key === 'F4') {
      event.preventDefault();
      if (this.serviceSearchInput) {
        this.serviceSearchInput.nativeElement.focus();
        this.activeTab = 'services';
      }
    } else if (event.key === 'F8' || (event.ctrlKey && event.key === 'Enter')) {
      event.preventDefault();
      if (this.cart.length > 0 && this.client && !this.client.is_anonymous) {
        this.openCheckoutModal();
      }
    } else if (event.key === 'Escape') {
      if (this.showCheckoutModal) {
        this.closeCheckoutModal();
      } else if (this.selectedItemForConfig) {
        this.selectedItemForConfig = null;
        this.configType = null;
        this.activeTab = 'search';
      } else if (this.cart.length > 0) {
        // Escape used to silently wipe the entire bill. On a POS keyboard that
        // is one stray keystroke away from losing a customer's whole order.
        this.confirmClearCart();
      }
    }
  }

  /** Ask before discarding a bill in progress. */
  async confirmClearCart() {
    if (this.cart.length === 0) {
      return;
    }
    const count = this.cart.length;
    const proceed = await this.confirmDialog.ask(
      `Clear this bill? ${count} item${count === 1 ? '' : 's'} will be removed.`,
    );
    if (!proceed) {
      return;
    }
    this.cart = [];
    this.invoiceDiscount = 0;
    this.invoiceCGST = 0;
    this.invoiceSGST = 0;
    this.selectedValueCardId = null;
    this.serverQuote = null;
    this.clearDraft();
    this.cdr.detectChanges();
  }

  processBarcode(code: string) {
    const term = (code || '').trim();
    if (!term) {
      return;
    }

    // Barcodes belong to products. Match on the barcode first, then on the
    // product code, and only then on a numeric id. The old lookup searched
    // only the services list, so a product barcode never matched anything.
    const lower = term.toLowerCase();
    const byCode = (s: any) => {
      const barcode = `${s.barcode ?? ''}`.trim();
      const productCode = `${s.product_code ?? ''}`.trim();
      return (barcode && barcode === term) || (productCode && productCode.toLowerCase() === lower);
    };
    const match =
      this.products.find(byCode) ||
      this.services.find(byCode) ||
      this.products.find((s: any) => s.id != null && `${s.id}` === term) ||
      this.services.find((s: any) => s.id != null && `${s.id}` === term);

    if (!match) {
      this.showToast(`No item found for "${term}".`, 'error');
      return;
    }
    this.sound.play('scan');

    // Decide the type from the item's own shape, not from whether it happens
    // to have a `price` field. `price` is the PRODUCT field, so the old
    // `item.price ? 'service' : 'product'` typed every scanned product as a
    // service and sent the wrong content_type to the backend.
    const type = this.products.includes(match) ? 'product' : this.detectItemType(match);
    this.openConfig(match, type as any);

    // Every line needs its staff, so open the item for the cashier instead of
    // auto-adding: the auto-add always stopped at "select at least one staff".
    if (this.selectedItemForConfig === match) {
      this.showToast(`Scanned ${match.name || term}. Pick the staff, then add it.`, 'info');
    }
  }

  /** Infer the catalog type of an item from its distinguishing fields. */
  private detectItemType(
    item: any,
  ): 'card' | 'membership' | 'package' | 'advance' | 'service' | 'product' {
    if (!item) {
      return 'service';
    }
    if (item.item_type) {
      return item.item_type;
    }
    // Products carry gst_percent / barcode / current_stock; services carry
    // default_price / tax_percentage / duration_mins.
    if (
      item.gst_percent !== undefined ||
      item.barcode !== undefined ||
      item.current_stock !== undefined
    ) {
      return 'product';
    }
    if (item.expiry_days !== undefined && item.value !== undefined) {
      return 'card';
    }
    if (item.validity_days !== undefined || item.services_json !== undefined) {
      return 'package';
    }
    if (item.discount_percent !== undefined && item.expiry_days !== undefined) {
      return 'membership';
    }
    return 'service';
  }

  saveDraft() {
    if (this.currentInvoiceStatus !== 'draft') return;
    if (!this.cart || this.cart.length === 0) {
      this.clearDraft();
      return;
    }
    const draft = {
      cart: this.cart,
      client: this.client,
      selectedPromotion: this.selectedPromotion,
      managerDiscountPercent: this.managerDiscountPercent,
      managerDiscountAmount: this.managerDiscountAmount,
      activeTab: this.activeTab,
      configStaffIds: this.configStaffIds
    };
    localStorage.setItem('billing_draft', JSON.stringify(draft));
  }

  loadDraft() {
    // Silent draft restore without blocking browser confirm popups
    const d = localStorage.getItem('billing_draft');
    if (d) {
      try {
        const draft = JSON.parse(d);
        if (draft.cart && draft.cart.length > 0) {
          this.cart = draft.cart;
          this.client = draft.client;
          this.selectedPromotion = draft.selectedPromotion;
          this.managerDiscountPercent = draft.managerDiscountPercent;
          this.managerDiscountAmount = draft.managerDiscountAmount;
          this.activeTab = draft.activeTab;
          this.configStaffIds = draft.configStaffIds;
          this.applyPromotion();
          this.calcTaxes();
        } else {
          this.clearDraft();
        }
      } catch (e) {
        this.clearDraft();
      }
    }

    // Also load held carts
    const held = localStorage.getItem('held_carts');
    if (held) {
      try {
        this.heldCarts = JSON.parse(held);
      } catch (e) { }
    }
  }

  clearDraft() {
    localStorage.removeItem('billing_draft');
  }

  holdCurrentCart() {
    if (this.cart.length === 0) return;
    this.heldCarts.push({
      id: new Date().getTime(),
      time: new Date(),
      client: this.client,
      cart: [...this.cart],
      total: this.finalTotalAmount
    });
    localStorage.setItem('held_carts', JSON.stringify(this.heldCarts));

    // Reset state without changing viewMode
    this.isSaving = false;
    this.cart = [];
    this.client = null;
    this.searchPhone = '';
    this.currentInvoiceId = null;
    this.currentInvoiceStatus = 'draft';
    this.invoiceDiscount = 0;
    this.invoiceCGST = 0;
    this.invoiceSGST = 0;
    this.useAdvancePayment = false;
    this.selectedValueCardId = null;
    this.serverQuote = null;
    this.clearDraft();
    this.showToast('Cart placed on hold. Ready for next customer.', 'success');
  }

  async resumeCart(heldCart: any) {
    if (this.cart.length > 0) {
      if (!await this.confirmDialog.ask("Discard current cart to resume held cart?")) return;
    }
    this.setViewMode('new-invoice');
    this.currentInvoiceStatus = 'draft';
    this.client = heldCart.client || { name: 'Walk-in Client', contact_number: '' };
    this.selectedAppointmentId = heldCart.appointment || null;
    this.cart = heldCart.cart || [];
    this.heldCarts = this.heldCarts.filter(c => c.id !== heldCart.id);
    localStorage.setItem('held_carts', JSON.stringify(this.heldCarts));
    this.calcTaxes();
    this.showHeldCartsModal = false;
  }

  /**
   * Recompute the GST shown at the till.
   *
   * This was a second, subtly different implementation of the server's pricing:
   * it left the apportioned invoice discount unrounded, accumulated line tax as
   * raw floats instead of rounding each line to paise, and produced CGST and
   * SGST by halving the total and rounding each half on its own - so the two
   * halves did not always add back to the tax charged. It also ignored
   * `Center.gst_enabled` entirely, showing GST for a branch the server taxes at
   * zero.
   *
   * All of it now comes from `priceCart`, which mirrors
   * `InvoiceSerializer._price_invoice`.
   */
  calcTaxes() {
    if (!this.cart || this.cart.length === 0) {
      this.invoiceCGST = 0;
      this.invoiceSGST = 0;
      this.invoiceDiscount = 0;
      this.serverQuote = null;
      this.clearDraft();
      return;
    }

    const priced = this.pricedCart;
    this.invoiceCGST = priced.cgst;
    this.invoiceSGST = priced.sgst;
    // The server caps an invoice-level discount at the subtotal; reflect that
    // on screen rather than showing a discount larger than the bill.
    if (this.invoiceDiscount > priced.subtotal) {
      this.invoiceDiscount = priced.subtotal;
    }
    this.saveDraft();
    this.refreshQuote();
  }

  /**
   * Build the payment rows sent with a bill.
   *
   * Two fixes here:
   *  - The value card id is carried in its own field. It used to be encoded
   *    into the method string ("Value Card 42") and parsed back out; when that
   *    parse produced NaN the id arrived as null, the backend's guard was
   *    false, and the card was never debited - the customer paid nothing.
   *  - A tip is its own row flagged is_tip, so it is collected and reported
   *    without inflating paid_amount above the bill total.
   */
  private buildPaymentRows(): any[] {
    const rows = this.checkoutPayments
      .filter((p: any) => Number(p.amount) > 0)
      .map((p: any) => {
        let method: string = p.method;
        let valueCardId: number | null = null;

        if (method === 'Card') {
          method = 'Credit Card';
        }

        const row: any = {
          payment_method: method,
          amount: Number(p.amount),
          value_card_id: valueCardId,
          is_tip: false,
          // UPI transaction id, card slip / last four, cheque number.
          reference: `${p.reference || ''}`.trim().slice(0, 64),
        };
        // Cash handed over; the change is worked out from it (#38).
        if (method === 'Cash' && p.tendered !== undefined && p.tendered !== null
            && `${p.tendered}` !== '' && Number(p.tendered) >= Number(p.amount)) {
          row.tendered = Number(p.tendered).toFixed(2);
        }
        return row;
      });

    // Tips are no longer taken (owner decision #39); the server refuses them.
    return rows;
  }

  /** True if any value-card payment is missing its card id. */
  get hasUnresolvedValueCardPayment(): boolean {
    return this.checkoutPayments.some((p: any) => {
      if (Number(p.amount) <= 0) return false;
      const method = `${p.method || ''}`;
      if (!method.startsWith('Value Card')) return false;
      if (p.value_card_id != null && `${p.value_card_id}` !== '') return false;
      return !Number.isFinite(parseInt(method.replace('Value Card', '').trim(), 10));
    });
  }

  get roundingAmount(): number {
    return this.pricedCart.rounding;
  }



  isSaving = false;

  /**
   * The PIN box. The client's card, advance or wallet is used only with their PIN (or the branch's master PIN): the
   * server refuses the bill with `pin_required`, the box opens, and the pass it gets back goes with the next try.
   */
  pinAsk = false;
  private pinToken: string | null = null;

  private needsPin(err: any): boolean {
    if (err?.status !== 400 || !err?.error || typeof err.error !== 'object' || !('pin_required' in err.error)) return false;
    this.pinToken = null;
    this.pinAsk = true;
    this.cdr.detectChanges();
    return true;
  }

  /**
   * The PIN is asked as soon as the client's card, advance or wallet is chosen, not only when the bill is finished:
   * the client is standing there with the PIN in mind, and a wrong or forgotten one is found before the bill is built.
   * `undo` takes the choice back if the PIN is not given. The check at the end of the bill stays as the safety net.
   */
  private pinUndo: (() => void) | null = null;

  private askPinEarly(undo: () => void) {
    if (this.pinToken || this.pinAsk || !this.client?.id || !this.selectedCenterId) return;
    const clientId = this.client.id;
    this.apiService.post('clients/api/pin/needed/', { client: clientId, center: this.selectedCenterId })
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (r: any) => {
          if (!r?.needed || this.client?.id !== clientId || this.pinToken) return;
          this.pinUndo = undo;
          this.pinAsk = true;
          this.cdr.detectChanges();
        },
        error: () => { /* the check when the bill is saved still asks */ },
      });
  }

  /** A payment row was changed in the checkout box: a wallet row asks for the PIN right away. */
  methodChanged() {
    this.calcCheckoutRemaining();
    this.capWallets(true);
    if (this.checkoutPayments.some((p: any) => p.method === 'Advance' || p.method === 'Cashback Wallet')) {
      this.askPinEarly(() => {
        // The wallet rows go, and what they covered goes back onto the other rows (or onto cash when none are
        // left). A new list, not a changed one: the checkout box only redraws when it is handed a new list.
        const rest = this.checkoutPayments.filter((p: any) => p.method !== 'Advance' && p.method !== 'Cashback Wallet');
        this.checkoutPayments = rest.length ? rest : [{ method: 'Cash', amount: this.payableNow }];
        if (rest.length === 1) rest[0]['amount'] = this.payableNow;
        else if (rest.length > 1) rebalancePayments(rest, this.payableNow);
        this.calcCheckoutRemaining();
      });
    }
  }

  onPinVerified(token: string) {
    this.pinToken = token;
    this.pinAsk = false;
    this.cdr.detectChanges();
    if (this.pinUndo) {
      // Asked when the card or wallet was chosen: nothing to save yet.
      this.pinUndo = null;
      this.showToast('PIN accepted.', 'success');
      return;
    }
    this.saveInvoice(false);
  }

  onPinClosed() {
    this.pinAsk = false;
    const undo = this.pinUndo;
    this.pinUndo = null;
    if (undo) {
      undo();
      this.showToast('No PIN, so the card or wallet was not used.', 'info');
    }
    this.cdr.detectChanges();
  }

  /**
   * Identifies this checkout ATTEMPT to the server, so a retry cannot become a
   * second bill.
   *
   * Generated once when a save begins and reused for every retry of that same
   * save. It is deliberately NOT derived from the cart: two walk-ins buying the
   * same haircut send identical carts, and treating that as a duplicate would
   * lose a real sale. Only the till knows whether it is retrying.
   *
   * Cleared once the bill is accepted, so the next customer gets a fresh one.
   */
  private checkoutAttemptKey: string | null = null;

  private newAttemptKey(): string {
    const random = (globalThis.crypto && 'randomUUID' in globalThis.crypto)
      ? globalThis.crypto.randomUUID()
      : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    return `till-${random}`;
  }

  saveInvoice(onHold: boolean) {
    if (this.isSaving) return;

    if (!this.client || (!this.client.id && !this.client.is_anonymous)) {

      this.showToast('Please select or create a client first.', 'error');

      return;

    }



    if (!onHold && this.useAdvancePayment) {

      if (this.clientAdvanceBalance < this.finalTotalAmount) {

        this.showToast("Insufficient Advance Balance to cover the total amount.", 'error');

        return;

      }

    }

    if (!onHold) {
      if (this.checkoutRemaining < 0) {
        this.showToast('Payment entered exceeds the total due. Please adjust the payment amount.', 'error');
        return;
      }
      if (this.checkoutRemaining > 0 && !this.useAdvancePayment) {
        this.showToast('Full payment is required to complete the invoice.', 'error');
        return;
      }
      // Do not close the modal here; let the user see the "SAVING..." state until the API returns
    }

    // A service whose recipe asks for a choice (the colour, the shampoo) cannot be completed without it.
    if (!onHold) {
      const problem = this.recipeProblem();
      if (problem) {
        this.showToast(`Choose the ${problem.names.join(' and ')} for ${this.cart[problem.index].description}.`, 'error');
        this.openRecipe(problem.index);
        return;
      }
    }

    // The server's quote says what needs an approver's yes; ask before taking money.
    if (!onHold && !this.approvalIds.length && this.liveQuote?.approvals_required?.length) {
      this.askForApproval();
      return;
    }

    const payload: any = {
      ...this.buildBillPayload(onHold),
      payments: onHold ? [] : this.buildPaymentRows(),
    };
    if (this.pinToken && !onHold) payload.pin_token = this.pinToken;

    if (this.currentInvoiceId) {
      this.isSaving = true;
      this.apiService.put(`billing/invoices/${this.currentInvoiceId}/`, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.handlePostSave(onHold, res);
        },

        error: (err: any) => {
          this.isSaving = false;
          if (this.needsApproval(err)) return;
          if (this.needsPin(err)) return;
          const errMsg = this.extractApiError(err, 'Failed to update invoice');
          this.showToast(errMsg, 'error');
          console.error(err);
        }
      });
    } else {
      this.isSaving = true;
      // One key per attempt, reused across retries of that attempt. Without
      // it the server cannot tell a retried checkout from the next customer,
      // so it lets both through - and a duplicated bill means a duplicated GST
      // serial, stock deducted twice and the wallet debited twice.
      if (!this.checkoutAttemptKey) {
        this.checkoutAttemptKey = this.newAttemptKey();
      }
      const idempotencyHeaders = {
        headers: { 'Idempotency-Key': this.checkoutAttemptKey },
      };

      this.apiService.post(`billing/invoices/`, payload, idempotencyHeaders).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          // This attempt is finished. The next customer must get a new key, or
          // their identical basket would be answered with THIS bill.
          this.checkoutAttemptKey = null;
          this.handlePostSave(onHold, res);
        },

        error: (err: any) => {
          this.isSaving = false;
          if (this.needsApproval(err)) return;
          if (this.needsPin(err)) return;
          const errMsg = this.extractApiError(err, 'Failed to create invoice');
          this.showToast(errMsg, 'error');
          console.error(err);
        }

      });

    }

  }


  /** The server refused the bill because it needs approval: ask for it. */
  private needsApproval(err: any): boolean {
    if (err?.status !== 409 || err?.error?.code !== 'approval_required') return false;
    this.approvalIds = [];
    this.askForApproval();
    return true;
  }

  askForApproval() {
    this.isSaving = true;
    this.apiService.post('billing/invoices/request_approval/', this.buildBillPayload(false))
      .pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
          this.isSaving = false;
          const requests = res?.requests || [];
          if (!requests.length) {
            // Nothing needs approving after all (the cart changed): finish the bill.
            this.saveInvoice(false);
            return;
          }
          this.approvalWait = requests;
          this.cdr.detectChanges();
        },
        error: (err: any) => {
          this.isSaving = false;
          this.showToast(this.extractApiError(err, 'Could not ask for approval.'), 'error');
          this.cdr.detectChanges();
        },
      });
  }

  onApprovalDone(ids: number[]) {
    this.approvalIds = ids;
    this.approvalWait = null;
    this.saveInvoice(false);
  }

  onApprovalClosed(approvedIds: number[]) {
    this.approvalIds = approvedIds;
    this.approvalWait = null;
    this.cdr.detectChanges();
  }

  onApprovalAskAgain() {
    this.approvalWait = null;
    this.approvalIds = [];
    this.askForApproval();
  }

  scaleInvoiceForDisplay(invoice: any): any {
    // We no longer scale the frontend receipt display.
    // We want the client to see the exact face value of what they paid.
    return invoice;
  }

  handlePostSave(onHold: boolean, invoice: any) {
    this.approvalIds = [];
    this.pinToken = null;
    try {
      this.clearDraft();
      this.closeCheckoutModal();

      const finish = () => {
        if (!onHold) {
          this.completedInvoiceData = this.scaleInvoiceForDisplay(invoice);
          this.showSuccessModal = true;
          this.sound.play('cash');

          // Clear cart immediately so background looks fresh
          this.cart = [];
          this.client = null;
          this.searchPhone = '';
          this.currentInvoiceId = null;
          this.currentInvoiceStatus = 'draft';
          this.invoiceDiscount = 0;
          this.invoiceCGST = 0;
          this.invoiceSGST = 0;
          this.useAdvancePayment = false;
          this.selectedPromotion = null;
          this.selectedValueCardId = null;
          this.serverQuote = null;
          this.resetConfig();
          this.loadLandingData();
        } else {
          this.showToast('Draft saved successfully', 'success');
          // For save draft, we don't want to actually delete it, we just want to clear the UI.
          // Since discardInvoice now deletes the invoice, we should call finalizeDiscard() here instead!
          this.finalizeDiscard();
        }
        this.isSaving = false;
        setTimeout(() => this.cdr.detectChanges(), 0);
      };

      // An advance line is credited to the wallet by the server when the bill is
      // completed (from the money actually paid); the till makes no second call.
      finish();
    } catch (e) {
      console.error('CRITICAL ERROR IN HANDLE POST SAVE:', e);
      this.isSaving = false;
      this.showToast('Error displaying receipt', 'error');
    }
  }





  payInvoice(invoice: any) {
    this.loadInvoice(invoice);
    this.openCheckoutModal();
  }

  loadInvoice(invoice: any) {

    if (invoice.status !== 'draft') {

      this.showToast('Can only load open (draft) invoices.', 'error');

      return;

    }

    this.setViewMode('new-invoice');

    this.currentInvoiceId = invoice.id;
    this.currentInvoiceStatus = invoice.status;





    // Assuming backend returns client details. If just ID, fetch client.
    if (invoice.client) {
      // Just fetch it by ID to be safe and populate all fields
      const clientId = typeof invoice.client === 'object' ? invoice.client.id : invoice.client;
      // What the held bill was using: its value card, or its one offer.
      const covered = (invoice.items || []).find((it: any) => it.value_card);
      this.apiService.get(`clients/api/clients/${clientId}/`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe((c: any) => {
        if (!(c && c.id)) return;
        this.selectClient(c);
        if (covered) {
          this.selectedPromotion = null;
          this.invoiceDiscount = 0;
          this.selectedValueCardId = Number(covered.value_card);
        } else if (invoice.promotion || invoice.membership_used) {
          const wanted = invoice.membership_used ? `m_${invoice.membership_used}` : invoice.promotion;
          const offer = this.promotions.find((p: any) => `${p.id}` === `${wanted}`);
          if (offer) this.selectedPromotion = offer;
          this.applyPromotion();
        }
        this.calcTaxes();
        this.cdr.detectChanges();
      });
    }

    this.cart = [];
    if (invoice.items) {
      invoice.items.forEach((it: any) => {
        let tax_percentage = 0;
        if (it.tax_percentage !== undefined && it.tax_percentage !== null) {
          tax_percentage = Number(it.tax_percentage);
        } else {
          if (it.content_type === 'services.servicemaster') {
            const svc = this.services.find((s: any) => s.id === it.object_id);
            if (svc) tax_percentage = svc.tax_percentage || 0;
          } else if (it.content_type === 'inventory.product') {
            const prod = this.products.find((p: any) => p.id === it.object_id);
            if (prod) tax_percentage = prod.gst_percent || 0;
          }
        }

        this.cart.push({
          content_type: it.content_type,
          object_id: it.object_id,
          id: it.object_id,
          name: it.description,
          description: it.description,
          unit_price: Number(it.unit_price),
          discount: Number(it.discount),
          manager_discount: Number(it.manager_discount || 0),
          quantity: it.quantity,
          tax_percentage: tax_percentage,
          staff: it.staff ? (it.staff.id || it.staff) : null,
          staff_members: (it.staff_members || []).map((s: any) => s.id),
          recipe_choices: it.recipe_choices && Object.keys(it.recipe_choices).length ? it.recipe_choices : undefined
        });
      });
      this.ensureRecipeDefaults();
    }

    this.invoiceDiscount = Number(invoice.discount);
    this.invoiceCGST = Number(invoice.cgst);
    this.invoiceSGST = Number(invoice.sgst);

    if (this.promotions && this.promotions.length > 0) {
      // Just let it manually override for now or auto-select if it matches
    }

    this.cdr.detectChanges();
  }

  viewInvoice(invoiceId: number) {
    this.isSaving = true;
    this.cdr.detectChanges();
    this.apiService.get(`billing/invoices/${invoiceId}/`).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (inv: any) => {
        this.completedInvoiceData = this.scaleInvoiceForDisplay(inv);
        this.showSuccessModal = true;
        this.isSaving = false;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.isSaving = false;
        this.cdr.detectChanges();
        this.showToast('Failed to load invoice details', 'error');
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
