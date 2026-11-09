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
  selector: 'app-inventory',
  imports: [CommonModule, FormsModule, LocationSelectorComponent],
  templateUrl: './inventory.html',
  styleUrl: './inventory.css'
})
export class InventoryComponent implements OnInit {
  todayDate: string = todayLocal();
  private destroyRef = inject(DestroyRef);
  private confirmDialog = inject(ConfirmService);
  private locationMemory = inject(LocationMemory);
  apiService = inject(ApiService);
  toastService = inject(ToastService);
  csvService = inject(CsvService);
  cdr = inject(ChangeDetectorRef);

  activeTab = 'Product Master';
  tabs = ['Checkout', 'Stock History', 'Purchase Orders', 'GRPO', 'Stock Transfers', 'Product Master', 'Vendor Master', 'PO History', 'Audit'];

  isOwner = false;
  hasGlobalAccess = false;
  permissions: any = {};
  centers: any[] = [];
  selectedCenterId: number | null = null;

  products: any[] = [];
  topLowStock: any[] = [];
  fastMovers: any[] = [];
  totalValue: number = 0;
  vendors: any[] = [];
  purchaseOrders: any[] = [];
  stockTransfers: any[] = [];
  grpoOrders: any[] = [];
  
  stockRequests: any[] = [];
  newStockRequest: any = {
    items: [],
    notes: '',
    stock_type: '',
    brands: []
  };
  selectedStockRequest: any = null;
  showStockRequestForm: boolean = false;

  newStockTransfer: any = {
    from_center_id: null,
    to_center_id: null,
    items: [],
    notes: '',
    stock_request_id: null,
    wanted: []
  };
  // Products of the chosen source centre only: a transfer item must be that centre's own row.
  transferProducts: any[] = [];
  selectedStockTransfer: any = null;
  grpoQuantities: { [key: number]: number } = {};
  grpoBatches: { [key: number]: string } = {};
  grpoMfgDates: { [key: number]: string } = {};
  grpoExpDates: { [key: number]: string } = {};

  newProduct: any = {
    product_id_str: '',
    product_code: '',
    name: '',
    brand: '',
    category: '',
    sub_category: '',
    vendor_name: '',
    is_active: true,
    price: null,
    mrp: null,
    gst_percent: 0,
    barcode: '',
    sac_code: '',
    reorder_level: 0,
    reorder_quantity: 0
  };

  newVendor: any = {
    vendor_code: '',
    name: '',
    short_name: '',
    phone: '',
    email: '',
    cst_number: '',
    pan_number: '',
    address: '',
    city: '',
    state: '',
    pin_code: '',
    mapped_products: []
  };

  showMappedVendorsOnly = false;
  selectedProduct: any = null;
  newLot: any = { lot_number: '', net_price: 0, mrp: 0, expiry_date: '' };

  newPO: any = {
    vendor_id: null,
    invoice_number: '',
    items: []
  };

  selectedPO: any = null;

  // Checkout Data
  checkoutSearchTerm: string = '';
  checkoutCurrentPage: number = 1;
  checkoutPageSize: number = 100;
  showInStockOnly: boolean = false;
  todayCheckouts: any[] = [];
  checkoutQuantities: { [key: number]: number } = {};

  // Audit Data
  auditSearchTerm: string = '';
  auditCurrentPage: number = 1;
  auditPageSize: number = 100;
  auditQuantities: { [key: number]: number } = {};

  // Product Master Data
  productMasterSearchTerm: string = '';
  productMasterCurrentPage: number = 1;
  productMasterPageSize: number = 100;

  // Stock History Data
  stockHistoryDates: string[] = [];
  selectedHistoryDate: string | null = null;
  selectedHistoryRecords: any[] = [];
  isLoadingHistory: boolean = false;
  isSaving: boolean = false;

  selectPO(po: any) {
    this.selectedPO = po;
    // Populate vendor details for display if missing
    if (po && po.vendor && !po.vendor_details) {
      const v = this.vendors.find(v => v.id === po.vendor);
      if (v) po.vendor_details = v;
    }
  }

  poStatuses = ['Draft', 'Finalized', 'Approved', 'Ordered', 'Shipped', 'Delivered'];

