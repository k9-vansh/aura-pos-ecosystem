import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import { saveSession } from '../session';
import type { Me } from '../types';

export function Login({ onSignedIn }: { onSignedIn: (token: string, me: Me) => void }) {
  const [phone, setPhone] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setInfo(null);
    try {
      const { token, ...me } = await api<Me & { token: string }>('/login/', { method: 'POST', body: { phone, pin } });
      saveSession({ token, me });
      onSignedIn(token, me);
    } catch (err) {
      const left = err instanceof ApiError ? err.body.attempts_left : undefined;
      setError(err instanceof ApiError ? err.message + (typeof left === 'number' && left > 0 ? ` ${left} ${left === 1 ? 'try' : 'tries'} left.` : '') : 'Something went wrong. Try again.');
      setPin('');
      setBusy(false);
    }
  }

  async function forgot() {
    setError(null);
    setInfo(null);
    if (phone.replace(/\D/g, '').length < 10) {
      setError('Type your mobile number first, then tap “Forgot your PIN?”.');
      return;
    }
    setBusy(true);
    try {
      const res = await api<{ detail: string }>('/forgot/', { method: 'POST', body: { phone } });
      setInfo(res.detail);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="box" onSubmit={submit}>
        <img className="mark" src="/icon.svg" alt="" />
        <div>
          <h1>My Salon</h1>
          <p className="muted">Your wallet, bills and visits. Sign in with your mobile number and the 4-digit PIN the salon gave you.</p>
        </div>
        <div className="field">
          <label htmlFor="phone">Mobile number</label>
          <input id="phone" className="input" inputMode="tel" autoComplete="username" placeholder="98765 43210" value={phone} onChange={(e) => setPhone(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="pin">PIN</label>
          <input
            id="pin" className="input pin" type="password" inputMode="numeric" autoComplete="current-password" maxLength={4}
            placeholder="••••" value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 4))}
          />
        </div>
        {error ? <div className="notice bad" role="alert">{error}</div> : null}
        {info ? <div className="notice good" role="status">{info}</div> : null}
        <button className="btn primary block" disabled={busy || phone.replace(/\D/g, '').length < 10 || pin.length !== 4}>
          {busy ? 'Please wait…' : 'Sign in'}
        </button>
        <button type="button" className="link" onClick={forgot} disabled={busy}>Forgot your PIN?</button>
      </form>
    </main>
  );
}
