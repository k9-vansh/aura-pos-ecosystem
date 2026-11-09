import { useEffect, useRef, useState } from 'react';
import { dayLabel, today } from '../dates';
import { count, inr } from '../format';
import { useApi } from '../hooks';
import type { BillRow, Receipt } from '../types';
import { Card, Notice, Page, StaleNote, Waiting, WeekStrip } from '../ui/bits';
import { Icon } from '../ui/Icon';

const METHOD_NAMES: Record<string, string> = { Cash: 'Cash', 'Credit Card': 'Card', 'Debit Card': 'Card' };

/** A bill opened as its own page: the way it is made up and how it was paid. */
function BillDetails({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, loading, stale, reload } = useApi<Receipt>(`/invoices/${id}/`, {});
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onClose]);
  return (
    <div className="detail" role="dialog" aria-modal="true" aria-label="Bill details">
      <div className="inner receipt">
        <div className="bar no-print">
          <button className="round" aria-label="Back" onClick={onClose}><Icon name="left" size={20} /></button>
          <h3>Details</h3>
          <span style={{ width: 42 }} />
        </div>
        {!data ? <Waiting error={error} stale={stale} reload={reload} loading={loading} h={300} /> : (
          <>
            <Card>
              <div className="row" style={{ borderTop: 0, padding: 0 }}>
                <div className="lead">
                  <span className="chip orange"><Icon name="invoices" size={20} /></span>
                  <div className="name"><small>Bill number</small><b>{data.number}</b></div>
                </div>
                <span className={`badge ${data.status === 'paid' ? 'green' : 'orange'}`}>{data.status === 'paid' ? 'Paid' : data.status}</span>
              </div>
              <div className="num" style={{ fontSize: 34, fontWeight: 800, letterSpacing: '-0.04em', marginTop: 14 }}>{inr(data.total, true)}</div>
              <p className="muted" style={{ fontWeight: 600 }}>{data.date}</p>
            </Card>

            <button className="bigbtn no-print" onClick={() => window.print()}><Icon name="print" size={20} /> Print receipt</button>

            <Card kind="tint" title="Bill details">
              <div className="kv" style={{ background: '#fff', borderRadius: 16, padding: 14 }}>
                <div><span>Client</span><b>{data.client}</b></div>
                <div><span>Salon</span><b>{data.salon}</b></div>
                <div><span>Items</span><b>{data.items.length}</b></div>
                <div><span>Subtotal</span><b className="num">{inr(data.subtotal, true)}</b></div>
                {data.discount ? <div><span>Discount</span><b className="num">-{inr(data.discount, true)}</b></div> : null}
                <div><span>Tax</span><b className="num">{inr(data.tax, true)}</b></div>
                {data.rounding ? <div><span>Round off</span><b className="num">{inr(data.rounding, true)}</b></div> : null}
              </div>
            </Card>

            <Card title="What was billed">
              {data.items.map((it, i) => (
                <div className="row" key={i}>
                  <div className="name"><b>{it.name || 'Item'}</b><small>{it.qty} × {it.redeemed ? 'redeemed from a package' : `tax ${inr(it.tax, true)}`}</small></div>
                  <div className="val num">{inr(it.amount, true)}</div>
                </div>
              ))}
            </Card>

            <Card title="Payment">
              <div className="steps">
                {data.payments.map((p, i) => (
                  <div className="step" key={i}>
                    <small>{METHOD_NAMES[p.method] ?? p.method}</small>
                    <span className="tick"><Icon name="check" size={12} /></span>
                    <p>{inr(p.amount, true)}{p.tip ? ' (tip)' : ''} received</p>
                  </div>
                ))}
              </div>
            </Card>
          </>
        )}
      </div>
    </div>
  );
}

export function BillsScreen({ salon }: { salon: string }) {
  const [day, setDay] = useState(today());
  const [jumpedFrom, setJumpedFrom] = useState<string | null>(null);
  const jumped = useRef(false);
  const [open, setOpen] = useState<number | null>(null);
  const { data, error, loading, stale, reload } = useApi<{ salon: string; rows: BillRow[]; latest_day: string | null }>('/invoices/', { center_id: salon, date: day });
  const total = data?.rows.reduce((s, r) => s + r.amount, 0) ?? 0;
  const showSalon = salon === 'all';

  useEffect(() => {
    if (data && !stale && !jumped.current && day === today() && data.rows.length === 0 && data.latest_day && data.latest_day !== day) {
      jumped.current = true;
      setJumpedFrom(day);
      setDay(data.latest_day);
    }
  }, [data, stale, day]);

  return (
    <Page>
      <WeekStrip value={day} onChange={(d) => { jumped.current = true; setJumpedFrom(null); setDay(d); }} />
      {jumpedFrom ? (
        <Notice action={<button className="link" onClick={() => { setJumpedFrom(null); setDay(jumpedFrom); }}>Show today</button>}>
          No bills yet today, so this is {dayLabel(day)}, the latest day with bills.
        </Notice>
      ) : null}
      {!data ? <Waiting error={error} stale={stale} reload={reload} loading={loading} /> : (
        <>
          <StaleNote stale={stale} error={error} />
          <Card title="Recent activities" aside={<span className="num">{count(data.rows.length)} bill{data.rows.length === 1 ? '' : 's'} · {inr(total)}</span>}>
            {data.rows.length === 0 ? <p className="empty">No bills on this day.</p> : data.rows.map((r) => (
              <button key={r.id} className="row" onClick={() => setOpen(r.id)}>
                <div className="lead">
                  <span className="chip orange"><Icon name="invoices" size={19} /></span>
                  <div className="name">
                    <b>{r.client}</b>
                    <small>{r.time} · {r.number}{showSalon && r.salon ? ` · ${r.salon}` : ''}{r.status === 'partial' ? ' · part paid' : ''}</small>
                  </div>
                </div>
                <div className="val num">{inr(r.amount)}</div>
              </button>
            ))}
          </Card>
        </>
      )}
      {open !== null ? <BillDetails id={open} onClose={() => setOpen(null)} /> : null}
    </Page>
  );
}
