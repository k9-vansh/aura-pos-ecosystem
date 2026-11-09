import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';

import { ApiService } from './api';

/**
 * The Inventory screen's reads at a large size: the server does the folding and the filtering, so
 * only the rows the screen shows travel.
 */
describe('ApiService inventory reads', () => {
  let api: ApiService;
  let ctl: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    api = TestBed.inject(ApiService);
    ctl = TestBed.inject(HttpTestingController);
  });

  afterEach(() => ctl.verify());

  it('asks for the plain product list of one centre', () => {
    api.getProducts(7).subscribe();
    ctl.expectOne(r => r.url.endsWith('/inventory/api/products/?center_id=7')).flush([]);
  });

  it('asks the server to add the stock up per product when there is no single centre', () => {
    api.getProducts(undefined, true).subscribe();
    ctl.expectOne(r => r.url.endsWith('/inventory/api/products/?group=code')).flush([]);
    api.getProducts(3, true).subscribe();
    ctl.expectOne(r => r.url.endsWith('/inventory/api/products/?center_id=3&group=code')).flush([]);
  });

  it('asks for one row per vendor only when there is no single centre', () => {
    api.getVendors(4).subscribe();
    ctl.expectOne(r => r.url.endsWith('/inventory/api/vendors/?center_id=4')).flush([]);
    api.getVendors(undefined, true).subscribe();
    ctl.expectOne(r => r.url.endsWith('/inventory/api/vendors/?group=code')).flush([]);
  });

  it('asks the server for one kind of ledger row on one day, not the whole ledger', () => {
    api.getStockTransactionsOn('CHECKOUT', '2026-10-28', 5).subscribe();
    ctl.expectOne(r => r.url.endsWith(
      '/inventory/api/stock-transactions/?transaction_type=CHECKOUT&date=2026-10-28&center_id=5')).flush([]);
    api.getStockTransactionsOn('CHECKOUT', '2026-10-28').subscribe();
    ctl.expectOne(r => r.url.endsWith(
      '/inventory/api/stock-transactions/?transaction_type=CHECKOUT&date=2026-10-28')).flush([]);
  });
});
