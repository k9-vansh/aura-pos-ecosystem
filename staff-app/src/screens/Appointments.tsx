import { useState } from 'react';
import { today } from '../dates';
import { clock } from '../format';
import type { Appointments } from '../types';
import { Card, Empty, Waiting, useLoad } from '../ui/bits';
import { DayStrip } from '../ui/Strip';

export function AppointmentsScreen() {
  const [date, setDate] = useState(today());
  const { data, error, loading, reload } = useLoad<Appointments>(`/appointments/?date=${date}`);
  return (
    <>
      <DayStrip value={date} onChange={setDate} future counts={data?.counts} />
      {!data ? <Waiting error={error} loading={loading} reload={reload} h={240} /> : (
        <Card title="Appointments" aside={data.rows.length ? `${data.rows.length} booked` : undefined}>
          {data.rows.length === 0 ? <Empty icon="visits">No appointment on your name for this day.</Empty> : data.rows.map((r) => {
            const off = r.status === 'Cancelled' || r.status === 'No-show';
            return (
              <div className="appt" key={r.id}>
                <div className="when"><b className="num">{clock(r.time)}</b><small>{r.end ? `to ${clock(r.end)}` : `${r.minutes} min`}</small></div>
                <div className="body">
                  <b>{r.client}</b>
                  <small>{r.service} · {r.minutes} min{r.salon ? ` · ${r.salon}` : ''}</small>
                  {r.notes ? <small className="n">{r.notes}</small> : null}
                </div>
                <span className={`badge${off ? ' orange' : r.status === 'Completed' ? ' green' : ''}`}>{r.status}</span>
              </div>
            );
          })}
        </Card>
      )}
    </>
  );
}
