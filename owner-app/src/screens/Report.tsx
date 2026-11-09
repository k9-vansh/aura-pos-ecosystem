import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import { weekLabel, monthShort } from '../dates';
import { change, count, inr, inrShort, tone } from '../format';
import { useApi } from '../hooks';
import type { Kind, Me, Report, ReportList } from '../types';
import { HatchBars } from '../ui/Charts';
import { Card, Hero, Notice, Page, Seg, StaleNote, Stat, Waiting } from '../ui/bits';
import { Icon } from '../ui/Icon';
import { KIND_ICON, KIND_LABELS } from './Today';

function Delta({ pct }: { pct: number | null | undefined }) {
  const t = tone(pct);
  return <span className={`num ${t}`}>{t === 'up' ? '▲ ' : t === 'down' ? '▼ ' : ''}{change(pct)}</span>;
}

function ReportView({ id }: { id: number }) {
  const { data, error, loading, stale, reload } = useApi<Report>(`/reports/${id}/`, {}, true, false);   // a report can be slow to build: asked once, not every few seconds
  if (!data) return <Waiting error={error} stale={stale} reload={reload} loading={loading} h={420} />;
  const n = data.numbers;
  const t = data.narrative;
  return (
    <div className="grid three">
      <Hero
        className="wide"
        tag={data.salon}
        title={`Week of ${weekLabel(data.week_start, data.week_end)}`}
        amount={inr(n.sales)}
        sub={`Sales before tax · ${change(n.sales_change_pct)} on the week before`}
        pct={n.target_pct}
        centre={n.target_pct === null ? count(n.bills) : `${n.target_pct}%`}
        caption={n.target_pct === null ? 'bills' : 'of target'}
      />
      <div className="cols-2 wide">
        <Stat icon="scissors" tone="orange" title="Services and products" value={inrShort(n.work_delivered)} />
        <Stat icon="invoices" tone="blue" title="Bills" value={count(n.bills)} unit={change(n.bills_change_pct)} />
        <Stat icon="staff" tone="green" title="Clients" value={count(n.clients)} unit={`was ${count(n.clients_before)}`} />
        <Stat icon="card" tone="violet" title="Average bill" value={inr(n.avg_bill)} />
      </div>

      <Card kind="tint" className="wide">
        <p className="headline">{t.headline}</p>
        {t.summary ? <p style={{ marginTop: 10, fontWeight: 600 }}>{t.summary}</p> : null}
        {t.bullets.length ? <ul className="bullets" style={{ marginTop: 12 }}>{t.bullets.map((b, i) => <li key={i}>{b}</li>)}</ul> : null}
        {t.watch.length ? (
          <div style={{ marginTop: 14 }}>
            <h2 style={{ marginBottom: 8, display: 'block' }}>Look at next week</h2>
            <ul className="bullets">{t.watch.map((b, i) => <li key={i}>{b}</li>)}</ul>
          </div>
        ) : null}
        <p className="report-note" style={{ marginTop: 12 }}>
          {t.status === 'ok' ? `Written by ${data.model}, only from the figures on this page.` : 'The figures on this page come straight from the bills.'}
        </p>
      </Card>

      <Card title="Last 8 weeks" aside={<span>before tax{n.target ? ` · target ${inrShort(n.target)}` : ''}</span>}>
        <HatchBars
          fmt={inrShort}
          highlight={n.weeks.length - 1}
          items={n.weeks.map((w) => ({ label: monthShort(w.start.slice(0, 7)) + ' ' + Number(w.start.slice(8)), value: w.sales, top: inrShort(w.sales), note: `target ${inrShort(w.target)}` }))}
        />
      </Card>

      <Card title="Rhythm of the week">
        <HatchBars fmt={inrShort} items={n.rhythm.map((d) => ({ label: d.day, value: d.sales, top: inrShort(d.sales) }))} />
      </Card>

      <Card title="Where the sales came from">
        {(Object.keys(KIND_LABELS) as Kind[]).map((k) => (
          <div className="row" key={k}>
            <div className="lead">
              <span className={`chip ${KIND_ICON[k].tone}`}><Icon name={KIND_ICON[k].icon} size={19} /></span>
              <div className="name"><b>{KIND_LABELS[k]}</b><small>was {inrShort(n.breakdown[k].before)}</small></div>
            </div>
            <div className="val num">{inrShort(n.breakdown[k].amount)}<small><Delta pct={n.breakdown[k].change_pct} /></small></div>
          </div>
        ))}
      </Card>

      <Card title="Top services">
        {n.top_services.length === 0 ? <p className="muted">No services this week.</p> : n.top_services.map((s) => (
          <div className="row" key={s.name}><div className="name"><b>{s.name}</b><small>{count(s.count)} done</small></div><div className="val num">{inrShort(s.revenue)}</div></div>
        ))}
      </Card>

      <Card title="Top team members">
        {n.top_staff.by_revenue.length === 0 ? <p className="muted">No services this week.</p> : n.top_staff.by_revenue.map((p) => (
          <div className="row" key={p.id}>
            <div className="lead"><span className="initial">{p.name.charAt(0).toUpperCase()}</span><div className="name"><b>{p.name}</b><small>{count(p.items)} items</small></div></div>
            <div className="val num">{inrShort(p.revenue)}</div>
          </div>
        ))}
      </Card>
    </div>
  );
}

