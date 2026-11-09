import { useEffect, useState, type ReactNode } from 'react';
import { addDays, addMonths, fromIso, monthLabel, monthYear, thisMonth, today } from '../dates';
import { Ring } from './Charts';
import { Icon } from './Icon';

export type CardKind = '' | 'tint' | 'lime' | 'black';

export function Card({ title, aside, children, className = '', kind = '' }: { title?: string; aside?: ReactNode; children: ReactNode; className?: string; kind?: CardKind }) {
  return (
    <section className={`card${kind ? ` ${kind}` : ''} ${className}`}>
      {title ? (
        <h2>
          <span>{title}</span>
          {aside ? <span className="aside">{aside}</span> : null}
        </h2>
      ) : null}
      {children}
    </section>
  );
}

export function Skeleton({ h = 120 }: { h?: number }) {
  return <div className="skeleton" style={{ height: h }} />;
}

export function Page({ children }: { children: ReactNode }) {
  return <div className="stage">{children}</div>;
}

export function Notice({ kind = '', children, action }: { kind?: '' | 'bad' | 'good'; children: ReactNode; action?: ReactNode }) {
  return (
    <div className={`notice ${kind}`} role={kind === 'bad' ? 'alert' : 'status'}>
      {children} {action}
    </div>
  );
}

/** What to show while a screen has no figures yet, or could not get any. */
export function Waiting({ error, stale, reload, loading, h = 220 }: { error: string | null; stale: boolean; reload: () => void; loading: boolean; h?: number }) {
  if (error) {
    return (
      <Notice kind="bad" action={<button className="link" onClick={reload}>Try again</button>}>
        {error}
      </Notice>
    );
  }
  return loading || stale ? <Skeleton h={h} /> : null;
}

/** Shown above old figures: the last ones this phone saved, while fresh ones load or the signal is gone. */
export function StaleNote({ stale, error }: { stale: boolean; error: string | null }) {
  if (!stale) return null;
  return <p className="report-note">{error ? `${error} ` : ''}These are the last figures saved on this phone.</p>;
}

export function Seg<T extends string>({ value, options, onChange, full = false }: { value: T; options: { id: T; label: string }[]; onChange: (v: T) => void; full?: boolean }) {
  return (
    <div className={`seg${full ? ' full' : ''}`} role="group">
      {options.map((o) => (
        <button key={o.id} aria-pressed={value === o.id} onClick={() => onChange(o.id)}>
          {o.label}
        </button>
      ))}
    </div>
  );
}

/** A small tile: an icon chip, a title and a figure (like "Step to walk 5,500 steps"). */
export function Stat({ icon, tone, title, value, unit }: { icon: string; tone: 'orange' | 'blue' | 'red' | 'green' | 'violet'; title: string; value: ReactNode; unit?: string }) {
  return (
    <div className="stat">
      <div className="info">
        <small>{title}</small>
        <b className="num">{value}{unit ? <i>{unit}</i> : null}</b>
      </div>
      <span className={`chip ${tone}`}><Icon name={icon} size={20} /></span>
    </div>
  );
}

