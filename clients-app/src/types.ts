export interface Me {
  name: string;
  first_name: string;
  phone: string;
  salon: string;
  email: string;
}

export interface ValueCard {
  id: number;
  number: string;
  title: string;
  balance: number;
  face_value: number;
  expires: string;
  salon: string;
}

export interface Membership {
  id: number;
  name: string;
  starts: string;
  expires: string;
  salon: string;
}

export interface Package {
  id: number;
  name: string;
  expires: string;
  salon: string;
  services: { name: string; left: number }[];
}

export interface Home extends Me {
  advance: number;
  cashback: number;
  value_cards: ValueCard[];
  value_card_total: number;
  memberships: Membership[];
  packages: Package[];
  has_pin: boolean;
}

export interface BillRow {
  id: number;
  number: string;
  date: string;
  salon: string;
  total: number;
  status: string;
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

export interface BookOptions {
  salon: string;
  date: string;
  open: string;
  close: string;
  closed_days: string[];
  max_days: number;
  services: { id: number; name: string; category: string; price: number; minutes: number }[];
  slots: string[];
}

export interface Visit {
  id: number;
  date: string;
  time: string;
  status: string;
  can_cancel: boolean;
  salon: string;
  services: { name: string; staff: string; minutes: number }[];
}

export interface Visits {
  upcoming: Visit[];
  past: Visit[];
}
