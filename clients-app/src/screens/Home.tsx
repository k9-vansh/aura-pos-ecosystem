import type { Home } from '../types';
import { day, expiry, wallet } from '../format';
import { Card, Empty, Waiting, useLoad } from '../ui/bits';
import { Icon } from '../ui/Icon';

export function HomeScreen({ goPin, goVisits }: { goPin: () => void; goVisits: () => void }) {
  const { data, error, loading, reload } = useLoad<Home>('/home/');
  if (!data) return <Waiting error={error} loading={loading} reload={reload} h={320} />;
  const total = data.advance + data.cashback + data.value_card_total;
  const nothing = total === 0 && !data.memberships.length && !data.packages.length;

  return (
    <>
      <Card kind="black" className="wallet">
        <div className="topline">
          <span className="tag"><Icon name="wallet" size={14} /> Wallet</span>
          <span className="brand">{data.salon || 'Your salon'}</span>
        </div>
        <small className="cap">Available to spend</small>
        <div className="big num">{wallet(total)}</div>
        <div className="three">
          <div><small>Advance</small><b className="num">{wallet(data.advance)}</b></div>
          <div><small>Cashback</small><b className="num">{wallet(data.cashback)}</b></div>
          <div><small>Cards</small><b className="num">{wallet(data.value_card_total)}</b></div>
        </div>
      </Card>

      <div className="quick">
        <button onClick={goVisits}><span className="chip accent"><Icon name="plus" size={20} /></span>Book a visit</button>
        <button onClick={goPin}><span className="chip accent"><Icon name="lock" size={20} /></span>My PIN</button>
      </div>

      {data.has_pin ? (
        <button className="pinnote" onClick={goPin}>
          <span className="chip accent"><Icon name="lock" size={20} /></span>
          <span><b>Your PIN protects this</b><small>The salon asks for it before your card, advance or membership is used.</small></span>
          <Icon name="right" size={18} />
        </button>
      ) : null}

      {nothing ? <Card><Empty icon="gift">Nothing saved with the salon yet. Cards, advances, cashback and memberships will show up here.</Empty></Card> : null}

      {data.value_cards.length ? (
        <section className="stack">
          <h3 className="sec">Value cards</h3>
          {data.value_cards.map((c) => (
            <div className="vcard" key={c.id}>
              <div className="top"><span>{c.title}</span><small>{c.number}</small></div>
              <div className="bal num">{wallet(c.balance)}</div>
              <div className="foot"><span>of {wallet(c.face_value)}</span><span>{expiry(c.expires)}</span></div>
              <div className="meter"><i style={{ width: `${Math.max(4, Math.min(100, c.face_value ? (c.balance / c.face_value) * 100 : 100))}%` }} /></div>
              {c.salon ? <small className="where">Use at {c.salon}</small> : null}
            </div>
          ))}
        </section>
      ) : null}

      {data.memberships.length ? (
        <Card title="Memberships">
          {data.memberships.map((m) => (
            <div className="row" key={m.id}>
              <div className="lead"><span className="chip accent"><Icon name="crown" size={20} /></span>
                <div className="name"><b>{m.name}</b><small>{m.salon}</small></div></div>
              <div className="val"><small>{expiry(m.expires)}</small></div>
            </div>
          ))}
        </Card>
      ) : null}

      {data.packages.length ? (
        <Card title="Packages">
          {data.packages.map((p) => (
            <div className="pack" key={p.id}>
              <div className="row">
                <div className="lead"><span className="chip accent"><Icon name="box" size={20} /></span>
                  <div className="name"><b>{p.name}</b><small>{p.salon} · valid till {day(p.expires)}</small></div></div>
              </div>
              <ul>{p.services.map((s) => <li key={s.name}><span>{s.name}</span><b className="num">{s.left} left</b></li>)}</ul>
            </div>
          ))}
        </Card>
      ) : null}
    </>
  );
}
