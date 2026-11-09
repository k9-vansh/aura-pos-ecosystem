import { describe, expect, it } from 'vitest';
import {
  formatAmount,
  formatChartValue,
  formatCompact,
  formatRupees,
  roundAmount,
  sumAmounts,
  toAmount,
} from './money';

describe('toAmount', () => {
  it('never yields NaN, whatever the API sent', () => {
    expect(toAmount(null)).toBe(0);
    expect(toAmount(undefined)).toBe(0);
    expect(toAmount('')).toBe(0);
    expect(toAmount('   ')).toBe(0);
    expect(toAmount('abc')).toBe(0);
    expect(toAmount(NaN)).toBe(0);
    expect(toAmount(Infinity)).toBe(0);
    expect(toAmount(true)).toBe(0);
    expect(toAmount({})).toBe(0);
    expect(toAmount([])).toBe(0);
  });

  it('reads the values the API does send', () => {
    expect(toAmount('12.50')).toBe(12.5);
    expect(toAmount(12.5)).toBe(12.5);
    expect(toAmount('-40')).toBe(-40);
  });

  it('tolerates rupee formatting', () => {
    expect(toAmount('₹1,234.50')).toBe(1234.5);
    expect(toAmount('Rs 500')).toBe(500);
    expect(toAmount('Rs.500')).toBe(500);
    expect(toAmount(' 1,00,000 ')).toBe(100000);
  });

  it('honours an explicit fallback', () => {
    expect(toAmount('', 99)).toBe(99);
    expect(toAmount(null, -1)).toBe(-1);
  });
});

describe('the Expected Cash defect', () => {
  it('does not render NaN when a closing field is null', () => {
    // The template computed
    //   parseFloat(opening) + parseFloat(system_cash) - parseFloat(expenses)
    // and any null among the three put the text "NaN" on a cash panel.
    const expected =
      toAmount(null) + toAmount('5000') - toAmount(undefined);

    expect(expected).toBe(5000);
    expect(formatAmount(expected)).toBe('5,000.00');
    expect(formatAmount(expected)).not.toContain('NaN');
  });
});

describe('sumAmounts', () => {
  it('ignores the unusable values', () => {
    expect(sumAmounts('100', null, 50, 'abc', undefined)).toBe(150);
  });

  it('is zero for no arguments', () => {
    expect(sumAmounts()).toBe(0);
  });
});

describe('roundAmount', () => {
  it('breaks ties away from zero, as the backend does', () => {
    // Math.round(-0.5) is -0, but Python's ROUND_HALF_UP gives -1, so refunds
    // and negative cash differences disagreed with the invoice by a paisa.
    expect(roundAmount(0.125, 2)).toBe(0.13);
    expect(roundAmount(-0.125, 2)).toBe(-0.13);
    expect(roundAmount(2.5, 0)).toBe(3);
    expect(roundAmount(-2.5, 0)).toBe(-3);
  });

  it('is not defeated by binary representation', () => {
    expect(roundAmount(1.005, 2)).toBe(1.01);
    expect(roundAmount(8.615, 2)).toBe(8.62);
  });

  it('coerces before rounding', () => {
    expect(roundAmount(null)).toBe(0);
    expect(roundAmount('12.345')).toBe(12.35);
  });
});

