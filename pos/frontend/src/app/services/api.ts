import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { Observable, Subject, share, tap } from 'rxjs';
import { environment } from '../../environments/environment';

@Injectable({
  providedIn: 'root'
})
export class ApiService {
  private http = inject(HttpClient);
  public baseUrl = environment.apiUrl;

  /**
   * Real-time dashboard updates over a WebSocket.
   *
   * The socket opens when the first screen subscribes and closes when the
   * last one unsubscribes. It used to open in the constructor, on every page
   * including the sign-in screen, and reconnect every 5 seconds for ever
   * whether or not anything was listening.
   */
  public dashboardUpdates$: Observable<any> = new Observable<any>(subscriber => {
    // The socket authenticates with the same token as the REST API and only
    // receives the branches this user may see.
    const token = (() => { try { return localStorage.getItem('token') || ''; } catch { return ''; } })();
    const wsUrl = this.baseUrl.replace('http://', 'ws://').replace('https://', 'wss://')
      + '/ws/dashboard/?token=' + encodeURIComponent(token);
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = 5000;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      ws = new WebSocket(wsUrl);
      ws.onopen = () => { delay = 5000; };
      ws.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === 'dashboard_update') {
            subscriber.next(message);
          }
        } catch (e) {
          console.error('Failed to parse WebSocket message', e);
        }
      };
      ws.onclose = () => {
        if (stopped) return;
        // Back off up to a minute so a missing socket server is not hammered.
        retryTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 60000);
      };
    };
    connect();

    return () => {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }).pipe(share());


  /**
   * The signed-in user's private live channel: bell notifications and approval
   * decisions. Same life cycle as the dashboard socket - opens with the first
   * subscriber, closes with the last - and retries with backoff.
   */
  public userEvents$: Observable<any> = new Observable<any>(subscriber => {
    const token = (() => { try { return localStorage.getItem('token') || ''; } catch { return ''; } })();
    if (!token) { return undefined; }
    const wsUrl = this.baseUrl.replace('http://', 'ws://').replace('https://', 'wss://')
      + '/ws/user/?token=' + encodeURIComponent(token);
    let ws: WebSocket | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let delay = 5000;
    let stopped = false;

    const connect = () => {
      if (stopped) return;
      try { ws = new WebSocket(wsUrl); } catch { return; }
      ws.onopen = () => { delay = 5000; };
      ws.onmessage = (event) => {
        try { subscriber.next(JSON.parse(event.data)); } catch { /* ignore a malformed frame */ }
      };
      ws.onclose = (event) => {
        if (stopped || event.code === 4001) return;       // 4001: not signed in
        retryTimer = setTimeout(connect, delay);
        delay = Math.min(delay * 2, 60000);
      };
    };
    connect();

    return () => {
      stopped = true;
      if (retryTimer) clearTimeout(retryTimer);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }).pipe(share());

  // Generic methods
  get(path: string, params?: Record<string, any>, options?: any): Observable<any> {
    let url = `${this.baseUrl}/${path}`;
    if (params) {
      const queryParts: string[] = [];
      Object.entries(params).forEach(([k, v]) => {
        if (v !== undefined && v !== null && v !== '') {
          queryParts.push(`${encodeURIComponent(k)}=${encodeURIComponent(v)}`);
        }
      });
      if (queryParts.length > 0) {
        url += (url.includes('?') ? '&' : '?') + queryParts.join('&');
      }
    }
    return this.http.get<any>(url, options);
  }

  post(path: string, body: any, options?: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/${path}`, body, options);
  }

  put(path: string, body: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/${path}`, body);
  }

  patch(path: string, body: any): Observable<any> {
    return this.http.patch<any>(`${this.baseUrl}/${path}`, body);
  }

  delete(path: string): Observable<any> {
    return this.http.delete<any>(`${this.baseUrl}/${path}`);
  }

  // Chat
  getUnreadChatCount(): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/accounts/api/chat/unread/`);
  }

  // Centers
  getCenters(withRevenue = false, showInactive = false): Observable<any[]> {
    let url = `${this.baseUrl}/salon_admin/api/centers/?`;
    if (withRevenue) url += '&with_revenue=true';
    if (showInactive) url += '&show_inactive=true';
    return this.http.get<any[]>(url);
  }

  /**
   * Fires after a centre is added, changed, activated or deactivated, so every part of the
   * app that holds a list of centres (the Admin location drop-down) can reload it at once
   * instead of showing the old list until the page is refreshed.
   */
  readonly centersChanged = new Subject<void>();

  createCenter(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/salon_admin/api/centers/`, data)
      .pipe(tap(() => this.centersChanged.next()));
  }

  updateCenter(id: number, data: any): Observable<any> {
    return this.http.patch<any>(`${this.baseUrl}/salon_admin/api/centers/${id}/`, data)
      .pipe(tap(() => this.centersChanged.next()));
  }

  bulkImportCenters(file: File): Observable<any> {
    const formData = new FormData();
    formData.append('file', file);
    return this.http.post<any>(`${this.baseUrl}/salon_admin/api/centers/bulk-import/`, formData)
      .pipe(tap(() => this.centersChanged.next()));
  }

  downloadCentersTemplate(): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/salon_admin/api/centers/bulk-import-template/`, { responseType: 'blob' });
  }

  // Users
  getUsers(): Observable<any[]> {
    return this.http.get<any[]>(`${this.baseUrl}/accounts/api/users/`);
  }

  getClients(q?: string, centerId?: number, page?: number): Observable<any> {
    let url = `${this.baseUrl}/clients/api/clients/`;
    if (q) url += `${url.includes('?') ? '&' : '?'}q=${encodeURIComponent(q)}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (page) url += `${url.includes('?') ? '&' : '?'}page=${page}`;
    return this.http.get<any>(url);
  }

  createClient(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/clients/api/clients/`, data);
  }

  updateClient(id: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/clients/api/clients/${id}/`, data);
  }

  /** Records that share a phone number with another client (left by the phone clean-up). */
  getClientDuplicates(): Observable<any[]> {
    return this.http.get<any[]>(`${this.baseUrl}/clients/api/clients/duplicates/`);
  }

  /** Move everything of client `fromId` onto `keepId` and archive `fromId`. */
  mergeClient(keepId: number, fromId: number): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/clients/api/clients/${keepId}/merge/`, { from_id: fromId });
  }

  getClientProfile(id: number): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/clients/api/clients/${id}/profile/`);
  }

  createUser(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/accounts/api/users/`, data);
  }

  updateUser(id: number, data: any): Observable<any> {
    return this.http.patch<any>(`${this.baseUrl}/accounts/api/users/${id}/`, data);
  }

  /** An administrator sets another user's password (the plain user update ignores a password). */
  resetUserPassword(id: number, newPassword: string): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/accounts/api/users/${id}/reset-password/`, { new_password: newPassword });
  }

  // Roles & Designations
  getRoles(): Observable<any[]> {
    return this.http.get<any[]>(`${this.baseUrl}/salon_admin/api/roles/`);
  }

  createRole(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/salon_admin/api/roles/`, data);
  }

  updateRole(id: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/salon_admin/api/roles/${id}/`, data);
  }

  deleteRole(id: number): Observable<any> {
    return this.http.delete<any>(`${this.baseUrl}/salon_admin/api/roles/${id}/`);
  }

  getDesignations(): Observable<any[]> {
    return this.http.get<any[]>(`${this.baseUrl}/staff/api/designations/`);
  }

  createDesignation(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/staff/api/designations/`, data);
  }

  updateDesignation(id: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/staff/api/designations/${id}/`, data);
  }

  deleteDesignation(id: number): Observable<any> {
    return this.http.delete<any>(`${this.baseUrl}/staff/api/designations/${id}/`);
  }

  // --- Inventory ---
  /** `groupByCode`: one row per product across the chosen centres, stock added up (the "All locations" view). */
  getProducts(centerId?: number, groupByCode: boolean = false): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/products/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (groupByCode) url += `${url.includes('?') ? '&' : '?'}group=code`;
    return this.http.get<any[]>(url);
  }

  getLowStockAlerts(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/low_stock/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  createProduct(product: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/products/`, product);
  }

  updateProduct(id: number, product: any, updateAllCenters: boolean = false): Observable<any> {
    let url = `${this.baseUrl}/inventory/api/products/${id}/`;
    if (updateAllCenters) url += `?update_all_centers=true`;
    return this.http.put(url, product);
  }

  deleteProduct(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/inventory/api/products/${id}/`);
  }

  downloadProductsTemplate(): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/inventory/api/products/bulk_upload_template/`, { responseType: 'blob' });
  }

  createProductLot(lot: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/lots/`, lot);
  }

  updateProductLot(id: number, lot: any): Observable<any> {
    return this.http.put(`${this.baseUrl}/inventory/api/lots/${id}/`, lot);
  }

  deleteProductLot(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/inventory/api/lots/${id}/`);
  }

  /** `groupByCode`: one row per vendor across the chosen centres (the "All locations" view). */
  getVendors(centerId?: number, groupByCode: boolean = false): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/vendors/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (groupByCode) url += `${url.includes('?') ? '&' : '?'}group=code`;
    return this.http.get<any[]>(url);
  }

  createVendor(vendor: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/vendors/`, vendor);
  }

  updateVendor(id: number, vendor: any): Observable<any> {
    return this.http.put(`${this.baseUrl}/inventory/api/vendors/${id}/`, vendor);
  }

  deleteVendor(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/inventory/api/vendors/${id}/`);
  }

  downloadVendorsTemplate(): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/inventory/api/vendors/bulk_upload_template/`, { responseType: 'blob' });
  }

  getPurchaseOrders(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/purchase-orders/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  createPurchaseOrder(po: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/purchase-orders/`, po);
  }

  updatePurchaseOrder(id: number, po: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/inventory/api/purchase-orders/${id}/`, po);
  }

  deletePurchaseOrder(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/inventory/api/purchase-orders/${id}/`);
  }

  receiveGRPO(id: number, data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/purchase-orders/${id}/receive_grpo/`, data);
  }

  getStockTransfers(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/stock_transfers/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  /** Received transfers with their bills and a summary across the centres (filters: see the server view). */
  getTransferBills(filters: { [key: string]: any }): Observable<any> {
    const params = Object.entries(filters || {})
      .filter(([, v]) => v !== '' && v !== null && v !== undefined && v !== false)
      .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v === true ? 1 : v))}`)
      .join('&');
    return this.http.get<any>(`${this.baseUrl}/inventory/api/transfer_bills/${params ? '?' + params : ''}`);
  }

  createStockTransfer(transfer: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/stock_transfers/`, transfer);
  }

  updateStockTransfer(id: number, transfer: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/inventory/api/stock_transfers/${id}/`, transfer);
  }

  /** The receiving branch signs a transfer in: counts, a note and the bill (multipart). */
  receiveStockTransfer(id: number, form: FormData): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/stock_transfers/${id}/receive/`, form);
  }

  /** Record the hand count (scanned + typed) of a transfer's goods; moves no stock. */
  verifyStockTransfer(id: number, counts: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/stock_transfers/${id}/verify/`, counts);
  }

  getStockRequests(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/stock_requests/`;
    if (centerId) url += `?center=${centerId}`;
    return this.http.get<any[]>(url);
  }

  createStockRequest(request: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/stock_requests/`, request);
  }

  updateStockRequest(id: number, request: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/inventory/api/stock_requests/${id}/`, request);
  }

  decideStockRequest(id: number, action: 'approve' | 'reject' | 'cancel'): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/stock_requests/${id}/${action}/`, {});
  }

  inventoryCheckout(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/products/checkout/`, data);
  }

  inventoryAudit(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/inventory/api/products/audit/`, data);
  }

  getStockHistory(date: string, centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/products/stock_history/?date=${date}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  getStockTransactions(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/stock-transactions/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  /** The ledger rows of one type on one day (YYYY-MM-DD): the server filters, so only those rows travel. */
  getStockTransactionsOn(transactionType: string, date: string, centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/inventory/api/stock-transactions/?transaction_type=${encodeURIComponent(transactionType)}&date=${encodeURIComponent(date)}`;
    if (centerId) url += `&center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  // --- Marketing ---
  getWhatsAppMessages(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/marketing/api/whatsapp/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  sendWhatsAppCampaign(centerId: number | 'all', message: string, confirmRepeat = false): Observable<any> {
    return this.http.post(`${this.baseUrl}/marketing/api/whatsapp/send_campaign/`,
      { center_id: centerId, message: message, ...(confirmRepeat ? { confirm_repeat: true } : {}) });
  }

  /** How many customers a broadcast would reach, before anything is sent. */
  previewWhatsAppCampaign(centerId: number | 'all', message: string): Observable<any> {
    return this.http.post(`${this.baseUrl}/marketing/api/whatsapp/campaign_preview/`, { center_id: centerId, message: message });
  }


  getPromotionUsage(startDate?: string, endDate?: string, centerId?: number | null): Observable<any[]> {
    let url = `${this.baseUrl}/marketing/api/promotions/usage_report/`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  // Marketing - Promotions
  getPromotions(centerId?: any, showInactive: boolean = false, showExpired: boolean = false) { 
    let url = `${this.baseUrl}/marketing/api/promotions/`;
    if (centerId !== undefined && centerId !== null && centerId !== 'null') url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (showExpired) url += `${url.includes('?') ? '&' : '?'}show_expired=true`;
    if (showInactive) url += `${url.includes('?') ? '&' : '?'}show_inactive=true`;
    return this.http.get<any[]>(url); 
  }
  createPromotion(data: any) { return this.http.post(`${this.baseUrl}/marketing/api/promotions/`, data); }
  updatePromotion(id: number, data: any) { return this.http.patch(`${this.baseUrl}/marketing/api/promotions/${id}/`, data); }
  togglePromotion(id: number) { return this.http.patch(`${this.baseUrl}/marketing/api/promotions/${id}/toggle-status/`, {}); }
  deletePromotion(id: number) { return this.http.delete(`${this.baseUrl}/marketing/api/promotions/${id}/`); }

  // Marketing - Promotional coupons: the brand's codes behind a coupon promotion
  private couponQuery(params: Record<string, any>): string {
    const parts = Object.keys(params)
      .filter(k => params[k] !== undefined && params[k] !== null && params[k] !== '')
      .map(k => `${k}=${encodeURIComponent(String(params[k]))}`);
    return parts.length ? `?${parts.join('&')}` : '';
  }
  getCouponCodes(promoId: number, params: { status?: string; q?: string; page?: number; page_size?: number } = {}) {
    return this.http.get<any>(`${this.baseUrl}/marketing/api/promotions/${promoId}/codes/${this.couponQuery(params)}`);
  }
  exportCouponCodes(promoId: number, params: { status?: string; q?: string } = {}) {
    return this.http.get(`${this.baseUrl}/marketing/api/promotions/${promoId}/export_codes/${this.couponQuery(params)}`,
      { responseType: 'blob' });
  }
  addCouponCodes(promoId: number, codesText: string) {
    return this.http.post<any>(`${this.baseUrl}/marketing/api/promotions/${promoId}/add_codes/`, { codes_text: codesText });
  }
  generateCouponCodes(promoId: number, quantity: number, prefix: string) {
    return this.http.post<any>(`${this.baseUrl}/marketing/api/promotions/${promoId}/generate_codes/`,
      { quantity: quantity, code_prefix: prefix });
  }
  voidCouponCode(promoId: number, codeId: number, voidIt: boolean) {
    return this.http.post<any>(`${this.baseUrl}/marketing/api/promotions/${promoId}/void_code/`,
      { code_id: codeId, void: voidIt });
  }
  /** The till's "Check" on a Gift Card code: would it pay on this bill, and how much? */
  checkGiftCard(code: string, amount: number, centerId: number | null) {
    return this.http.post<any>(`${this.baseUrl}/billing/invoices/check_coupon/`,
      { code: code, amount: amount, center: centerId },
      { headers: { 'X-Background-Request': '1' } });
  }

  // Marketing - Value Cards
  getValueCards(centerId?: any, showInactive: boolean = false) { 
    let url = `${this.baseUrl}/marketing/api/cards/`;
    if (centerId !== undefined && centerId !== null && centerId !== 'null') url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (showInactive) url += `${url.includes('?') ? '&' : '?'}show_inactive=true`;
    return this.http.get<any[]>(url); 
  }
  createValueCard(data: any) { return this.http.post(`${this.baseUrl}/marketing/api/cards/`, data); }
  updateValueCard(id: number, data: any) { return this.http.patch(`${this.baseUrl}/marketing/api/cards/${id}/`, data); }
  toggleValueCard(id: number) { return this.http.patch(`${this.baseUrl}/marketing/api/cards/${id}/toggle-status/`, {}); }
  deleteValueCard(id: number) { return this.http.delete(`${this.baseUrl}/marketing/api/cards/${id}/`); }

  // Marketing - Memberships
  getMemberships(centerId?: any, showInactive: boolean = false) { 
    let url = `${this.baseUrl}/marketing/api/memberships/`;
    if (centerId !== undefined && centerId !== null && centerId !== 'null') url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (showInactive) url += `${url.includes('?') ? '&' : '?'}show_inactive=true`;
    return this.http.get<any[]>(url); 
  }
  createMembership(data: any) { return this.http.post(`${this.baseUrl}/marketing/api/memberships/`, data); }
  updateMembership(id: number, data: any) { return this.http.patch(`${this.baseUrl}/marketing/api/memberships/${id}/`, data); }
  toggleMembership(id: number) { return this.http.patch(`${this.baseUrl}/marketing/api/memberships/${id}/toggle-status/`, {}); }
  deleteMembership(id: number) { return this.http.delete(`${this.baseUrl}/marketing/api/memberships/${id}/`); }

  // Marketing - Packages
  getPackages(centerId?: any, showInactive: boolean = false) { 
    let url = `${this.baseUrl}/marketing/api/packages/`;
    if (centerId !== undefined && centerId !== null && centerId !== 'null') url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (showInactive) url += `${url.includes('?') ? '&' : '?'}show_inactive=true`;
    return this.http.get<any[]>(url); 
  }
  createPackage(data: any) { return this.http.post(`${this.baseUrl}/marketing/api/packages/`, data); }
  updatePackage(id: number, data: any) { return this.http.patch(`${this.baseUrl}/marketing/api/packages/${id}/`, data); }
  togglePackage(id: number) { return this.http.patch(`${this.baseUrl}/marketing/api/packages/${id}/toggle-status/`, {}); }
  deletePackage(id: number) { return this.http.delete(`${this.baseUrl}/marketing/api/packages/${id}/`); }

  // Services
  getServices(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/services/api/master/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  overrideCenterService(centerId: number, serviceId: number, price: any, isActive: boolean = true): Observable<any> {
    const payload: any = { center_id: centerId, service_id: serviceId, price: price, is_active: isActive };
    return this.http.post(`${this.baseUrl}/services/api/center/override/`, payload);
  }

  createService(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/services/api/master/`, data);
  }

  updateService(id: number, data: any, centerId?: number | null): Observable<any> {
    let url = `${this.baseUrl}/services/api/master/${id}/`;
    if (centerId) url += `?center_id=${centerId}`;
    return this.http.put(url, data);
  }

  deleteService(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/services/api/master/${id}/`);
  }


  // --- Staff ---

  getStaffMembers(centerId?: number, includeInactive: boolean = false): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/members/?include_inactive=${includeInactive}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }


  // --- Finance Export ---
  exportFinance(centerId: number | null, startDate: string, endDate: string): Observable<Blob> {
    let url = `${this.baseUrl}/finance/api/export/?start_date=${startDate}&end_date=${endDate}&type=detailed`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get(url, { responseType: 'blob' });
  }

  getStaffActivityFeed(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/members/activity_feed/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  createStaffMember(staff: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/members/`, staff);
  }

  updateStaffMember(id: number, staff: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/staff/api/members/${id}/`, staff);
  }

  uploadStaffImage(id: number, formData: FormData): Observable<any> {
    return this.http.patch(`${this.baseUrl}/staff/api/members/${id}/`, formData);
  }

  deleteStaffMember(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/staff/api/members/${id}/`);
  }

  downloadStaffTemplate(): Observable<Blob> {
    return this.http.get(`${this.baseUrl}/staff/api/members/bulk_upload_template/`, { responseType: 'blob' });
  }

  getServiceLogs(staffId?: number, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/logs/`;
    if (staffId) url += `${url.includes('?') ? '&' : '?'}staff_id=${staffId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  getStaffSummary(staffId: number, startDate: string, endDate: string): Observable<any> {
    return this.http.get<any>(
      `${this.baseUrl}/staff/api/logs/staff_summary/?staff_id=${staffId}&start_date=${startDate}&end_date=${endDate}`);
  }

  createServiceLog(log: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/logs/`, log);
  }

  getServiceUsageReport(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/staff/api/reports/usage/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getStaffRevenueReport(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/staff/api/reports/revenue/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getStaffConsumptions(staffId?: number, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/consumptions/`;
    if (staffId) url += `${url.includes('?') ? '&' : '?'}staff_id=${staffId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  createStaffConsumption(log: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/consumptions/`, log);
  }

  getStaffConsumptionReport(centerId?: number, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/reports/consumption/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate && endDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}&end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  // Staff Transfers
  getStaffTransfers(staffId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/transfers/`;
    if (staffId) url += `${url.includes('?') ? '&' : '?'}staff_id=${staffId}`;
    return this.http.get<any[]>(url);
  }
  createStaffTransfer(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/transfers/`, data);
  }
  updateStaffTransfer(id: number, data: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/staff/api/transfers/${id}/`, data);
  }

  getPayrolls(staffId?: number, centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/payrolls/`;
    if (staffId) url += `${url.includes('?') ? '&' : '?'}staff_id=${staffId}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  lockPayroll(id: number): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/payrolls/${id}/lock/`, {});
  }

  markPayrollPaid(id: number): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/payrolls/${id}/mark_paid/`, {});
  }

  // Staff Tool Tracker
  getStaffTools(staffId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/tools/`;
    if (staffId) url += `${url.includes('?') ? '&' : '?'}staff_id=${staffId}`;
    return this.http.get<any[]>(url);
  }
  createStaffTool(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/staff/api/tools/`, data);
  }
  updateStaffTool(id: number, data: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/staff/api/tools/${id}/`, data);
  }

  // --- Billing ---
  getInvoices(clientId?: number, centerId?: number, status?: string): Observable<any[]> {
    let url = `${this.baseUrl}/billing/invoices/`;
    if (clientId) url += `${url.includes('?') ? '&' : '?'}client_id=${clientId}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (status) url += `${url.includes('?') ? '&' : '?'}status=${status}`;
    return this.http.get<any[]>(url);
  }

  getServiceLogsByClient(clientName: string): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/logs/?client_name=${encodeURIComponent(clientName)}`;
    return this.http.get<any[]>(url);
  }

  createInvoice(invoice: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/billing/invoices/`, invoice);
  }

  updateInvoice(id: number, invoice: any): Observable<any> {
    return this.http.put(`${this.baseUrl}/billing/invoices/${id}/`, invoice);
  }

  deleteInvoice(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/billing/invoices/${id}/`);
  }

  payInvoice(id: number, amount: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/billing/invoices/${id}/pay/`, { amount });
  }

  createAdvance(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/billing/advances/`, data);
  }

  getAdvances(clientId?: number, centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/billing/advances/`;
    if (clientId) url += `${url.includes('?') ? '&' : '?'}client_id=${clientId}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  // --- Appointments ---
  /** Branch booking hours, slot size and default duration (Admin > Settings > Appointments). */
  getAppointmentHours(centerId?: number): Observable<any> {
    const q = centerId ? `?center_id=${centerId}` : '';
    return this.http.get<any>(`${this.baseUrl}/appointments/api/appointments/hours/${q}`);
  }

  getAppointments(centerId?: number, date?: string, clientPhone?: string): Observable<any[]> {
    let url = `${this.baseUrl}/appointments/api/appointments/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (date) url += `${url.includes('?') ? '&' : '?'}date=${date}`;
    if (clientPhone) url += `${url.includes('?') ? '&' : '?'}client_phone=${clientPhone}`;
    return this.http.get<any[]>(url);
  }

  getAppointmentById(id: number): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/appointments/api/appointments/${id}/`);
  }

  createAppointment(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/appointments/api/appointments/`, data);
  }

  updateAppointment(id: number, data: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/appointments/api/appointments/${id}/`, data);
  }

  deleteAppointment(id: number): Observable<any> {
    return this.http.delete(`${this.baseUrl}/appointments/api/appointments/${id}/`);
  }

  getRevenueReport(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/staff/api/reports/revenue/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getUsageReport(startDate?: string, endDate?: string, centerId?: number): Observable<any> {
    let url = `${this.baseUrl}/staff/api/reports/usage/`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any>(url);
  }

  getCommissionReport(startDate: string, endDate: string): Observable<any[]> {
    let url = `${this.baseUrl}/staff/api/members/commission_report/?start_date=${startDate}&end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  getIncentiveReport(startDate: string, endDate: string, centerId?: number, frequency: string = 'monthly', centerIds: number[] = []): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/reports/incentive-calculation/?start_date=${startDate}&end_date=${endDate}&frequency=${frequency}`;
    if (centerIds.length) url += `&center_ids=${centerIds.join(',')}`;
    else if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  getIncentiveRules(centerId?: any, category?: string, frequency?: string): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/rules/`;
    if (centerId !== undefined && centerId !== null) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (category) url += `${url.includes('?') ? '&' : '?'}category=${category}`;
    if (frequency) url += `${url.includes('?') ? '&' : '?'}frequency=${frequency}`;
    return this.http.get<any[]>(url);
  }

  createIncentiveRule(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/finance/api/rules/`, data);
  }

  updateIncentiveRule(id: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/finance/api/rules/${id}/`, data);
  }

  duplicateIncentiveRule(id: number, targetCenterId?: number | null): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/finance/api/rules/${id}/duplicate/`, { target_center_id: targetCenterId });
  }

  deleteIncentiveRule(id: number): Observable<any> {
    return this.http.delete<any>(`${this.baseUrl}/finance/api/rules/${id}/`);
  }

  getIncentiveConfigs(centerId?: number): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/incentives/`;
    if (centerId) url += `?center_id=${centerId}`;
    return this.http.get<any[]>(url);
  }

  createIncentiveConfig(data: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/finance/api/incentives/`, data);
  }

  updateIncentiveConfig(id: number, data: any): Observable<any> {
    return this.http.put<any>(`${this.baseUrl}/finance/api/incentives/${id}/`, data);
  }

  deleteIncentiveConfig(id: number): Observable<any> {
    return this.http.delete<any>(`${this.baseUrl}/finance/api/incentives/${id}/`);
  }

  getDashboardStaff(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/salon_admin/api/dashboard/staff/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getDashboardServicesProducts(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/salon_admin/api/dashboard/services_products/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getDashboardData(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/salon_admin/api/dashboard/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getClientServiceHistory(clientId: string | number): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/clients/api/clients/${clientId}/service_history/`);
  }

  carryOverClientPerk(clientId: string | number, payload: any): Observable<any> {
    return this.http.post<any>(`${this.baseUrl}/clients/api/clients/${clientId}/carry-over/`, payload);
  }

  // --- Finance ---
  getRegisterSummary(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/finance/api/register_summary/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getPettyCashEntries(centerId?: number, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/petty-cash/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  createPettyCashEntry(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/finance/api/petty-cash/`, data);
  }

  updatePettyCashEntry(id: number, data: any): Observable<any> {
    return this.http.patch(`${this.baseUrl}/finance/api/petty-cash/${id}/`, data);
  }

  getDailyClosings(centerId?: number, date?: string, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/daily-closing/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (date) url += `${url.includes('?') ? '&' : '?'}date=${date}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  getDailyClosingOpeningBalance(centerId: number, date: string): Observable<any> {
    return this.http.get<any>(`${this.baseUrl}/finance/api/daily-closing/opening_balance/?center_id=${centerId}&date=${date}`);
  }


  submitDailyClosing(data: any): Observable<any> {
    return this.http.post(`${this.baseUrl}/finance/api/daily-closing/`, data);
  }

  getShifts(centerId?: number, status?: string): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/shifts/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (status) url += `${url.includes('?') ? '&' : '?'}status=${status}`;
    return this.http.get<any[]>(url);
  }

  openShift(centerId: number, startingFloat: number): Observable<any> {
    return this.http.post(`${this.baseUrl}/finance/api/shifts/`, { center: centerId, starting_float: startingFloat });
  }

  closeShift(shiftId: number, actualCash: number, expectedCash: number): Observable<any> {
    return this.http.post(`${this.baseUrl}/finance/api/shifts/${shiftId}/close_shift/`, { actual_cash: actualCash, expected_cash: expectedCash });
  }

  getMonthlySales(centerId?: number, startDate?: string, endDate?: string): Observable<any[]> {
    let url = `${this.baseUrl}/finance/api/monthly_sales/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any[]>(url);
  }

  getDetailedRevenues(centerId?: number, startDate?: string, endDate?: string, page: number = 1, pageSize: number = 100): Observable<any> {
    let url = `${this.baseUrl}/finance/api/detailed_revenues/?page=${page}&page_size=${pageSize}`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getFinanceRefunds(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/finance/api/refunds/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  getProcurementReport(centerId?: number, startDate?: string, endDate?: string): Observable<any> {
    let url = `${this.baseUrl}/finance/api/procurement/`;
    if (centerId) url += `${url.includes('?') ? '&' : '?'}center_id=${centerId}`;
    if (startDate) url += `${url.includes('?') ? '&' : '?'}start_date=${startDate}`;
    if (endDate) url += `${url.includes('?') ? '&' : '?'}end_date=${endDate}`;
    return this.http.get<any>(url);
  }

  // --- File Upload ---
  /**
   * Upload a spreadsheet for import.
   *
   * `centerId` null means "every branch I can write to" - which is the owner's
   * intended behaviour for catalogue data (a product or vendor row per branch,
   * independently editable afterwards). `applyToAllCenters` states that
   * intention explicitly rather than leaving the server to infer it from a
   * missing field, so the two cases can be told apart in the log.
   */
  uploadFile(endpoint: string, file: File, centerId?: number | null,
             applyToAllCenters = false): Observable<any> {
    const formData = new FormData();
    formData.append('file', file);
    if (centerId) {
      formData.append('center_id', String(centerId));
    } else if (applyToAllCenters) {
      formData.append('apply_to_all_centers', 'true');
    }
    return this.http.post(`${this.baseUrl}/${endpoint}`, formData);
  }

  /**
   * A human sentence describing where an import landed.
   *
   * The server returns `applied_to`, `branch_count` and `note`; every caller
   * showed only `message`, so an operator could not tell a one-branch import
   * from an all-branches one - and an accidental all-branches import of a price
   * list is tedious to unpick.
   */
  describeImportTargets(response: any): string {
    if (!response) {
      return '';
    }
    const branches: string[] = response.applied_to || [];
    if (!branches.length) {
      return '';
    }
    if (branches.length === 1) {
      return `Imported into ${branches[0]}.`;
    }
    return `Imported into all ${branches.length} branches: ${branches.join(', ')}. `
         + 'Each branch keeps its own copy, so changing one does not affect the others.';
  }

}
