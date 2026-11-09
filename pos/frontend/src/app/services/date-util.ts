/**
 * Local-calendar date helpers.
 *
 * The codebase had 48 places that derived a date string from `toISOString()`,
 * across 25 files. That is UTC, while the backend runs
 * TIME_ZONE='Asia/Kolkata' with USE_TZ=False - so between 00:00 and 05:30 IST
 * every "today" the frontend computed was YESTERDAY. That fed default date
 * filters, transfer start dates, tool return dates and the appointments day
 * view.
 *
 * One file already contained the correct offset arithmetic
 * (`new Date(t.getTime() - t.getTimezoneOffset() * 60000)`), applied in exactly
 * one of those 48 places. This module is that fix, once.
 *
 * Everything here works in the browser's local calendar, which is what a
 * salon operator means by "today".
 */

const MS_PER_DAY = 86_400_000;
const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * Format a Date as `YYYY-MM-DD` using its LOCAL calendar fields.
 *
 * Built from getFullYear/getMonth/getDate rather than toISOString, so it can
 * never shift the date across a timezone boundary.
 */
export function toLocalDateString(value: Date | string | null | undefined): string {
  const date = toDate(value);
  if (!date) {
    return '';
  }
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, '0');
  const day = `${date.getDate()}`.padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** Today, in the local calendar, as `YYYY-MM-DD`. */
export function todayLocal(): string {
  return toLocalDateString(new Date());
}

/** Format a Date as `HH:MM` in local time. */
export function toLocalTimeString(value: Date | string | null | undefined): string {
  const date = toDate(value);
  if (!date) {
    return '';
  }
  const hours = `${date.getHours()}`.padStart(2, '0');
  const minutes = `${date.getMinutes()}`.padStart(2, '0');
  return `${hours}:${minutes}`;
}

/**
 * Parse a `YYYY-MM-DD` string into a local-midnight Date.
 *
 * `new Date('2026-08-20')` is parsed as UTC midnight, which in a
 * negative-offset timezone renders as the 19th. This builds the date from its
 * parts instead.
 */
export function fromLocalDateString(value: string | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value).trim());
  if (!match) {
    const parsed = new Date(value);
    return Number.isNaN(parsed.getTime()) ? null : parsed;
  }
  return new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

/** Shift a `YYYY-MM-DD` string by whole days, staying in the local calendar. */
export function addDays(value: string | Date, days: number): string {
  const date = typeof value === 'string' ? fromLocalDateString(value) : toDate(value);
  if (!date) {
    return '';
  }
  return toLocalDateString(
    new Date(date.getFullYear(), date.getMonth(), date.getDate() + days),
  );
}

/** `YYYY-MM-DD` for N days before today. */
export function daysAgo(days: number): string {
  return addDays(todayLocal(), -Math.abs(days));
}

/** First day of the month containing `value` (default: today). */
export function startOfMonth(value?: string | Date): string {
  const date = value
    ? typeof value === 'string'
      ? fromLocalDateString(value)
      : value
    : new Date();
  if (!date) {
    return todayLocal();
  }
  return toLocalDateString(new Date(date.getFullYear(), date.getMonth(), 1));
}

/** Last day of the month containing `value` (default: today). */
export function endOfMonth(value?: string | Date): string {
  const date = value
    ? typeof value === 'string'
      ? fromLocalDateString(value)
      : value
    : new Date();
  if (!date) {
    return todayLocal();
  }
  return toLocalDateString(new Date(date.getFullYear(), date.getMonth() + 1, 0));
}

/** Whole days between two `YYYY-MM-DD` strings (b - a). */
export function daysBetween(a: string, b: string): number {
  const from = fromLocalDateString(a);
  const to = fromLocalDateString(b);
  if (!from || !to) {
    return 0;
  }
  return Math.round((to.getTime() - from.getTime()) / MS_PER_DAY);
}

/** True if `value` falls on today's local date. */
export function isToday(value: string | Date | null | undefined): boolean {
  if (!value) {
    return false;
  }
  const asString =
    typeof value === 'string' ? value.slice(0, 10) : toLocalDateString(value);
  return asString === todayLocal();
}

function toDate(value: Date | string | null | undefined): Date | null {
  if (!value) {
    return null;
  }
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? null : value;
  }
  const text = String(value).trim();
  const parsed = DATE_ONLY.test(text) ? fromLocalDateString(text) : new Date(text);
  return parsed && !Number.isNaN(parsed.getTime()) ? parsed : null;
}