describe('formatting', () => {
  it('groups digits the Indian way', () => {
    expect(formatAmount(1234567.5)).toBe('12,34,567.50');
    expect(formatAmount(100000)).toBe('1,00,000.00');
    expect(formatAmount(999)).toBe('999.00');
    expect(formatAmount(0)).toBe('0.00');
  });

  it('keeps a minus sign outside the rupee symbol', () => {
    expect(formatRupees(1234.5)).toBe('₹1,234.50');
    expect(formatRupees(-1234.5)).toBe('-₹1,234.50');
    expect(formatRupees(null)).toBe('₹0.00');
  });

  it('abbreviates in lakh and crore, not thousands', () => {
    expect(formatCompact(1500000)).toBe('15L');
    expect(formatCompact(15000000)).toBe('1.5Cr');
    expect(formatCompact(2500)).toBe('2.5k');
    expect(formatCompact(999)).toBe('999');
    expect(formatCompact(-1500000)).toBe('-15L');
    // The axis callback it replaces rendered fifteen lakh as '1500k'.
    expect(formatCompact(1500000)).not.toBe('1500k');
  });

  it('makes chart labels agree with the tooltips beside them', () => {
    expect(formatChartValue(1234567)).toBe('12,34,567');
    expect(formatChartValue(1234567.5)).toBe('12,34,567.50');
    expect(formatChartValue(0)).toBe('0');
    expect(formatChartValue(null)).toBe('0');
    // The regex it replaces produced Western grouping.
    expect(formatChartValue(1234567)).not.toBe('1,234,567');
    expect(formatChartValue(1234567)).toBe(
      Number(1234567).toLocaleString('en-IN'));
  });
});

// ─── Till/server pricing conformance ───────────────────────────────────────

import cases from './till_pricing_cases.json';
import { money, priceCart, rupees, splitHalves, type CartLine } from './money';

/**
 * The other half of `backend/billing/tests_till_conformance.py`.
 *
 * That test asserts the SERVER produces the amounts in
 * `till_pricing_cases.json`, which it also generates. This asserts `priceCart`
 * produces the same amounts for the same carts. Both sides are pinned to one
 * file, so the till cannot drift from the invoice without a failure here.
 */
describe('the till prices a cart exactly as the server does', () => {
  it('has cases to check', () => {
    expect(cases.length).toBeGreaterThan(8);
  });

  for (const testCase of cases as any[]) {
    it(testCase.label, () => {
      const lines: CartLine[] = testCase.lines.map((line: any) => ({
        unit_price: line.unit_price,
        quantity: line.qty,
        discount: line.discount,
        tax_percentage: line.tax_percentage,
        is_redemption: line.is_redemption,
        description: line.description,
        content_type: line.ct,
      }));

      const priced = priceCart(
        lines, testCase.invoice_discount, testCase.gst_enabled);

      const expected = testCase.expected;
      expect(priced.subtotal.toFixed(2)).toBe(expected.subtotal);
      expect(priced.discount.toFixed(2)).toBe(expected.discount);
      expect(priced.cgst.toFixed(2)).toBe(expected.cgst);
      expect(priced.sgst.toFixed(2)).toBe(expected.sgst);
      expect(priced.rounding.toFixed(2)).toBe(expected.rounding);
      expect(priced.total.toFixed(2)).toBe(expected.total);
    });
  }

  it('reconciles the header for every case', () => {
    for (const testCase of cases as any[]) {
      const lines: CartLine[] = testCase.lines.map((line: any) => ({
        unit_price: line.unit_price,
        quantity: line.qty,
        discount: line.discount,
        tax_percentage: line.tax_percentage,
        is_redemption: line.is_redemption,
        description: line.description,
        content_type: line.ct,
      }));
      const p = priceCart(lines, testCase.invoice_discount, testCase.gst_enabled);
      // subtotal - discount + cgst + sgst + rounding == total, to the paisa.
      const reconciled = money(
        p.subtotal - p.discount + p.cgst + p.sgst + p.rounding);
      expect(reconciled).toBe(money(p.total));
    }
  });
});

