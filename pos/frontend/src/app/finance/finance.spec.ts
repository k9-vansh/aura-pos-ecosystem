import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';

import { FinanceComponent } from './finance';

/** Finance > Manage: what the rule screens show, and what Save sends. */
describe('FinanceComponent rules', () => {
  let component: any;

  beforeEach(async () => {
    localStorage.setItem('user', JSON.stringify({ is_superuser: true, permissions: {}, centers: [] }));
    await TestBed.configureTestingModule({
      imports: [FinanceComponent],
      providers: [provideHttpClient(), provideHttpClientTesting(), provideRouter([])]
    }).compileComponents();
    component = TestBed.createComponent(FinanceComponent).componentInstance;
    component.manageViewMode = 'daily';
    component.manageDailySubTab = 'business_slabs';
  });

  afterEach(() => localStorage.removeItem('user'));

  const rule = (id: number, role: string, extra: any = {}) => ({
    id, name: 'R' + id, category: 'daily_business', applicable_role: role, is_active: true,
    effective_from: '2026-01-01', effective_to: null, center: null, tiers: [], ...extra,
  });

  it('lists a daily rule for Everyone (it was shown nowhere before)', () => {
    component.incentiveRules = [rule(1, 'lhds_uhds'), rule(2, 'mhds_beauty'), rule(3, 'all'), rule(4, 'staff')];
    expect(component.getDailyBusinessRules('lhds_uhds').map((r: any) => r.id)).toEqual([1]);
    expect(component.getDailyBusinessRules('mhds_beauty').map((r: any) => r.id)).toEqual([2]);
    expect(component.getDailyBusinessRules('other').map((r: any) => r.id)).toEqual([3, 4]);
  });

  it('is not hidden by a monthly category pill', () => {
    component.manageCategoryFilter = 'services';
    component.incentiveRules = [rule(1, 'all')];
    expect(component.getDailyBusinessRules('other').length).toBe(1);
  });

  it('lists add-on and target rules from the saved rules', () => {
    component.manageDailySubTab = 'service_addons_targets';
    component.incentiveRules = [rule(1, 'all', { category: 'service_addon' }), rule(2, 'all', { category: 'service_target' }),
                                rule(3, 'all')];
    expect(component.getServiceAddonRules().map((r: any) => r.id)).toEqual([1]);
    expect(component.getServiceTargetRules().map((r: any) => r.id)).toEqual([2]);
  });

  it('says whether a rule is in force', () => {
    expect(component.ruleStatus(rule(1, 'all'))).toBe('Active');
    expect(component.ruleStatus(rule(1, 'all', { is_active: false }))).toBe('Inactive');
    expect(component.ruleStatus(rule(1, 'all', { effective_from: '2999-01-01' }))).toBe('Starts 2999-01-01');
    expect(component.ruleStatus(rule(1, 'all', { effective_to: '2000-01-01' }))).toBe('Ended 2000-01-01');
  });

  it('names who a rule is for', () => {
    expect(component.roleLabel('all')).toBe('Everyone (managers and staff)');
    expect(component.roleLabel('lhds_uhds')).toContain('LHDS');
  });

  it('opens a new daily rule with its rows in the table, not only hidden in tiers', () => {
    component.openCreateRuleModal('daily_business');
    expect(component.currentRule.slabs.length).toBe(component.currentRule.tiers.length);
    expect(component.currentRule.slabs[0]).toEqual({ min_amount: 8000, type: 'flat', incentive_amount: 350 });
    component.openCreateDailyBusinessModal('mhds_beauty');
    expect(component.currentRule.slabs[0].min_amount).toBe(6000);
  });

  it('replaces the rows when the category is changed, so no other category\'s rows are stored', () => {
    component.openCreateRuleModal('services');
    component.currentRule.category = 'daily_business';
    component.onCategoryChangeInModal();
    expect(component.currentRule.tiers[0].bonus_type).toBe('flat');
    expect(component.currentRule.slabs[0].type).toBe('flat');
    component.currentRule.category = 'services';
    component.onCategoryChangeInModal();
    expect(component.currentRule.tiers[0].min_multiple).toBe(5);
    expect(component.currentRule.slabs).toEqual([]);
  });

  it('refuses to save a daily, add-on or target rule that has no rows', () => {
    const alertSpy = vi.spyOn(window, 'alert').mockImplementation(() => undefined);
    component.currentRule = { name: 'Empty', category: 'daily_business', effective_from: '2026-01-01', tiers: [], slabs: [] };
    component.saveRule();
    expect(alertSpy).toHaveBeenCalledWith('Add at least a daily slab before saving.');
    alertSpy.mockRestore();
  });

  it('shows stored slab / percentage / flat rules in the screen\'s own terms, and leaves row categories alone', () => {
    expect(component.screenRuleType({ category: 'value_cards', rule_type: 'slab' }).rule_type).toBe('slabs');
    expect(component.screenRuleType({ category: 'services', rule_type: 'percentage' }).rule_type).toBe('flat_percentage');
    expect(component.screenRuleType({ category: 'products', rule_type: 'flat' }).rule_type).toBe('flat_amount');
    expect(component.screenRuleType({ category: 'services', rule_type: 'multiple' }).rule_type).toBe('multiple');
    expect(component.screenRuleType({ category: 'daily_business', rule_type: 'slab' }).rule_type).toBe('slab');
  });

  it('lists only the bill lines that earned an add-on', () => {
    const staff = { items: [{ item_name: 'Haircut', addon_amount: 0 }, { item_name: 'Fusio', addon_amount: '100.00' }] };
    expect(component.addonLines(staff).map((l: any) => l.item_name)).toEqual(['Fusio']);
    expect(component.addonLines({})).toEqual([]);
  });

  it('totals each incentive column, and the daily total on its own', () => {
    component.incentivesData = [
      { monthly_incentive: 100, daily_incentive: 30, daily_business_incentive: 10 },
      { monthly_incentive: '50.5', daily_incentive: '20' },
      {},
    ];
    expect(component.sumIncentive('monthly_incentive')).toBe(150.5);
    expect(component.sumIncentive('daily_business_incentive')).toBe(10);
    expect(component.getTotalDailyIncentive()).toBe(50);
  });
});
