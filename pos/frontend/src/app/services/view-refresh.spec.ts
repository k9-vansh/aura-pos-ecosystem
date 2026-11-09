import { TestBed } from '@angular/core/testing';
import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ViewRefresh, viewRefreshInterceptor } from './view-refresh';

describe('viewRefreshInterceptor', () => {
  let http: HttpClient;
  let backend: HttpTestingController;
  let marked: number;

  beforeEach(() => {
    marked = 0;
    TestBed.configureTestingModule({
      providers: [provideHttpClient(withInterceptors([viewRefreshInterceptor])), provideHttpClientTesting()],
    });
    TestBed.inject(ViewRefresh).attachRef({ injector: { get: () => ({ markForCheck: () => { marked++; } }) } } as any);
    http = TestBed.inject(HttpClient);
    backend = TestBed.inject(HttpTestingController);
  });

  it('redraws the open page when a request succeeds', () => {
    http.get('/x').subscribe();
    backend.expectOne('/x').flush({ ok: true });
    expect(marked).toBe(1);
  });

  it('redraws the open page when a request fails, so a "Saving..." button does not stay stuck', () => {
    http.get('/x').subscribe({ error: () => {} });
    backend.expectOne('/x').flush({ detail: 'no' }, { status: 400, statusText: 'Bad Request' });
    expect(marked).toBe(1);
  });

  it('does nothing when no page is open', () => {
    TestBed.inject(ViewRefresh).attachRef(null);
    http.get('/x').subscribe();
    backend.expectOne('/x').flush({});
    expect(marked).toBe(0);
  });

  it('survives a page that was destroyed', () => {
    TestBed.inject(ViewRefresh).attachRef({ injector: { get: () => ({ markForCheck: () => { throw new Error('gone'); } }) } } as any);
    http.get('/x').subscribe();
    expect(() => backend.expectOne('/x').flush({})).not.toThrow();
  });
});
