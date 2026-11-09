import { useCallback, useEffect, useState } from 'react';
import { api, setSignedOutHandler } from './api';
import { clearSession, loadSession, saveSession, type Session } from './session';
import type { Me } from './types';
import { AppointmentsScreen } from './screens/Appointments';
import { JobCardScreen } from './screens/JobCard';
import { Login } from './screens/Login';
import { MonthScreen } from './screens/Month';
import { ProfileScreen } from './screens/Profile';
import { Icon } from './ui/Icon';

type Tab = 'jobcard' | 'month' | 'visits' | 'me';
const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: 'jobcard', label: 'Job Card', icon: 'jobcard' },
  { id: 'month', label: 'Month', icon: 'chart' },
  { id: 'visits', label: 'Appointments', icon: 'visits' },
  { id: 'me', label: 'Profile', icon: 'me' },
];

function tabFromHash(): Tab {
  const id = window.location.hash.replace('#', '') as Tab;
  return TABS.some((t) => t.id === id) ? id : 'jobcard';
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
        <span className="today">{me.designation || me.salon}</span>
      </header>
      <main className="stage">
        {tab === 'jobcard' && <JobCardScreen key="jobcard" />}
        {tab === 'month' && <MonthScreen key="month" />}
        {tab === 'visits' && <AppointmentsScreen key="visits" />}
        {tab === 'me' && <ProfileScreen key="me" onSignOut={onSignOut} />}
      </main>
      <nav className="tabbar" aria-label="Screens">
        {TABS.map((t) => (
          <button key={t.id} aria-current={tab === t.id ? 'page' : undefined} aria-label={t.label} title={t.label} onClick={() => go(t.id)}>
            <Icon name={t.icon} size={21} />
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
