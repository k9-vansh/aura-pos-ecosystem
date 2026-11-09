import { useEffect, useMemo, useState } from 'react';
import { api, ApiError } from '../api';
import { clock, day, inr } from '../format';
import type { BookOptions } from '../types';
import { Notice, Skeleton } from '../ui/bits';
import { Icon } from '../ui/Icon';

const WEEK = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

function iso(d: Date): string {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

/** Book a visit: services, a day, a start time. The salon's front desk sees it on their calendar at once. */
export function BookFlow({ onClose, onBooked }: { onClose: () => void; onBooked: () => void }) {
  const [date, setDate] = useState(() => iso(new Date()));
  const [opts, setOpts] = useState<BookOptions | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<number[]>([]);
  const [time, setTime] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    let alive = true;
    api<BookOptions>(`/book/options/?date=${date}`)
      .then((o) => alive && (setOpts(o), setError(null)))
      .catch((e) => alive && setError(e instanceof ApiError ? e.message : 'Something went wrong. Try again.'));
    return () => {
      alive = false;
    };
  }, [date]);

  // The strip of days: today and the next two weeks, closed weekdays greyed out.
  const days = useMemo(() => Array.from({ length: 15 }, (_, i) => {
    const d = new Date();
    d.setDate(d.getDate() + i);
    return d;
  }), []);
  const groups = useMemo(() => {
    const out = new Map<string, BookOptions['services']>();
    (opts?.services ?? []).forEach((s) => out.set(s.category, [...(out.get(s.category) ?? []), s]));
    return [...out.entries()];
  }, [opts]);
  const chosen = (opts?.services ?? []).filter((s) => picked.includes(s.id));
  const total = chosen.reduce((n, s) => n + s.price, 0);
  const mins = chosen.reduce((n, s) => n + s.minutes, 0);
  const slots = opts?.slots ?? [];
  const ready = chosen.length > 0 && time !== '' && slots.includes(time);

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api('/appointments/book/', { method: 'POST', body: { date, time, services: picked, note } });
      setDone(true);
      onBooked();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong. Try again.');
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="detail" role="dialog" aria-modal="true" aria-label="Booked">
        <div className="inner done">
          <span className="tickbig"><Icon name="check" size={34} /></span>
          <h2 className="display">You are booked</h2>
          <p className="muted">{day(date)} at {clock(time)} · {opts?.salon}</p>
          <div className="card" style={{ width: '100%' }}>
            {chosen.map((s) => <div className="row" key={s.id}><div className="name"><b>{s.name}</b><small>{s.minutes} min</small></div><div className="val num">{inr(s.price)}</div></div>)}
          </div>
          <p className="muted">The salon will confirm your stylist. You can cancel from the Visits tab.</p>
          <button className="btn primary block" onClick={onClose}>Done</button>
        </div>
      </div>
    );
  }

  return (
    <div className="detail" role="dialog" aria-modal="true" aria-label="Book a visit">
      <div className="inner book">
        <div className="bar">
          <button className="round" onClick={onClose} aria-label="Back"><Icon name="left" size={19} /></button>
          <h3>Book a visit</h3>
          <span style={{ width: 42 }} />
        </div>

        <section className="step">
          <h4><span>1</span> Pick a day</h4>
          <div className="dates" role="listbox" aria-label="Day">
            {days.map((d) => {
              const closed = opts ? opts.closed_days.includes(WEEK[(d.getDay() + 6) % 7]) : false;
              const on = iso(d) === date;
              return (
                <button key={iso(d)} role="option" aria-selected={on} disabled={closed} onClick={() => { setDate(iso(d)); setTime(''); }}>
                  <small>{d.toLocaleDateString('en-IN', { weekday: 'short' })}</small>
                  <b className="num">{d.getDate()}</b>
                  <small>{d.toLocaleDateString('en-IN', { month: 'short' })}</small>
                </button>
              );
            })}
          </div>
        </section>

        <section className="step">
          <h4><span>2</span> Choose services</h4>
          {!opts ? <Skeleton h={160} /> : groups.length === 0 ? <p className="muted">This salon has not listed services for online booking yet. Please call them.</p> : groups.map(([cat, list]) => (
            <div key={cat} className="svcgroup">
              <small className="cat">{cat}</small>
              {list.map((s) => {
                const on = picked.includes(s.id);
                return (
                  <button key={s.id} className="svc" aria-pressed={on} onClick={() => setPicked(on ? picked.filter((x) => x !== s.id) : [...picked, s.id])}>
                    <span className="box">{on ? <Icon name="check" size={14} /> : null}</span>
                    <span className="nm"><b>{s.name}</b><small>{s.minutes} min</small></span>
                    <span className="num pr">{inr(s.price)}</span>
                  </button>
                );
              })}
            </div>
          ))}
        </section>

        <section className="step">
          <h4><span>3</span> Pick a time</h4>
          {!opts ? <Skeleton h={90} /> : slots.length === 0 ? (
            <Notice>The salon is not taking bookings on this day. Try another day.</Notice>
          ) : (
            <div className="slots" role="listbox" aria-label="Time">
              {slots.map((t) => <button key={t} role="option" aria-selected={t === time} onClick={() => setTime(t)}>{clock(t)}</button>)}
            </div>
          )}
        </section>

        <section className="step">
          <h4><span>4</span> Anything we should know? <em>optional</em></h4>
          <textarea className="input" rows={2} maxLength={300} placeholder="Allergies, a preferred stylist, an occasion…" value={note} onChange={(e) => setNote(e.target.value)} />
        </section>

        {error ? <Notice kind="bad">{error}</Notice> : null}
        <div className="dock">
          <div><small>{chosen.length ? `${chosen.length} service${chosen.length > 1 ? 's' : ''} · ${mins} min` : 'No service chosen'}</small><b className="num">{inr(total)}</b></div>
          <button className="btn primary" disabled={!ready || busy} onClick={confirm}>{busy ? 'Booking…' : time ? `Book ${clock(time)}` : 'Book'}</button>
        </div>
      </div>
    </div>
  );
}
