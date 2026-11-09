/**
 * Money helpers for display.
 *
 * Two problems this exists to fix.
 *
 * 1. Digit grouping was inconsistent. The dashboard's chart tooltips already
 *    used `toLocaleString('en-IN')`, giving Indian grouping - 12,34,567 - while
 *    the data labels drawn on the same chart used
 *    `replace(/\B(?=(\d{3})+(?!\d))/g, ',')`, giving Western grouping -
 *    1,234,567. The same figure appeared two different ways on one screen.
 *
 * 2. Raw arithmetic on API values produced "NaN" on screen. The day-closing
 *    review computed Expected Cash as
 *    `parseFloat(a) + parseFloat(b) - parseFloat(c)`, and any of those three
 *    fields arriving null - which they do before a closing is completed - made
 *    the whole figure render as the text "NaN" on a cash-reconciliation panel.
 *
 * `toAmount` is the counterpart of the backend's `pos_backend.money.D`: it
 * coerces anything to a finite number and never throws.
 */

/** Coerce any API value to a finite number. Never NaN, never throws. */
export function toAmount(value: unknown, fallback = 0): number {
  if (typeof value === 'number') {
    return Number.isFinite(value) ? value : fallback;
  }
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed) {
      return fallback;
    }
    // Tolerate a currency symbol and grouping separators, the way the backend's
    // spreadsheet importer does.
    const cleaned = trimmed.replace(/[₹,\s]/g, '').replace(/^Rs\.?/i, '');
    const parsed = Number(cleaned);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  if (value === null || value === undefined || typeof value === 'boolean') {
    return fallback;
  }
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

/** Sum of any mix of API values, ignoring the unusable ones. */
export function sumAmounts(...values: unknown[]): number {
  return values.reduce<number>((total, value) => total + toAmount(value), 0);
}

/**
 * Round to whole paise, away from zero on a tie.
 *
 * `Math.round` breaks ties towards +Infinity, so it rounds -0.5 to -0 while the
 * backend's ROUND_HALF_UP takes it to -1. That only shows up on negative
 * figures - refunds, credit notes, a negative cash difference - but on those it
 * is a one paise disagreement with the invoice.
 */
export function roundAmount(value: unknown, decimals = 2): number {
  const amount = toAmount(value);
  const factor = Math.pow(10, decimals);
  const scaled = amount * factor;
  // Nudge away from a binary-representation edge (e.g. 1.005 * 100 = 100.49999)
  // before deciding the tie.
  const corrected = Number(scaled.toPrecision(15));
  const rounded = corrected < 0
    ? -Math.round(-corrected)
    : Math.round(corrected);
  return rounded / factor;
}

