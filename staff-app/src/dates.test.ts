import { describe, expect, it } from 'vitest';
import { addDays, addMonths, dayLabel, monthLabel } from './dates';

describe('dates', () => {
  it('moves by days across a month end', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
  });

  it('moves by months across a year end', () => {
    expect(addMonths('2026-12', 1)).toBe('2027-01');
    expect(addMonths('2026-01', -1)).toBe('2025-12');
  });

  it('words a day and a month', () => {
    expect(monthLabel('2026-10')).toBe('October 2026');
    expect(dayLabel('2026-10-31')).toMatch(/31 Oct/);
  });
});
