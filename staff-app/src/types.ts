export interface Me {
  name: string;
  first_name: string;
  designation: string;
  salon: string;
}

export interface Kind {
  key: 'services' | 'products' | 'memberships' | 'cards' | 'packages';
  count: number;
  amount: number;
}

export interface Summary {
  total: number;
  kinds: Kind[];
  redeemed: { count: number; amount: number };
}

export interface JobCard extends Summary {
  date: string;
  is_today: boolean;
  lines: { client: string; item: string; amount: number; qty: number; kind: string; redeemed: boolean; time: string; salon: string }[];
}

export interface Month extends Summary {
  month: string;
  current: boolean;
  days: { day: number; amount: number }[];
  days_worked: number;
  best_day: number | null;
  best_amount: number;
  average_day: number;
}

export interface Appointments {
  date: string;
  counts: Record<string, number>;
  rows: { id: number; appointment: number; time: string; end: string; minutes: number; client: string; service: string; status: string; salon: string; notes: string }[];
}

export interface Profile extends Me {
  phone: string;
  email: string;
  staff_code: string;
  salon_phone: string;
  address: string;
  aadhaar: string;
  joined: string | null;
  perk_points: number;
  commission: number;
  product_commission: number;
}