describe('splitHalves', () => {
  it('always sums back to the whole', () => {
    for (const value of [0, 0.01, 5, 10.01, 10.03, 99.99, 216.63, 1000.005]) {
      const [a, b] = splitHalves(value);
      expect(money(a + b)).toBe(money(value));
    }
  });

  it('gives the odd paisa to the second half, as split() does', () => {
    // The server's split() distributes the remainder from index 0, and CGST is
    // index 0 - so for 10.01 the halves are 5.00 and 5.01 after rounding down.
    const [cgst, sgst] = splitHalves(10.01);
    expect(money(cgst + sgst)).toBe(10.01);
    expect(Math.abs(cgst - sgst)).toBeCloseTo(0.01, 10);
  });

  it('never produces two independently rounded halves', () => {
    // The till did `parseFloat((t/2).toFixed(2))` twice, which for 10.01 gave
    // 5.00 + 5.00 = 10.00 - a paisa lost between the screen and the invoice.
    const naive = Number.parseFloat((10.01 / 2).toFixed(2));
    expect(naive * 2).not.toBe(10.01);
    const [a, b] = splitHalves(10.01);
    expect(money(a + b)).toBe(10.01);
  });
});

describe('rupees', () => {
  it('rounds a grand total the way the server stores it', () => {
    expect(rupees(65.62)).toBe(66);
    expect(rupees(105.53)).toBe(106);
    expect(rupees(1166.66)).toBe(1167);
    expect(rupees(0.5)).toBe(1);
    expect(rupees(1.5)).toBe(2);
  });
});

describe('a non-GST branch', () => {
  it('charges no tax at all', () => {
    const lines: CartLine[] = [
      { unit_price: '500.00', quantity: 1, discount: '0', tax_percentage: '18.00' },
    ];
    const withGst = priceCart(lines, 0, true);
    const withoutGst = priceCart(lines, 0, false);

    expect(withGst.cgst).toBe(45);
    expect(withGst.total).toBe(590);
    // The till used to show the GST figures regardless, so staff collected 590
    // for a bill the server stored at 500.
    expect(withoutGst.cgst).toBe(0);
    expect(withoutGst.sgst).toBe(0);
    expect(withoutGst.total).toBe(500);
  });
});

describe('a package redemption', () => {
  it('contributes no value and no tax, by flag or by marker', () => {
    const byFlag = priceCart([
      { unit_price: '500.00', quantity: 1, discount: '0',
        tax_percentage: '18.00', is_redemption: true },
    ], 0, true);
    expect(byFlag.subtotal).toBe(0);
    expect(byFlag.total).toBe(0);

    const byMarker = priceCart([
      { unit_price: '500.00', quantity: 1, discount: '0',
        tax_percentage: '18.00', description: '🎁 [Redeem] Haircut' },
    ], 0, true);
    expect(byMarker.subtotal).toBe(0);
  });
});

describe('degenerate carts', () => {
  it('an empty cart is all zeroes, not NaN', () => {
    const priced = priceCart([], 0, true);
    expect(priced.subtotal).toBe(0);
    expect(priced.total).toBe(0);
    expect(Number.isNaN(priced.cgst)).toBe(false);
  });

  it('junk values do not produce NaN anywhere', () => {
    const priced = priceCart([
      { unit_price: null, quantity: 'abc', discount: undefined,
        tax_percentage: '' },
      { unit_price: 'x', quantity: 0, discount: '-5', tax_percentage: 'NaN' },
    ] as any, 'nonsense', true);

    for (const value of Object.values(priced)) {
      expect(Number.isFinite(value)).toBe(true);
    }
    expect(priced.total).toBe(0);
  });

  it('a line discount larger than the line is clamped, never negative', () => {
    const priced = priceCart([
      { unit_price: '100.00', quantity: 1, discount: '500.00',
        tax_percentage: '18.00' },
    ], 0, true);
    expect(priced.subtotal).toBe(0);
    expect(priced.total).toBe(0);
  });

  it('an invoice discount larger than the subtotal is capped at it', () => {
    const priced = priceCart([
      { unit_price: '500.00', quantity: 1, discount: '0',
        tax_percentage: '18.00' },
    ], 99999, true);
    expect(priced.discount).toBe(500);
    expect(priced.total).toBe(0);
  });
});
