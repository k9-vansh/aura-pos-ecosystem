import { useState } from 'react';
import { addMonths, monthLabel, thisMonth } from '../dates';
import { inr } from '../format';
import type { Month } from '../types';
import { Card, Waiting, useLoad } from '../ui/bits';
import { Icon } from '../ui/Icon';
import { SummaryTable } from './JobCard';

export function MonthScreen() {
  const [month, setMonth] = useState(thisMonth());
  const [pick, setPick] = useState<number | null>(null);
  const { data, error, loading, reload } = useLoad<Month>(`/month/?month=${month}`);
  const top = data ? Math.max(...data.days.map((d) => d.amount), 1) : 1;
  const shown = data && pick !== null ? data.days[pick] : null;
  return (
    <>
      <section className="card cal">
        <div className="calhead">
          <b>{monthLabel(month)}</b>
          <div className="arrows">
            <button aria-label="Previous month" onClick={() => { setMonth(addMonths(month, -1)); setPick(null); }}><Icon name="left" size={16} /></button>
            <button aria-label="Next month" disabled={month >= thisMonth()} onClick={() => { setMonth(addMonths(month, 1)); setPick(null); }}><Icon name="right" size={16} /></button>
          </div>
        </div>
      </section>
      {!data ? <Waiting error={error} loading={loading} reload={reload} h={320} /> : (
        <>
          <Card kind="black" className="total">
            <span className="tag">{monthLabel(month)} · Revenue credited to you</span>
            <div className="big num">{inr(data.total, true)}</div>
            <div className="three">
              <div><small>Days worked</small><b className="num">{data.days_worked}</b></div>
              <div><small>Average a day</small><b className="num">{inr(data.average_day)}</b></div>
              <div><small>Best day</small><b className="num">{data.best_day ? `${data.best_day} · ${inr(data.best_amount)}` : '—'}</b></div>
            </div>
          </Card>
          <Card title="Day by day" aside={shown ? `${shown.day} ${monthLabel(month).split(' ')[0].slice(0, 3)}: ${inr(shown.amount)}` : 'Tap a bar'}>
            <div className="bars" role="list">
              {data.days.map((d, i) => (
                <button key={d.day} role="listitem" className={i === pick ? 'on' : ''} aria-label={`${d.day}: ${inr(d.amount)}`} onClick={() => setPick(i === pick ? null : i)}>
                  <i style={{ height: `${Math.max(d.amount / top * 100, d.amount ? 6 : 2)}%` }} />
                </button>
              ))}
            </div>
            <div className="barlab"><span>1</span><span>{Math.ceil(data.days.length / 2)}</span><span>{data.days.length}</span></div>
          </Card>
          <SummaryTable data={data} />
        </>
      )}
    </>
  );
}
