import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import { phone as prettyPhone } from '../format';
import { loadSession, saveSession } from '../session';
import { loadTheme, saveTheme, THEMES } from '../theme';
import type { Me } from '../types';
import { Card, Notice } from '../ui/bits';
import { Icon } from '../ui/Icon';

/** The client's own PIN: hidden until asked for, held in memory only, hidden again when they leave. */
function PinCard() {
  const [pin, setPin] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function show() {
    setError(null);
    try {
      setPin((await api<{ pin: string }>('/pin/')).pin || '');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
    }
  }

  return (
    <Card title="Your PIN" kind="accent">
      <p className="muted" style={{ marginBottom: 12 }}>
        The salon asks for this PIN before your card, advance or membership is used. Tell it only to the person at the counter, and never to anyone who calls you.
      </p>
      <div className="pinshow" aria-live="polite">{pin === null ? '••••' : pin || 'No PIN yet'}</div>
      {error ? <Notice kind="bad">{error}</Notice> : null}
      <button className="btn block" onClick={pin === null ? show : () => setPin(null)}>
        <Icon name={pin === null ? 'eye' : 'eyeoff'} size={18} /> {pin === null ? 'Show my PIN' : 'Hide'}
      </button>
    </Card>
  );
}

function ChangePin() {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: 'bad' | 'good'; text: string } | null>(null);
  const digits = (v: string) => v.replace(/\D/g, '').slice(0, 4);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setNote(null);
    try {
      const res = await api<{ token: string; detail: string }>('/pin/', { method: 'POST', body: { current, new: next } });
      const session = loadSession();
      if (session) saveSession({ ...session, token: res.token });          // the old sign-in ended; this one carries on
      setNote({ kind: 'good', text: res.detail });
      setCurrent('');
      setNext('');
      setOpen(false);
    } catch (err) {
      setNote({ kind: 'bad', text: err instanceof ApiError ? err.message : 'Something went wrong. Try again.' });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Change PIN">
      {!open ? (
        <>
          {note ? <Notice kind={note.kind}>{note.text}</Notice> : <p className="muted">Pick a new 4-digit PIN that only you know.</p>}
          <button className="btn block" style={{ marginTop: 12 }} onClick={() => { setOpen(true); setNote(null); }}>Change my PIN</button>
        </>
      ) : (
        <form onSubmit={submit} style={{ display: 'grid', gap: 12 }}>
          <div className="field"><label htmlFor="cur">Current PIN</label>
            <input id="cur" className="input pin" type="password" inputMode="numeric" maxLength={4} autoComplete="current-password" value={current} onChange={(e) => setCurrent(digits(e.target.value))} /></div>
          <div className="field"><label htmlFor="new">New PIN</label>
            <input id="new" className="input pin" type="password" inputMode="numeric" maxLength={4} autoComplete="new-password" value={next} onChange={(e) => setNext(digits(e.target.value))} /></div>
          {note ? <Notice kind={note.kind}>{note.text}</Notice> : null}
          <button className="btn primary block" disabled={busy || current.length !== 4 || next.length !== 4}>{busy ? 'Saving…' : 'Save new PIN'}</button>
          <button type="button" className="link" onClick={() => setOpen(false)}>Cancel</button>
        </form>
      )}
    </Card>
  );
}

function LookCard() {
  const [current, setCurrent] = useState(loadTheme);
  return (
    <Card title="Look & feel" aside={THEMES.find((t) => t.id === current)?.name}>
      <div className="themes" role="radiogroup" aria-label="Theme">
        {THEMES.map((t) => (
          <button key={t.id} role="radio" aria-checked={t.id === current} onClick={() => { saveTheme(t.id); setCurrent(t.id); }}>
            <span className="sw" style={{ background: t.swatch[0] }}>
              <i style={{ background: t.swatch[1] }} /><i style={{ background: t.swatch[2] }} />
            </span>
            <small>{t.name}</small>
          </button>
        ))}
      </div>
    </Card>
  );
}

export function ProfileScreen({ me, onSignOut }: { me: Me; onSignOut: () => void }) {
  return (
    <>
      <Card>
        <div className="row" style={{ padding: 0, border: 0 }}>
          <div className="lead">
            <span className="initial big">{(me.first_name || me.name || '?').charAt(0).toUpperCase()}</span>
            <div className="name"><b style={{ fontSize: 17 }}>{me.name}</b><small>{prettyPhone(me.phone)}{me.salon ? ` · ${me.salon}` : ''}</small></div>
          </div>
        </div>
      </Card>
      <PinCard />
      <ChangePin />
      <LookCard />
      <button className="btn danger block" onClick={onSignOut}><Icon name="logout" size={18} /> Sign out</button>
    </>
  );
}
