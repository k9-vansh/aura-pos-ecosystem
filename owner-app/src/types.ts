export type Screen = 'today' | 'month' | 'invoices' | 'staff' | 'ai_report' | 'settings';

export interface Me {
  name: string;
  phone: string;
  screens: Record<Screen, boolean>;
  can_create_report: boolean;
  can_edit_settings: boolean;
  can_see_master_pins: boolean;
  can_manage_ai_keys: boolean;
  salons: { id: number; name: string }[];
}

export interface AiProvider {
  id: string;
  label: string;
  note: string;
  where: string;
  default_model: string;
  needs_url: boolean;
  has_key: boolean;
  last4: string;
  model: string;
  base_url: string;
  primary: boolean;
}

export interface AiKeys {
  providers: AiProvider[];
  in_use: string | null;
  server_key: boolean;
  test?: { provider: string; ok: boolean; message: string };
}

export interface MasterPins {
  date: string;
  pins: { center_id: number; center: string; pin: string }[];
}

export interface Payments {
  cash: number;
  card: number;
  other: number;
  by_method: { key: string; label: string; amount: number; count: number }[];
}

export type Kind = 'services' | 'products' | 'memberships' | 'value_cards' | 'packages';
export type Breakdown = Record<Kind, { count: number; amount: number }>;

export interface Sales {
  salon: string;
  sales_ex_tax: number;
  sales_incl_tax: number;
  bills: number;
  clients: number;
  last_bill: string | null;
  payments: Payments;
  breakdown: Breakdown;
  redemptions: number;
  discounts: number;
  tips: number;
}

export interface Person {
  id: number;
  name: string;
  revenue: number;
  items: number;
}

export interface Today extends Sales {
  date: string;
  is_today: boolean;
  /** The day itself when it has bills, else the latest earlier day that has (or null when there never were any). */
  latest_day: string | null;
  daily_target: number;
  target_pct: number | null;
  clients_all_time: number;
  salon_count: number;
  liabilities: { value_cards: number; packages: number; advances: number; total: number };
  top: { revenue: Person | null; items: Person | null };
}

export interface Month extends Sales {
  month: string;
  current: boolean;
  through: string;
  target: number;
  target_pct: number | null;
  projection: number | null;
  new_clients: number;
  repeat_clients: number;
  daily: { day: number; sales: number | null; bills: number }[];
  weekday: { day: string; bills: number }[];
  retention: { month: string; pct: number | null }[];
  gender: { month: string; female: number; male: number; other: number }[];
}

export interface BillRow {
  id: number;
  time: string;
  number: string;
  client: string;
  amount: number;
  status: string;
  salon: string;
}

export interface Receipt {
  id: number;
  number: string;
  status: string;
  date: string;
  salon: string;
  client: string;
  items: { name: string; qty: number; amount: number; tax: number; redeemed: boolean }[];
  subtotal: number;
  discount: number;
  tax: number;
  rounding: number;
  total: number;
  paid: number;
  payments: { method: string; amount: number; tip: boolean }[];
}

export type Period = 'today' | 'yesterday' | 'this_month' | 'last_month';

export interface StaffData {
  salon: string;
  period: Period;
  from: string;
  to: string;
  revenues: { id: number; name: string; salon: string; services: number; products: number; other: number; total: number }[];
  usage: { id: number; name: string; salon: string; hours: number; appts: number }[];
}

export interface ReportRow {
  id: number;
  week_start: string;
  week_end: string;
}

export interface ReportList {
  salon: string;
  reports: ReportRow[];
  latest_week: { week_start: string; week_end: string; made: boolean };
}

export interface ReportNumbers {
  week: { start: string; end: string };
  sales: number;
  sales_before: number;
  sales_change_pct: number | null;
  target: number;
  target_pct: number | null;
  work_delivered: number;
  work_delivered_before: number;
  bills: number;
  bills_before: number;
  bills_change_pct: number | null;
  clients: number;
  clients_before: number;
  avg_bill: number;
  avg_bill_before: number;
  breakdown: Record<Kind, { count: number; amount: number; before: number; change_pct: number | null }>;
  payments: Payments;
  redemptions: number;
  weeks: { start: string; sales: number; target: number }[];
  rhythm: { day: string; date: string; sales: number }[];
  top_services: { name: string; count: number; revenue: number }[];
  top_staff: { by_revenue: Person[]; by_items: Person[] };
}

export interface Narrative {
  status: 'ok' | 'not_configured' | 'failed';
  headline: string;
  summary: string;
  bullets: string[];
  watch: string[];
}

export interface Report extends ReportRow {
  salon: string;
  numbers: ReportNumbers;
  narrative: Narrative;
  model: string;
  made_at: string;
}

export interface SalonSettings {
  salon: string;
  center_id: number;
  sms_numbers: string[];
  owner_emails: string[];
  accounts_emails: string[];
}