/** Indian digit grouping, two decimals. `1234567.5` -> `"12,34,567.50"`. */
export function formatAmount(value: unknown, decimals = 2): string {
  return roundAmount(value, decimals).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

/** Indian digit grouping with a rupee sign. */
export function formatRupees(value: unknown, decimals = 2): string {
  const amount = roundAmount(value, decimals);
  const sign = amount < 0 ? '-' : '';
  return `${sign}₹${Math.abs(amount).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })}`;
}

/**
 * Compact label for a chart axis or tick, in Indian units.
 *
 * Lakh and crore, not thousands and millions: `1500000` reads as `15L`, which is
 * how the figure would be said out loud in the salon.
 */
export function formatCompact(value: unknown): string {
  const amount = toAmount(value);
  const magnitude = Math.abs(amount);
  const sign = amount < 0 ? '-' : '';

  if (magnitude >= 1_00_00_000) {
    return `${sign}${trimZero(magnitude / 1_00_00_000)}Cr`;
  }
  if (magnitude >= 1_00_000) {
    return `${sign}${trimZero(magnitude / 1_00_000)}L`;
  }
  if (magnitude >= 1_000) {
    return `${sign}${trimZero(magnitude / 1_000)}k`;
  }
  return `${sign}${trimZero(magnitude, 0)}`;
}

/** A label with no trailing `.0`: `1.5` stays `1.5`, `2.0` becomes `2`. */
function trimZero(value: number, decimals = 1): string {
  const fixed = value.toFixed(decimals);
  return fixed.replace(/\.0+$/, '');
}

/**
 * Display string for a number on a chart data label.
 *
 * Replaces `rawVal % 1 !== 0 ? rawVal.toFixed(2) : rawVal.toString()` followed
 * by a Western-grouping regex: whole numbers stay whole, fractions keep two
 * places, and the grouping matches the tooltips on the same chart.
 */
export function formatChartValue(value: unknown): string {
  const amount = toAmount(value);
  const decimals = Number.isInteger(amount) ? 0 : 2;
  return roundAmount(amount, decimals).toLocaleString('en-IN', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

// ─── Backend-exact primitives ──────────────────────────────────────────────
//
// The till used to price a cart with a different algorithm from the one the
// server uses to store the invoice, so the total on screen and the total on the
// printed bill could differ. These mirror `pos_backend/money.py` exactly, so
// `priceCart` below can reproduce `InvoiceSerializer._price_invoice`.

/** `money()` in money.py: 2dp, half away from zero. */
export function money(value: unknown): number {
  return roundAmount(value, 2);
}

/** `rupees()` in money.py: whole rupees, half away from zero. */
export function rupees(value: unknown): number {
  return roundAmount(value, 0);
}

/**
 * `split(total, 2)` in money.py: two halves that sum back to exactly `total`.
 *
 * The till computed CGST and SGST as `totalTax / 2` rounded independently, so
 * for an odd number of paise the two halves did not add up to the tax charged -
 * a paisa appeared or vanished between the screen and the invoice.
 */
export function splitHalves(total: unknown): [number, number] {
  const whole = money(total);
  const base = money(whole / 2);
  const shares: [number, number] = [base, base];
  // Drift is a whole number of paise, positive or negative.
  const steps = Math.round(money(whole - base * 2) * 100);
  const step = steps > 0 ? 0.01 : -0.01;
  for (let i = 0; i < Math.abs(steps); i++) {
    shares[i % 2] = money(shares[i % 2] + step);
  }
  return shares;
}

/** One cart line, as the till holds it. */
export interface CartLine {
  unit_price?: unknown;
  quantity?: unknown;
  discount?: unknown;
  tax_percentage?: unknown;
  is_redemption?: boolean;
  description?: string;
  name?: string;
  /** `inventory.product` lines are priced at MRP, which INCLUDES GST (F22). */
  content_type?: string;
}

/** A product's price is its MRP and already contains its GST (#27, F22). */
export function isTaxInclusiveLine(line: CartLine): boolean {
  return line.content_type === 'inventory.product';
}

/** The header amounts an invoice carries. */
export interface PricedCart {
  subtotal: number;
  discount: number;
  cgst: number;
  sgst: number;
  taxTotal: number;
  rounding: number;
  total: number;
}

/** The legacy marker the backend still accepts for a package redemption. */
export const REDEEM_MARKER = '🎁 [Redeem]';

/** True when this line is a package redemption: no value, no tax. */
export function isRedemptionLine(line: CartLine): boolean {
  if (line.is_redemption !== undefined && line.is_redemption !== null) {
    return Boolean(line.is_redemption);
  }
  return `${line.description ?? ''}${line.name ?? ''}`.includes(REDEEM_MARKER);
}

/**
 * Price a cart exactly as `InvoiceSerializer._price_invoice` does.
 *
 * The order of rounding is the whole point, and it is what the till got wrong in
 * three separate places:
 *
 *  1. the apportioned share of an invoice-level discount is rounded to paise
 *     PER LINE before it is subtracted - the till subtracted a raw fraction;
 *  2. each line's tax is rounded to paise BEFORE being added to the total - the
 *     till accumulated unrounded floats and rounded once at the end;
 *  3. CGST and SGST come from an exact split of the tax total - the till halved
 *     and rounded each half on its own.
 *
 * Individually these are paise. But the grand total is then rounded to whole
 * rupees, so near a 50 paise boundary the accumulated difference flips the
 * rupee: the customer is asked for a rupee more or less than the screen showed,
 * and the GST breakdown on the invoice does not match the one that was
 * displayed.
 *
 * `gstEnabled` mirrors `Center.gst_enabled`. The till never read it, so a branch
 * configured as non-GST displayed CGST and SGST and a tax-inclusive total while
 * the server stored zero tax - a whole-percentage error, not a rounding one.
 */
export function priceCart(
  lines: CartLine[],
  invoiceDiscount: unknown = 0,
  gstEnabled = true,
): PricedCart {
  // Per-line taxable value, each rounded to paise, exactly as the server stores
  // `InvoiceItem.total_price`.
  const taxable: number[] = [];
  const rates: number[] = [];

  for (const line of lines ?? []) {
    const quantity = Math.trunc(toAmount(line.quantity, 1)) || 1;
    const unitPrice = toAmount(line.unit_price);
    const discount = Math.max(0, toAmount(line.discount));
    const gross = unitPrice * quantity;

    if (isRedemptionLine(line)) {
      // Redeemed from a package: no value and no tax, but it still occupies a
      // line on the bill.
      taxable.push(0);
      rates.push(0);
      continue;
    }

    const rate = gstEnabled ? Math.max(0, toAmount(line.tax_percentage)) : 0;
    let lineTaxable = money(Math.max(0, gross - discount));
    if (isTaxInclusiveLine(line) && rate > 0) {
      // The MRP contains the GST: the taxable value is what is left once the
      // GST inside it is taken out, rounded exactly as the server does.
      lineTaxable = money((lineTaxable * 100) / (100 + rate));
    }
    taxable.push(lineTaxable);
    rates.push(rate);
  }

  const subtotal = money(taxable.reduce((sum, value) => sum + value, 0));

  // The invoice-level discount cannot exceed the subtotal.
  let discount = money(Math.max(0, toAmount(invoiceDiscount)));
  if (discount > subtotal) {
    discount = subtotal;
  }

  let taxTotal = 0;
  for (let i = 0; i < taxable.length; i++) {
    if (taxable[i] <= 0 || rates[i] <= 0) {
      continue;
    }
    const apportioned = subtotal > 0 && discount > 0
      ? money((discount * taxable[i]) / subtotal)
      : 0;
    const netTaxable = Math.max(0, taxable[i] - apportioned);
    taxTotal += money((netTaxable * rates[i]) / 100);
  }
  taxTotal = money(taxTotal);

  const [cgst, sgst] = taxTotal > 0 ? splitHalves(taxTotal) : [0, 0];

  const rawTotal = money(subtotal - discount + taxTotal);
  const total = rupees(rawTotal);

  return {
    subtotal,
    discount,
    cgst,
    sgst,
    taxTotal,
    rounding: money(total - rawTotal),
    total,
  };
}
