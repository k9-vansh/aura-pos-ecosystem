import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import type { AiKeys, AiProvider } from '../types';
import { Card, Notice } from '../ui/bits';

type Say = { kind: '' | 'bad' | 'good'; text: string };

/** One service: paste its key, pick a model, test it, make it the one in use, or take the key off. The key is sent once
 *  and never comes back (only its last four characters), so there is nothing here to read it from. */
function Editor({ p, onData, say }: { p: AiProvider; onData: (d: AiKeys) => void; say: (s: Say | null) => void }) {
  const [key, setKey] = useState('');
  const [model, setModel] = useState(p.model);
  const [url, setUrl] = useState(p.base_url);
  const [busy, setBusy] = useState('');

  useEffect(() => {
    setModel(p.model);
    setUrl(p.base_url);
  }, [p.model, p.base_url]);

  async function run(what: string, body: Record<string, unknown>, done?: (d: AiKeys) => Say | null) {
    setBusy(what);
    say(null);
    try {
      const d = await api<AiKeys>('/ai/', { method: 'PUT', body: { provider: p.id, ...body } });
      onData(d);
      setKey('');
      say(done ? done(d) : null);
    } catch (err) {
      say({ kind: 'bad', text: err instanceof ApiError ? err.message : 'Something went wrong. Try again.' });
    } finally {
      setBusy('');
    }
  }

  const save = () => run('save', { ...(key.trim() ? { key: key.trim() } : {}), model: model.trim(), ...(p.needs_url ? { base_url: url.trim() } : {}) },
    () => ({ kind: 'good', text: `${p.label} saved.` }));
  const test = () => run('test', { test: true }, (d) => (d.test ? { kind: d.test.ok ? 'good' : 'bad', text: `${p.label}: ${d.test.message}` } : null));
  const remove = () => run('remove', { remove: true }, () => ({ kind: 'good', text: `${p.label} key removed.` }));
  const use = () => run('use', { primary: true }, () => ({ kind: 'good', text: `${p.label} will write the reports.` }));
  const changed = key.trim() !== '' || model.trim() !== p.model || (p.needs_url && url.trim() !== p.base_url);

  return (
    <div style={{ padding: '4px 0 14px' }}>
      {p.note ? <p className="muted" style={{ marginBottom: 10 }}>{p.note}</p> : null}
      <div className="field">
        <label>{p.has_key ? 'Replace the key' : 'Paste the key'}</label>
        <input className="input" type="password" autoComplete="off" spellCheck={false} placeholder={p.has_key ? `Saved key ends …${p.last4}` : 'Paste it here'}
          value={key} onChange={(e) => setKey(e.target.value)} />
        {p.where ? <small className="muted">Get one at {p.where}</small> : null}
      </div>
      {p.needs_url ? (
        <div className="field" style={{ marginTop: 10 }}>
          <label>Service address</label>
          <input className="input" inputMode="url" autoComplete="off" placeholder="https://api.example.com/v1/chat/completions" value={url} onChange={(e) => setUrl(e.target.value)} />
        </div>
      ) : null}
      <div className="field" style={{ marginTop: 10 }}>
        <label>Model {p.needs_url ? '' : '(optional)'}</label>
        <input className="input" autoComplete="off" spellCheck={false} placeholder={p.default_model || 'Model name'} value={model} onChange={(e) => setModel(e.target.value)} />
      </div>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 12 }}>
        <button className="btn primary" disabled={!!busy || !changed} onClick={save}>{busy === 'save' ? 'Saving…' : 'Save'}</button>
        {p.has_key ? <button className="btn" disabled={!!busy} onClick={test}>{busy === 'test' ? 'Testing…' : 'Test'}</button> : null}
        {p.has_key && !p.primary ? <button className="btn" disabled={!!busy} onClick={use}>Use this one</button> : null}
        {p.has_key ? <button className="btn danger" disabled={!!busy} onClick={remove}>Remove key</button> : null}
      </div>
    </div>
  );
}

/** Settings > AI keys: the services that can write the weekly report. The one "in use" is asked first; if it fails the
 *  next saved one is tried, so a report still gets written. */
export function AiKeysCard() {
  const [data, setData] = useState<AiKeys | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [note, setNote] = useState<Say | null>(null);

  const load = () => api<AiKeys>('/ai/').then((d) => { setData(d); setError(null); })
    .catch((err) => setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.'));
  useEffect(() => { load(); }, []);

  if (error) return <Card title="AI keys"><Notice kind="bad" action={<button className="link" onClick={load}>Try again</button>}>{error}</Notice></Card>;
  if (!data) return <Card title="AI keys"><p className="muted">Loading…</p></Card>;

  const saved = data.providers.filter((p) => p.has_key).length;
  return (
    <Card title="AI keys" aside={<span>{saved ? `${saved} saved` : 'none yet'}</span>}>
      <p className="muted" style={{ marginBottom: 6 }}>
        Paste a key from any of these and the weekly report is written by it. Save more than one and, if the one in use fails, the next is tried.
      </p>
      {data.server_key ? <p className="muted" style={{ marginBottom: 6 }}>The server already has an OpenRouter key of its own; a key pasted here is used before it.</p> : null}
      {data.providers.map((p) => (
        <div key={p.id} style={{ borderTop: '1px solid var(--line, rgba(0,0,0,.08))' }}>
          <button className="row" style={{ width: '100%', textAlign: 'left', background: 'none', border: 0, cursor: 'pointer' }}
            aria-expanded={open === p.id} onClick={() => { setOpen(open === p.id ? null : p.id); setNote(null); }}>
            <div className="name"><b>{p.label}</b><small>{p.has_key ? `Key ending …${p.last4}${p.model ? ` · ${p.model}` : ''}` : 'No key'}</small></div>
            <div className="val">{p.primary ? <span className="tagchip" style={{ padding: '4px 10px' }}>In use</span> : <small>{open === p.id ? 'Close' : p.has_key ? 'Edit' : 'Add'}</small>}</div>
          </button>
          {open === p.id ? <Editor p={p} onData={setData} say={setNote} /> : null}
        </div>
      ))}
      {note ? <div style={{ marginTop: 10 }}><Notice kind={note.kind}>{note.text}</Notice></div> : null}
    </Card>
  );
}
