import { useCallback, useEffect, useState } from 'react';
import { api, setSignedOutHandler } from './api';
import { clearSession, loadSession, saveSession, type Session } from './session';
import type { Me } from './types';
import { BillsScreen } from './screens/Bills';
import { HomeScreen } from './screens/Home';
import { Login } from './screens/Login';
import { ProfileScreen } from './screens/Profile';
import { VisitsScreen } from './screens/Visits';
import { Icon } from './ui/Icon';

type Tab = 'home' | 'bills' | 'visits' | 'me';
const TABS: { id: Tab; label: string }[] = [
  { id: 'home', label: 'Wallet' },
  { id: 'bills', label: 'Bills' },
  { id: 'visits', label: 'Visits' },
  { id: 'me', label: 'Me' },
];

function tabFromHash(): Tab {
  const id = window.location.hash.replace('#', '') as Tab;
  return TABS.some((t) => t.id === id) ? id : 'home';
}

function Shell({ session, onSignOut }: { session: Session; onSignOut: () => void }) {
  const [me, setMe] = useState<Me>(session.me);
  const [tab, setTab] = useState<Tab>(tabFromHash);

  useEffect(() => {
    // Now, then every few seconds and whenever the app comes back to the front: a name changed at the salon shows up here.
    const refresh = () => {
      if (document.hidden) return;
      api<Me>('/me/').then((fresh) => {
        setMe((old) => (JSON.stringify(old) === JSON.stringify(fresh) ? old : fresh));
        saveSession({ token: loadSession()?.token ?? session.token, me: fresh });
      }).catch(() => undefined);
    };
    refresh();
    const timer = setInterval(refresh, 15000);
    document.addEventListener('visibilitychange', refresh);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', refresh);
    };
  }, [session.token]);

  useEffect(() => {
    const onHash = () => setTab(tabFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const go = (id: Tab) => {
    window.location.hash = id;
    setTab(id);
    window.scrollTo({ top: 0 });
  };
  const hour = new Date().getHours();
  const greeting = hour < 12 ? 'Good morning' : hour < 17 ? 'Good afternoon' : 'Good evening';

  return (
    <div className="app">
      <header className="head">
        <button className="avatar" onClick={() => go('me')} aria-label="My profile">{(me.first_name || me.name || '?').charAt(0).toUpperCase()}</button>
        <div className="who"><small>{greeting}</small><b className="display">{me.first_name || me.name}</b></div>
        <span className="today">{new Date().toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' })}</span>
      </header>
      <main className="stage">
        {tab === 'home' && <HomeScreen key="home" goPin={() => go('me')} goVisits={() => go('visits')} />}
        {tab === 'bills' && <BillsScreen key="bills" />}
        {tab === 'visits' && <VisitsScreen key="visits" />}
        {tab === 'me' && <ProfileScreen key="me" me={me} onSignOut={onSignOut} />}
      </main>
      <nav className="tabbar" aria-label="Screens">
        {TABS.map((t) => (
          <button key={t.id} aria-current={tab === t.id ? 'page' : undefined} aria-label={t.label} title={t.label} onClick={() => go(t.id)}>
            <Icon name={t.id} size={21} />
            <span>{t.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}

export default function App() {
  const [session, setSession] = useState<Session | null>(() => loadSession());
  const signOut = useCallback(() => {
    api('/logout/', { method: 'POST' }).catch(() => undefined);   // best effort: a dead signal must not trap anyone in
    clearSession();
    setSession(null);
  }, []);
  useEffect(() => setSignedOutHandler(signOut), [signOut]);

  return session
    ? <Shell session={session} onSignOut={signOut} />
    : <Login onSignedIn={(token, me) => setSession({ token, me })} />;
}
