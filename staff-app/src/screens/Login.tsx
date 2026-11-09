import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import { saveSession } from '../session';
import type { Me } from '../types';

export function Login({ onSignedIn }: { onSignedIn: (token: string, me: Me) => void }) {
  const [phone, setPhone] = useState('');
  const [pin, setPin] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
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

  return (
    <main className="login">
      <form className="box" onSubmit={submit}>
        <img className="mark" src="/icon.svg" alt="" />
        <div>
          <h1>My Work</h1>
          <p className="muted">Your job card, appointments and profile. Sign in with your mobile number and the PIN your manager set for you.</p>
        </div>
        <div className="field">
          <label htmlFor="phone">Mobile number</label>
          <input id="phone" className="input" inputMode="tel" autoComplete="username" placeholder="98765 43210" value={phone} onChange={(e) => setPhone(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="pin">PIN</label>
          <input id="pin" className="input pin" type="password" inputMode="numeric" autoComplete="current-password" maxLength={8}
            placeholder="••••" value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))} />
        </div>
        {error ? <div className="notice bad" role="alert">{error}</div> : null}
        <button className="btn primary block" disabled={busy || phone.replace(/\D/g, '').length < 10 || pin.length < 3}>
          {busy ? 'Please wait…' : 'Sign in'}
        </button>
        <p className="note">Forgot your PIN? Ask your manager to set a new one.</p>
      </form>
    </main>
  );
}
