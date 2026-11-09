import { useState, type FormEvent } from 'react';
import { api, ApiError } from '../api';
import type { Me } from '../types';
import { saveSession } from '../session';

export function Login({ onSignedIn }: { onSignedIn: (token: string, me: Me) => void }) {
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const { token, ...me } = await api<Me & { token: string }>('/login/', { method: 'POST', body: { phone, password } });
      saveSession({ token, me });
      onSignedIn(token, me);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form className="box" onSubmit={submit}>
        <img className="mark" src="/icon.svg" alt="" />
        <div>
          <h1>Salon Owner</h1>
          <p className="muted">Sign in with the mobile number and password made for this app.</p>
        </div>
        <div className="field">
          <label htmlFor="phone">Mobile number</label>
          <input id="phone" className="input" autoComplete="username" autoCapitalize="none" placeholder="98765 43210" value={phone} onChange={(e) => setPhone(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input id="password" className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} />
        </div>
        {error ? <div className="notice bad" role="alert">{error}</div> : null}
        <button className="btn primary block" disabled={busy || !phone.trim() || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </main>
  );
}
