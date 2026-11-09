import { useEffect, useRef, useState } from 'react';
import { dayLabel, today } from '../dates';
import { count, inr, inrShort } from '../format';
import { useApi } from '../hooks';
import type { Kind, Today } from '../types';
import { Donut, HatchBars, PALETTE } from '../ui/Charts';
import { Card, Hero, Milestones, Notice, Page, Stat, StaleNote, Waiting, WeekStrip } from '../ui/bits';
import { Icon } from '../ui/Icon';

export const KIND_LABELS: Record<Kind, string> = {
  services: 'Services',
  products: 'Products',
  memberships: 'Memberships',
  value_cards: 'Value cards',
  packages: 'Packages',
};
export const KIND_SHORT: Record<Kind, string> = { services: 'Serv.', products: 'Prod.', memberships: 'Memb.', value_cards: 'Cards', packages: 'Pack.' };
export const KIND_ICON: Record<Kind, { icon: string; tone: string }> = {
  services: { icon: 'scissors', tone: 'orange' },
  products: { icon: 'bag', tone: 'blue' },
  memberships: { icon: 'ticket', tone: 'violet' },
  value_cards: { icon: 'card', tone: 'green' },
  packages: { icon: 'box', tone: 'red' },
};

export function TodayScreen({ salon }: { salon: string }) {
  const [day, setDay] = useState(today());
  const [jumpedFrom, setJumpedFrom] = useState<string | null>(null);
  const jumped = useRef(false);
  const [hidden, setHidden] = useState(false);
  const { data, error, loading, stale, reload } = useApi<Today>('/today/', { center_id: salon, date: day });

  // Today has no bills yet but an earlier day does: open that day once, and say so, instead of an empty screen.
  useEffect(() => {
    if (data && !stale && !jumped.current && day === today() && data.bills === 0 && data.latest_day && data.latest_day !== day) {
      jumped.current = true;
      setJumpedFrom(day);
      setDay(data.latest_day);
    }
  }, [data, stale, day]);

  const total = data ? Object.values(data.breakdown).reduce((s, k) => s + k.amount, 0) : 0;

  return (
    <Page>
      {data ? (
        <>
          <StaleNote stale={stale} error={error} />
          <div className="grid">
            <Hero
              className="wide"
              tag={data.salon}
              title={data.is_today ? 'Your sales today' : `Sales, ${dayLabel(day)}`}
              amount={inr(data.sales_ex_tax)}
              sub={`${inr(data.sales_incl_tax)} with tax${data.last_bill ? ` · last bill ${data.last_bill}` : ''}`}
              pct={data.target_pct}
              centre={data.target_pct === null ? count(data.bills) : `${data.target_pct}%`}
              caption={data.target_pct === null ? 'bills' : 'of target'}
            />
            <div className="cols-2 wide">
              <Stat icon="invoices" tone="orange" title="Bills" value={count(data.bills)} unit="today" />
              <Stat icon="staff" tone="blue" title="Clients" value={count(data.clients)} unit={`of ${count(data.clients_all_time)}`} />
            </div>
          </div>
        </>
      ) : null}

      <WeekStrip value={day} onChange={(d) => { jumped.current = true; setJumpedFrom(null); setDay(d); }} />
      {jumpedFrom ? (
        <Notice action={<button className="link" onClick={() => { setJumpedFrom(null); setDay(jumpedFrom); }}>Show today</button>}>
          No bills yet today, so this is {dayLabel(day)}, the latest day with bills.
        </Notice>
      ) : null}
      {!data ? <Waiting error={error} stale={stale} reload={reload} loading={loading} h={360} /> : (
        <div className="grid three">
          {data.bills === 0 ? <div className="wide"><Notice>No bills {data.is_today ? 'yet today' : `on ${dayLabel(day)}`} for {data.salon}.</Notice></div> : null}

          {data.daily_target ? (
            <Card title="Target tracking" aside={<span className="badge green">{data.target_pct ?? 0}%</span>}>
              <Milestones pct={data.target_pct ?? 0} startLabel="From" endLabel="Daily target" from="₹0" to={inr(data.daily_target)} nowLabel={`${inr(data.sales_ex_tax)} so far`} />
            </Card>
          ) : null}

          <Card kind="black" title="Clients still hold" aside={<button className="round" style={{ width: 34, height: 34, background: '#1d1e23', border: 0, color: '#fff' }} aria-label={hidden ? 'Show amounts' : 'Hide amounts'} onClick={() => setHidden(!hidden)}><Icon name="eye" size={17} /></button>}>
            <div className="num" style={{ fontSize: 34, fontWeight: 800, letterSpacing: '-0.04em', marginBottom: 14 }}>{hidden ? '₹ ••••••' : inr(data.liabilities.total)}</div>
            <div className="pills">
              <span className="pillbtn">Cards <small className="num">{hidden ? '••••' : inrShort(data.liabilities.value_cards)}</small></span>
              <span className="pillbtn">Packages <small className="num">{hidden ? '••••' : inrShort(data.liabilities.packages)}</small></span>
              <span className="pillbtn">Advances <small className="num">{hidden ? '••••' : inrShort(data.liabilities.advances)}</small></span>
            </div>
          </Card>

          <Card title="Payments" aside={<span className="num">{inr(data.payments.cash + data.payments.card + data.payments.other)}</span>}>
            <Donut
              fmt={inr}
              parts={[
                { label: 'Cash', value: data.payments.cash, color: PALETTE.lime },
                { label: 'Card', value: data.payments.card, color: PALETTE.blue },
                { label: 'UPI & other', value: data.payments.other, color: PALETTE.orange },
              ]}
            />
            {data.redemptions > 0 ? <p className="report-note" style={{ marginTop: 10 }}>{inr(data.redemptions)} paid from cards, advances and gift cards is not counted.</p> : null}
          </Card>

          <Card title="What was sold" aside={<span>before tax</span>} className="wide">
            <HatchBars
              fmt={inr}
              items={(Object.keys(KIND_LABELS) as Kind[]).map((k) => ({
                label: KIND_SHORT[k],
                value: data.breakdown[k].amount,
                top: total ? `${Math.round((data.breakdown[k].amount / total) * 100)}%` : '',
                note: `${data.breakdown[k].count} sold`,
              }))}
            />
            <div style={{ marginTop: 6 }}>
              {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
                <div className="row" key={k}>
                  <div className="lead">
                    <span className={`chip ${KIND_ICON[k].tone}`}><Icon name={KIND_ICON[k].icon} size={19} /></span>
                    <div className="name"><b>{KIND_LABELS[k]}</b><small>{count(data.breakdown[k].count)} sold</small></div>
                  </div>
                  <div className="val num">{data.breakdown[k].amount > 0 ? inr(data.breakdown[k].amount) : '—'}</div>
                </div>
              ))}
            </div>
          </Card>

          <Card title="Top performers">
            {[
              { label: 'Most sales', p: data.top.revenue, text: data.top.revenue ? inr(data.top.revenue.revenue) : '—' },
              { label: 'Most items', p: data.top.items, text: data.top.items ? `${count(data.top.items.items)} items` : '—' },
            ].map((t) => (
              <div className="row" key={t.label}>
                <div className="lead">
                  <span className="initial">{t.p ? t.p.name.charAt(0).toUpperCase() : '–'}</span>
                  <div className="name"><b>{t.p ? t.p.name : 'No services yet'}</b><small>{t.label}</small></div>
                </div>
                <div className="val num">{t.text}</div>
              </div>
            ))}
          </Card>
        </div>
      )}
    </Page>
  );
}
