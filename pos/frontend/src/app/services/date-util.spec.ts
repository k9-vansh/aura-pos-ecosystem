import { describe, expect, it } from 'vitest';
import {
  addDays,
  daysAgo,
  daysBetween,
  endOfMonth,
  fromLocalDateString,
  isToday,
  startOfMonth,
  todayLocal,
  toLocalDateString,
} from './date-util';

/**
 * M1 - the UTC date bug. `new Date().toISOString().split('T')[0]` was used in
 * 48 places; between 00:00 and 05:30 IST it returns YESTERDAY, because
 * toISOString() is UTC and the backend runs on Asia/Kolkata local time.
 */
describe('toLocalDateString', () => {
  it('uses local calendar fields, not UTC', () => {
    // 00:30 local on 21 Aug. In any timezone ahead of UTC this is still
    // 20 Aug in UTC, which is exactly the bug.
    const justAfterMidnight = new Date(2026, 7, 21, 0, 30, 0);
    expect(toLocalDateString(justAfterMidnight)).toBe('2026-08-21');
  });

  it('handles the last minute of a day', () => {
    const justBeforeMidnight = new Date(2026, 7, 20, 23, 59, 59);
    expect(toLocalDateString(justBeforeMidnight)).toBe('2026-08-20');
  });

  it('pads single-digit months and days', () => {
    expect(toLocalDateString(new Date(2026, 0, 5))).toBe('2026-01-05');
  });

  it('round-trips a date-only string unchanged', () => {
    expect(toLocalDateString('2026-08-20')).toBe('2026-08-20');
  });

  it('returns an empty string for unusable input', () => {
    expect(toLocalDateString(null)).toBe('');
    expect(toLocalDateString(undefined)).toBe('');
    expect(toLocalDateString('')).toBe('');
    expect(toLocalDateString('not a date')).toBe('');
  });

  it('never differs from the local calendar date, at any hour', () => {
    for (let hour = 0; hour < 24; hour += 1) {
      const date = new Date(2026, 7, 20, hour, 30);
      expect(toLocalDateString(date)).toBe('2026-08-20');
    }
  });
});

describe('todayLocal', () => {
  it('matches the browser local date', () => {
    const now = new Date();
    const expected = `${now.getFullYear()}-${`${now.getMonth() + 1}`.padStart(2, '0')}-${`${now.getDate()}`.padStart(2, '0')}`;
    expect(todayLocal()).toBe(expected);
  });

  it('is recomputed on each call rather than frozen', () => {
    // M1b: AdminFilterService computed `today` once in its constructor, so a
    // terminal left open overnight kept filtering to yesterday.
    expect(todayLocal()).toBe(todayLocal());
    expect(typeof todayLocal()).toBe('string');
  });
});

describe('fromLocalDateString', () => {
  it('parses to local midnight, not UTC midnight', () => {
    const parsed = fromLocalDateString('2026-08-20');
    expect(parsed).not.toBeNull();
    expect(parsed!.getFullYear()).toBe(2026);
    expect(parsed!.getMonth()).toBe(7);
    expect(parsed!.getDate()).toBe(20);
    expect(parsed!.getHours()).toBe(0);
  });

  it('returns null for junk', () => {
    expect(fromLocalDateString('')).toBeNull();
    expect(fromLocalDateString(null)).toBeNull();
  });
});

describe('addDays', () => {
  it('moves forward and back', () => {
    expect(addDays('2026-08-20', 1)).toBe('2026-08-21');
    expect(addDays('2026-08-20', -1)).toBe('2026-08-19');
  });

  it('crosses month boundaries', () => {
    expect(addDays('2026-08-31', 1)).toBe('2026-09-01');
    expect(addDays('2026-09-01', -1)).toBe('2026-08-31');
  });

  it('crosses year boundaries', () => {
    expect(addDays('2026-12-31', 1)).toBe('2027-01-01');
  });

  it('handles a leap day', () => {
    expect(addDays('2028-02-28', 1)).toBe('2028-02-29');
    expect(addDays('2028-02-29', 1)).toBe('2028-03-01');
  });

  it('handles a non-leap February', () => {
    expect(addDays('2026-02-28', 1)).toBe('2026-03-01');
  });
});

describe('daysAgo', () => {
  it('is consistent with addDays from today', () => {
    expect(daysAgo(30)).toBe(addDays(todayLocal(), -30));
  });

  it('treats a negative argument as a magnitude', () => {
    expect(daysAgo(-7)).toBe(daysAgo(7));
  });
});

describe('startOfMonth / endOfMonth', () => {
  it('finds month boundaries', () => {
    expect(startOfMonth('2026-08-20')).toBe('2026-08-01');
    expect(endOfMonth('2026-08-20')).toBe('2026-08-31');
  });

  it('handles a 30-day month', () => {
    expect(endOfMonth('2026-09-15')).toBe('2026-09-30');
  });

  it('handles February in a leap year', () => {
    expect(endOfMonth('2028-02-10')).toBe('2028-02-29');
  });

  it('handles February in a common year', () => {
    expect(endOfMonth('2026-02-10')).toBe('2026-02-28');
  });
});

describe('daysBetween', () => {
  it('counts whole days', () => {
    expect(daysBetween('2026-08-15', '2026-08-18')).toBe(3);
    expect(daysBetween('2026-08-18', '2026-08-15')).toBe(-3);
    expect(daysBetween('2026-08-18', '2026-08-18')).toBe(0);
  });

  it('is unaffected by a DST transition', () => {
    // A 23- or 25-hour day must still count as one day.
    expect(daysBetween('2026-03-28', '2026-03-30')).toBe(2);
    expect(daysBetween('2026-10-24', '2026-10-26')).toBe(2);
  });
});

describe('isToday', () => {
  it('recognises today and rejects other dates', () => {
    expect(isToday(todayLocal())).toBe(true);
    expect(isToday(daysAgo(1))).toBe(false);
    expect(isToday(null)).toBe(false);
  });

  it('accepts a full ISO timestamp', () => {
    expect(isToday(new Date())).toBe(true);
  });
});
