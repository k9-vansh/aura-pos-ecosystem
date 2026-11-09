import { addDays, fromIso, today } from '../dates';
import { Icon } from './Icon';

/** Seven days as small letters over dates, with arrows to move a week. `counts` puts a dot under days that have something. */
export function DayStrip({ value, onChange, future = false, counts }: { value: string; onChange: (d: string) => void; future?: boolean; counts?: Record<string, number> }) {
  const now = today();
  const days = Array.from({ length: 7 }, (_, i) => addDays(value, i - 3));
  const letter = new Intl.DateTimeFormat('en-IN', { weekday: 'short' });
  return (
    <section className="card cal">
      <div className="calhead">
        <b>{fromIso(value).toLocaleDateString('en-IN', { month: 'long', year: 'numeric' })}</b>
        <div className="arrows">
          <button aria-label="Earlier" onClick={() => onChange(addDays(value, -7))}><Icon name="left" size={16} /></button>
          <button className="todaybtn" disabled={value === now} onClick={() => onChange(now)}>Today</button>
          <button aria-label="Later" disabled={!future && addDays(value, 7) > now} onClick={() => onChange(addDays(value, 7))}><Icon name="right" size={16} /></button>
        </div>
      </div>
      <div className="week" role="group" aria-label="Days">
        {days.map((d) => (
          <button key={d} aria-pressed={d === value} disabled={!future && d > now} aria-label={d} className={d === now ? 'now' : ''} onClick={() => onChange(d)}>
            <small>{letter.format(fromIso(d))}</small>
            <b className="num">{Number(d.slice(8))}</b>
            <i className={counts && counts[d] ? 'dot on' : 'dot'} />
          </button>
        ))}
      </div>
    </section>
  );
}
