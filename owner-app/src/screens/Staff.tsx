import { useState } from 'react';
import { count, inr } from '../format';
import { useApi } from '../hooks';
import type { Period, StaffData } from '../types';
import { Card, Page, Seg, StaleNote, Waiting } from '../ui/bits';

const PERIODS: { id: Period; label: string }[] = [
  { id: 'today', label: 'Today' },
  { id: 'yesterday', label: 'Yesterday' },
  { id: 'this_month', label: 'This month' },
  { id: 'last_month', label: 'Last month' },
];

export function StaffScreen({ salon }: { salon: string }) {
  const [view, setView] = useState<'sales' | 'hours'>('sales');
  const [period, setPeriod] = useState<Period>('this_month');
  const { data, error, loading, stale, reload } = useApi<StaffData>('/staff/', { center_id: salon, period });
  const many = salon === 'all';
  const best = Math.max(1, ...(data?.revenues.map((r) => r.total) ?? [1]));
  const bestHours = Math.max(1, ...(data?.usage.map((r) => r.hours) ?? [1]));

  return (
    <Page>
      <Seg full value={view} onChange={setView} options={[{ id: 'sales', label: 'Sales' }, { id: 'hours', label: 'Hours worked' }]} />
      <Seg full value={period} onChange={setPeriod} options={PERIODS} />
      {!data ? <Waiting error={error} stale={stale} reload={reload} loading={loading} /> : (
        <>
          <StaleNote stale={stale} error={error} />
          {view === 'sales' ? (
            data.revenues.length === 0 ? <p className="empty">No sales credited to staff in this period. Try “This month”.</p> : (
              <div className="grid three">
                {data.revenues.map((r, i) => (
                  <Card key={r.id}>
                    <div className="row" style={{ border: 0, padding: 0 }}>
                      <div className="lead">
                        <span className="initial">{r.name.charAt(0).toUpperCase()}</span>
                        <div className="name"><b>{r.name}</b><small>{many && r.salon ? `${r.salon} · ` : ''}No. {i + 1} this period</small></div>
                      </div>
                      <div className="val num" style={{ fontSize: 18 }}>{inr(r.total)}</div>
                    </div>
                    <div className="meter" style={{ margin: '12px 0 10px' }}><i style={{ width: `${(r.total / best) * 100}%` }} /></div>
                    <div className="pills">
                      <span className="badge green">Services {inr(r.services)}</span>
                      <span className="badge">Products {inr(r.products)}</span>
                      {r.other ? <span className="badge orange">Other {inr(r.other)}</span> : null}
                    </div>
                  </Card>
                ))}
              </div>
            )
          ) : data.usage.length === 0 ? <p className="empty">No appointments are booked in this period.</p> : (
            <Card title="Hours on appointments" aside={<span>{count(data.usage.length)} staff</span>}>
              {data.usage.map((u) => (
                <div className="row" key={u.id}>
                  <div className="lead" style={{ flex: 1 }}>
                    <span className="initial">{u.name.charAt(0).toUpperCase()}</span>
                    <div className="name" style={{ flex: 1 }}>
                      <b>{u.name}</b>
                      <div className="meter" style={{ marginTop: 6 }}><i style={{ width: `${(u.hours / bestHours) * 100}%` }} /></div>
                    </div>
                  </div>
                  <div className="val num">{u.hours} h<small>{u.appts} appt{u.appts === 1 ? '' : 's'}</small></div>
                </div>
              ))}
            </Card>
          )}
        </>
      )}
    </Page>
  );
}
