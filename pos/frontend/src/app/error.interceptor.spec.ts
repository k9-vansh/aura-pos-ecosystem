import { TestBed } from '@angular/core/testing';
import { HttpClient, provideHttpClient, withInterceptors } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';

import { errorInterceptor } from './error.interceptor';
import { ToastService } from './services/toast.service';

describe('errorInterceptor: the server turned a big report away', () => {
  const shown: string[] = [];
  let http: HttpClient;
  let backend: HttpTestingController;

  beforeEach(() => {
    shown.length = 0;
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([errorInterceptor])),
        provideHttpClientTesting(),
        { provide: ToastService, useValue: { showError: (m: string) => shown.push(m) } },
      ],
    });
    http = TestBed.inject(HttpClient);
    backend = TestBed.inject(HttpTestingController);
  });

  function fail(status: number, body: any) {
    http.get('/salon_admin/api/dashboard/summary/').subscribe({ error: () => undefined });
    backend.expectOne('/salon_admin/api/dashboard/summary/').flush(body, { status, statusText: 'x' });
  }

  it('shows the busy message as the server wrote it', () => {
    fail(503, { detail: 'Many big reports are being built right now. Please try again in a moment.', code: 'reports_busy' });
    expect(shown).toEqual(['Many big reports are being built right now. Please try again in a moment.']);
  });

  it('shows the too-big message as the server wrote it', () => {
    fail(503, { detail: 'This report is too big to build in one go. Choose a shorter period or fewer centres and try again.', code: 'report_too_big' });
    expect(shown[0]).toContain('shorter period or fewer centres');
  });

  it('keeps the generic message for any other 503', () => {
    fail(503, { detail: 'upstream said something' });
    expect(shown).toEqual(['The server is not responding. Try again in a moment.']);
  });
});
