import { describe, expect, it } from 'vitest';
import { addDays, addMonths, dayLabel, iso, monthLabel, weekLabel } from './dates';
import { change, inr, inrShort, phone, targetTone, tone } from './format';

describe('money', () => {
  it('groups the Indian way', () => {
    expect(inr(1234567)).toBe('₹12,34,567');
    expect(inr(15623)).toBe('₹15,623');
    expect(inr(1234.5, true)).toBe('₹1,234.50');
    expect(inr(-500)).toBe('-₹500');
    expect(inr(null)).toBe('—');
  });
  it('shortens lakhs and crores', () => {
    expect(inrShort(908000)).toBe('₹9.08L');
    expect(inrShort(12500000)).toBe('₹1.25Cr');
    expect(inrShort(99999)).toBe('₹99,999');
    expect(inrShort(1000000)).toBe('₹10L');
  });
});

describe('changes and targets', () => {
  it('signs a change and says when there is nothing to compare', () => {
    expect(change(13.44)).toBe('+13.4%');
    expect(change(-2)).toBe('-2.0%');
    expect(change(null)).toBe('—');
    expect(tone(0.01)).toBe('flat');
    expect(tone(5)).toBe('up');
    expect(tone(-5)).toBe('down');
  });
  it('colours the share of a target', () => {
    expect(targetTone(13)).toBe('bad');
    expect(targetTone(60)).toBe('warn');
    expect(targetTone(100)).toBe('good');
    expect(targetTone(null)).toBe('none');
  });
  it('writes a mobile number in two halves', () => {
    expect(phone('+91 98732-98293')).toBe('98732 98293');
    expect(phone('')).toBe('');
  });
});

describe('dates', () => {
  it('moves by days across a month end and names the near days', () => {
    expect(addDays('2026-10-31', 1)).toBe('2026-11-01');
    expect(addDays('2026-03-01', -1)).toBe('2026-02-28');
    expect(dayLabel('2026-10-31', '2026-10-31')).toBe('Today');
    expect(dayLabel('2026-10-30', '2026-10-31')).toBe('Yesterday');
    expect(iso(new Date(2026, 0, 5))).toBe('2026-01-05');
  });
  it('moves by months and writes a week', () => {
    expect(addMonths('2026-12', 1)).toBe('2027-01');
    expect(addMonths('2026-01', -1)).toBe('2025-12');
    expect(monthLabel('2026-10')).toBe('October 2026');
    expect(weekLabel('2026-10-23', '2026-10-29')).toBe('23–29 Oct');
    expect(weekLabel('2026-10-30', '2026-11-05')).toBe('30 Oct – 5 Nov');
  });
});
