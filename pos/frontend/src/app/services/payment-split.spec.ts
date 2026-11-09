import { describe, expect, it } from 'vitest';
import { capWalletRows, giftCardProblem, normaliseCouponCode, rebalancePayments, walletLimit } from './payment-split';

describe('capWalletRows', () => {
  it('brings a cashback row down to the cashback balance and says how much was cut', () => {
    const rows = [{ method: 'Cashback Wallet', amount: 1000 }, { method: 'UPI', amount: 50 }];
    const cuts = capWalletRows(rows, 45, 240.5);
    expect(rows[0]['amount']).toBe(240.5);
    expect(cuts).toEqual([{ row: rows[0], cut: 759.5 }]);
  });

  it('uses the advance balance for an Advance row and leaves other methods alone', () => {
    const rows = [{ method: 'Advance', amount: 100 }, { method: 'Cash', amount: 5000 }];
    capWalletRows(rows, 45, 240.5);
    expect(rows.map(r => r['amount'])).toEqual([45, 5000]);
  });

  it('changes nothing when the amount is within the balance', () => {
    const rows = [{ method: 'Advance', amount: 45 }];
    expect(capWalletRows(rows, 45, 0)).toEqual([]);
    expect(rows[0]['amount']).toBe(45);
  });

  it('a wallet with nothing in it takes nothing', () => {
    const rows = [{ method: 'Cashback Wallet', amount: 10 }];
    capWalletRows(rows, 45, 0);
    expect(rows[0]['amount']).toBe(0);
    expect(walletLimit('Cash', 45, 0)).toBeNull();
  });
});

describe('rebalancePayments', () => {
  it('cash 10,000 on a 15,750 bill leaves 5,750 for the UPI row', () => {
    const rows = [{ method: 'Cash', amount: 10000, touched: true }, { method: 'UPI', amount: 0 }];
    rebalancePayments(rows, 15750);
    expect(rows[1]['amount']).toBe(5750);
    expect(rows[0]['amount']).toBe(10000);
  });

  it('never changes a row the person typed in', () => {
    const rows = [{ method: 'Cash', amount: 10000, touched: true }, { method: 'UPI', amount: 3000, touched: true }];
    rebalancePayments(rows, 15750);
    expect(rows.map(r => r['amount'])).toEqual([10000, 3000]);
  });

  it('with three rows the last untouched one takes the rest', () => {
    const rows = [{ amount: 5000, touched: true }, { amount: 2000, touched: true }, { amount: 0 }];
    rebalancePayments(rows, 15750);
    expect(rows[2]['amount']).toBe(8750);
  });

  it('never goes below zero and keeps paise exact', () => {
    const over = [{ amount: 20000, touched: true }, { amount: 5 }];
    rebalancePayments(over, 15750);
    expect(over[1]['amount']).toBe(0);
    const paise = [{ amount: 100.1, touched: true }, { amount: 0 }];
    rebalancePayments(paise, 200.3);
    expect(paise[1]['amount']).toBe(100.2);
  });

  it('a single row is left alone', () => {
    const rows = [{ amount: 100 }];
    rebalancePayments(rows, 15750);
    expect(rows[0]['amount']).toBe(100);
  });

  it('a coupon row takes its share and cash gets the rest', () => {
    const rows = [{ method: 'Cash', amount: 1050 }, { method: 'Gift Card', amount: 500, touched: true }];
    rebalancePayments(rows, 1050);
    expect(rows.map(r => r['amount'])).toEqual([550, 500]);
  });
});

describe('gift card rows', () => {
  const checked = (extra: Record<string, any> = {}) => ({
    method: 'Gift Card', amount: 500, reference: 'blk-aaaa-1111', giftStatus: 'ok',
    giftChecked: 'BLKAAAA1111', giftApplies: 500, ...extra,
  });

  it('reads a code the way the server does', () => {
    expect(normaliseCouponCode(' blk-aaaa 1111 ')).toBe('BLKAAAA1111');
    expect(normaliseCouponCode(null)).toBe('');
  });

  it('other methods and empty amounts are never a problem', () => {
    expect(giftCardProblem({ method: 'Cash', amount: 100 })).toBeNull();
    expect(giftCardProblem(checked({ amount: 0 }))).toBeNull();
  });

  it('a checked code for no more than it pays is fine', () => {
    expect(giftCardProblem(checked())).toBeNull();
    expect(giftCardProblem(checked({ amount: 300 }))).toBeNull();
  });

  it('an unchecked or edited code is refused', () => {
    expect(giftCardProblem(checked({ giftStatus: 'idle' }))).toContain('Check');
    expect(giftCardProblem(checked({ reference: 'blk-aaaa-9999' }))).toContain('Check');
    expect(giftCardProblem(checked({ reference: '' }))).toContain('Enter');
    expect(giftCardProblem(checked({ giftStatus: 'bad' }))).toContain('Check');
  });

  it('more than the coupon pays is refused', () => {
    expect(giftCardProblem(checked({ amount: 600 }))).toContain('at most ₹500.00');
  });
});
