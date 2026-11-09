import { useState } from 'react';
import { api, ApiError } from '../api';
import { clock, dayWithWeek } from '../format';
import type { Visit, Visits } from '../types';
import { Card, Empty, Notice, Seg, Waiting, useLoad } from '../ui/bits';
import { Icon } from '../ui/Icon';
import { BookFlow } from './Book';

function VisitRow({ v, onCancelled }: { v: Visit; onCancelled: () => void }) {
  const { week, date } = dayWithWeek(v.date);
  const off = v.status === 'Cancelled' || v.status === 'No-show';
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function cancel() {
    setError(null);
    try {
      await api(`/appointments/${v.id}/cancel/`, { method: 'POST' });
      onCancelled();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Something went wrong. Try again.');
    }
  }

  return (
    <div className="visitwrap">
      <div className="visit">
        <div className="date"><small>{week}</small><b className="num">{date}</b></div>
        <div className="body">
          <b>{clock(v.time)} · {v.salon}</b>
          <small>{v.services.length ? v.services.map((s) => s.name + (s.staff ? ` with ${s.staff}` : '')).join(', ') : 'Services to be decided'}</small>
        </div>
        <span className={`badge${off ? ' orange' : v.status === 'Completed' ? ' green' : ''}`}>{v.status}</span>
      </div>
      {v.can_cancel ? (
        asking ? (
          <div className="ask"><span>Cancel this visit?</span><button className="btn danger" onClick={cancel}>Yes, cancel</button><button className="link" onClick={() => setAsking(false)}>Keep it</button></div>
        ) : <button className="link small" onClick={() => setAsking(true)}>Cancel visit</button>
      ) : null}
      {error ? <Notice kind="bad">{error}</Notice> : null}
    </div>
  );
}

export function VisitsScreen() {
  const { data, error, loading, reload } = useLoad<Visits>('/appointments/');
  const [which, setWhich] = useState<'upcoming' | 'past'>('upcoming');
  const [booking, setBooking] = useState(false);
  if (!data) return <Waiting error={error} loading={loading} reload={reload} h={260} />;
  const list = data[which];
  return (
    <>
      <button className="bookcta" onClick={() => setBooking(true)}>
        <span className="chip accent"><Icon name="plus" size={20} /></span>
        <span><b>Book a visit</b><small>Pick services, a day and a time</small></span>
        <Icon name="right" size={18} />
      </button>
      <Seg value={which} onChange={setWhich} options={[{ id: 'upcoming', label: `Coming up (${data.upcoming.length})` }, { id: 'past', label: 'Past visits' }]} />
      <Card>
        {list.length === 0
          ? <Empty icon="visits">{which === 'upcoming' ? 'No visit booked yet. Tap “Book a visit” above.' : 'No past visits yet.'}</Empty>
          : list.map((v) => <VisitRow key={v.id} v={v} onCancelled={reload} />)}
      </Card>
      {booking ? <BookFlow onClose={() => setBooking(false)} onBooked={reload} /> : null}
    </>
  );
}
