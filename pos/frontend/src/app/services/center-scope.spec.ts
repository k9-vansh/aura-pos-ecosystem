import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';

import { CenterScope, centerScopeInterceptor } from './center-scope';

describe('centre selection on requests', () => {
  let http: HttpClient;
  let backend: HttpTestingController;
  let scope: CenterScope;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(withInterceptors([centerScopeInterceptor])), provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpClient);
    backend = TestBed.inject(HttpTestingController);
    scope = TestBed.inject(CenterScope);
  });

  afterEach(() => backend.verify());

  function sentParams(url: string, method: 'get' | 'post' = 'get'): string {
    (method === 'get' ? http.get(url) : http.post(url, {})).subscribe();
    const req = backend.expectOne(r => r.url.startsWith(url.split('?')[0]));
    req.flush({});
    return req.request.params.get('center_ids') ?? '';
  }

  it('adds the ticked centres to a report', () => {
    scope.set([3, 7]);
    expect(sentParams('http://x/finance/api/register_summary/?start_date=2026-10-01')).toBe('3,7');
    expect(sentParams('http://x/salon_admin/api/dashboard/summary/')).toBe('3,7');
    expect(sentParams('http://x/billing/invoices/?page=1')).toBe('3,7');
  });

  it('adds nothing for one centre, none, a write, or another screen\'s data', () => {
    scope.set([3]);
    expect(sentParams('http://x/finance/api/register_summary/')).toBe('');
    scope.clear();
    expect(sentParams('http://x/finance/api/register_summary/')).toBe('');
    scope.set([3, 7]);
    expect(sentParams('http://x/finance/api/petty-cash/', 'post')).toBe('');
    expect(sentParams('http://x/salon_admin/api/centers/')).toBe('');
    expect(sentParams('http://x/accounts/api/login/')).toBe('');
  });

  it('leaves a request that names one centre, or one record, alone', () => {
    scope.set([3, 7]);
    expect(sentParams('http://x/finance/api/register_summary/?center_id=5')).toBe('');
    expect(sentParams('http://x/billing/invoices/123/')).toBe('');
    expect(sentParams('http://x/inventory/api/purchase-orders/9/')).toBe('');
  });

  it('still adds them when the centre is sent empty or as "null"', () => {
    scope.set([3, 7]);
    expect(sentParams('http://x/billing/invoices/?center_id=&page=1')).toBe('3,7');
    expect(sentParams('http://x/staff/api/members/?center_id=null')).toBe('3,7');
  });

  it('keeps no centre when cleared and ignores junk', () => {
    scope.set([3, 7, 7, NaN as any]);
    expect(scope.ids).toEqual([3, 7]);
    scope.clear();
    expect(scope.ids).toEqual([]);
  });
});
