import { useId, useState } from 'react';

const W = 320;
export const PALETTE = {
  lime: '#8fc93a',
  limePale: '#cfe99a',
  limeDeep: '#5fa021',
  orange: '#ff9f1c',
  blue: '#2ea3f2',
  violet: '#7a5af8',
  red: '#ef4444',
  ink: '#101216',
  grey: '#d9dce3',
};

export interface BarItem {
  label: string;
  value: number | null;
  note?: string;
  /** A short text above the bar (for example its share of the total). */
  top?: string;
}

/** A progress ring with a big figure in the middle. The ring fills up to 100% (the figure can say more). */
export function Ring({ pct, centre, caption, size = 92, track = 'rgba(255,255,255,0.75)' }: { pct: number | null; centre: string; caption: string; size?: number; track?: string }) {
  const R = 36;
  const C = 2 * Math.PI * R;
  const filled = pct === null ? 0 : Math.max(0, Math.min(pct, 100)) / 100;
  return (
    <svg className="ring" width={size} height={size} viewBox="0 0 100 100" role="img" aria-label={`${centre} ${caption}`}>
      <circle cx="50" cy="50" r={R} fill="none" stroke={track} strokeWidth="11" />
      <circle cx="50" cy="50" r={R} fill="none" stroke={PALETTE.lime} strokeWidth="11" strokeLinecap="round"
        strokeDasharray={`${filled * C} ${C}`} transform="rotate(-90 50 50)" style={{ transition: 'stroke-dasharray .6s ease' }} />
      <text className="t1" x="50" y="52" textAnchor="middle">{centre}</text>
      <text className="t2" x="50" y="65" textAnchor="middle">{caption}</text>
    </svg>
  );
}

