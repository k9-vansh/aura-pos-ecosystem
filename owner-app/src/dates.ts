const pad = (n: number) => String(n).padStart(2, '0');

/** 2026-10-31 in the device's own calendar (not UTC, which can be the day before in India's early morning). */
export function iso(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function fromIso(text: string): Date {
  const [y, m, d] = text.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

export function today(): string {
  return iso(new Date());
}

export function addDays(text: string, days: number): string {
  const d = fromIso(text);
  d.setDate(d.getDate() + days);
  return iso(d);
}

const dayFmt = new Intl.DateTimeFormat('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
const monthFmt = new Intl.DateTimeFormat('en-IN', { month: 'long', year: 'numeric' });
const shortMonthFmt = new Intl.DateTimeFormat('en-IN', { month: 'short' });

export function dayLabel(text: string, now = today()): string {
  if (text === now) return 'Today';
  if (text === addDays(now, -1)) return 'Yesterday';
  return dayFmt.format(fromIso(text));
}

export function monthKey(d: Date): string {
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

export function thisMonth(): string {
  return monthKey(new Date());
}

export function addMonths(key: string, n: number): string {
  const [y, m] = key.split('-').map(Number);
  return monthKey(new Date(y, m - 1 + n, 1));
}

export function monthLabel(key: string): string {
  const [y, m] = key.split('-').map(Number);
  return monthFmt.format(new Date(y, m - 1, 1));
}

/** "October, 2026" for the month a day falls in. */
export function monthYear(text: string): string {
  const d = fromIso(text);
  return `${new Intl.DateTimeFormat('en-IN', { month: 'long' }).format(d)}, ${d.getFullYear()}`;
}

export function monthShort(key: string): string {
  const [y, m] = key.split('-').map(Number);
  return shortMonthFmt.format(new Date(y, m - 1, 1));
}

/** "23–29 Oct" for a week that starts and ends in one month, "30 Oct – 5 Nov" otherwise. */
export function weekLabel(start: string, end: string): string {
  const s = fromIso(start);
  const e = fromIso(end);
  const sm = shortMonthFmt.format(s);
  const em = shortMonthFmt.format(e);
  return sm === em ? `${s.getDate()}–${e.getDate()} ${em}` : `${s.getDate()} ${sm} – ${e.getDate()} ${em}`;
}
