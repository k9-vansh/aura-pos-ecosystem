import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import Icon from './Icon'
import CountUp from './CountUp'
import { Area, HBars, Stack } from './charts'
import type { ReportDoc } from '../lib/api'
import { useAuth } from '../lib/auth'
import { downloadCsv, downloadExcel, printPdf } from '../lib/exporting'
import { inr } from '../lib/erp/format'
import { cell, chartFor, keyFigures, lineOf, NUMERIC, outline, periodText, rowTone, stat } from '../lib/reportFormat'

const ease = [0.16, 1, 0.3, 1] as const

interface Props {
  doc: ReportDoc
  /** card: under a chat answer (compact; the answer already narrates the insights) · page: the Reports page */
  variant?: 'card' | 'page'
  /** a newer run is on its way — dim this one */
  stale?: boolean
  onAsk?: (prompt: string) => void
}

/** A report or table from SAP, laid out like a finance team's report: headline figures (with the change on
    an earlier period), the chart that tells its story, what stands out, the details, and downloads. */
export default function ReportDocument({ doc, variant = 'card', stale = false, onAsk }: Props) {
  const page = variant === 'page'
  const figures = keyFigures(doc.summary, 4)
  const others = page ? (doc.summary ?? []).filter((s) => !figures.includes(s)) : []
  const deltas = new Map((doc.comparison?.deltas ?? []).map((d) => [d.label, d]))
  const plan = chartFor(doc)
  const [showAll, setShowAll] = useState(true)
  // a financial statement is the answer itself: its outline shows at once, under the chat answer too
  const [tableOpen, setTableOpen] = useState(page || doc.kind === 'table' || !!doc.statement)
  const limit = showAll ? doc.rows.length : page ? 25 : 6
  // a statement shows its outline (drawers, titles, profit lines, totals) until every account is asked for
  const statement = !!doc.statement
  const short = statement ? outline(doc.rows) : doc.rows
  const rows = statement ? (showAll ? doc.rows : short) : doc.rows.slice(0, limit)
  const hiddenAccounts = statement && !showAll ? doc.rows.length - short.length : 0
  // the column a statement indents: its line or account name
  const nameKey = doc.columns.find((c) => c.key === 'line' || c.key === 'name')?.key
  const read = doc.generated_at ? new Date(doc.generated_at).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : ''

  return (
    <motion.article
      className={`rdoc ${variant} ${stale ? 'stale' : ''}`}
      initial={{ opacity: 0, y: 14 }}
      animate={{ opacity: stale ? 0.55 : 1, y: 0 }}
      transition={{ duration: 0.5, ease }}
    >
      <header className="rdoc-head">
        <span className="rdoc-icon" aria-hidden="true">
          <Icon name={doc.kind === 'report' ? 'layers' : 'grid'} size={16} />
        </span>
        <div className="rdoc-titles">
          <p className="rdoc-kicker">
            {doc.kind === 'report' ? 'Report' : 'SAP data'} · live from SAP{read ? ` · read ${read}` : ''}
          </p>
          <h3>{doc.title}</h3>
          <p className="rdoc-period">
            {periodText(doc.period)}
            {doc.comparison ? ` · compared with ${doc.comparison.label}` : ''}
            {doc.kind === 'table' ? `${doc.period ? ' · ' : ''}${(doc.total_rows ?? doc.rows.length).toLocaleString('en-IN')} rows` : ''}
            {typeof doc.balanced === 'boolean' && (
              <span className={`rdoc-check ${doc.balanced ? 'ok' : 'bad'}`}>
                <Icon name={doc.balanced ? 'check' : 'alert'} size={11} stroke={2.6} /> {doc.balanced ? 'Balanced' : 'Doesn’t balance'}
              </span>
            )}
          </p>
        </div>
        <Downloads doc={doc} />
      </header>

      {figures.length > 0 && (
        <div className="rdoc-kpis">
          {figures.map((s, i) => {
            const d = deltas.get(s.label)
            return (
              <motion.div key={s.label} className="rdoc-kpi" initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08 + i * 0.06, duration: 0.5, ease }}>
                <span>{s.label}</span>
                <b>{typeof s.value === 'number' ? <CountUp text={stat(s.value, s.kind)} /> : stat(s.value, s.kind)}</b>
                {d && d.change_pct !== null && (
                  <em className={`delta ${d.change_pct >= 0 ? 'good' : 'bad'}`} title={`${stat(d.before, d.kind)} in ${doc.comparison?.label}`}>
                    <Icon name={d.change_pct >= 0 ? 'up' : 'down'} size={11} stroke={2.4} />
                    {Math.abs(d.change_pct) >= 1000 ? `${(d.now / d.before).toFixed(0)}×` : `${Math.abs(d.change_pct).toFixed(1)}%`}
                  </em>
                )}
              </motion.div>
            )
          })}
        </div>
      )}

      {plan && (
        <div className="rdoc-chart">
          {plan.type === 'series' ? (
            <Area labels={plan.labels} series={plan.series} format="inr" size={page ? 'detail' : 'card'} />
          ) : plan.type === 'bars' ? (
            <HBars bars={plan.bars.map((b) => ({ label: b.label, value: b.value, display: `${inr(b.value)}${b.share !== undefined && !Number.isNaN(b.share) ? ` · ${b.share.toFixed(1)}%` : ''}` }))} size="detail" />
          ) : (
            <Stack parts={plan.parts.map((p, i) => ({ ...p, tone: i === 0 ? ('good' as const) : i >= 3 ? ('bad' as const) : undefined }))} format="inr" size="detail" />
          )}
        </div>
      )}

      {page && doc.insights && doc.insights.length > 0 && (
        <section className="rdoc-insights">
          <h4>
            <Icon name="spark" size={14} /> What stands out
          </h4>
          <ul>
            {doc.insights.map((line, i) => (
              <motion.li key={line} initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: 0.15 + i * 0.07, duration: 0.45, ease }}>
                {line}
              </motion.li>
            ))}
          </ul>
          {onAsk && (
            <button className="rdoc-ask" onClick={() => onAsk(`Analyse the ${doc.title} report${doc.period ? ` (${periodText(doc.period)})` : ''} and tell me what to do about it.`)}>
              <Icon name="wave" size={13} /> Ask CIRA what to do
            </button>
          )}
        </section>
      )}

      {others.length > 0 && (
        <div className="rdoc-more">
          {others.map((s) => (
            <span key={s.label}>
              {s.label}
              <b>{stat(s.value, s.kind)}</b>
            </span>
          ))}
        </div>
      )}

      {doc.columns.length > 0 && (
        <div className="rdoc-table-wrap">
          {!page && (
            <button className="rdoc-toggle" onClick={() => setTableOpen((o) => !o)} aria-expanded={tableOpen}>
              <Icon name="chevron" size={14} className={tableOpen ? 'up' : undefined} />
              {tableOpen ? 'Hide the details' : statement ? 'Show the statement' : `Show the details · ${doc.rows.length} row${doc.rows.length === 1 ? '' : 's'}`}
            </button>
          )}
          <AnimatePresence initial={false}>
            {tableOpen && (
              <motion.div className="rdoc-table" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.35, ease }}>
                <div className="rdoc-scroll">
                  <table>
                    <thead>
                      <tr>
                        {doc.columns.map((c) => (
                          <th key={c.key} className={NUMERIC.includes(c.kind) ? 'r' : undefined}>
                            {c.label}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row, i) => {
                        const line = lineOf(row)
                        const cls = [rowTone(row) ? `tone-${rowTone(row)}` : '', line ? `ln-${line.kind}` : ''].filter(Boolean).join(' ')
                        return (
                          <tr key={i} className={cls || undefined}>
                            {doc.columns.map((c) => {
                              const v = row[c.key]
                              const numeric = NUMERIC.includes(c.kind)
                              const indent = line && c.key === nameKey && line.level > 0 ? { paddingLeft: 11 + line.level * 16 } : undefined
                              // a statement's empty figure (a heading without one) is blank, not a dash
                              const text = line && (v === null || v === undefined || v === '') ? '' : cell(v, c.kind)
                              return (
                                <td key={c.key} className={[numeric ? 'r' : '', numeric && typeof v === 'number' && v < 0 && c.kind === 'money' ? 'neg' : ''].filter(Boolean).join(' ') || undefined} style={indent}>
                                  {text}
                                </td>
                              )
                            })}
                          </tr>
                        )
                      })}
                      {doc.rows.length === 0 && (
                        <tr>
                          <td colSpan={doc.columns.length}>Nothing to show for this selection.</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
                {statement
                  ? hiddenAccounts > 0 && (
                      <button className="rdoc-all" onClick={() => setShowAll(true)}>
                        Show every account ({hiddenAccounts} more)
                      </button>
                    )
                  : doc.rows.length > limit && (
                      <button className="rdoc-all" onClick={() => setShowAll(true)}>
                        Show all {doc.rows.length} rows
                      </button>
                    )}
                {doc.total_rows !== undefined && doc.total_rows > doc.rows.length && (
                  <p className="rdoc-cap">The first {doc.rows.length} of {doc.total_rows.toLocaleString('en-IN')} rows — download Excel for every row.</p>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      )}

      {page && doc.notes && doc.notes.length > 0 && (
        <ul className="rdoc-notes">
          {doc.notes.map((n) => (
            <li key={n}>{n}</li>
          ))}
        </ul>
      )}
    </motion.article>
  )
}

/** Excel (from the server, every row), PDF (a branded A4 document) and CSV. */
function Downloads({ doc }: { doc: ReportDoc }) {
  const { state } = useAuth()
  const [busy, setBusy] = useState<'xlsx' | 'pdf' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fresh = doc.kind === 'report' && !!doc.report && !!state?.access?.can_reports
  const excel = async () => {
    setBusy('xlsx')
    setError(null)
    try {
      await downloadExcel(doc, fresh)
    } catch (err) {
      setError((err as Error).message)
    } finally {
      setBusy(null)
    }
  }
  const pdf = () => {
    setBusy('pdf')
    printPdf(doc, state?.company ?? '')
    window.setTimeout(() => setBusy(null), 1600)
  }
  return (
    <div className="rdoc-dl">
      <button onClick={excel} disabled={!!busy || !doc.columns.length} title={fresh ? 'Excel — read fresh from SAP, every row' : 'Excel'}>
        {busy === 'xlsx' ? <span className="spinner" /> : <Icon name="download" size={13} />} Excel
      </button>
      <button onClick={pdf} disabled={!!busy} title="PDF — a report you can print or save">
        {busy === 'pdf' ? <span className="spinner" /> : <Icon name="file" size={13} />} PDF
      </button>
      <button onClick={() => downloadCsv(doc)} disabled={!doc.columns.length} title="CSV">
        CSV
      </button>
      {error && <span className="rdoc-dl-error" title={error}>Download failed</span>}
    </div>
  )
}
