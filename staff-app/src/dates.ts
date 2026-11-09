export function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function today(): string {
  return iso(new Date());
}

export function fromIso(s: string): Date {
  return new Date(`${s}T00:00:00`);
}

export function addDays(s: string, n: number): string {
  const d = fromIso(s);
  d.setDate(d.getDate() + n);
  return iso(d);
}

export function thisMonth(): string {
  return today().slice(0, 7);
}

export function addMonths(m: string, n: number): string {
  const [y, mo] = m.split('-').map(Number);
  const d = new Date(y, mo - 1 + n, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

export function monthLabel(m: string): string {
  const [y, mo] = m.split('-').map(Number);
  return new Date(y, mo - 1, 1).toLocaleDateString('en-IN', { month: 'long', year: 'numeric' });
}

/** "Sat, 31 Oct" */
export function dayLabel(s: string): string {
  return fromIso(s).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' });
}
