import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, setSignedOutHandler } from './api';
import { clearSession, loadSalon, loadSession, saveSalon, saveSession, type Session } from './session';
import type { Me, Screen } from './types';
import { BillsScreen } from './screens/Bills';
import { Login } from './screens/Login';
import { MonthScreen } from './screens/Month';
import { ReportScreen } from './screens/Report';
import { SettingsScreen } from './screens/Settings';
import { StaffScreen } from './screens/Staff';
import { TodayScreen } from './screens/Today';
import { Icon } from './ui/Icon';
import { Sheet } from './ui/Sheet';

const TABS: { id: Screen; label: string }[] = [
  { id: 'today', label: 'Today' },
  { id: 'month', label: 'Month' },
  { id: 'invoices', label: 'Bills' },
  { id: 'staff', label: 'Staff' },
  { id: 'ai_report', label: 'Report' },
  { id: 'settings', label: 'Settings' },
];

function tabFromHash(allowed: Screen[]): Screen {
  const id = window.location.hash.replace('#', '') as Screen;
  return allowed.includes(id) ? id : allowed[0];
}

function SalonSheet({ me, value, onPick, onClose }: { me: Me; value: string; onPick: (v: string) => void; onClose: () => void }) {
  const [q, setQ] = useState('');
  const shown = me.salons.filter((s) => s.name.toLowerCase().includes(q.trim().toLowerCase()));
  return (
    <Sheet title="Choose a salon" onClose={onClose}>
      {me.salons.length > 6 ? (
        <div className="field" style={{ marginBottom: 8 }}>
          <input className="input" autoFocus placeholder={`Search ${me.salons.length} salons`} value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search salons" />
        </div>
      ) : null}
      <div className="scroll" role="listbox">
        {me.salons.length > 1 && !q ? (
          <button className="pick" role="option" aria-selected={value === 'all'} onClick={() => onPick('all')}>
            <span>All my salons</span>{value === 'all' ? <Icon name="check" size={18} /> : null}
          </button>
        ) : null}
        {shown.map((s) => (
          <button key={s.id} className="pick" role="option" aria-selected={value === String(s.id)} onClick={() => onPick(String(s.id))}>
            <span>{s.name}</span>{value === String(s.id) ? <Icon name="check" size={18} /> : null}
          </button>
        ))}
        {shown.length === 0 ? <p className="empty">No salon matches “{q}”.</p> : null}
      </div>
    </Sheet>
  );
}

function Shell({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const [me, setMe] = useState<Me>(session.me);
  const allowed = useMemo(() => TABS.filter((t) => me.screens[t.id]).map((t) => t.id), [me]);
  const [tab, setTab] = useState<Screen>(() => tabFromHash(allowed));
  const [salon, setSalon] = useState(() => loadSalon(session.me));
  const [picking, setPicking] = useState(false);
  const [round, setRound] = useState(0);
  const [spinning, setSpinning] = useState(false);

  // The role may have changed since the last sign-in: ask who we are now (quietly; the saved copy is used meanwhile).
  useEffect(() => {
    api<Me>('/me/').then((fresh) => {
      setMe(fresh);
      saveSession({ token: session.token, me: fresh });
      setSalon((s) => (s === 'all' || fresh.salons.some((x) => String(x.id) === s) ? s : loadSalon(fresh)));
    }).catch(() => undefined);
  }, [session.token]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash(allowed));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [allowed]);

  const go = (id: Screen) => {
    window.location.hash = id;
    setTab(id);
    window.scrollTo({ top: 0 });
  };
  const choose = (value: string) => {
    saveSalon(value);
    setSalon(value);
    setPicking(false);
  };
  const refresh = useCallback(() => {
    setRound((n) => n + 1);
    setSpinning(true);
    window.setTimeout(() => setSpinning(false), 900);
  }, []);

  if (allowed.length === 0) {
    return (
      <div className="login"><div className="box">
        <h1>No screens yet</h1>
        <p className="muted">Your role has no Owner App screens ticked. Ask the person who manages roles.</p>
        <button className="btn" onClick={onSignOut}>Sign out</button>
      </div></div>
    );
  }

  const salonName = salon === 'all' ? `All ${me.salons.length} salons` : me.salons.find((s) => String(s.id) === salon)?.name ?? 'Salon';
  const key = `${tab}-${salon}-${round}`;
  const initial = (me.name || '?').trim().charAt(0).toUpperCase();
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';

  return (
    <div className="app">
      <header className="head">
        <button className="avatar" onClick={() => go('settings')} aria-label="Settings">{initial}</button>
        <div className="who">
          <small>{greeting},</small>
          <b>{me.name}</b>
          <button className="where" onClick={() => setPicking(true)} aria-label="Change salon">
            <Icon name="pin" size={13} /><span>{salonName}</span><Icon name="chevron" size={13} />
          </button>
        </div>
        <button className={`round${spinning ? ' spin' : ''}`} onClick={refresh} aria-label="Refresh"><Icon name="refresh" size={19} /></button>
        <button className="round" onClick={() => setPicking(true)} aria-label="Salons"><Icon name="store" size={19} /></button>
      </header>

      <main className="stage">
        {tab === 'today' && <TodayScreen key={key} salon={salon} />}
        {tab === 'month' && <MonthScreen key={key} salon={salon} />}
        {tab === 'invoices' && <BillsScreen key={key} salon={salon} />}
        {tab === 'staff' && <StaffScreen key={key} salon={salon} />}
        {tab === 'ai_report' && <ReportScreen key={key} salon={salon} me={me} pickSalon={() => setPicking(true)} />}
        {tab === 'settings' && <SettingsScreen key={key} salon={salon} me={me} onSignOut={onSignOut} pickSalon={() => setPicking(true)} />}
      </main>

      <nav className="tabbar" aria-label="Screens">
        {TABS.filter((t) => allowed.includes(t.id)).map((t) => (
          <button key={t.id} aria-current={tab === t.id ? 'page' : undefined} aria-label={t.label} title={t.label} onClick={() => go(t.id)}>
            <Icon name={t.id} size={21} />
            <span>{t.label}</span>
          </button>
        ))}
      </nav>
      {picking ? <SalonSheet me={me} value={salon} onPick={choose} onClose={() => setPicking(false)} /> : null}
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(() => loadSession());
  const signOut = useCallback(() => {
    // End this device's sign-in on the server too (best effort: a dead signal must not trap the person in).
    api('/logout/', { method: 'POST' }).catch(() => undefined).finally(() => undefined);
    clearSession();
    setSession(null);
  }, []);
  useEffect(() => setSignedOutHandler(signOut), [signOut]);

  return session
    ? <Shell session={session} onSignOut={signOut} />
    : <Login onSignedIn={(token, me) => setSession({ token, me })} />;
}
