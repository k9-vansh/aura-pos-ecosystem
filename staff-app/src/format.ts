const grouped = new Intl.NumberFormat('en-IN', { maximumFractionDigits: 0 });
const grouped2 = new Intl.NumberFormat('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** ₹1,23,456 (Indian grouping). Paise only when asked for. */
export function inr(value: number | null | undefined, paise = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—';
  const text = (paise ? grouped2 : grouped).format(Math.abs(value));
  return `${value < 0 ? '-' : ''}₹${text}`;
}

/** The money a client holds shows paise when there are any (₹380.50), else whole rupees. */
export function wallet(value: number | null | undefined): string {
  return inr(value, typeof value === 'number' && Math.abs(value % 1) > 0.004);
}

const dayFmt = new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short', year: 'numeric' });
const dayShort = new Intl.DateTimeFormat('en-IN', { day: 'numeric', month: 'short' });
const weekday = new Intl.DateTimeFormat('en-IN', { weekday: 'short' });

/** "12 Mar 2026" from '2026-03-12' or a full timestamp. */
export function day(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return Number.isNaN(d.getTime()) ? '—' : dayFmt.format(d);
}

export function dayWithWeek(iso: string): { week: string; date: string } {
  const d = new Date(`${iso.slice(0, 10)}T00:00:00`);
  return { week: weekday.format(d), date: dayShort.format(d) };
}

/** "11:30 am" from '11:30'. */
export function clock(hhmm: string): string {
  const [h, m] = hhmm.split(':').map(Number);
  if (Number.isNaN(h)) return hhmm;
  return `${h % 12 || 12}:${String(m).padStart(2, '0')} ${h < 12 ? 'am' : 'pm'}`;
}

/** Whole days from today to a date: 0 on the day itself, negative once it has passed. */
export function daysLeft(iso: string, now = new Date()): number {
  const end = new Date(`${iso}T00:00:00`);
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((end.getTime() - start.getTime()) / 86_400_000);
}

export function expiry(iso: string): string {
  const n = daysLeft(iso);
  if (n < 0) return `Expired ${day(iso)}`;
  if (n === 0) return 'Expires today';
  if (n <= 30) return `Expires in ${n} day${n === 1 ? '' : 's'}`;
  return `Valid till ${day(iso)}`;
}

/** "98732 98293" from any spelling of an Indian mobile number. */
export function phone(raw: string | null | undefined): string {
  const d = (raw ?? '').replace(/\D/g, '').slice(-10);
  return d.length === 10 ? `${d.slice(0, 5)} ${d.slice(5)}` : (raw ?? '');
}