/** Tall rounded bars standing in a hatched track, the best one in the stronger green. Tap one to read it. */
export function HatchBars({
  items,
  fmt,
  height = 170,
  highlight,
}: {
  items: BarItem[];
  fmt: (n: number) => string;
  height?: number;
  highlight?: number;
}) {
  const id = useId().replace(/:/g, '');
  const [picked, setPicked] = useState<number | null>(null);
  const top = 22;
  const bottom = 20;
  const trackH = height - top - bottom;
  const max = Math.max(1, ...items.map((i) => i.value ?? 0));
  const slot = W / Math.max(items.length, 1);
  const bw = Math.min(slot * 0.5, 28);
  const best = highlight ?? items.reduce((b, it, i) => ((it.value ?? -1) > (items[b]?.value ?? -1) ? i : b), 0);
  const shown = picked ?? null;

  return (
    <div>
      <svg className="chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Chart">
        <defs>
          <pattern id={`h${id}`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
            <line x1="0" y1="0" x2="0" y2="5" stroke="#d9dce3" strokeWidth="1.6" />
          </pattern>
        </defs>
        {items.map((it, i) => {
          const x = i * slot + (slot - bw) / 2;
          const h = it.value === null ? 0 : Math.max(it.value > 0 ? bw : 0, (it.value / max) * trackH);
          const on = i === (shown ?? best);
          return (
            <g key={i} className="bar" onClick={() => setPicked(picked === i ? null : i)}>
              <rect x={x} y={top} width={bw} height={trackH} rx={bw / 2} fill={`url(#h${id})`} opacity="0.9" />
              {h > 0 ? <rect x={x} y={top + trackH - h} width={bw} height={h} rx={bw / 2} fill={on ? PALETTE.lime : PALETTE.limePale} /> : null}
              {it.top !== undefined && it.value !== null && h > 0 ? (
                <text className="val" x={x + bw / 2} y={top + trackH - h - 5} textAnchor="middle" style={{ fontSize: 9.5 }}>{it.top}</text>
              ) : null}
              <rect x={i * slot} y={0} width={slot} height={height - bottom} fill="transparent" />
              <text x={x + bw / 2} y={height - 5} textAnchor="middle" style={on ? { fill: 'var(--ink)', fontWeight: 800 } : undefined}>{it.label}</text>
            </g>
          );
        })}
      </svg>
      <p className="tip num">
        {shown !== null && items[shown] ? (
          <>
            {items[shown].label}: {items[shown].value === null ? '—' : fmt(items[shown].value as number)}
            {items[shown].note ? <span className="muted"> · {items[shown].note}</span> : null}
          </>
        ) : (
          <span className="muted">Tap a bar for its figure</span>
        )}
      </p>
    </div>
  );
}

/** Plain thin bars for many values (every day of a month); a dashed line can mark a target. */
export function BarChart({
  items,
  fmt,
  color = PALETTE.lime,
  line,
  lineLabel,
  height = 150,
  labelEvery = 1,
  highlight,
}: {
  items: BarItem[];
  fmt: (n: number) => string;
  color?: string;
  line?: number;
  lineLabel?: string;
  height?: number;
  labelEvery?: number;
  highlight?: number;
}) {
  const [picked, setPicked] = useState<number | null>(null);
  const top = 16;
  const bottom = 18;
  const max = Math.max(1, line ?? 0, ...items.map((i) => i.value ?? 0));
  const slot = W / Math.max(items.length, 1);
  const bw = Math.min(slot * 0.62, 26);
  const y = (v: number) => top + (height - top - bottom) * (1 - v / max);
  const shown = picked ?? highlight ?? null;
  const best = items.reduce((b, it, i) => ((it.value ?? -1) > (items[b]?.value ?? -1) ? i : b), 0);

  return (
    <div>
      <svg className="chart" viewBox={`0 0 ${W} ${height}`} role="img" aria-label="Chart">
        <line className="grid" x1="0" x2={W} y1={height - bottom} y2={height - bottom} />
        {line ? (
          <g>
            <line x1="0" x2={W} y1={y(line)} y2={y(line)} stroke={PALETTE.orange} strokeWidth="1.4" strokeDasharray="4 4" />
            {lineLabel ? <text x={0} y={y(line) - 4}>{lineLabel}</text> : null}
          </g>
        ) : null}
        {items.map((it, i) => {
          if (it.value === null) return null;
          const h = Math.max(it.value > 0 ? 2 : 0, height - bottom - y(it.value));
          const x = i * slot + (slot - bw) / 2;
          const on = shown === null ? i === best : shown === i;
          return (
            <g key={i} className="bar" onClick={() => setPicked(picked === i ? null : i)}>
              <rect x={x} y={height - bottom - h} width={bw} height={h} rx={Math.min(7, bw / 2)} fill={on ? color : PALETTE.limePale} />
              <rect x={i * slot} y={0} width={slot} height={height - bottom} fill="transparent" />
              {on && it.value > 0 ? <text className="val" x={x + bw / 2} y={height - bottom - h - 4} textAnchor="middle">{fmt(it.value)}</text> : null}
              {i % labelEvery === 0 ? <text x={x + bw / 2} y={height - 4} textAnchor="middle">{it.label}</text> : null}
            </g>
          );
        })}
      </svg>
      <p className="tip num">
        {shown !== null && items[shown] ? (
          <>
            {items[shown].label}: {items[shown].value === null ? '—' : fmt(items[shown].value as number)}
            {items[shown].note ? <span className="muted"> · {items[shown].note}</span> : null}
          </>
        ) : (
          <span className="muted">Tap a bar for its figure</span>
        )}
      </p>
    </div>
  );
}

/** A tiny trend: bars, or a line. */
export function Spark({ values, kind = 'bars', color = PALETTE.lime }: { values: number[]; kind?: 'bars' | 'line'; color?: string }) {
  const H = 34;
  const w = 100;
  const max = Math.max(1, ...values);
  const n = Math.max(values.length, 1);
  if (kind === 'line') {
    const pts = values.map((v, i) => `${(i * w) / Math.max(n - 1, 1)},${H - 3 - (v / max) * (H - 8)}`).join(' ');
    return (
      <svg viewBox={`0 0 ${w} ${H}`} width="100%" height={H} preserveAspectRatio="none" aria-hidden="true">
        <polyline points={pts} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" vectorEffect="non-scaling-stroke" />
      </svg>
    );
  }
  const bw = (w / n) * 0.6;
  return (
    <svg viewBox={`0 0 ${w} ${H}`} width="100%" height={H} preserveAspectRatio="none" aria-hidden="true">
      {values.map((v, i) => {
        const h = Math.max(v > 0 ? 2 : 0, (v / max) * (H - 2));
        return <rect key={i} x={(i * w) / n + ((w / n) - bw) / 2} y={H - h} width={bw} height={h} rx={bw / 2} fill={color} />;
      })}
    </svg>
  );
}

/** A line over months, 0 to 100 percent. Months with no figure leave a gap. */
export function PercentLine({ items, color = PALETTE.limeDeep }: { items: { label: string; pct: number | null }[]; color?: string }) {
  const H = 130;
  const top = 14;
  const bottom = 20;
  const x = (i: number) => (items.length === 1 ? W / 2 : 16 + (i * (W - 32)) / (items.length - 1));
  const y = (p: number) => top + (H - top - bottom) * (1 - p / 100);
  const pts = items.map((it, i) => (it.pct === null ? null : { x: x(i), y: y(it.pct), pct: it.pct }));
  let path = '';
  let drawing = false;
  for (const p of pts) {
    if (!p) {
      drawing = false;
      continue;
    }
    path += `${drawing ? 'L' : 'M'}${p.x},${p.y}`;
    drawing = true;
  }
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Chart">
      {[0, 50, 100].map((g) => (
        <g key={g}>
          <line className="grid" x1="0" x2={W} y1={y(g)} y2={y(g)} />
          <text x="0" y={y(g) - 3}>{g}%</text>
        </g>
      ))}
      <path d={path} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      {pts.map((p, i) =>
        p ? (
          <g key={i}>
            <circle cx={p.x} cy={p.y} r="5.5" fill="#fff" stroke={color} strokeWidth="3" />
            <text className="val" x={p.x} y={p.y - 11} textAnchor="middle">{p.pct}%</text>
          </g>
        ) : null,
      )}
      {items.map((it, i) => (
        <text key={i} x={x(i)} y={H - 4} textAnchor="middle">{it.label}</text>
      ))}
    </svg>
  );
}