  updatePOStatus(newStatus: string) {
    if (!this.selectedPO || this.isSaving || !this.can('purchase_orders', 'update')) return;

    const currentIndex = this.poStatuses.indexOf(this.selectedPO.status);
    const newIndex = this.poStatuses.indexOf(newStatus);

    // Prevent manually marking as Delivered to enforce GRPO workflow
    if (newStatus === 'Delivered') {
      this.toastService.showError('Please use the GRPO tab to receive stock. This will automatically mark the PO as Delivered.');
      return;
    }

    // Prevent reverting to a previous status
    if (newIndex < currentIndex) {
      this.toastService.showError('Cannot revert to a previous status.');
      return;
    }

    // Already at this status
    if (newIndex === currentIndex) return;

    this.isSaving = true;
    this.apiService.updatePurchaseOrder(this.selectedPO.id, { status: newStatus }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
         this.selectedPO.status = newStatus;
         const po = this.purchaseOrders.find(p => p.id === this.selectedPO.id);
         if (po) po.status = newStatus;

         if (newStatus === 'Delivered') {
             this.loadData(); // Refresh stock instantly
         } else {
             this.cdr.detectChanges();
         }
         this.isSaving = false;
         this.toastService.showSuccess(`Purchase Order status updated to ${newStatus}`);
      },
      error: (err) => {
          this.toastService.showError('Failed to update status: ' + JSON.stringify(err.error));
          this.isSaving = false;
      }
    });
  }

  isStatusCompleted(status: string): boolean {
    if (!this.selectedPO) return false;
    const currentIndex = this.poStatuses.indexOf(this.selectedPO.status);
    const targetIndex = this.poStatuses.indexOf(status);
    return currentIndex >= targetIndex;
  }

  selectedVendor: any = null;
  activeCenterDetails: any = null;

  // Who may approve, and who may send an approved request, are permission ticks.
  get hasFulfillAccess(): boolean {
    return this.isOwner || (this.permissions.inventory?.fulfill_stock_requests?.update === true);
  }

  /** True if the role holds `verb` on the inventory submodule `sub` (the server enforces it too). */
  can(sub: string, verb: 'read' | 'create' | 'update' | 'delete'): boolean {
    return this.isOwner || this.permissions.inventory?.[sub]?.[verb] === true;
  }

  // Sending makes a Stock Transfer, so the stock person needs that tick as well.
  get canSendStock(): boolean {
    return this.hasFulfillAccess && (this.isOwner || this.permissions.inventory?.stock_transfers?.create === true);
  }

  get canApproveRequests(): boolean {
    return this.isOwner || (this.permissions.inventory?.approve_stock_requests?.update === true);
  }

  myUserId: number | null = null;

  /** Nobody approves their own request (the owner may); the server refuses it too. */
  canDecide(req: any): boolean {
    return this.canApproveRequests && (this.isOwner || !req?.created_by || req.created_by !== this.myUserId);
  }

  /** A request is withdrawn or changed by the person who made it, or by an approver. */
  canWithdraw(req: any): boolean {
    return this.can('stock_requests', 'update')
      && (this.isOwner || (!!req?.created_by && req.created_by === this.myUserId) || this.canApproveRequests);
  }

  get selectedCenterName(): string {
    const c = this.centers.find((x: any) => x.id == this.selectedCenterId);
    return c ? (c.center_name || c.display_name) : '';
  }

  // A request is for Retail stock or Technical stock; every other SAP category (consumables, tools,
  // refreshments, residual ...) is Technical. The server applies the same rule.
  static categoryOf(p: any): string {
    return (p?.category || '').trim().toLowerCase() === 'retail' ? 'Retail' : 'Technical';
  }

  get requestCategories(): { name: string; count: number }[] {
    if (!this.selectedCenterId) return [];        // a request is always for one centre
    const counts = { Retail: 0, Technical: 0 } as Record<string, number>;
    for (const p of this.products) if (p.is_active !== false) counts[InventoryComponent.categoryOf(p)]++;
    return ['Retail', 'Technical'].filter(name => counts[name] > 0).map(name => ({ name, count: counts[name] }));
  }

  /**
   * The brand of a product, as the salon groups them: SAP's item "sub group" field (Kerastase, LP LOREAL, OPI ...),
   * which every item carries, else its "brand" field; products with neither are grouped as "(No brand)".
   */
  static brandOf(p: any): string {
    return (p?.sub_category || '').trim() || (p?.brand || '').trim() || '(No brand)';
  }

  /**
   * Every product of the chosen category (before a brand is chosen). A product switched off in the item master
   * is not offered, and neither is its brand if nothing else under that brand is still active.
   */
  private get requestCategoryProducts(): any[] {
    const wanted = (this.newStockRequest.stock_type || '').trim().toLowerCase();
    if (!wanted) return [];
    return this.products.filter((p: any) => p.is_active !== false && InventoryComponent.categoryOf(p).toLowerCase() === wanted);
  }

  /** The brands ticked in the request form (several can be ticked together). */
  get pickedBrands(): string[] {
    return this.newStockRequest.brands || [];
  }

  /** The brands inside the chosen category, with how many products each has. */
  get requestBrands(): { name: string; count: number }[] {
    const counts = new Map<string, { name: string; count: number }>();
    for (const p of this.requestCategoryProducts) {
      const name = InventoryComponent.brandOf(p);
      const entry = counts.get(name.toLowerCase());
      if (entry) entry.count++;
      else counts.set(name.toLowerCase(), { name, count: 1 });
    }
    return Array.from(counts.values()).sort((a, b) => a.name.localeCompare(b.name));
  }

  /** Products of the chosen category and of every ticked brand. Nothing until a brand is ticked. */
  get requestProducts(): any[] {
    const picked = new Set(this.pickedBrands.map(b => b.toLowerCase()));
    if (!picked.size) return [];
    return this.requestCategoryProducts.filter((p: any) => picked.has(InventoryComponent.brandOf(p).toLowerCase()));
  }

  /**
   * What one line's box offers: the chosen brand's products, plus that line's own product (kept if the brand
   * changes). A product already on another line is not offered again: raise its quantity instead.
   */
  requestProductsFor(item: any): any[] {
    const taken = new Set((this.newStockRequest.items || []).filter((i: any) => i !== item && i.product_id).map((i: any) => i.product_id));
    const list = this.requestProducts.filter((p: any) => !taken.has(p.id));
    if (!item?.product_id || list.some((p: any) => p.id === item.product_id)) return list;
    const own = this.products.find((p: any) => p.id === item.product_id && p.is_active !== false);
    return own ? [own, ...list] : list;
  }

  /** The same for a transfer line: the products of the sending location not yet on another line. */
  transferProductsFor(item: any): any[] {
    const taken = new Set((this.newStockTransfer.items || []).filter((i: any) => i !== item && i.product_id).map((i: any) => i.product_id));
    return this.transferProducts.filter((p: any) => !taken.has(p.id));
  }

  /** What a line is worth with its GST, at the product's rate. */
  static lineAmount(quantity: any, price: any, gst: any): number {
    const base = (Number(quantity) || 0) * (Number(price) || 0);
    return Math.round((base + base * (Number(gst) || 0) / 100) * 100) / 100;
  }

  requestProduct(item: any): any {
    return this.products.find((p: any) => p.id === item?.product_id) || null;
  }

  transferProduct(item: any): any {
    return this.transferProducts.find((p: any) => p.id === item?.product_id) || null;
  }

  /** Worth of the request being made (with GST), and of the transfer being made. */
  get newRequestTotal(): number {
    return (this.newStockRequest.items || []).reduce((sum: number, i: any) => {
      const p = this.requestProduct(i);
      return sum + (p ? InventoryComponent.lineAmount(i.quantity, p.price, p.gst_percent) : 0);
    }, 0);
  }

  get newTransferTotal(): number {
    return (this.newStockTransfer.items || []).reduce((sum: number, i: any) => {
      const p = this.transferProduct(i);
      return sum + (p ? InventoryComponent.lineAmount(i.quantity, p.price, p.gst_percent) : 0);
    }, 0);
  }

  lineAmountOf = InventoryComponent.lineAmount;

  /** True when two lines carry the same product. */
  private static hasRepeatedProduct(items: any[]): boolean {
    const seen = new Set<any>();
    for (const i of items || []) {
      if (!i.product_id) continue;
      if (seen.has(i.product_id)) return true;
      seen.add(i.product_id);
    }
    return false;
  }

  isBrandPicked(name: string): boolean {
    return this.pickedBrands.some(b => b.toLowerCase() === name.toLowerCase());
  }

  /** Tick a brand, or untick it if it is ticked already. */
  toggleRequestBrand(name: string) {
    this.newStockRequest.brands = this.isBrandPicked(name)
      ? this.pickedBrands.filter(b => b.toLowerCase() !== name.toLowerCase())
      : [...this.pickedBrands, name];
  }

  get allBrandsPicked(): boolean {
    const all = this.requestBrands;
    return all.length > 0 && all.every(b => this.isBrandPicked(b.name));
  }

  /** "All" ticks every brand of the category; pressed again it clears them. */
  toggleAllBrands() {
    this.newStockRequest.brands = this.allBrandsPicked ? [] : this.requestBrands.map(b => b.name);
  }

  ngOnInit() {
  let user: any = null;
  const userStr = localStorage.getItem('user');
  if (userStr) {
    try {
      user = JSON.parse(userStr);
      this.isOwner = user.is_superuser === true;
      this.myUserId = user.user_id ?? user.id ?? null;
        this.hasGlobalAccess = this.isOwner || (user?.permissions?.all_centers === true) || (user?.role?.permissions?.all_centers === true);
      this.permissions = user.permissions || {};
    } catch (e) {
      this.toastService.showError(String('Failed to parse user from localStorage') + ((e) ? ' ' + JSON.stringify(e) : ''));
    }
  }

  const availableTabs = [];
  if (this.isOwner || this.permissions.inventory?.checkout?.read) availableTabs.push('Checkout');
  if (this.isOwner || this.permissions.inventory?.stock_history?.read) availableTabs.push('Stock History');
  if (this.isOwner || this.permissions.inventory?.purchase_orders?.read) availableTabs.push('Purchase Orders');
  if (this.isOwner || this.permissions.inventory?.products?.read) availableTabs.push('Product Master');
  if (this.isOwner || this.permissions.inventory?.vendors?.read) availableTabs.push('Vendor Master');
  if (this.isOwner || this.permissions.inventory?.po_history?.read) availableTabs.push('PO History');
  if (this.isOwner || this.permissions.inventory?.purchase_orders?.read) availableTabs.push('GRPO');
  if (this.isOwner || this.permissions.inventory?.stock_transfers?.read) availableTabs.push('Stock Transfers');
  // The fulfil screen lives on the Stock Requests tab, so holding either shows it.
  if (this.isOwner || this.permissions.inventory?.stock_requests?.read
      || this.permissions.inventory?.approve_stock_requests?.read
      || this.permissions.inventory?.fulfill_stock_requests?.read) availableTabs.push('Stock Requests');
  if (this.isOwner || this.permissions.inventory?.transfer_bills?.read) availableTabs.push('Transfer Bills');
  if (this.isOwner || this.permissions.inventory?.audit?.read) availableTabs.push('Audit');

  this.tabs = availableTabs;
  if (this.tabs.length > 0 && !this.tabs.includes(this.activeTab)) {
    this.activeTab = this.tabs[0];
  }

  this.apiService.getCenters().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
    this.centers = data || [];
    if (this.hasGlobalAccess) {
      if (this.centers.length > 0 && !this.selectedCenterId) {
        // Open where the person last was, "All Locations" included. It used to forget "All Locations" and fall to the
        // first centre in the list, which can be a centre with no vendors (a blank Vendor Master).
        const remembered = this.locationMemory.pick(this.centers, this.hasGlobalAccess, true);
        this.selectedCenterId = remembered === undefined ? this.centers[0].id : remembered;
      }
    } else {
      this.selectedCenterId = user?.center_id || null;
      if (this.centers.length > 0 && !this.centers.some(c => c.id == this.selectedCenterId)) {
        this.selectedCenterId = this.centers[0].id;
      }
    }
    this.loadData();
  });
}

  onCenterChange() {
    this.loadData();
  }

  setTab(tab: string) {
    this.activeTab = tab;
    if (tab === 'Transfer Bills') this.loadBills();
  }

  // --- Transfer Bills: every received transfer with the bill its centre attached ---
  bills: { summary: any; results: any[]; page: number; page_size: number } = { summary: null, results: [], page: 1, page_size: 50 };
  billFilter: any = { center_id: '', q: '', date_from: '', date_to: '', has_bill: '', difference: false };
  billsLoading = false;

  loadBills(page = 1) {
    if (this.billsLoading) return;
    this.billsLoading = true;
    this.apiService.getTransferBills({ ...this.billFilter, page }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        this.bills = res;
        this.billsLoading = false;
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.billsLoading = false;
        const e = err?.error || {};
        this.toastService.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || 'Could not load the bills.');
        this.cdr.detectChanges();
      }
    });
  }

  clearBillFilters() {
    this.billFilter = { center_id: '', q: '', date_from: '', date_to: '', has_bill: '', difference: false };
    this.loadBills();
  }

  get billPages(): number {
    const total = this.bills?.summary?.count || 0;
    return Math.max(1, Math.ceil(total / (this.bills?.page_size || 50)));
  }

  /** The link a saved bill opens from. */
  billHref(url: string): string {
    return url ? this.apiService.baseUrl.replace(/\/$/, '') + url : '';
  }

  loadData() {
    const cid = this.selectedCenterId ? this.selectedCenterId : undefined;

    // Load Fast Movers (the usage report needs Marketing > Usage)
    if (this.isOwner || this.permissions.marketing?.usage?.read) {
      this.apiService.getUsageReport(undefined, undefined, cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: (res: any) => {
            if (res && res.breakdown) {
                this.fastMovers = res.breakdown.filter((item: any) => item.service_type === 'Product');
                this.cdr.detectChanges();
            }
        },
        error: (err: any) => console.error('Failed to load breakdown:', err)
      });
    }

    // With no single centre the server already adds each product's stock up across the centres (one row per
    // product); the fold below then has nothing left to merge. It stays for a server that does not do that yet.
    this.apiService.getProducts(cid, !cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      // When "All Locations" selected (no cid), aggregate products by name/code so the same
      // product uploaded to multiple centers shows once with total global stock.
      if (!cid) {
        const aggregatedProducts = new Map<string, any>();
        data.forEach((p: any) => {
          const key = (p.product_code || p.product_id_str || p.name || '').toString().toLowerCase();
          if (aggregatedProducts.has(key)) {
            const existing = aggregatedProducts.get(key);
            existing.current_stock += (p.current_stock || 0);
          } else {
            aggregatedProducts.set(key, { ...p, current_stock: p.current_stock || 0 });
          }
        });
        this.products = Array.from(aggregatedProducts.values());
      } else {
        this.products = data;
      }

      // Calculate Smart Metrics
      this.totalValue = this.products.reduce((sum, p) => sum + ((parseFloat(p.price) || 0) * (p.current_stock || 0)), 0);

      this.topLowStock = [...this.products]
          .filter(p => p.current_stock <= p.reorder_level)
          .map(p => ({ ...p, deficit: p.reorder_level - p.current_stock }))
          .sort((a, b) => b.deficit - a.deficit);

      this.cdr.detectChanges();
    });
    // Vendors carry purchase costs: readable with Vendor Master or Purchase Orders only, so a role
    // with just Stock Requests would otherwise get a 403 on every page load.
    if (this.isOwner || this.permissions.inventory?.vendors?.read || this.permissions.inventory?.purchase_orders?.read) {
      // With no single centre the server sends one row per vendor, not one copy per centre.
      this.apiService.getVendors(cid, !cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
        this.vendors = data;
        this.cdr.detectChanges();
      });
    }
    // Purchase orders are readable only with Purchase Orders or PO History; a role with
    // just Products or Vendors would otherwise trigger a 403 on every page load.
    if (this.isOwner || this.permissions.inventory?.purchase_orders?.read || this.permissions.inventory?.po_history?.read) {
      this.apiService.getPurchaseOrders(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
        this.purchaseOrders = data;
        this.grpoOrders = data.filter((po: any) => ['Draft', 'Finalized', 'Approved', 'Ordered', 'Shipped', 'Partially Received', 'Delivered'].includes(po.status));
        this.cdr.detectChanges();
      });
    }
    this.apiService.getStockTransfers(cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.stockTransfers = data;
      this.cdr.detectChanges();
    });
    // Requests are listed across every centre the person can access, not just the one picked
    // above: the manager and the stock person work through requests from all centres, and each
    // row names its centre.
    this.apiService.getStockRequests().pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.stockRequests = data;
      this.cdr.detectChanges();
    });
    // Only today's checkouts are shown: the server picks those rows out of the ledger, so the newest ten
    // thousand rows of every kind no longer travel to the browser just to be thrown away. The check below
    // is kept so a server that does not know the filter yet still gives the same list.
    const today = todayLocal();
    this.apiService.getStockTransactionsOn('CHECKOUT', today, cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe(data => {
      this.todayCheckouts = data.filter(t => t.transaction_type === 'CHECKOUT' && t.created_at.startsWith(today));
      this.cdr.detectChanges();
    });

    this.generateStockHistoryDates();

    // Set active center details for the PO view
    if (cid && this.centers) {
      this.activeCenterDetails = this.centers.find(c => c.id == cid) || { center_name: `Center (ID: ${cid})`, address: '', phone: '', cst_number: '' };
    } else {
      const userStr = localStorage.getItem('user');
      if (userStr) {
        const u = JSON.parse(userStr);
        this.activeCenterDetails = { center_name: `Assigned Center (ID: ${u.center_id})`, address: '', phone: '', cst_number: '' };
      }
    }
  }

  onVendorSelect() {
    if (this.newPO.vendor_id) {
      this.selectedVendor = this.vendors.find(v => v.id == this.newPO.vendor_id);
    } else {
      this.selectedVendor = null;
    }
  }

  // --- GRPO Logic ---
  selectGRPO(po: any) {
    this.selectedPO = po;
    this.grpoQuantities = {};
    this.grpoBatches = {};
    this.grpoMfgDates = {};
    this.grpoExpDates = {};
    if (po && po.items) {
      po.items.forEach((i: any) => {
        // default to remaining quantity
        const remaining = i.quantity - (i.received_quantity || 0);
        this.grpoQuantities[i.id] = remaining > 0 ? remaining : 0;
        this.grpoBatches[i.id] = i.batch_number || '';
        this.grpoMfgDates[i.id] = i.mfg_date || '';
        this.grpoExpDates[i.id] = i.expiry_date || '';
      });
    }
  }

  submitGRPO() {
    if (!this.selectedPO) return;
    
    const payloadItems = this.selectedPO.items.map((i: any) => ({
      id: i.id,
      received_quantity: this.grpoQuantities[i.id] || 0,
      batch_number: this.grpoBatches[i.id] || null,
      mfg_date: this.grpoMfgDates[i.id] || null,
      expiry_date: this.grpoExpDates[i.id] || null
    })).filter((i: any) => i.received_quantity > 0);
    
    if (payloadItems.length === 0) {
      this.toastService.showError("Please enter quantity to receive.");
      return;
    }
    
    this.isSaving = true;
    this.apiService.receiveGRPO(this.selectedPO.id, { items: payloadItems }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res: any) => {
        // Whether it reached SAP is shown on the order ("SAP Synced" / "Pending SAP Sync"); the
        // receipt itself is saved here either way, so that is all this message may claim.
        this.toastService.showSuccess("Stock received.");
        this.isSaving = false;
        this.selectedPO = null;
        this.loadData();
      },
      error: (err: any) => {
        this.toastService.showError("Failed to receive stock: " + JSON.stringify(err.error));
        this.isSaving = false;
      }
    });
  }

  // --- Stock Transfer Logic ---
  selectStockTransfer(st: any) {
    this.selectedStockTransfer = st;
    this.transferScan = '';
    this.receiveNote = '';
    this.receiveBill = null;
    // The counts shown in the boxes: what is saved on the transfer, editable until it is completed.
    this.countDraft = {};
    for (const item of st?.items || []) {
      this.countDraft[item.id] = {
        sScan: item.scanned_quantity, sMan: item.manual_quantity,
        rScan: item.received_scanned_quantity, rMan: item.received_manual_quantity,
      };
    }
  }

  // --- Counting the goods of a transfer: scanned with the barcode reader, or typed in ---
  /** What is typed in the scan box (a barcode reader types the code and presses Enter). */
  transferScan = '';
  /** Draft counts of the open transfer, by line: sent (s) and received (r), scanned and manual. */
  countDraft: { [itemId: number]: { sScan: any; sMan: any; rScan: any; rMan: any } } = {};

  /** True when a line's scanned + typed equals its quantity (the create form's tick). */
  inventoryCountOk(item: any): boolean {
    return InventoryComponent.countCheck(item.quantity, item.scanned, item.manual) === 'ok';
  }

  countStatusText(quantity: any, scanned: any, manual: any): string {
    const check = InventoryComponent.countCheck(quantity, scanned, manual);
    if (check === 'none') return '';
    const counted = (Number(scanned) || 0) + (Number(manual) || 0);
    return check === 'ok' ? `${counted} of ${quantity} ✓` : (check === 'short' ? `${counted} of ${quantity}: short` : `${counted} of ${quantity}: over`);
  }

  countStatusColor(quantity: any, scanned: any, manual: any): string {
    const check = InventoryComponent.countCheck(quantity, scanned, manual);
    return check === 'ok' ? '#059669' : (check === 'none' ? '#64748b' : '#d97706');
  }

  /** 'ok' when scanned + typed equals the quantity, 'short' / 'over' when not, 'none' before any count. */
  static countCheck(quantity: any, scanned: any, manual: any): 'none' | 'ok' | 'short' | 'over' {
    const blank = (v: any) => v === null || v === undefined || v === '';
    if (blank(scanned) && blank(manual)) return 'none';
    const counted = (Number(scanned) || 0) + (Number(manual) || 0);
    const want = Number(quantity) || 0;
    return counted === want ? 'ok' : (counted < want ? 'short' : 'over');
  }

  /** The line of a product by what a reader scans: its barcode or its product code. */
  private static matchesCode(p: any, code: string): boolean {
    const wanted = code.trim().toLowerCase();
    return !!wanted && ((p?.barcode || '').toString().toLowerCase() === wanted
      || (p?.product_code || '').toString().toLowerCase() === wanted);
  }

  /** A code was scanned while making a transfer: its line gains one scanned (the line is added if new). */
  scanIntoNewTransfer() {
    const code = this.transferScan;
    this.transferScan = '';
    if (!code.trim()) return;
    const product = this.transferProducts.find((p: any) => InventoryComponent.matchesCode(p, code));
    if (!product) {
      this.toastService.showError(this.newStockTransfer.from_center_id
        ? `No product with the code ${code.trim()} at the sending location.`
        : 'Choose the From location first.');
      return;
    }
    let line = this.newStockTransfer.items.find((i: any) => i.product_id === product.id);
    if (!line) {
      line = { product_id: product.id, quantity: 0, scanned: 0, manual: null };
      this.newStockTransfer.items = this.newStockTransfer.items.filter((i: any) => i.product_id || i.quantity);
      this.newStockTransfer.items.push(line);
    }
    line.scanned = (Number(line.scanned) || 0) + 1;
    if ((Number(line.quantity) || 0) < line.scanned + (Number(line.manual) || 0)) line.quantity = line.scanned + (Number(line.manual) || 0);
  }

  /** A code was scanned on the open transfer: the received count while it is on the road, else the sent count. */
  scanIntoOpenTransfer() {
    const code = this.transferScan;
    this.transferScan = '';
    const transfer = this.selectedStockTransfer;
    if (!transfer || !code.trim()) return;
    const item = (transfer.items || []).find((i: any) =>
      InventoryComponent.matchesCode({ barcode: i.product_barcode, product_code: i.product_code }, code));
    if (!item) {
      this.toastService.showError(`${code.trim()} is not on this transfer.`);
      return;
    }
    const draft = this.countDraft[item.id] || (this.countDraft[item.id] = { sScan: null, sMan: null, rScan: null, rMan: null });
    if (transfer.status === 'In Transit') draft.rScan = (Number(draft.rScan) || 0) + 1;
    else draft.sScan = (Number(draft.sScan) || 0) + 1;
  }

  /** Save the counts shown in the open transfer's boxes. */
  saveTransferCounts() {
    const transfer = this.selectedStockTransfer;
    if (!transfer || this.isSaving) return;
    const rows = (key1: 'sScan' | 'rScan', key2: 'sMan' | 'rMan') => (transfer.items || []).map((item: any) => ({
      id: item.id, scanned: this.countDraft[item.id]?.[key1] ?? null, manual: this.countDraft[item.id]?.[key2] ?? null }));
    const body: any = {};
    if (transfer.status !== 'Completed') body.sent = rows('sScan', 'sMan');
    if (transfer.status === 'In Transit') body.received = rows('rScan', 'rMan');
    this.isSaving = true;
    this.apiService.verifyStockTransfer(transfer.id, body).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (saved: any) => {
        this.isSaving = false;
        this.toastService.showSuccess('Counts saved.');
        const at = this.stockTransfers.findIndex((t: any) => t.id === saved.id);
        if (at >= 0) this.stockTransfers[at] = saved;
        this.selectStockTransfer(saved);
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.isSaving = false;
        const e = err?.error || {};
        this.toastService.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || 'Could not save the counts.');
      }
    });
  }

  // --- Sending and signing in a transfer ---
  receiveNote = '';
  receiveBill: File | null = null;

  /** What a transfer's status is called on screen. */
  static transferStatusText(status: string): string {
    return status === 'Draft' ? 'Not sent yet' : status === 'In Transit' ? 'On the way' : status === 'Completed' ? 'Received' : status;
  }

  transferStatusText = InventoryComponent.transferStatusText;

  /** The branch that sent a transfer does not sign it in (the owner may). */
  canReceive(t: any): boolean {
    return !!t && t.status === 'In Transit' && this.can('stock_transfers', 'update')
      && (this.isOwner || !t.dispatched_by || t.dispatched_by !== this.myUserId);
  }

  /** The lines whose sent count does not add up to the quantity: they must be put right before sending. */
  sendProblems(t: any): string[] {
    const out: string[] = [];
    for (const item of t?.items || []) {
      const d = this.countDraft[item.id];
      if (!d) continue;
      const check = InventoryComponent.countCheck(item.quantity, d.sScan, d.sMan);
      if (check === 'short' || check === 'over') out.push(`${item.product_name}: ${this.countStatusText(item.quantity, d.sScan, d.sMan)}`);
    }
    return out;
  }

  /** Lines where what was counted on arrival is less than what was sent. */
  receiveShortLines(t: any): string[] {
    const out: string[] = [];
    for (const item of t?.items || []) {
      const d = this.countDraft[item.id];
      if (!d) continue;
      const check = InventoryComponent.countCheck(item.quantity, d.rScan, d.rMan);
      if (check === 'short') out.push(item.product_name);
    }
    return out;
  }

  receiveOverLines(t: any): string[] {
    return (t?.items || []).filter((item: any) => {
      const d = this.countDraft[item.id];
      return d && InventoryComponent.countCheck(item.quantity, d.rScan, d.rMan) === 'over';
    }).map((item: any) => item.product_name);
  }

  /** Fill every line as arrived in full (typed in), for a box that matches its challan. */
  everythingArrived() {
    for (const item of this.selectedStockTransfer?.items || []) {
      const d = this.countDraft[item.id] || (this.countDraft[item.id] = { sScan: null, sMan: null, rScan: null, rMan: null });
      d.rScan = null;
      d.rMan = Number(item.quantity);
    }
  }

  onBillPicked(event: Event) {
    const file = (event.target as HTMLInputElement).files?.[0] || null;
    if (file && file.size > 8 * 1024 * 1024) {
      this.toastService.showError('The bill is too big; keep it under 8 MB.');
      (event.target as HTMLInputElement).value = '';
      this.receiveBill = null;
      return;
    }
    this.receiveBill = file;
  }

  /** Where a saved bill can be opened from. */
  billLink(t: any): string {
    return t?.bill_url ? this.apiService.baseUrl.replace(/\/$/, '') + t.bill_url : '';
  }

  /** Send the goods: the counts typed so far are saved first, and a count that does not add up stops it. */
  sendTransfer() {
    const transfer = this.selectedStockTransfer;
    if (!transfer || this.isSaving) return;
    const problems = this.sendProblems(transfer);
    if (problems.length) {
      this.toastService.showError('Fix the count before sending - ' + problems[0]);
      return;
    }
    this.isSaving = true;
    const sent = (transfer.items || []).map((item: any) => ({
      id: item.id, scanned: this.countDraft[item.id]?.sScan ?? null, manual: this.countDraft[item.id]?.sMan ?? null }));
    this.apiService.verifyStockTransfer(transfer.id, { sent }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => { this.isSaving = false; this.updateStockTransferStatus('In Transit'); },
      error: (err: any) => {
        this.isSaving = false;
        const e = err?.error || {};
        this.toastService.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || 'Could not save the counts.');
      }
    });
  }

  /** The receiving branch signs the transfer in with what it counted, a note and the bill. */
  signInTransfer() {
    const transfer = this.selectedStockTransfer;
    if (!transfer || this.isSaving) return;
    if (this.receiveOverLines(transfer).length) {
      this.toastService.showError(`More than was sent: ${this.receiveOverLines(transfer)[0]}. Check the count.`);
      return;
    }
    if (!this.receiveBill) {
      this.toastService.showError('Attach the bill (a PDF or a picture) that came with the goods.');
      return;
    }
    if (this.receiveShortLines(transfer).length && !this.receiveNote.trim()) {
      this.toastService.showError('Less arrived than was sent - write a note saying what is missing.');
      return;
    }
    const lines = (transfer.items || []).map((item: any) => ({
      id: item.id, scanned: this.countDraft[item.id]?.rScan ?? null, manual: this.countDraft[item.id]?.rMan ?? null }));
    const form = new FormData();
    form.append('lines', JSON.stringify(lines));
    form.append('note', this.receiveNote.trim());
    if (this.receiveBill) form.append('bill', this.receiveBill, this.receiveBill.name);
    this.isSaving = true;
    this.apiService.receiveStockTransfer(transfer.id, form).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (saved: any) => {
        this.isSaving = false;
        this.toastService.showSuccess(saved.has_difference ? 'Received - the difference was recorded.' : 'Received. The stock is now at your centre.');
        const at = this.stockTransfers.findIndex((t: any) => t.id === saved.id);
        if (at >= 0) this.stockTransfers[at] = saved;
        this.selectStockTransfer(saved);
        this.loadData();
        this.cdr.detectChanges();
      },
      error: (err: any) => {
        this.isSaving = false;
        const e = err?.error || {};
        this.toastService.showError((Array.isArray(e.detail) ? e.detail[0] : e.detail) || 'Could not receive the transfer.');
      }
    });
  }

  /** The steps a stock request has been through, for the progress strip: done / current / waiting. */
  requestSteps(req: any): { label: string; state: 'done' | 'current' | 'todo' }[] {
    if (!req) return [];
    if (req.status === 'Rejected') return [{ label: 'Asked', state: 'done' }, { label: 'Rejected', state: 'current' }];
    if (req.status === 'Cancelled') return [{ label: 'Asked', state: 'done' }, { label: 'Cancelled', state: 'current' }];
    const approved = req.status !== 'Pending';
    const sent = !!req.transfer_id && req.transfer_status !== 'Draft';
    const received = req.status === 'Fulfilled' || req.transfer_status === 'Completed';
    const steps = [
      { label: 'Asked', done: true },
      { label: 'Approved', done: approved },
      { label: 'Sent', done: sent },
      { label: 'Received', done: received },
    ];
    const firstTodo = steps.findIndex(s => !s.done);
    return steps.map((s, i) => ({ label: s.label, state: s.done ? 'done' as const : (i === firstTodo ? 'current' as const : 'todo' as const) }));
  }

  /** Print the open transfer as a transfer note (a delivery challan), in its own window. */
  printTransfer() {
    const t = this.selectedStockTransfer;
    if (!t) return;
    const esc = (v: any) => String(v ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' } as any)[c]);
    const money = (n: number) => (Math.round(n * 100) / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    const when = (v: any) => v ? new Date(v).toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' }) : '';
    const kind: string = t.transfer_kind || 'unknown';
    const from = t.from_info || {}, to = t.to_info || {};
    // Stock sent to another branch (another GSTIN) travels with a tax invoice raised in SAP: its number and
    // figures (IGST worked out by SAP) are printed, not estimated here from the product prices.
    const sapInvoice = !!t.sap_ar_doc_num;
    const taxName = t.supply_kind === 'cgst_sgst' ? 'CGST + SGST' : 'IGST';
    const kindText = sapInvoice
      ? `SUPPLY TO ANOTHER BRANCH (different GSTIN): goods go against TAX INVOICE ${esc(t.sap_ar_doc_num)} raised in SAP. ${taxName} as per that invoice. An e-way bill must travel with these goods where the value requires it.`
      : kind === 'interstate'
      ? `INTER-STATE transfer (${esc(from.state)} to ${esc(to.state)}) - IGST applies. A tax invoice and an e-way bill must travel with these goods.`
      : kind === 'same_state'
        ? `WITHIN-STATE transfer (${esc(from.state)}) - CGST + SGST apply when the two locations have different GSTINs; none when they share one.`
        : 'State of one or both locations is not known - check the GST treatment before sending.';

    let taxable = 0, tax = 0;
    const rows = (t.items || []).map((i: any, n: number) => {
      const qty = Number(i.quantity) || 0, rate = Number(i.product_price) || 0, gst = Number(i.product_gst) || 0;
      const value = qty * rate, amount = value * gst / 100;
      taxable += value; tax += amount;
      const taxCell = sapInvoice
        ? `${taxName} ${gst}% as per tax invoice`
        : kind === 'interstate'
          ? `IGST ${gst}% = ${money(amount)}`
          : `CGST ${gst / 2}% + SGST ${gst / 2}% = ${money(amount / 2)} + ${money(amount / 2)}`;
      const count = (a: any, b: any) => (a === null || a === undefined) && (b === null || b === undefined)
        ? '-' : `${Number(a) || 0} + ${Number(b) || 0} = ${(Number(a) || 0) + (Number(b) || 0)}`;
      return `<tr><td>${n + 1}</td><td>${esc(i.product_code)}</td><td>${esc(i.product_name)}</td><td>${esc(i.product_hsn)}</td>
        <td class="r">${esc(i.quantity)} ${esc(i.product_unit)}</td><td class="r">${sapInvoice ? '-' : money(rate)}</td><td class="r">${sapInvoice ? '-' : money(value)}</td>
        <td>${taxCell}</td><td>${count(i.scanned_quantity, i.manual_quantity)}</td><td>${count(i.received_scanned_quantity, i.received_manual_quantity)}</td></tr>`;
    }).join('');
    if (sapInvoice) {
      taxable = Number(t.sap_taxable_value) || 0;
      tax = Number(t.sap_tax_amount) || 0;
    }
    const total = sapInvoice ? (Number(t.sap_invoice_total) || taxable + tax) : taxable + tax;
    const party = (title: string, p: any) => `<div class="box"><b>${title}</b><br>${esc(p.name)}${p.legal_name && p.legal_name !== p.name ? `<br><small>${esc(p.legal_name)}</small>` : ''}
      <br>${esc(p.address)} ${esc(p.pin_code)}<br>State: ${esc(p.state) || '-'}<br>GSTIN: ${esc(p.gstin) || '-'}${p.phone ? `<br>Phone: ${esc(p.phone)}` : ''}</div>`;

    const html = `<!doctype html><html><head><meta charset="utf-8"><title>Stock transfer ST-${t.id}</title><style>
      body{font:12px Arial,sans-serif;color:#111;margin:24px} h1{font-size:18px;margin:0} .top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px}
      .kind{border:1.5px solid #111;padding:6px 10px;margin:8px 0 12px;font-weight:700} .parties{display:flex;gap:12px;margin-bottom:12px}
      .box{flex:1;border:1px solid #999;padding:8px;line-height:1.5} table{width:100%;border-collapse:collapse} th,td{border:1px solid #999;padding:4px 6px;vertical-align:top}
      th{background:#eee;text-align:left} .r{text-align:right} .totals{margin:10px 0;text-align:right;line-height:1.7} .sign{display:flex;gap:12px;margin-top:46px}
      .sign div{flex:1;border-top:1px solid #111;padding-top:4px;text-align:center} small{color:#555}
      @media print{body{margin:10mm}}</style></head><body>
      <div class="top"><div><h1>STOCK TRANSFER NOTE / DELIVERY CHALLAN</h1><small>${sapInvoice ? 'Goods supplied to another branch against a tax invoice' : 'Goods moved between our own locations - not a sale'}</small></div>
      <div style="text-align:right"><b>No. ST-${t.id}</b><br>Date: ${esc(when(t.created_at))}<br>Status: ${esc(t.status)}${t.stock_request ? `<br>For request REQ-${t.stock_request}` : ''}</div></div>
      <div class="kind">${kindText}</div>
      <div class="parties">${party('FROM (sending)', from)}${party('TO (receiving)', to)}</div>
      <table><thead><tr><th>#</th><th>Code</th><th>Description</th><th>HSN</th><th class="r">Qty</th><th class="r">Rate</th><th class="r">Taxable value</th><th>GST</th>
        <th>Sent: scanned + typed</th><th>Received: scanned + typed</th></tr></thead><tbody>${rows}</tbody></table>
      <div class="totals">Taxable value: <b>${money(taxable)}</b><br>${sapInvoice ? taxName : (kind === 'interstate' ? 'IGST' : 'CGST + SGST')}: <b>${money(tax)}</b><br>Total value: <b>${money(total)}</b>${sapInvoice ? `<br><small>Tax invoice ${esc(t.sap_ar_doc_num)} (SAP)</small>` : ''}</div>
      ${t.notes ? `<p><b>Notes:</b> ${esc(t.notes)}</p>` : ''}
      <div class="sign"><div>Prepared by<br><small>${esc(t.created_by_name)}</small></div>
        <div>Sent by<br><small>${esc(t.dispatched_by_name)} ${esc(when(t.dispatched_at))}</small></div>
        <div>Received by<br><small>${esc(t.received_by_name)} ${esc(when(t.received_at))}</small></div></div>
      <script>window.onload=function(){window.print()}</script></body></html>`;
    const win = window.open('', '_blank', 'width=1000,height=800');
    if (!win) { this.toastService.showError('Allow pop-ups to print the transfer.'); return; }
    win.document.open();
    win.document.write(html);
    win.document.close();
  }
  
  onTransferSourceChange(centerId: number | null) {
    this.newStockTransfer.from_center_id = centerId;
    this.newStockTransfer.items = [];
    // Sending an approved request: once the source is chosen, fill in what was asked for,
    // matching each product to the source's own row by product code.
    const wanted: any[] = this.newStockTransfer.wanted || [];
    this.loadTransferProducts(centerId, () => {
      if (!wanted.length) return;
      const items: any[] = [];
      const missing: string[] = [];
      wanted.forEach((w: any) => {
        const p = this.transferProducts.find((x: any) => x.product_code && x.product_code === w.code);
        if (p) items.push({ product_id: p.id, quantity: w.quantity });
        else missing.push(w.code || '?');
      });
      this.newStockTransfer.items = items;
      if (missing.length) {
        this.toastService.showError(`Not stocked at the source location: ${missing.join(', ')}`);
      }
    });
  }

  loadTransferProducts(centerId: number | null, then?: () => void) {
    this.transferProducts = [];
    if (!centerId) { then?.(); return; }
    this.apiService.getProducts(centerId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data: any[]) => {
        // Guard against a late reply for a centre that is no longer selected.
        if (this.newStockTransfer.from_center_id !== centerId) return;
        this.transferProducts = data;
        then?.();
        this.cdr.detectChanges();
      },
      error: () => this.toastService.showError("Could not load products for that location.")
    });
  }

  transferStock(item: any): number | null {
    const p = this.transferProducts.find((x: any) => x.id === item.product_id);
    return p ? (p.current_stock || 0) : null;
  }

  addStockTransferItem() {
    this.newStockTransfer.items.push({ product_id: null, quantity: 1 });
  }

  removeStockTransferItem(index: number) {
    this.newStockTransfer.items.splice(index, 1);
  }

  saveStockTransfer() {
    if (!this.newStockTransfer.from_center_id || !this.newStockTransfer.to_center_id) {
      this.toastService.showError("Select both From and To locations.");
      return;
    }
    if (this.newStockTransfer.items.length === 0) {
      this.toastService.showError("Add at least one item.");
      return;
    }
    
    if (this.isSaving) return;
    if (this.newStockTransfer.from_center_id === this.newStockTransfer.to_center_id) {
      this.toastService.showError("From and To locations must be different.");
      return;
    }
    const validItems = this.newStockTransfer.items.filter((i: any) => i.product_id && i.quantity > 0);
    if (validItems.length === 0) {
      this.toastService.showError("Invalid items or quantities.");
      return;
    }
    if (new Set(validItems.map((i: any) => i.product_id)).size !== validItems.length) {
      this.toastService.showError("The same product is listed twice. Combine it into one line.");
      return;
    }
    const short = validItems.find((i: any) => { const s = this.transferStock(i); return s !== null && i.quantity > s; });
    if (short) {
      const p = this.transferProducts.find((x: any) => x.id === short.product_id);
      this.toastService.showError(`${p?.name}: only ${p?.current_stock} in stock at the source location.`);
      return;
    }

    const payload = {
      from_center: this.newStockTransfer.from_center_id,
      to_center: this.newStockTransfer.to_center_id,
      notes: this.newStockTransfer.notes,
      stock_request: this.newStockTransfer.stock_request_id || null,
      items: validItems.map((i: any) => ({
        product: i.product_id, quantity: i.quantity,
        scanned_quantity: (i.scanned === '' || i.scanned === undefined) ? null : i.scanned,
        manual_quantity: (i.manual === '' || i.manual === undefined) ? null : i.manual,
      }))
    };

    this.isSaving = true;
    this.apiService.createStockTransfer(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.toastService.showSuccess("Transfer created. Check the counts and press Send; the centre then counts it and signs it in.");
        this.newStockTransfer = { from_center_id: null, to_center_id: null, items: [], notes: '', stock_request_id: null, wanted: [] };
        this.transferProducts = [];
        this.isSaving = false;
        this.loadData();
      },
      error: (err: any) => {
        const e = err?.error || {};
        const detail = Array.isArray(e.detail) ? e.detail[0] : e.detail;
        const first = Object.values(e)[0];
        this.toastService.showError(detail || ("Failed to create transfer: " + (Array.isArray(first) ? first[0] : JSON.stringify(e))));
        this.isSaving = false;
      }
    });
  }

  updateStockTransferStatus(newStatus: string) {
    if (!this.selectedStockTransfer || this.isSaving) return;
    this.isSaving = true;
    this.apiService.updateStockTransfer(this.selectedStockTransfer.id, { status: newStatus }).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.toastService.showSuccess(`Transfer updated to ${newStatus}`);
        this.isSaving = false;
        this.selectedStockTransfer = null;
        this.loadData();
      },
      error: (err: any) => {
        const e = err?.error || {};
        const detail = Array.isArray(e.detail) ? e.detail[0] : e.detail;
        this.toastService.showError(detail || ("Failed to update transfer: " + JSON.stringify(e)));
        this.isSaving = false;
      }
    });
  }

  // --- Stock Request Logic ---
  selectStockRequest(req: any) {
    this.selectedStockRequest = req;
  }

  // Stock of different categories is taxed and handled differently, so a request is for
  // one category only. Changing it drops the lines picked for the other.
  setStockType(type: string) {
    // Pressing the chosen category again clears it.
    const next = this.newStockRequest.stock_type === type ? '' : type;
    this.newStockRequest.stock_type = next;
    this.newStockRequest.items = [];
    // A category with a single brand needs no question; otherwise the brands are ticked next.
    this.newStockRequest.brands = [];
    const brands = this.requestBrands;
    if (brands.length === 1) this.newStockRequest.brands = [brands[0].name];
  }

  addStockRequestItem() {
    this.newStockRequest.items.push({ product_id: null, quantity: 1 });
  }

  removeStockRequestItem(index: number) {
    this.newStockRequest.items.splice(index, 1);
  }

  saveStockRequest() {
    if (!this.newStockRequest.stock_type) {
      this.toastService.showError("Choose the category of stock first.");
      return;
    }
    if (this.newStockRequest.items.length === 0) {
      this.toastService.showError("Add at least one item.");
      return;
    }
    const validItems = this.newStockRequest.items.filter((i: any) => i.product_id && i.quantity > 0);
    if (validItems.length === 0) {
      this.toastService.showError("Invalid items or quantities.");
      return;
    }
    if (InventoryComponent.hasRepeatedProduct(validItems)) {
      this.toastService.showError("A product is on the list twice. Put the whole quantity on one line.");
      return;
    }
    
    if (!this.selectedCenterId) {
      this.toastService.showError("Select the centre the stock is for first.");
      return;
    }
    // The requesting center is the currently selected one
    const payload = {
      requesting_center: this.selectedCenterId,
      stock_type: this.newStockRequest.stock_type,
      notes: this.newStockRequest.notes,
      items: validItems.map((i: any) => ({ product: i.product_id, quantity: i.quantity }))
    };

    this.isSaving = true;
    this.apiService.createStockRequest(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.toastService.showSuccess("Stock request sent for approval.");
        this.newStockRequest = { items: [], notes: '', stock_type: '', brands: [] };
        this.showStockRequestForm = false;
        this.isSaving = false;
        this.loadData();
      },
      error: (err: any) => {
        this.toastService.showError(this.requestError(err, "Failed to create request"));
        this.isSaving = false;
      }
    });
  }

  private requestError(err: any, fallback: string): string {
    const e = err?.error || {};
    const detail = Array.isArray(e.detail) ? e.detail[0] : e.detail;
    if (detail) return detail;
    const first: any = Object.values(e)[0];
    return `${fallback}: ${Array.isArray(first) ? first[0] : (first || JSON.stringify(e))}`;
  }

  // Approve, reject or cancel go through their own endpoints; the status is never typed in.
  decideStockRequest(req: any, action: 'approve' | 'reject' | 'cancel') {
    if (this.isSaving) return;
    this.isSaving = true;
    this.apiService.decideStockRequest(req.id, action).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (updated: any) => {
        const word = { approve: 'approved', reject: 'rejected', cancel: 'cancelled' }[action];
        this.toastService.showSuccess(`Request REQ-${req.id} ${word}.`);
        this.isSaving = false;
        this.selectedStockRequest = updated || null;
        this.loadData();
      },
      error: (err: any) => {
        this.toastService.showError(this.requestError(err, "Could not update the request"));
        this.isSaving = false;
      }
    });
  }

  // The stock person picks where the stock comes from (a warehouse or a centre) on the
  // Stock Transfers tab; the request's products are filled in from that place's own rows.
  sendStockRequest(req: any) {
    this.newStockTransfer = {
      from_center_id: null,
      to_center_id: req.requesting_center,
      notes: `For request REQ-${req.id}` + (req.notes ? `\n${req.notes}` : ''),
      items: [],
      stock_request_id: req.id,
      wanted: (req.items || []).map((i: any) => ({ code: i.product_code, quantity: i.quantity }))
    };
    this.transferProducts = [];
    this.selectedStockTransfer = null;
    this.selectedStockRequest = null;
    this.showStockRequestForm = false;
    this.setTab('Stock Transfers');
  }


  addPOItem() {
    this.newPO.items.push({
      product_id: null,
      quantity: 1,
      rate: 0,
      discount_percent: 0,
      tax_percent: 0
    });
  }

  removePOItem(index: number) {
    this.newPO.items.splice(index, 1);
  }

  onProductSelect(item: any) {
    if (item.product_id) {
      const prod = this.products.find(p => p.id == item.product_id);
      if (prod) {
        item.rate = prod.price;
      }
    }
  }

  getVendorProducts() {
    if (!this.selectedVendor || !this.selectedVendor.mapped_products || this.selectedVendor.mapped_products.length === 0) {
      return this.products; // Fallback to all if none mapped
    }
    return this.products.filter(p => this.selectedVendor.mapped_products.includes(p.id));
  }

  getProductName(productId: number): string {
    const prod = this.products.find(p => p.id === productId);
    return prod ? prod.name : 'Unknown';
  }

  getProductProperty(productId: number, prop: string): any {
    const prod = this.products.find(p => p.id === productId);
    return prod && prod[prop] ? prod[prop] : '-';
  }

  get masterFilteredProducts() {
    let filtered = this.products;
    if (this.productMasterSearchTerm) {
      const s = this.productMasterSearchTerm.toLowerCase();
      filtered = filtered.filter(p => p.name.toLowerCase().includes(s) || (p.product_code && p.product_code.toLowerCase().includes(s)) || (p.barcode && p.barcode.toLowerCase().includes(s)));
    }
    return filtered;
  }

  get masterPaginatedProducts() {
     const filtered = this.masterFilteredProducts;
     const startIndex = (this.productMasterCurrentPage - 1) * this.productMasterPageSize;
     return filtered.slice(startIndex, startIndex + this.productMasterPageSize);
  }

  get productMasterTotalPages() {
     return Math.max(1, Math.ceil(this.masterFilteredProducts.length / this.productMasterPageSize));
  }

  nextProductMasterPage() {
    if (this.productMasterCurrentPage < this.productMasterTotalPages) {
      this.productMasterCurrentPage++;
    }
  }

  prevProductMasterPage() {
    if (this.productMasterCurrentPage > 1) {
      this.productMasterCurrentPage--;
    }
  }

  onProductMasterSearch() {
    this.productMasterCurrentPage = 1;
  }

  calculateItemTotal(item: any): number {
    const base = item.quantity * item.rate;
    const discountAmount = base * (item.discount_percent / 100);
    const subtotal = base - discountAmount;
    const taxAmount = subtotal * (item.tax_percent / 100);
    return subtotal + taxAmount;
  }

  calculatePOTotal(): number {
    let total = 0;
    for (let item of this.newPO.items) {
      total += this.calculateItemTotal(item);
    }
    return total;
  }

  createPurchaseOrder() {
    if (this.isSaving) return;
    if (!this.selectedCenterId) {
      this.toastService.showError('Please select a specific center to create a Purchase Order.');
      return;
    }
    if (!this.newPO.vendor_id) {
      this.toastService.showError('Please select a vendor');
      return;
    }
    if (this.newPO.items.length === 0) {
      this.toastService.showError('Please add at least one product');
      return;
    }

    this.isSaving = true;
    const payload = {
      vendor: this.newPO.vendor_id,
      invoice_number: this.newPO.invoice_number,
      total_amount: this.calculatePOTotal(),
      status: 'Draft',
      items: this.newPO.items.map((i: any) => ({
        product: i.product_id,
        quantity: i.quantity,
        rate: i.rate,
        discount_percent: i.discount_percent,
        tax_percent: i.tax_percent,
        total_price: this.calculateItemTotal(i)
      }))
    } as any;

    if (this.selectedCenterId) {
      payload.center = Number(this.selectedCenterId);
    }

    if (this.newPO.id) {
      this.apiService.updatePurchaseOrder(this.newPO.id, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newPO = { vendor_id: null, invoice_number: '', items: [] };
          this.selectedVendor = null;
          this.isSaving = false;
          this.toastService.showSuccess('Purchase Order Updated Successfully!');
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to update Purchase Order: ' + JSON.stringify(err.error));
        }
      });
    } else {
      this.apiService.createPurchaseOrder(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newPO = { vendor_id: null, invoice_number: '', items: [] };
          this.selectedVendor = null;
          this.isSaving = false;
          this.toastService.showSuccess('Purchase Order Created Successfully!');
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to create Purchase Order: ' + JSON.stringify(err.error));
        }
      });
    }
  }

  editPO() {
    if (!this.selectedPO) return;
    this.newPO = {
      id: this.selectedPO.id,
      vendor_id: this.selectedPO.vendor,
      invoice_number: this.selectedPO.invoice_number,
      items: this.selectedPO.items.map((i: any) => ({
        product_id: i.product,
        quantity: i.quantity,
        rate: i.rate,
        discount_percent: i.discount_percent,
        tax_percent: i.tax_percent
      }))
    };
    this.onVendorSelect();
    this.selectedPO = null;
  }

  async deletePO(id: number) {
    if (!await this.confirmDialog.ask('Are you sure you want to delete this Purchase Order?')) return;
    this.apiService.deletePurchaseOrder(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.loadData();
        if (this.selectedPO?.id === id) this.selectedPO = null;
      },
      error: (err) => this.toastService.showError('Failed to delete Purchase Order: ' + JSON.stringify(err.error))
    });
  }

  editProduct(p: any) {
    this.newProduct = { ...p };
    this.selectedProduct = p;
  }

  onCostOrGstChange() {
    const cost = parseFloat(this.newProduct.price as any) || 0;
    const gst = parseFloat(this.newProduct.gst_percent as any) || 0;
    if (cost > 0) {
      this.newProduct.mrp = Number((cost + (cost * gst / 100)).toFixed(2));
    }
  }

  async deleteProduct(id: number) {
    if (!await this.confirmDialog.ask('Are you sure you want to delete this product?')) return;
    this.apiService.deleteProduct(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.loadData();
        if (this.selectedProduct?.id === id) this.selectedProduct = null;
      },
      error: (err) => this.toastService.showError('Failed to delete: ' + JSON.stringify(err.error))
    });
  }

  addProductLot() {
    if (!this.selectedProduct || this.isSaving) return;
    this.isSaving = true;
    const lotPayload = { ...this.newLot, product: this.selectedProduct.id };
    if (!lotPayload.expiry_date) lotPayload.expiry_date = null;
    this.apiService.createProductLot(lotPayload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.loadData(); // Re-fetches products to update selectedProduct's lots
        this.newLot = { lot_number: '', net_price: 0, mrp: 0, expiry_date: '' };
        this.isSaving = false;
        this.toastService.showSuccess('Lot added successfully');
      },
      error: (err) => {
          this.toastService.showError('Failed to add lot: ' + JSON.stringify(err.error));
          this.isSaving = false;
      }
    });
  }

  async deleteLot(id: number) {
    if (!await this.confirmDialog.ask('Delete this lot?')) return;
    this.apiService.deleteProductLot(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => this.loadData(),
      error: (err) => this.toastService.showError('Failed to delete lot: ' + JSON.stringify(err.error))
    });
  }

  addProduct() {
    if (this.isSaving) return;
    const payload = { ...this.newProduct };
    if (this.selectedCenterId) {
      payload.center = Number(this.selectedCenterId);
    } else {
      payload.create_all_centers = true;
    }

    this.isSaving = true;
    if (this.newProduct.id) {
      const updateAllCenters = !this.selectedCenterId;
      this.apiService.updateProduct(this.newProduct.id, payload, updateAllCenters).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newProduct = { product_id_str: '', product_code: '', name: '', brand: '', category: '', sub_category: '', vendor_name: '', is_active: true, price: null, mrp: null, gst_percent: 0, barcode: '', sac_code: '', reorder_level: 0, reorder_quantity: 0 };
          this.isSaving = false;
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to update product: ' + JSON.stringify(err.error));
        }
      });
    } else {
      this.apiService.createProduct(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newProduct = { product_id_str: '', product_code: '', name: '', brand: '', category: '', sub_category: '', vendor_name: '', is_active: true, price: null, mrp: null, gst_percent: 0, barcode: '', sac_code: '', reorder_level: 0, reorder_quantity: 0 };
          this.isSaving = false;
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to create product: ' + JSON.stringify(err.error));
        }
      });
    }
  }

  get filteredVendors() {
    if (this.showMappedVendorsOnly) {
      return this.vendors.filter(v => v.mapped_products && v.mapped_products.length > 0);
    }
    return this.vendors;
  }

  editVendor(v: any) {
    this.newVendor = { ...v, mapped_products: v.mapped_products || [] };
    this.selectedVendor = v;
  }

  toggleVendorMapping(productId: number) {
    if (!this.newVendor) return;
    if (!this.newVendor.mapped_products) this.newVendor.mapped_products = [];

    const index = this.newVendor.mapped_products.indexOf(productId);
    if (index > -1) {
      this.newVendor.mapped_products.splice(index, 1);
    } else {
      this.newVendor.mapped_products.push(productId);
    }
  }

  async deleteVendor(id: number) {
    if (!await this.confirmDialog.ask('Are you sure you want to delete this vendor?')) return;
    this.apiService.deleteVendor(id).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => this.loadData(),
      error: (err) => this.toastService.showError('Failed to delete: ' + JSON.stringify(err.error))
    });
  }

  addVendor() {
    if (this.isSaving) return;
    const payload = { ...this.newVendor };
    if (this.selectedCenterId) {
      payload.center = Number(this.selectedCenterId);
    } else if (this.centers && this.centers.length > 0) {
      payload.center = this.centers[0].id;
    } else {
      this.toastService.showError('Error: A center must be selected to add a vendor.');
      return;
    }

    this.isSaving = true;
    if (this.newVendor.id) {
      this.apiService.updateVendor(this.newVendor.id, payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newVendor = { vendor_code: '', name: '', short_name: '', phone: '', email: '', cst_number: '', pan_number: '', address: '', city: '', state: '', pin_code: '', mapped_products: [] };
          this.isSaving = false;
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to update vendor: ' + JSON.stringify(err.error));
        }
      });
    } else {
      this.apiService.createVendor(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
        next: () => {
          this.loadData();
          this.newVendor = { vendor_code: '', name: '', short_name: '', phone: '', email: '', cst_number: '', pan_number: '', address: '', city: '', state: '', pin_code: '', mapped_products: [] };
          this.isSaving = false;
        },
        error: (err) => {
          this.isSaving = false;
          this.toastService.showError('Failed to create vendor: ' + JSON.stringify(err.error));
        }
      });
    }
  }

  // Checkout Logic
  get checkoutFilteredProducts() {
    let filtered = this.products;
    if (this.showInStockOnly) {
      filtered = filtered.filter(p => p.current_stock > 0);
    }
    if (this.checkoutSearchTerm) {
      const s = this.checkoutSearchTerm.toLowerCase();
      filtered = filtered.filter(p => p.name.toLowerCase().includes(s) || (p.barcode && p.barcode.toLowerCase().includes(s)));
    }
    return filtered;
  }

  get checkoutPaginatedProducts() {
     const filtered = this.checkoutFilteredProducts;
     const startIndex = (this.checkoutCurrentPage - 1) * this.checkoutPageSize;
     return filtered.slice(startIndex, startIndex + this.checkoutPageSize);
  }

  get checkoutTotalPages() {
     return Math.max(1, Math.ceil(this.checkoutFilteredProducts.length / this.checkoutPageSize));
  }

  nextCheckoutPage() {
    if (this.checkoutCurrentPage < this.checkoutTotalPages) {
      this.checkoutCurrentPage++;
    }
  }

  prevCheckoutPage() {
    if (this.checkoutCurrentPage > 1) {
      this.checkoutCurrentPage--;
    }
  }

  onCheckoutSearch() {
    this.checkoutCurrentPage = 1;
  }

  processCheckout(product: any) {
    if (this.isSaving) return;
    if (!this.selectedCenterId) {
      this.toastService.showError('Please select a specific center to perform checkout, rather than All Locations.');
      return;
    }
    const qty = this.checkoutQuantities[product.id];
    if (!qty || qty <= 0) return;
    if (qty > product.current_stock) {
      this.toastService.showError(`Error: You cannot checkout more items (${qty}) than currently in stock (${product.current_stock}).`);
      return;
    }

    this.isSaving = true;
    const payload = {
      center_id: this.selectedCenterId,
      items: [{ product_id: product.id, quantity: qty }]
    };

    this.apiService.inventoryCheckout(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: () => {
        this.checkoutQuantities[product.id] = 0; // reset
        this.isSaving = false;
        this.loadData();
      },
      error: (err) => {
        this.isSaving = false;
        this.toastService.showError('Checkout failed: ' + JSON.stringify(err.error));
      }
    });
  }

  isDownloadingVendorTemplate = false;

  downloadVendorTemplate() {
    if (this.isDownloadingVendorTemplate) return;
    this.isDownloadingVendorTemplate = true;
    this.apiService.downloadVendorsTemplate().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (blob: any) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'vendors_import_template.xlsx';
        a.click();
        window.URL.revokeObjectURL(url);
        this.isDownloadingVendorTemplate = false;
      },
      error: (err: any) => {
        this.toastService.showError('Failed to download template.');
        this.isDownloadingVendorTemplate = false;
      }
    });
  }

  uploadVendors(event: any) {
    const file = event.target.files[0];
    if (!file) return;

    this.isSaving = true;
    this.apiService.uploadFile('inventory/api/vendors/bulk_upload/', file, this.selectedCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.isSaving = false;
        this.toastService.showSuccess('Vendors uploaded successfully.');
        this.loadData();
        event.target.value = '';
      },
      error: (e) => {
        this.isSaving = false;
        const errMsg = e.error?.error || e.error?.detail || e.message || 'Unknown error';
        this.toastService.showError('Failed to upload: ' + errMsg);
        event.target.value = '';
      }
    });
  }

  // --- Purchase Orders ---Logic
  // Audit Logic
  get auditFilteredProducts() {
    let filtered = this.products;
    if (this.auditSearchTerm) {
      const s = this.auditSearchTerm.toLowerCase();
      filtered = filtered.filter(p => p.name.toLowerCase().includes(s) || (p.barcode && p.barcode.toLowerCase().includes(s)));
    }
    return filtered;
  }

  get auditPaginatedProducts() {
     const filtered = this.auditFilteredProducts;
     const startIndex = (this.auditCurrentPage - 1) * this.auditPageSize;
     return filtered.slice(startIndex, startIndex + this.auditPageSize);
  }

  get auditTotalPages() {
     return Math.max(1, Math.ceil(this.auditFilteredProducts.length / this.auditPageSize));
  }

  nextAuditPage() {
    if (this.auditCurrentPage < this.auditTotalPages) {
      this.auditCurrentPage++;
    }
  }

  prevAuditPage() {
    if (this.auditCurrentPage > 1) {
      this.auditCurrentPage--;
    }
  }

  onAuditSearch() {
    this.auditCurrentPage = 1;
  }

  processAudit() {
    if (this.isSaving) return;
    if (!this.selectedCenterId) {
      this.toastService.showError('Please select a specific center to perform an audit.');
      return;
    }
    const items = [];
    for (const [productId, qty] of Object.entries(this.auditQuantities)) {
      if (qty !== null && qty !== undefined && String(qty).trim() !== '') { // allow 0
        items.push({ product_id: Number(productId), quantity: Number(qty) });
      }
    }

    if (items.length === 0) {
      this.toastService.showError("No quantities entered for audit.");
      return;
    }

    this.isSaving = true;
    const payload = {
      center_id: this.selectedCenterId,
      items: items
    };

    this.apiService.inventoryAudit(payload).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.toastService.showSuccess(res.message || "Audit completed successfully.");
        this.auditQuantities = {};
        this.isSaving = false;
        this.cdr.detectChanges();
        this.loadData();
      },
      error: (err) => {
        this.isSaving = false;
        this.cdr.detectChanges();
        this.toastService.showError('Audit failed: ' + JSON.stringify(err.error));
      }
    });
  }

  // Stock History Logic
  generateStockHistoryDates() {
    this.stockHistoryDates = [];
    const today = new Date();
    for (let i = 0; i < 20; i++) {
      const d = new Date();
      d.setDate(today.getDate() - i);
      const year = d.getFullYear();
      const month = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      this.stockHistoryDates.push(`${year}-${month}-${day}`);
    }
  }

  formatDisplayDate(dateStr: string): string {
    const d = new Date(dateStr);
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    return `${String(d.getDate()).padStart(2, '0')}-${months[d.getMonth()]}-${d.getFullYear()}`;
  }

  downloadStockHistoryExcel(dateStr: string) {
    if (this.isLoadingHistory) return;
    this.isLoadingHistory = true;
    const cid = this.selectedCenterId ? this.selectedCenterId : undefined;
    this.apiService.getStockHistory(dateStr, cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data) => {
        this.generateCSV(data, dateStr);
        this.isLoadingHistory = false;
      },
      error: (err) => {
          this.toastService.showError("Failed to fetch history: " + JSON.stringify(err.error));
          this.isLoadingHistory = false;
      }
    });
  }

  viewStockHistoryForDate(dateStr: string) {
    this.selectedHistoryDate = dateStr;
    this.isLoadingHistory = true;
    this.selectedHistoryRecords = [];
    const cid = this.selectedCenterId ? this.selectedCenterId : undefined;
    this.apiService.getStockHistory(dateStr, cid).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (data) => {
        this.selectedHistoryRecords = data;
        this.isLoadingHistory = false;
        this.cdr.detectChanges();
      },
      error: (err) => {
        this.toastService.showError("Failed to fetch history: " + JSON.stringify(err.error));
        this.isLoadingHistory = false;
        this.cdr.detectChanges();
      }
    });
  }

  generateCSV(data: any[], dateStr: string) {
    if (data.length === 0) {
      this.toastService.showError("No data available for this date.");
      return;
    }
    const headers = ['Product', 'Brand', 'Type', 'Price', 'Count', 'Value'];
    const rows = data.map((item: any) => {
      const val = (parseFloat(item.price) || 0) * (item.historical_stock || 0);
      return [
        item.name || '',
        item.brand || '',
        item.category || '',
        item.price || 0,
        item.historical_stock || 0,
        val.toFixed(2)
      ];
    });
    this.csvService.exportToCsv(`Stock_History_${dateStr}.csv`, headers, rows);
  }

  exportLowStockExcel() {
    const lowStockItems = this.products.filter(p => p.current_stock <= p.reorder_level);
    if (lowStockItems.length === 0) {
      this.toastService.showError("No low stock items to export.");
      return;
    }
    const headers = ['Product Name', 'Brand', 'Current Stock', 'Reorder Level', 'Deficit', 'Vendor Name', 'Vendor Phone'];
    const rows = lowStockItems.map(p => {
      const deficit = (p.reorder_level || 0) - (p.current_stock || 0);
      // Attempt to find mapped vendor for info (optional, helps with reordering)
      const vendor = this.vendors.find(v => v.mapped_products?.includes(p.id));
      return [
        p.name || '',
        p.brand || '',
        p.current_stock || 0,
        p.reorder_level || 0,
        deficit,
        vendor ? vendor.name : 'Unmapped',
        vendor ? vendor.phone : ''
      ];
    });
    this.csvService.exportToCsv('Low_Stock_Alerts.csv', headers, rows);
  }

  get deliveredPurchaseOrders() {
    return this.purchaseOrders.filter(po => po.status === 'Delivered');
  }


  uploadProducts(event: any) {
    const file = event.target.files[0];
    if (!file) return;
    this.isSaving = true;

    // Bulk upload is center-aware — passes center_id if a center is selected, or null for ALL centers
    this.apiService.uploadFile('inventory/api/products/bulk_upload/', file, this.selectedCenterId).pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (res) => {
        this.isSaving = false;
        const msg = res.message || 'File uploaded successfully';
        // Say which branches the rows went to. The server reports it;
        // every caller used to discard it.
        const where = this.apiService.describeImportTargets(res);
        const full = where ? `${msg} ${where}` : msg;
        this.toastService.showSuccess(full);
        if (res.warnings && res.warnings.length > 0) {
          console.warn('Bulk upload warnings:', res.warnings);
        }
        // Reload data — if on All Locations, show all; otherwise show current center
        this.loadData();
        event.target.value = '';
      },
      error: (e) => {
        this.isSaving = false;
        const errMsg = e.error?.error || e.error?.detail || e.message || 'Unknown error';
        this.toastService.showError('Failed to upload: ' + errMsg);
        event.target.value = '';
      }
    });
  }

  isDownloadingTemplate = false;

  downloadTemplate() {
    if (this.isDownloadingTemplate) return;
    this.isDownloadingTemplate = true;
    this.apiService.downloadProductsTemplate().pipe(takeUntilDestroyed(this.destroyRef)).subscribe({
      next: (blob: any) => {
        const url = window.URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = 'products_import_template.xlsx';
        a.click();
        window.URL.revokeObjectURL(url);
        this.isDownloadingTemplate = false;
      },
      error: (err: any) => {
        this.toastService.showError('Failed to download template.');
        this.isDownloadingTemplate = false;
      }
    });
  }

  exportExcel() {
    const headers = [
      'Product Code', 'Product Name', 'Category', 'Sub Category', 'Brand', 
      'Vendor Name', 'Default Price', 'Tax (%)', 'SAC Code', 'Barcode', 
      'Reorder Level', 'Reorder Quantity', 'Current Stock', 'Is Active'
    ];
    const rows = this.products.map(p => [
      p.product_code || p.sku || '',
      p.name || '',
      p.category || '',
      p.sub_category || '',
      p.brand || '',
      p.vendor_name || p.supplier_name || '',
      p.price || p.base_price || 0,
      p.gst_percent || 0,
      p.sac_code || '',
      p.barcode || '',
      p.reorder_level || 0,
      p.reorder_quantity || 0,
      p.current_stock || p.stock_quantity || 0,
      p.is_active !== false ? 'Yes' : 'No'
    ]);
    this.csvService.exportToCsv('Inventory_Report', headers, rows);
  }
  trackById(index: number, item: any): any {
    return item?.id ?? index;
  }

  trackByIndex(index: number, item: any): number {
    return index;
  }

}
