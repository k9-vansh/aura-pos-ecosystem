import { useCallback, useEffect, useMemo, useRef, useState, lazy, Suspense, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import Rail, { RailItem } from '../components/Rail'
import Orb from '../components/Orb'
import Icon, { type IconName } from '../components/Icon'
import TileCard, { KpiCard, type Tint } from './dashboard/TileCard'
import TileDetail from './dashboard/TileDetail'
import TasksPanel from './dashboard/TasksPanel'
import SchedulesPanel from './dashboard/SchedulesPanel'
import Attention from './dashboard/Attention'
import StandoutRow, { standoutsFor } from './dashboard/Standout'
import SectionHead from './dashboard/Section'
import AnalyticsView from './dashboard/AnalyticsView'
import ReportsView from './dashboard/ReportsView'
import UserBadge from '../components/UserBadge'
import AppDock from '../components/AppDock'
import { MODULES, type ModuleId, type Tile, type View } from '../lib/erp/tiles'
import { RANGE_DAYS, RANGE_LABEL, type Range } from '../lib/erp/range'
import { useOverview, usePeriod } from '../lib/api'
import { useAuth } from '../lib/auth'
import { useGreeting } from '../lib/greeting'
import { CUSTOMIZE_EVENT, CUSTOMIZE_KEY, openAdmin } from '../lib/nav'
import { toggleMode, useAppearance } from '../lib/theme'
import { sfx } from '../lib/sound'
import { buildLiveTiles, liveOverviewTiles, recsFor } from '../lib/liveTiles'
import { arrangeArea, arrangeOverview, hide, isCustomized, NO_PREFS, pin, show, toggleArea, unpin, useDashPrefs, type DashPrefs } from '../lib/dashPrefs'
import type { CardEdit } from './dashboard/TileCard'

/** Renders children only once they've scrolled into the viewport — avoids computing 60+ tiles at once on load. */
function LazyTile({ children, height = 220 }: { children: ReactNode; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const [visible, setVisible] = useState(false)
  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (visible) return
    const io = new IntersectionObserver(
      ([entry]) => { if (entry.isIntersecting) { setVisible(true); io.disconnect() } },
      { rootMargin: '300px' },
    )
    io.observe(el)
    return () => io.disconnect()
  }, [visible])
  return (
    <div ref={ref} style={visible ? undefined : { minHeight: height, contain: 'layout' }}>
      {visible ? children : <span className="sk sk-tile" style={{ display: 'block', minHeight: height }} />}
    </div>
  )
}

type DashView = View | 'analytics' | 'reports'
const ease = [0.16, 1, 0.3, 1] as const
const RANGES: Range[] = ['7d', '30d', '90d', '12m']
const ANALYTICS = { id: 'analytics' as const, label: 'CIRA analytics', icon: 'spark' as IconName, blurb: 'How CIRA is used, how accurate she is, and what she has learned' }
const REPORTS = { id: 'reports' as const, label: 'Reports', icon: 'file' as IconName, blurb: 'Exact reports from SAP — analysed, charted and ready to download' }
const NO_TILES: Record<ModuleId, Tile[]> = { sales: [], purchase: [], finance: [], production: [], inventory: [] }
// each view opens with a row of bright headline cards (the references' style); the rest stay quiet glass
const TINTS: Tint[] = ['lemon', 'lilac', 'sky', 'peach']

// good questions to start from, per view (the answers come from SAP when asked)
const SUGGEST: Record<View, string[]> = {
  overview: ['What should I focus on today?', 'Compare this month with last month', 'How is cash looking?'],
  sales: ['Who are my top customers this month?', 'Which invoices are overdue?', 'How are sales trending this year?'],
  purchase: ['Which vendors do we buy most from?', 'What payments are due this week?', 'Which purchase orders are late?'],
  finance: ['What is our cash position?', 'Show profit and loss this year', 'Who owes us the most?'],
  production: ['Which production orders are late?', 'What materials are short?', 'What is due this week?'],
  inventory: ['Which items are below minimum stock?', 'What stock is not moving?', 'Stock value by warehouse'],
}