/** The week as small letters over dates; the chosen day sits in a lime pill. Arrows move a week; the title opens a date picker. */
export function WeekStrip({ value, onChange }: { value: string; onChange: (d: string) => void }) {
  const now = today();
  const [end, setEnd] = useState(() => (value > now ? value : now));
  useEffect(() => {
    // A date chosen from the picker (or by an automatic jump) outside the days shown brings its week into view.
    const first = addDays(end, -6);
    if (value < first || value > end) setEnd(addDays(value, 3) > now ? now : addDays(value, 3));
  }, [value, end, now]);
  const days = Array.from({ length: 7 }, (_, i) => addDays(end, i - 6));
  const letter = new Intl.DateTimeFormat('en-IN', { weekday: 'narrow' });
  return (
    <section className="card cal">
      <div className="top">
        <label className="month-pick">
          <span>{monthYear(value)}</span>
          <Icon name="chevron" size={18} />
          <input type="date" value={value} max={now} aria-label="Pick a date" onChange={(e) => e.target.value && onChange(e.target.value)} />
        </label>
        <div className="arrows">
          <button aria-label="Earlier days" onClick={() => setEnd(addDays(end, -7))}><Icon name="left" size={16} /></button>
          <button aria-label="Later days" disabled={end >= now} onClick={() => setEnd(addDays(end, 7) > now ? now : addDays(end, 7))}><Icon name="right" size={16} /></button>
        </div>
      </div>
      <div className="week" role="group" aria-label="Days">
        {days.map((d) => (
          <button key={d} className={d === now ? 'today' : ''} aria-pressed={d === value} disabled={d > now} aria-label={d} onClick={() => onChange(d)}>
            <span className="w">{letter.format(fromIso(d))}</span>
            <span className="n num">{Number(d.slice(8))}</span>
          </button>
        ))}
      </div>
    </section>
  );
}

/** The month as a title with arrows, and a picker. */
export function MonthRow({ value, onChange }: { value: string; onChange: (m: string) => void }) {
  const current = thisMonth();
  return (
    <section className="card cal">
      <div className="top" style={{ marginBottom: 0 }}>
        <label className="month-pick">
          <span>{monthLabel(value)}</span>
          <Icon name="chevron" size={18} />
          <input type="month" value={value} max={current} aria-label="Pick a month" onChange={(e) => e.target.value && onChange(e.target.value)} />
        </label>
        <div className="arrows">
          <button aria-label="Previous month" onClick={() => onChange(addMonths(value, -1))}><Icon name="left" size={16} /></button>
          <button aria-label="Next month" disabled={value >= current} onClick={() => onChange(addMonths(value, 1))}><Icon name="right" size={16} /></button>
        </div>
      </div>
    </section>
  );
}

/** The lime card on top of a screen: what it is about, the big figure, and a ring for the target. */
export function Hero({
  tag,
  title,
  amount,
  sub,
  pct,
  centre,
  caption,
  className = '',
}: {
  tag: string;
  title: string;
  amount: string;
  sub: string;
  pct: number | null;
  centre: string;
  caption: string;
  className?: string;
}) {
  return (
    <section className={`card lime hero ${className}`}>
      <div style={{ minWidth: 0 }}>
        <span className="tag"><Icon name="pin" size={13} />{tag}</span>
        <h2>{title}</h2>
        <div className="big num">{amount}</div>
        <div className="sub">{sub}</div>
      </div>
      <Ring pct={pct} centre={centre} caption={caption} />
    </section>
  );
}

/** A black line with round stops: where the target stands. */
export function Milestones({ pct, startLabel, nowLabel, endLabel, from, to }: { pct: number; startLabel: string; nowLabel: string; endLabel: string; from: string; to: string }) {
  const p = Math.max(0, Math.min(pct, 100)) / 100;
  const at = (x: number) => `calc(12px + (100% - 24px) * ${x})`;
  return (
    <div className="track">
      <div className="ends">
        <div><small>{startLabel}</small><b className="num">{from}</b></div>
        <div><small>{endLabel}</small><b className="num">{to}</b></div>
      </div>
      <div className="line">
        <div className="fill" style={{ width: `calc((100% - 24px) * ${p})` }} />
        <span className="stop on" style={{ left: at(0) }}><Icon name="check" size={14} /></span>
        {p > 0 && p < 1 ? <span className="stop on" style={{ left: at(p) }}><Icon name="check" size={14} /></span> : null}
        <span className={`stop${p >= 1 ? ' on' : ''}`} style={{ left: at(1) }}>{p >= 1 ? <Icon name="check" size={14} /> : null}</span>
      </div>
      <div className="labels"><span>Start</span><span>{nowLabel}</span><span>Target</span></div>
    </div>
  );
}
