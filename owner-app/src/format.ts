const grouped = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const grouped2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** ₹1,23,456 (Indian grouping). Paise only when asked for. */
export function inr(value: number | null | undefined, paise = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const text = (paise ? grouped2 : grouped).format(Math.abs(value));
  return `${value < 0 ? '-' : ''}₹${text}`;
}

/** ₹9.08L, ₹1.25Cr: for tight places. Under a lakh the full figure is short enough. */
export function inrShort(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const v = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  if (v >= 1e7) return `${sign}₹${trim(v / 1e7)}Cr`;
  if (v >= 1e5) return `${sign}₹${trim(v / 1e5)}L`;
  return inr(value);
}

function trim(n: number): string {
  return n.toFixed(2).replace(/\.?0+$/, '');
}

export function count(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : grouped.format(value);
}

/** "+13.4%" / "-2.0%" / "—" when there is nothing to compare with. */
export function change(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
}

export function tone(value: number | null | undefined): 'up' | 'down' | 'flat' {
  if (value === null || value === undefined || Math.abs(value) < 0.05) return 'flat';
  return value > 0 ? 'up' : 'down';
}

/** The colour word for a share of a target: red under 60, amber under 100, green from 100. */
export function targetTone(pct: number | null | undefined): 'bad' | 'warn' | 'good' | 'none' {
  if (pct === null || pct === undefined) return 'none';
  if (pct >= 100) return 'good';
  return pct >= 60 ? 'warn' : 'bad';
}

/** "98732 98293" from any spelling of an Indian mobile number. */
export function phone(raw: string | null | undefined): string {
  const d = (raw ?? '').replace(/\D/g, '').slice(-10);
  return d.length === 10 ? `${d.slice(0, 5)} ${d.slice(5)}` : (raw ?? '');
}
