import { useState } from 'react';
import { monthLabel, monthShort, thisMonth } from '../dates';
import { count, inr, inrShort } from '../format';
import { useApi } from '../hooks';
import type { Kind, Month } from '../types';
import { BarChart, Donut, HatchBars, PALETTE, PercentLine, Spark, StackedColumns } from '../ui/Charts';
import { Card, Hero, Milestones, MonthRow, Notice, Page, StaleNote, Stat, Waiting } from '../ui/bits';
import { Icon } from '../ui/Icon';
import { KIND_ICON, KIND_LABELS } from './Today';

export function MonthScreen({ salon }: { salon: string }) {
  const [month, setMonth] = useState(thisMonth());
  const { data, error, loading, stale, reload } = useApi<Month>('/month/', { center_id: salon, month });
  const weekdayTotal = data ? data.weekday.reduce((s, w) => s + w.bills, 0) : 0;

  return (
    <Page>
      {data ? (
        <div className="grid">
          <Hero
            className="wide"
            tag={data.salon}
            title={`Sales, ${monthLabel(data.month)}`}
            amount={inr(data.sales_ex_tax)}
            sub={data.projection ? `Ends near ${inr(data.projection)} at this pace` : `${inr(data.sales_incl_tax)} with tax`}
            pct={data.target_pct}
            centre={data.target_pct === null ? count(data.bills) : `${data.target_pct}%`}
            caption={data.target_pct === null ? 'bills' : 'of target'}
          />
        </div>
      ) : null}
      <MonthRow value={month} onChange={setMonth} />
      {!data ? <Waiting error={error} stale={stale} reload={reload} loading={loading} h={360} /> : (
        <>
          <StaleNote stale={stale} error={error} />
          {data.bills === 0 ? <Notice>No bills in {monthLabel(data.month)} for {data.salon}. Try another month or choose all salons.</Notice> : null}
          <div className="grid three">
            <div className="cols-2 wide">
              <div className="stat" style={{ display: 'grid', gap: 6 }}>
                <div className="info"><small>Bills</small><b className="num">{count(data.bills)}</b></div>
                <Spark values={data.daily.map((d) => d.bills)} />
              </div>
              <div className="stat" style={{ display: 'grid', gap: 6 }}>
                <div className="info"><small>Clients</small><b className="num">{count(data.clients)}</b></div>
                <div className="meter" title="New clients out of all clients"><i style={{ width: `${data.clients ? Math.round((data.new_clients / data.clients) * 100) : 0}%` }} /></div>
              </div>
              <Stat icon="staff" tone="green" title="New clients" value={count(data.new_clients)} />
              <Stat icon="staff" tone="violet" title="Returning" value={count(data.repeat_clients)} />
            </div>

            {data.target ? (
              <Card title="Target tracking" aside={<span className="badge green">{data.target_pct ?? 0}%</span>}>
                <Milestones pct={data.target_pct ?? 0} startLabel="From" endLabel="Monthly target" from="₹0" to={inr(data.target)} nowLabel={`${inr(data.sales_ex_tax)} so far`} />
              </Card>
            ) : null}

            <Card title="Sales each day" aside={<span>before tax</span>} className="wide">
              <div className="bignum"><b className="num">{inrShort(data.sales_ex_tax)}</b>{data.target ? <span>Target {inrShort(data.target)}</span> : null}</div>
              <BarChart
                fmt={inr}
                labelEvery={data.daily.length > 20 ? 5 : 2}
                items={data.daily.map((d) => ({ label: String(d.day), value: d.sales, note: `${d.bills} bill${d.bills === 1 ? '' : 's'}` }))}
                line={data.target ? data.target / data.daily.length : undefined}
                lineLabel={data.target ? 'daily target' : undefined}
              />
            </Card>

            <Card title="Bills by weekday">
              <HatchBars
                fmt={(n) => `${count(n)} bills`}
                items={data.weekday.map((w) => ({ label: w.day.slice(0, 3), value: w.bills, top: weekdayTotal ? `${Math.round((w.bills / weekdayTotal) * 100)}%` : '' }))}
              />
            </Card>

            <Card title="Payments">
              <Donut
                fmt={inr}
                parts={[
                  { label: 'Cash', value: data.payments.cash, color: PALETTE.lime },
                  { label: 'Card', value: data.payments.card, color: PALETTE.blue },
                  { label: 'UPI & other', value: data.payments.other, color: PALETTE.orange },
                ]}
              />
              {data.redemptions > 0 ? <p className="report-note" style={{ marginTop: 10 }}>{inrShort(data.redemptions)} paid from cards, advances and gift cards is left out.</p> : null}
            </Card>

            <Card title="What was sold">
              {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
                <div className="row" key={k}>
                  <div className="lead">
                    <span className={`chip ${KIND_ICON[k].tone}`}><Icon name={KIND_ICON[k].icon} size={19} /></span>
                    <div className="name"><b>{KIND_LABELS[k]}</b><small>{count(data.breakdown[k].count)} sold</small></div>
                  </div>
                  <div className="val num">{data.breakdown[k].amount > 0 ? inr(data.breakdown[k].amount) : '—'}</div>
                </div>
              ))}
            </Card>

            <Card title="Clients who came back" aside={<span>of last month's</span>}>
              <PercentLine items={data.retention.map((r) => ({ label: monthShort(r.month), pct: r.pct }))} />
            </Card>

            <Card title="Men and women">
              <StackedColumns
                colors={[PALETTE.lime, PALETTE.blue, PALETTE.grey]}
                items={data.gender.map((g) => ({ label: monthShort(g.month), parts: [g.female, g.male, g.other] }))}
              />
              <div className="pills" style={{ marginTop: 10 }}>
                {[['Women', PALETTE.lime], ['Men', PALETTE.blue], ['Not stated', PALETTE.grey]].map(([l, c]) => (
                  <span key={l} className="muted" style={{ fontWeight: 600 }}><span className="dot" style={{ background: c }} />{l}</span>
                ))}
              </div>
            </Card>
          </div>
        </>
      )}
    </Page>
  );
}