/** The view's headline figures: its first single-size tiles whose chart fits a small card. */
function splitHeadlines(tiles: Tile[]): { heads: Tile[]; rest: Tile[] } {
  const heads: Tile[] = []
  for (const t of tiles) {
    if (heads.length === TINTS.length) break
    const [cols, rows] = t.span ?? [1, 1]
    const fits = !t.viz || t.viz.type === 'spark' || t.viz.type === 'bars' || t.viz.type === 'gauge' || t.viz.type === 'stack'
    if (cols === 1 && rows === 1 && fits) heads.push(t)
  }
  return { heads, rest: tiles.filter((t) => !heads.includes(t)) }
}

interface Props {
  initialView?: DashView
  onHome: () => void
  onChat: (prompt?: string) => void
}

export default function Dashboard({ initialView = 'overview', onHome, onChat }: Props) {
  // What this person's role includes (the server enforces it; the page just doesn't offer the rest)
  const auth = useAuth().state
  const access = auth?.access
  const hello = useGreeting(auth?.user?.name).hello
  const isAdmin = !!access?.admin
  const canDash = !!access?.can_dashboard
  const canReports = !!access?.can_reports
  const canChat = !!access?.can_chat
  const canAct = !!access?.can_act
  // this person's own layout (pinned, hidden, the period it opens on)
  const mine = useDashPrefs()
  const prefs = mine.prefs
  const canCustomize = canDash && mine.signedIn
  const [editing, setEditing] = useState(false)
  const changePrefs = (next: DashPrefs) => {
    sfx.swap()
    void mine.save(next).catch(() => sfx.error())
  }
  const areaIds = MODULES.filter((m) => m.id !== 'overview' && access?.modules.includes(m.id as ModuleId)).map((m) => m.id as ModuleId)
  const modules = MODULES.filter((m) => m.id === 'overview' || (areaIds.includes(m.id as ModuleId) && !prefs.hidden_areas.includes(m.id as ModuleId)))
  const allowedView = (v: DashView): boolean =>
    v === 'analytics' ? isAdmin : v === 'reports' ? canReports : canDash && (v === 'overview' || !!access?.modules.includes(v as ModuleId))
  const firstView: DashView = canDash ? 'overview' : canReports ? 'reports' : 'overview'

  const [view, setView] = useState<DashView>(() => (allowedView(initialView) ? initialView : firstView))
  const [range, setRange] = useState<Range>(() => prefs.range ?? '30d')
  const [query, setQuery] = useState('')
  const [open, setOpen] = useState<Tile | null>(null)
  const { mode } = useAppearance()
  const [navOpen, setNavOpen] = useState(false)
  const mainRef = useRef<HTMLElement>(null)

  // Every tile is SAP's own figure, read afresh by the server on each refresh — there is no sample data
  const { data: ov, error, noSap, refreshing, refresh, connecting } = useOverview(60_000, canDash)
  // the flows (sales, purchases, cash, who they came from) for the chosen period — read with each overview, and
  // only ever from the same SAP connection as the rest of the page
  const { data: period, loading: periodLoading } = usePeriod(range, ov?.built_at, canDash && !!ov, ov?.connection?.key)
  const readAt = ov ? new Date(ov.built_at * 1000).toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : ''
  const company = ov?.connection?.company_db ? ` · ${ov.connection.company_db}` : '' // which SAP company these figures are from
  const all = useMemo(() => (ov ? buildLiveTiles(ov, range, period) : NO_TILES), [ov, range, period])
  const overview = useMemo(
    // (CIRA's recommendations have their own section above the tiles, with actions)
    () => (ov ? liveOverviewTiles(all, [], `live from SAP${company} · read at ${readAt}`) : []),
    [all, ov, readAt, company],
  )
  const special = view === 'analytics' || view === 'reports'
  const q = query.trim().toLowerCase()
  const everyTile = useMemo(() => [...new Map([...overview, ...Object.values(all).flat()].map((t) => [t.id, t])).values()], [overview, all])
  const tiles = useMemo(
    () => (view === 'overview' ? arrangeOverview(overview, everyTile, prefs) : special ? [] : arrangeArea(all[view as ModuleId], prefs)),
    [view, overview, everyTile, prefs, special, all],
  )
  const hiddenTiles = prefs.hidden.map((id) => everyTile.find((t) => t.id === id)).filter((t): t is Tile => !!t)
  const editFor = (t: Tile): CardEdit | undefined =>
    editing && !q
      ? {
          pinned: prefs.pinned.includes(t.id),
          onPin: () => changePrefs(prefs.pinned.includes(t.id) ? unpin(prefs, t.id) : pin(prefs, t.id)),
          onHide: () => changePrefs(hide(prefs, t.id)),
        }
      : undefined
  const shown = q ? everyTile.filter((t) => `${t.title} ${t.info}`.toLowerCase().includes(q)) : tiles
  // searching shows every match as one grid; a view opens with its headline row
  const { heads, rest } = useMemo(() => (q ? { heads: [], rest: shown } : splitHeadlines(shown)), [shown, q])
  const mod = view === 'analytics' ? ANALYTICS : view === 'reports' ? REPORTS : MODULES.find((m) => m.id === view)!
  const analytics = view === 'analytics' && !q
  const reports = view === 'reports' && !q
  const partsMissing = ov ? Object.keys(ov.errors).length : 0

  // the command palette (or a link) switches the area while the dashboard is open
  useEffect(() => {
    if (allowedView(initialView)) setView(initialView)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialView])

  // "Customize my dashboard" from the command palette: straight into Customize mode
  useEffect(() => {
    const open = () => {
      try {
        sessionStorage.removeItem(CUSTOMIZE_KEY)
      } catch {
        /* storage blocked */
      }
      setEditing(true)
      setQuery('')
    }
    try {
      if (sessionStorage.getItem(CUSTOMIZE_KEY)) open()
    } catch {
      /* storage blocked */
    }
    window.addEventListener(CUSTOMIZE_EVENT, open)
    return () => window.removeEventListener(CUSTOMIZE_EVENT, open)
  }, [])

  // Follow the address bar too (typed URLs, back/forward), not just the rail
  useEffect(() => {
    const onPop = () => {
      if (!location.hash.startsWith('#/dashboard')) return
      const v = location.hash.split('/')[2]
      const next: DashView = v === 'analytics' || v === 'reports' ? v : (MODULES.find((m) => m.id === v)?.id ?? 'overview')
      setView(allowedView(next) ? next : firstView)
      setQuery('')
      setOpen(null)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [access])

  const go = (v: DashView) => {
    if (v !== view) sfx.swap()
    setView(v)
    setQuery('')
    setNavOpen(false)
    mainRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
    history.replaceState(null, '', v === 'overview' ? '#/dashboard' : `#/dashboard/${v}`)
    // the app follows along (its route is what the command palette and links compare against)
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  const openTile = useCallback((t: Tile) => setOpen(t), [])
  const closeTile = useCallback(() => setOpen(null), [])

  // The hero: CIRA's recommendations, ranked from the live numbers
  const recs = ov && !special ? recsFor(ov.recommendations, view as ModuleId | 'overview') : []
  const insight = recs.length ? recs.map((r) => `${r.title}.`).join(' ') : 'Nothing needs your attention here right now — the numbers look on track.'
  // below the headline cards: what to act on, then what stands out in the figures
  const attention = ov && !special ? recsFor(ov.recommendations, view as ModuleId | 'overview', view === 'overview' ? 6 : 4) : []
  const standouts = useMemo(
    () => (special ? [] : standoutsFor(ov, view as View, 6, period, RANGE_LABEL[range])),
    [ov, view, special, period, range],
  )

  return (
    <div className="shell dash" data-theme={mode}>
      <div className="dash-aurora" aria-hidden="true">
        <span />
        <span />
        <span />
      </div>

      <Rail label="Dashboard modules" mobileOpen={navOpen} onMobileClose={() => setNavOpen(false)}>
        <nav className="rail-nav">
          {canDash &&
            modules.map((m) => (
              <RailItem key={m.id} icon={<Icon name={m.icon} size={19} />} label={m.label} title={m.label} active={view === m.id && !q} onClick={() => go(m.id)} />
            ))}
          {(canReports || isAdmin) && <span className="rail-divider" aria-hidden="true" />}
          {canReports && <RailItem icon={<Icon name={REPORTS.icon} size={19} />} label={REPORTS.label} title={REPORTS.label} active={view === 'reports' && !q} onClick={() => go('reports')} />}
          {isAdmin && <RailItem icon={<Icon name={ANALYTICS.icon} size={19} />} label={ANALYTICS.label} title={ANALYTICS.label} active={view === 'analytics' && !q} onClick={() => go('analytics')} />}
        </nav>
        <div className="rail-foot">
          <UserBadge here="dashboard" />
        </div>
      </Rail>

      <main ref={mainRef} className="dash-main">
        {/* no navbar: the title is part of the page, the controls float in their own capsule */}
        <header className="dash-top">
          <button className="icon-btn nav-toggle" onClick={() => setNavOpen(true)} aria-label="Open menu">
            <Icon name="menu" />
          </button>
          <div className="dash-title">
            <AnimatePresence mode="wait">
              <motion.div key={q ? 'search' : view} initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -10 }} transition={{ duration: 0.4, ease }}>
                <h1>{q ? 'Search' : mod.label}</h1>
                <p>{q ? `${shown.length} tiles match “${query}”` : mod.blurb}</p>
              </motion.div>
            </AnimatePresence>
          </div>
        </header>
        <div className="capsule dash-tools">
          {!reports && (
            <div className={`cap-group ${periodLoading && !analytics ? 'reading' : ''}`} role="tablist" aria-label="Period" aria-busy={periodLoading && !analytics}>
              {RANGES.map((r) => (
                <button
                  key={r}
                  role="tab"
                  aria-selected={range === r}
                  className={`cap-item ${range === r ? 'on' : ''}`}
                  onClick={() => {
                    if (r !== range) sfx.swap()
                    setRange(r)
                  }}
                  title={analytics ? RANGE_LABEL[r] : `${RANGE_LABEL[r]} — sales, purchases, cash and the rankings follow it; balances are as they stand now`}
                >
                  {range === r && <motion.span layoutId="dash-range-pill" className="cap-pill" transition={{ type: 'spring', stiffness: 420, damping: 34 }} />}
                  <span>{r.toUpperCase()}</span>
                </button>
              ))}
            </div>
          )}
          {!reports && <span className="cap-sep" aria-hidden="true" />}
          {canDash && !special && (
            <label className="cap-search">
              <Icon name="search" size={14} />
              <input placeholder={`Search ${everyTile.length} tiles`} value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search tiles" />
            </label>
          )}
          {canCustomize && !special && (
            <button
              className={`cap-icon ${editing ? 'on' : ''}`}
              onClick={() => {
                sfx.swap()
                setEditing((e) => !e)
                setQuery('')
              }}
              aria-pressed={editing}
              aria-label="Customize my dashboard"
              title="Customize my dashboard — pin, hide, choose the period"
            >
              <Icon name="sliders" size={15} />
            </button>
          )}
          <button className="cap-icon" onClick={toggleMode} aria-label={mode === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'} title={mode === 'dark' ? 'Light theme' : 'Dark theme'}>
            <Icon name={mode === 'dark' ? 'sun' : 'moon'} size={15} />
          </button>
        </div>

        <AnimatePresence>
          {editing && !special && (
            <motion.section
              key="custom"
              className="dash-custom"
              aria-label="Customize your dashboard"
              initial={{ opacity: 0, y: -10, height: 0 }}
              animate={{ opacity: 1, y: 0, height: 'auto' }}
              exit={{ opacity: 0, y: -8, height: 0 }}
              transition={{ duration: 0.4, ease }}
            >
              <div className="dash-custom-in">
                <header className="dash-custom-head">
                  <span className="dash-custom-icon">
                    <Icon name="sliders" size={16} />
                  </span>
                  <div>
                    <b>Make it yours</b>
                    <p>
                      <Icon name="pin" size={11} /> pins a card first, <Icon name="eyeOff" size={11} /> hides it. Or ask CIRA: “pin top customers”, “hide production”.
                    </p>
                  </div>
                  <span className="dash-custom-actions">
                    {isCustomized(prefs) && (
                      <button className="ghost-btn" onClick={() => changePrefs(NO_PREFS)}>
                        <Icon name="refresh" size={13} /> Reset
                      </button>
                    )}
                    <button className="solid-btn" onClick={() => setEditing(false)}>
                      <Icon name="check" size={13} /> Done
                    </button>
                  </span>
                </header>
                {areaIds.length > 1 && (
                  <div className="dash-custom-row">
                    <span className="dash-custom-label">Areas</span>
                    {areaIds.map((a) => {
                      const on = !prefs.hidden_areas.includes(a)
                      const m = MODULES.find((x) => x.id === a)!
                      return (
                        <button key={a} className={`chip-toggle ${on ? 'on' : ''}`} aria-pressed={on} onClick={() => changePrefs(toggleArea(prefs, a))} title={on ? `Hide ${m.label}` : `Show ${m.label}`}>
                          <Icon name={m.icon} size={13} /> {m.label}
                        </button>
                      )
                    })}
                  </div>
                )}
                <div className="dash-custom-row">
                  <span className="dash-custom-label">Opens on</span>
                  {RANGES.map((r) => (
                    <button
                      key={r}
                      className={`chip-toggle ${(prefs.range ?? '30d') === r ? 'on' : ''}`}
                      aria-pressed={(prefs.range ?? '30d') === r}
                      onClick={() => {
                        setRange(r)
                        changePrefs({ ...prefs, range: r === '30d' ? null : r })
                      }}
                    >
                      {RANGE_LABEL[r]}
                    </button>
                  ))}
                </div>
                {hiddenTiles.length > 0 && (
                  <div className="dash-custom-row">
                    <span className="dash-custom-label">Hidden</span>
                    {hiddenTiles.map((t) => (
                      <button key={t.id} className="chip-toggle" onClick={() => changePrefs(show(prefs, t.id))} title={`Show ${t.title} again`}>
                        <Icon name="eye" size={13} /> {t.title}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </motion.section>
          )}
        </AnimatePresence>

        {!q && !analytics && !reports && canDash && (
          <section key={`hero-${view}`} className="dash-hero">
            <div className="dash-hero-orb">
              {view === 'overview' ? (
                <motion.div className="dash-hero-orb-in" initial={{ scale: 0.6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={{ duration: 0.9, ease }}>
                  <Orb />
                </motion.div>
              ) : (
                <ModuleEmblem key={view} icon={(mod as { icon: IconName }).icon} />
              )}
            </div>
            <motion.div className="dash-hero-text" initial={{ opacity: 0, x: 24 }} animate={{ opacity: 1, x: 0 }} transition={{ duration: 0.8, ease, delay: 0.12 }}>
              <p className="dash-hello">
                <b>{hello}</b> · {new Date().toLocaleDateString('en-IN', { weekday: 'long', day: 'numeric', month: 'long' })}
                <span className={`sync ${ov && !error ? '' : error ? 'offline' : 'syncing'}`}>
                  <i />
                  {ov
                    ? `Live from SAP${company} · read at ${readAt}`
                    : connecting
                      ? `Connecting to ${connecting.to}…`
                      : noSap
                        ? 'SAP not connected'
                        : error
                          ? 'SAP not reachable'
                          : 'Reading SAP…'}
                  {ov && error ? ' · last refresh failed' : ''}
                </span>
                {(ov || (error && !noSap)) && (
                  <button className="sync-refresh" onClick={refresh} disabled={refreshing} title="Read SAP again now">
                    {refreshing ? <span className="spinner" /> : <Icon name="refresh" size={13} />} {refreshing ? 'Reading SAP…' : 'Refresh now'}
                  </button>
                )}
              </p>
              {ov ? (
                // not keyed by the refresh: the line re-types only when what CIRA noticed changes, not every minute
                <Typewriter key={`${view}-${range}-${ov.connection?.key ?? ''}`} text={insight} />
              ) : (
                <p className="dash-insight">
                  {noSap
                    ? `The dashboard is built from SAP Business One, and ${auth?.org?.name ?? 'this company'} hasn’t connected it yet.${isAdmin ? ' Connect it in Admin → Connections and the figures fill in straight away.' : ' Ask your CIRA admin to connect it.'}`
                    : error
                      ? `CIRA can’t reach SAP right now (${error}). No figures are shown until it does — retrying every minute.`
                      : connecting
                        ? `Switching to ${connecting.to} — reading its figures from SAP. Nothing from the previous connection is shown.`
                        : 'Reading the latest numbers from SAP…'}
                </p>
              )}
              {noSap && isAdmin && (
                <div className="dash-asks">
                  <button className="ask-chip" onClick={() => openAdmin('connections')}>
                    <Icon name="plug" size={13} /> Connect SAP Business One
                  </button>
                </div>
              )}
              {canChat && (
                <div className="dash-asks">
                  {(SUGGEST[view as View] ?? []).map((ask, i) => (
                    <motion.button key={ask} className="ask-chip" onClick={() => onChat(ask)} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.35 + i * 0.07, duration: 0.5, ease }}>
                      <Icon name="spark" size={13} />
                      {ask}
                    </motion.button>
                  ))}
                </div>
              )}
            </motion.div>
          </section>
        )}

        <AnimatePresence mode="wait">
          {analytics ? (
            <motion.div key={`analytics-${range}`} className="dash-analytics" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.2 } }}>
              <AnalyticsView days={RANGE_DAYS[range]} onOpen={openTile} />
            </motion.div>
          ) : reports ? (
            <motion.div key="reports" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.2 } }}>
              <ReportsView onAsk={onChat} />
            </motion.div>
          ) : !canDash ? (
            <motion.div key="no-dash" className="no-access" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
              <b>The dashboard isn’t part of your role</b>
              Ask your CIRA admin to add it.
            </motion.div>
          ) : noSap ? (
            <motion.div key="no-sap" className="no-access" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
              <b>No SAP Business One connected yet</b>
              {isAdmin ? 'Admin → Connections sets it up in a minute — every card here then reads it live.' : 'Your CIRA admin can connect it; the chat can still answer from any other system your company connected.'}
            </motion.div>
          ) : !ov ? (
            // the cards' shapes, shimmering, while SAP answers — the page never looks empty or broken
            <motion.div key="waiting" className="dash-skeleton" initial={{ opacity: 0 }} animate={{ opacity: 1 }} aria-label={error ? 'Waiting for SAP' : 'Reading SAP'}>
              {connecting && (
                // a new SAP connection: the old company's figures are gone; this says so while the new one loads
                <motion.div className="dash-switch" role="status" initial={{ opacity: 0, y: -10, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
                  transition={{ duration: 0.5, ease }}>
                  <span className="dash-switch-ring" aria-hidden="true" />
                  <div className="dash-switch-text">
                    <b>Switching to {connecting.to}</b>
                    <em>{connecting.from ? `${connecting.from}’s figures are cleared — ` : ''}reading {connecting.to} live from SAP…</em>
                  </div>
                  <span className="dash-switch-bar" aria-hidden="true">
                    <i />
                  </span>
                </motion.div>
              )}
              <div className="dash-kpis">
                {[0, 1, 2, 3].map((i) => (
                  <span key={i} className="sk sk-kpi" style={{ animationDelay: `${i * 0.12}s` }} />
                ))}
              </div>
              <div className="dash-grid">
                {[2, 1, 1, 2, 1, 1, 1, 1].map((cols, i) => (
                  <span key={i} className="sk sk-tile" style={{ gridColumn: `span ${cols}`, animationDelay: `${0.3 + i * 0.08}s` }} />
                ))}
              </div>
              <p className="dash-empty">
                {error ? 'Waiting for SAP — the cards fill in as soon as it answers.' : connecting ? `Connecting to ${connecting.to} — reading its SAP data…` : 'Reading SAP…'}
              </p>
            </motion.div>
          ) : (
            // keyed by the SAP connection too: another company's figures never morph into these — the old ones leave, the new arrive
            <motion.div key={`${q ? 'q' : view}-${range}-${ov?.connection?.key ?? ''}`} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12, filter: 'blur(4px)', transition: { duration: 0.25 } }}>
              {heads.length > 0 && (
                <section className="dash-kpis" aria-label="Headline figures">
                  {heads.map((t, i) => (
                    <LazyTile key={t.id} height={160}>
                      <KpiCard tile={t} index={i} tint={TINTS[i]} onOpen={openTile} onAsk={canChat ? onChat : undefined} edit={editFor(t)} pinned={prefs.pinned.includes(t.id)} />
                    </LazyTile>
                  ))}
                </section>
              )}
              {!q && <Attention recs={attention} canChat={canChat} canAct={canAct} onAsk={onChat} />}
              {!q && <StandoutRow items={standouts} onAsk={onChat} canChat={canChat} />}
              {!q && (attention.length > 0 || standouts.length > 0) && rest.length > 0 && (
                <SectionHead
                  icon="grid"
                  title={view === 'overview' ? 'Your figures' : `All ${(mod as { label: string }).label.toLowerCase()} figures`}
                  count={rest.length}
                  note={`cards marked ${range.toUpperCase()} follow the period · the rest say what they cover · open any card for the breakdown`}
                  delay={0.1}
                />
              )}
              <section className="dash-grid">
                {view === 'overview' && !q && <TasksPanel index={heads.length} canAsk={canAct && canChat} />}
                {view === 'overview' && !q && canAct && <SchedulesPanel index={heads.length + 1} canAsk={canChat} />}
                {rest.map((t, i) => (
                  <LazyTile key={t.id} height={220}>
                    <TileCard tile={t} index={i + heads.length + 1} onOpen={openTile} onAsk={canChat ? onChat : undefined} edit={editFor(t)} pinned={prefs.pinned.includes(t.id)} />
                  </LazyTile>
                ))}
                {shown.length === 0 && <p className="dash-empty">No tiles match “{query}”.</p>}
              </section>
            </motion.div>
          )}
        </AnimatePresence>

        <p className="dash-foot">
          {analytics
            ? 'Analytics come from the CIRA server’s own logs and refresh every 30 seconds.'
            : reports
              ? 'Every report is read live from SAP when you run it — the same figures the chat and the tiles use.'
              : `Every figure is read live from SAP each time the dashboard refreshes (every minute, or when you press Refresh now) — nothing is cached or made up.${
                  partsMissing ? ` ${partsMissing} part(s) couldn’t be read this time and are left out rather than guessed.` : ''
                }`}
        </p>
      </main>

      <AppDock here="dashboard" />

      <AnimatePresence>
        {open && (
          <TileDetail
            key={open.id}
            tile={everyTile.find((t) => t.id === open.id) ?? open}
            range={range}
            onClose={closeTile}
            onAsk={(prompt) => {
              setOpen(null)
              onChat(prompt)
            }}
          />
        )}
      </AnimatePresence>
    </div>
  )
}

/** A module's emblem: a glass tile with its icon that flips in when you switch modules. */
function ModuleEmblem({ icon }: { icon: IconName }) {
  return (
    <motion.div
      className="mod-emblem"
      initial={{ rotateY: -110, scale: 0.55, opacity: 0 }}
      animate={{ rotateY: 0, scale: 1, opacity: 1 }}
      transition={{ type: 'spring', stiffness: 120, damping: 16, mass: 0.9 }}
    >
      <span className="mod-emblem-ring" aria-hidden="true" />
      <span className="mod-emblem-glass">
        <motion.span initial={{ scale: 0.3, rotate: -30, opacity: 0 }} animate={{ scale: 1, rotate: 0, opacity: 1 }} transition={{ delay: 0.25, duration: 0.7, ease }}>
          <Icon name={icon} size={46} stroke={1.6} />
        </motion.span>
      </span>
      <span className="mod-emblem-shine" aria-hidden="true" />
    </motion.div>
  )
}

/** CIRA's insight, typed out a few characters at a time. */
function Typewriter({ text }: { text: string }) {
  const [n, setN] = useState(0)
  useEffect(() => {
    setN(0)
    let i = 0
    const id = window.setInterval(() => {
      i += 3
      setN(i)
      if (i >= text.length) window.clearInterval(id)
    }, 18)
    return () => window.clearInterval(id)
  }, [text])
  return (
    <p className="dash-insight">
      <b>CIRA noticed</b> {text.slice(0, n)}
      {n < text.length && <span className="type-caret" />}
    </p>
  )
}
