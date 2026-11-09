import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, expect, it, beforeEach, afterEach } from 'vitest';

import { UsageReportComponent } from './usage-report';

describe('UsageReportComponent', () => {
  let component: UsageReportComponent;
  let fixture: ComponentFixture<UsageReportComponent>;
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [UsageReportComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])],
    }).compileComponents();
    fixture = TestBed.createComponent(UsageReportComponent);
    component = fixture.componentInstance;
    http = TestBed.inject(HttpTestingController);
    fixture.componentRef.setInput('startDate', '2026-10-01');
    fixture.componentRef.setInput('endDate', '2026-10-30');
    fixture.componentRef.setInput('centerId', 7);
    fixture.detectChanges();
  });

  afterEach(() => http.match(() => true));

  const calls = () => http.match(req => req.url.includes('usage-report'));
  const find = (reqs: any[], section: string) => reqs.find(r => new URL(r.request.url, 'http://x').pathname.endsWith('/usage-report/' + section + '/'));
  const q = (r: any) => new URL(r.request.url, 'http://x').searchParams;

  it('loads the overview and, quietly, the verify badge, for the picked centre and dates', () => {
    const reqs = calls();
    const urls = reqs.map((r: any) => new URL(r.request.url, 'http://x').pathname);
    expect(urls.some(u => u.endsWith('/usage-report/overview/'))).toBe(true);
    expect(urls.some(u => u.endsWith('/usage-report/verify/'))).toBe(true);
    const overview = find(reqs, 'overview')!;
    expect(q(overview).get('center_id')).toBe('7');
    expect(q(overview).get('start_date')).toBe('2026-10-01');
    expect(q(overview).get('end_date')).toBe('2026-10-30');
  });

  it('asks the server for fresh figures after Refresh, once for each tab, and not for a filter change', () => {
    expect(q(find(calls(), 'overview')!).has('fresh')).toBe(false);          // the first load may use a kept answer
    fixture.componentRef.setInput('refresh', 0);
    fixture.detectChanges();
    calls();
    fixture.componentRef.setInput('refresh', 1);                             // the Refresh button
    fixture.detectChanges();
    const afterRefresh = calls();
    expect(q(find(afterRefresh, 'overview')!).get('fresh')).toBe('1');
    expect(q(find(afterRefresh, 'verify')!).get('fresh')).toBe('1');
    component.filtersChanged();                                              // the overview was just worked out afresh
    expect(q(find(calls(), 'overview')!).has('fresh')).toBe(false);
  });

  it('sends the marketing filters it was given, and only those', () => {
    calls();
    component.perks = ['Coupon', 'Cashback'];
    component.q = ' blinkit ';
    component.customer = 'new';
    component.status = 'running';
    component.filtersChanged();
    const overview = find(calls(), 'overview')!;
    expect(q(overview).get('perk')).toBe('Coupon,Cashback');
    expect(q(overview).get('q')).toBe('blinkit');
    expect(q(overview).get('customer')).toBe('new');
    expect(q(overview).get('status')).toBe('running');
  });

  it('reloads when a filter changes and clears them all with one button', () => {
    calls();
    component.togglePerk('Coupon');
    expect(component.perks).toEqual(['Coupon']);
    expect(component.anyFilter).toBe(true);
    component.togglePerk('Coupon');
    expect(component.perks).toEqual([]);
    component.q = 'x';
    component.status = 'ended';
    component.clearFilters();
    expect(component.anyFilter).toBe(false);
    expect([component.q, component.customer, component.status, component.perks.length]).toEqual(['', '', '', 0]);
  });

  it('says which filters a tab does not use', () => {
    calls();
    component.perks = ['Coupon'];
    component.q = 'abc';
    component.customer = 'new';
    component.section = 'cashback';
    expect(component.ignored).toEqual(['name']);          // cashback ignores the name (and state), not kind or customers
    component.section = 'verify';
    expect(component.ignored.sort()).toEqual(['kind', 'name']);
    component.section = 'whatsapp';
    expect(component.ignored.sort()).toEqual(['customer type', 'kind']);
    component.section = 'offers';
    expect(component.ignored).toEqual([]);
  });

  it('reads the dashboard\'s own reports for memberships and value cards', () => {
    calls();
    component.section = 'memberships';
    expect(component.embedded).toBe('memberships');
    component.section = 'cards';
    expect(component.embedded).toBe('value-cards');
    component.section = 'offers';
    expect(component.embedded).toBeNull();
  });

  it('shows the verify badge from what verify found', () => {
    expect(component.verifyKnown).toBe(false);
    component.data['verify'] = { summary: { failed: 3 } };
    expect(component.verifyKnown).toBe(true);
    expect(component.verifyFailed).toBe(3);
  });

  it('formats money, changes and colours the way the dashboard does', () => {
    expect(component.inr(1234567)).toBe('₹12,34,567');
    expect(component.inr(-50)).toBe('−₹50');
    expect(component.inr(12.5, 2)).toBe('₹12.50');
    expect(component.change(12.4)).toBe('▲ 12%');
    expect(component.change(-3)).toBe('▼ 3%');
    expect(component.change(null)).toBe('—');
    expect(component.tone(10)).toBe('up');
    expect(component.tone(-10)).toBe('down');
    expect(component.tone(10, true)).toBe('down');          // a cost going up is not good news
    expect(component.tone(-10, true)).toBe('up');
    expect(component.tone(0)).toBe('');
    expect(component.tone(null)).toBe('');
    expect(component.pct(33.333)).toBe('33.3%');
    expect(component.dmy('2026-10-05')).toBe('05-Oct-2026');
    expect(component.dmy('None')).toBe('—');
  });

  it('shows only the first rows of a long table until asked', () => {
    const list = Array.from({ length: 30 }, (_, i) => ({ i }));
    expect(component.rows(list, 'k', 10).length).toBe(10);
    expect(component.more(list, 'k', 10)).toBe(true);
    component.expand('k');
    expect(component.rows(list, 'k', 10).length).toBe(30);
    expect(component.more(list, 'k', 10)).toBe(false);
  });

  it('turns a failed check into a mark and a colour', () => {
    expect(component.checkTone('ok')).toBe('ok');
    expect(component.checkTone('fail')).toBe('fail');
    expect(component.checkTone('info')).toBe('info');
    expect(component.checkMark('ok')).toBe('✓');
    expect(component.checkMark('fail')).toBe('✕');
  });

  it('keeps a message when the server refuses', () => {
    const verify = find(calls(), 'overview')!;
    verify.flush({ detail: 'no' }, { status: 403, statusText: 'Forbidden' });
    expect(component.error['overview']).toContain('do not have access');
    expect(component.loading['overview']).toBe(false);
  });
});
