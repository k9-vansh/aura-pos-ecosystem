import { useState } from 'react';
import { inr } from '../format';
import { today } from '../dates';
import type { JobCard, Kind, Summary } from '../types';
import { Card, Empty, Waiting, useLoad } from '../ui/bits';
import { DayStrip } from '../ui/Strip';

export const LABEL: Record<Kind['key'], string> = { services: 'Services', products: 'Products', memberships: 'Memberships', cards: 'Cards', packages: 'Packages' };

/** The Job Card table: what was sold and what it is worth, plus what was paid from a card or package. */
export function SummaryTable({ data }: { data: Summary }) {
  return (
    <Card title="Summary">
      <table className="grid">
        <thead><tr><th>Type</th><th>Count</th><th>Amount</th></tr></thead>
        <tbody>
          {data.kinds.map((k) => (
            <tr key={k.key} className={k.count ? '' : 'zero'}><td>{LABEL[k.key]}</td><td className="num">{k.count}</td><td className="num">{inr(k.amount)}</td></tr>
          ))}
          <tr className="sep"><td>Redeemed from cards and packages</td><td className="num">{data.redeemed.count}</td><td className="num">{inr(data.redeemed.amount)}</td></tr>
        </tbody>
      </table>
      <p className="note">Redeemed work is shown for your record. It is not counted in your total.</p>
    </Card>
  );
}

export function JobCardScreen() {
  const [date, setDate] = useState(today());
  const { data, error, loading, reload } = useLoad<JobCard>(`/jobcard/?date=${date}`);
  return (
    <>
      <DayStrip value={date} onChange={setDate} />
      {!data ? <Waiting error={error} loading={loading} reload={reload} h={300} /> : (
        <>
          <Card kind="black" className="total">
            <span className="tag">{data.is_today ? 'Today' : 'On this day'} · Total revenue credited</span>
            <div className="big num">{inr(data.total, true)}</div>
            <div className="sub">{data.lines.filter((l) => !l.redeemed).length} item{data.lines.filter((l) => !l.redeemed).length === 1 ? '' : 's'} on your name</div>
          </Card>
          <SummaryTable data={data} />
          <Card title="Service list" aside={data.lines.length ? `${data.lines.length} line${data.lines.length === 1 ? '' : 's'}` : undefined}>
            {data.lines.length === 0 ? <Empty icon="jobcard">Nothing credited to you on this day.</Empty> : data.lines.map((l, i) => (
              <div className="row" key={i}>
                <div className="name"><b>{l.item}</b><small>{l.client} · {l.time}{l.qty > 1 ? ` · ×${l.qty}` : ''}{l.redeemed ? ' · from a card' : ''}</small></div>
                <div className="val num">{l.redeemed ? '—' : inr(l.amount, true)}</div>
              </div>
            ))}
          </Card>
        </>
      )}
    </>
  );
}
