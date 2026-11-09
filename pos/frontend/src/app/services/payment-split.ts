/**
 * Splitting a bill over several payment methods.
 *
 * A row the person typed into is marked `touched` and never changed by us. The last row they have not typed
 * in yet takes up whatever is left of the bill, so Cash 10,000 on a 15,750 bill makes the UPI row 5,750 by itself.
 */
export function rebalancePayments(rows: Record<string, any>[], payable: number): void {
  if (rows.length < 2) return;
  const balancing = [...rows].reverse().find(p => !p['touched']);
  if (!balancing) return;
  const others = rows.filter(p => p !== balancing).reduce((sum, p) => sum + Number(p['amount'] || 0), 0);
  balancing['amount'] = Math.max(0, Math.round((payable - others) * 100) / 100);
}

/** The most a row can take from the client's own money: their advance or cashback balance. Null for any other method. */
export function walletLimit(method: any, advance: number, cashback: number): number | null {
  if (method === 'Advance') return Math.max(0, Number(advance) || 0);
  if (method === 'Cashback Wallet') return Math.max(0, Number(cashback) || 0);
  return null;
}

/**
 * A wallet row is never given more than the balance it draws on: the amount is brought down to the balance.
 * Returns what was cut off, row by row, so the till can say so and put the rest on another method.
 */
export function capWalletRows(rows: Record<string, any>[], advance: number, cashback: number): { row: Record<string, any>; cut: number }[] {
  const cuts: { row: Record<string, any>; cut: number }[] = [];
  for (const p of rows) {
    const limit = walletLimit(p['method'], advance, cashback);
    const amount = Number(p['amount'] || 0);
    if (limit !== null && amount > limit + 0.004) {
      p['amount'] = Math.round(limit * 100) / 100;
      cuts.push({ row: p, cut: Math.round((amount - limit) * 100) / 100 });
    }
  }
  return cuts;
}

/** The form a coupon code is compared in: capitals and digits only (the server reads it the same way). */
export function normaliseCouponCode(raw: any): string {
  return String(raw ?? '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}

/**
 * Why a Gift Card row cannot be taken yet, or null when it can.
 *
 * The code has to have been checked (and not edited since), and the amount cannot be more than the coupon
 * pays on this bill. The server checks all of this again when the bill is saved; this only keeps the
 * Complete button honest.
 */
export function giftCardProblem(row: Record<string, any>): string | null {
  if (row['method'] !== 'Gift Card' || !(Number(row['amount']) > 0)) return null;
  const code = normaliseCouponCode(row['reference']);
  if (!code) return 'Enter the coupon code.';
  if (row['giftStatus'] !== 'ok' || row['giftChecked'] !== code) return 'Check the coupon code first.';
  if (Number(row['amount']) > Number(row['giftApplies'] || 0) + 0.004) {
    return `This coupon pays at most ₹${Number(row['giftApplies']).toFixed(2)} on this bill.`;
  }
  return null;
}