export function ReportScreen({ salon, me, pickSalon }: { salon: string; me: Me; pickSalon: () => void }) {
  const single = salon !== 'all';
  const list = useApi<ReportList>('/reports/', { center_id: salon }, single);
  const [picked, setPicked] = useState<number | null>(null);
  const [making, setMaking] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const rows = list.data?.reports ?? [];
  useEffect(() => {
    // Open the newest report when the salon (or the list) changes.
    setPicked(rows.length ? rows[0].id : null);
    setProblem(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [salon, rows[0]?.id]);

  if (!single) {
    return (
      <Page>
        <Notice action={<button className="link" onClick={pickSalon}>Choose a salon</button>}>
          A weekly report is written for one salon at a time.
        </Notice>
      </Page>
    );
  }

  async function write(force: boolean) {
    setMaking(true);
    setProblem(null);
    try {
      const made = await api<{ id: number }>('/reports/create/', { method: 'POST', body: { center_id: Number(salon), force } });
      setPicked(made.id);
      list.reload();
    } catch (err) {
      setProblem(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
    } finally {
      setMaking(false);
    }
  }

  const latest = list.data?.latest_week;
  return (
    <Page>
      {!list.data ? <Waiting error={list.error} stale={list.stale} reload={list.reload} loading={list.loading} /> : (
        <>
          <StaleNote stale={list.stale} error={list.error} />
          {rows.length > 0 ? (
            <Seg
              value={String(picked ?? rows[0].id)}
              onChange={(v) => setPicked(Number(v))}
              options={rows.slice(0, 6).map((r) => ({ id: String(r.id), label: weekLabel(r.week_start, r.week_end) }))}
            />
          ) : null}
          {latest && !latest.made ? (
            <Card>
              <p><b>Week of {weekLabel(latest.week_start, latest.week_end)}</b> has no report yet.</p>
              {me.can_create_report ? (
                <button className="btn primary" style={{ marginTop: 12 }} disabled={making} onClick={() => write(false)}>
                  {making ? 'Writing the report…' : 'Write this week’s report'}
                </button>
              ) : <p className="muted" style={{ marginTop: 8 }}>You can read reports but not ask for a new one.</p>}
            </Card>
          ) : null}
          {problem ? <Notice kind="bad">{problem}</Notice> : null}
          {picked !== null ? <ReportView key={picked} id={picked} /> : null}
          {picked !== null && me.can_create_report ? (
            <button className="btn" disabled={making} onClick={() => write(true)}>
              {making ? 'Writing…' : 'Write this report again'}
            </button>
          ) : null}
        </>
      )}
    </Page>
  );
}
