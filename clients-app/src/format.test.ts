import { describe, expect, it } from 'vitest';
import { clock, daysLeft, expiry, inr, phone, wallet } from './format';

describe('format', () => {
  it('groups rupees the Indian way', () => {
    expect(inr(123456)).toBe('₹1,23,456');
    expect(inr(-50)).toBe('-₹50');
    expect(inr(null)).toBe('—');
  });

  it('shows paise on a wallet only when there are some', () => {
    expect(wallet(380)).toBe('₹380');
    expect(wallet(380.5)).toBe('₹380.50');
    expect(wallet(0)).toBe('₹0');
  });

  it('writes the clock in 12 hours', () => {
    expect(clock('11:30')).toBe('11:30 am');
    expect(clock('00:05')).toBe('12:05 am');
    expect(clock('15:00')).toBe('3:00 pm');
  });

  it('counts the days to an expiry and words it', () => {
    const now = new Date('2026-10-31T10:00:00');
    expect(daysLeft('2026-11-02', now)).toBe(2);
    expect(daysLeft('2026-10-31', now)).toBe(0);
    expect(daysLeft('2026-10-29', now)).toBe(-2);
    expect(phone('+91 98732-98293')).toBe('98732 98293');
    expect(expiry('2000-01-01')).toMatch(/^Expired/);
  });
});
