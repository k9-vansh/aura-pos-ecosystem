import { TestBed } from '@angular/core/testing';
import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';

import { clearMasterCache, masterCacheInterceptor } from './master-cache.interceptor';

describe('masterCacheInterceptor', () => {
  let http: HttpClient;
  let ctl: HttpTestingController;
  const URL = 'http://api.test/inventory/api/products/?center_id=1';

  beforeEach(() => {
    clearMasterCache();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(withInterceptors([masterCacheInterceptor])), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpClient);
    ctl = TestBed.inject(HttpTestingController);
  });

  afterEach(() => ctl.verify());

  it('serves a repeat request from memory', () => {
    const seen: unknown[] = [];
    http.get(URL).subscribe(v => seen.push(v));
    ctl.expectOne(URL).flush([{ id: 1 }]);
    http.get(URL).subscribe(v => seen.push(v));
    ctl.expectNone(URL);
    expect(seen).toEqual([[{ id: 1 }], [{ id: 1 }]]);
  });

  it('never keeps the centre list, so a centre switched on or off shows on the next screen', () => {
    const centres = 'http://api.test/salon_admin/api/centers/';
    http.get(centres).subscribe();
    ctl.expectOne(centres).flush([{ id: 1, is_active: true }]);
    const seen: unknown[] = [];
    http.get(centres).subscribe(v => seen.push(v));
    ctl.expectOne(centres).flush([{ id: 1, is_active: false }]);
    expect(seen).toEqual([[{ id: 1, is_active: false }]]);
  });

  it('shares one request between callers that ask at the same time', () => {
    const seen: unknown[] = [];
    http.get(URL).subscribe(v => seen.push(v));
    http.get(URL).subscribe(v => seen.push(v));
    ctl.expectOne(URL).flush([{ id: 2 }]);
    expect(seen.length).toBe(2);
  });

  it('gives every caller its own copy of the data', () => {
    let first: any;
    http.get<any[]>(URL).subscribe(v => (first = v));
    ctl.expectOne(URL).flush([{ id: 1 }]);
    first.push({ id: 99 });
    let second: any;
    http.get<any[]>(URL).subscribe(v => (second = v));
    expect(second).toEqual([{ id: 1 }]);
  });

  it('forgets everything after a write', () => {
    http.get(URL).subscribe();
    ctl.expectOne(URL).flush([]);
    http.post('http://api.test/billing/invoices/', {}).subscribe();
    ctl.expectOne('http://api.test/billing/invoices/').flush({});
    http.get(URL).subscribe();
    ctl.expectOne(URL).flush([]);
  });

  it('does not keep a response that was requested before a write finished', () => {
    http.get(URL).subscribe();
    const slow = ctl.expectOne(URL);
    http.post('http://api.test/inventory/api/products/', {}).subscribe();
    ctl.expectOne('http://api.test/inventory/api/products/').flush({});
    slow.flush([{ id: 'old' }]);
    http.get(URL).subscribe();
    ctl.expectOne(URL).flush([{ id: 'new' }]);
  });

  it('leaves other URLs alone', () => {
    const other = 'http://api.test/clients/api/clients/?search=a';
    http.get(other).subscribe();
    ctl.expectOne(other).flush([]);
    http.get(other).subscribe();
    ctl.expectOne(other).flush([]);
  });

  it('never caches a failure', () => {
    http.get(URL).subscribe({ error: () => undefined });
    ctl.expectOne(URL).flush('nope', { status: 500, statusText: 'Server Error' });
    http.get(URL).subscribe();
    ctl.expectOne(URL).flush([]);
  });
});
