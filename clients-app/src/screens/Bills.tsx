import { useState } from 'react';
import { day, inr } from '../format';
import type { BillRow, Receipt } from '../types';
import { Card, Empty, Waiting, useLoad } from '../ui/bits';
import { Icon } from '../ui/Icon';

function Detail({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, error, loading, reload } = useLoad<Receipt>(`/bills/${id}/`);
  return (
    <div className="detail" role="dialog" aria-modal="true" aria-label="Bill">
      <div className="inner">
        <div className="bar">
          <button className="round" onClick={onClose} aria-label="Back"><Icon name="left" size={19} /></button>
          <h3>{data?.number ?? 'Bill'}</h3>
          <span style={{ width: 42 }} />
        </div>
        {!data ? <Waiting error={error} loading={loading} reload={reload} h={300} /> : (
          <>
            <Card kind="accent" className="receipt-head">
              <span className="tag">{data.salon}</span>
              <div className="big num">{inr(data.total, true)}</div>
              <div className="sub">{data.date}{data.status === 'cancelled' ? ' · Cancelled' : ''}</div>
            </Card>
            <Card title="What you had">
              {data.items.map((it, i) => (
                <div className="row" key={i}>
                  <div className="name"><b>{it.name}</b><small>{it.qty > 1 ? `${it.qty} × ` : ''}{it.redeemed ? 'Paid from your card or package' : ''}</small></div>
                  <div className="val num">{it.redeemed ? '—' : inr(it.amount, true)}</div>
                </div>
              ))}
            </Card>
            <Card title="Totals">
              <div className="kv">
                <div><span>Subtotal</span><b className="num">{inr(data.subtotal, true)}</b></div>
                {data.discount ? <div><span>Discount</span><b className="num">-{inr(data.discount, true)}</b></div> : null}
                <div><span>GST</span><b className="num">{inr(data.tax, true)}</b></div>
                {data.rounding ? <div><span>Rounding</span><b className="num">{inr(data.rounding, true)}</b></div> : null}
                <div><span>Total</span><b className="num">{inr(data.total, true)}</b></div>
              </div>
            </Card>
            {data.payments.length ? (
              <Card title="Paid with">
                <div className="kv">
                  {data.payments.map((p, i) => <div key={i}><span>{p.method}{p.tip ? ' (tip)' : ''}</span><b className="num">{inr(p.amount, true)}</b></div>)}
                </div>
              </Card>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}

export function BillsScreen() {
  const { data, error, loading, reload } = useLoad<{ bills: BillRow[] }>('/bills/');
  const [open, setOpen] = useState<number | null>(null);
  if (!data) return <Waiting error={error} loading={loading} reload={reload} h={300} />;
  return (
    <>
      <Card title="Your bills" aside={data.bills.length ? `${data.bills.length} latest` : undefined}>
        {data.bills.length === 0 ? <Empty icon="bills">No bills yet.</Empty> : data.bills.map((b) => (
          <button className="row" key={b.id} onClick={() => setOpen(b.id)}>
            <div className="lead"><span className="chip accent"><Icon name="bills" size={20} /></span>
              <div className="name"><b>{b.number}</b><small>{day(b.date)} · {b.salon}</small></div></div>
            <div className="val num">{inr(b.total, true)}{b.status === 'cancelled' ? <small>Cancelled</small> : null}</div>
          </button>
        ))}
      </Card>
      {open !== null ? <Detail id={open} onClose={() => setOpen(null)} /> : null}
    </>
  );
}