export interface Part {
  label: string;
  value: number;
  color: string;
}

/** A ring split into parts; the centre says the biggest part's share. */
export function Donut({ parts, fmt, centre }: { parts: Part[]; fmt: (n: number) => string; centre?: string }) {
  const total = parts.reduce((s, p) => s + p.value, 0);
  const R = 52;
  const C = 2 * Math.PI * R;
  let offset = 0;
  const lead = parts.reduce((b, p) => (p.value > b.value ? p : b), parts[0]);
  return (
    <div>
      <svg className="chart" viewBox="0 0 160 160" style={{ maxWidth: 170, margin: '0 auto' }} role="img" aria-label="Split">
        <circle cx="80" cy="80" r={R} fill="none" stroke="#eef0f4" strokeWidth="22" />
        {total > 0 ? parts.map((p) => {
          const len = (p.value / total) * C;
          const seg = (
            <circle key={p.label} cx="80" cy="80" r={R} fill="none" stroke={p.color} strokeWidth="22" strokeLinecap="butt"
              strokeDasharray={`${Math.max(len - 3, 0)} ${C}`} strokeDashoffset={-offset} transform="rotate(-90 80 80)" />
          );
          offset += len;
          return seg;
        }) : null}
        <text className="val" x="80" y="82" textAnchor="middle" style={{ fontSize: 26 }}>{total > 0 ? `${Math.round((lead.value / total) * 100)}%` : '—'}</text>
        <text x="80" y="98" textAnchor="middle" style={{ fontSize: 10 }}>{total > 0 ? (centre ?? lead.label) : 'no sales'}</text>
      </svg>
      <div className="legend">
        {parts.map((p) => (
          <div className="row" key={p.label}>
            <span><span className="dot" style={{ background: p.color }} />{p.label}</span>
            <span className="val num">{fmt(p.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** Columns each split into stacked parts (for example female / male / other per month). */
export function StackedColumns({ items, colors }: { items: { label: string; parts: number[] }[]; colors: string[] }) {
  const H = 140;
  const top = 8;
  const bottom = 18;
  const max = Math.max(1, ...items.map((i) => i.parts.reduce((s, p) => s + p, 0)));
  const slot = W / Math.max(items.length, 1);
  const bw = Math.min(slot * 0.5, 44);
  return (
    <svg className="chart" viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Chart">
      <line className="grid" x1="0" x2={W} y1={H - bottom} y2={H - bottom} />
      {items.map((it, i) => {
        let used = 0;
        const x = i * slot + (slot - bw) / 2;
        const total = it.parts.reduce((s, p) => s + p, 0);
        return (
          <g key={i}>
            {it.parts.map((p, k) => {
              const h = ((H - top - bottom) * p) / max;
              used += h;
              return p > 0 ? <rect key={k} x={x} y={H - bottom - used} width={bw} height={Math.max(h - 1.5, 1)} rx="6" fill={colors[k]} /> : null;
            })}
            <text className="val" x={x + bw / 2} y={H - bottom - used - 4} textAnchor="middle">{total}</text>
            <text x={x + bw / 2} y={H - 4} textAnchor="middle">{it.label}</text>
          </g>
        );
      })}
    </svg>
  );
}
