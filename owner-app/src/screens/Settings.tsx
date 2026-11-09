import { useEffect, useState } from 'react';
import { api, ApiError } from '../api';
import { phone as prettyPhone } from '../format';
import { useApi } from '../hooks';
import type { MasterPins, Me, SalonSettings } from '../types';
import { Card, Notice, Page, Waiting } from '../ui/bits';
import { Icon } from '../ui/Icon';
import { AiKeysCard } from './AiKeys';

function ChipList({ values, onRemove, format = (v: string) => v }: { values: string[]; onRemove?: (v: string) => void; format?: (v: string) => string }) {
  if (!values.length) return <p className="muted">None yet.</p>;
  return (
    <div className="chips">
      {values.map((v) => (
        <span className="tagchip" key={v} style={onRemove ? undefined : { padding: '6px 12px' }}>
          {format(v)}
          {onRemove ? <button aria-label={`Remove ${v}`} onClick={() => onRemove(v)}><Icon name="close" size={14} /></button> : null}
        </span>
      ))}
    </div>
  );
}

function AddRow({ label, placeholder, type = 'text', inputMode, onAdd, disabled }: { label: string; placeholder: string; type?: string; inputMode?: 'tel' | 'email'; onAdd: (v: string) => void; disabled?: boolean }) {
  const [text, setText] = useState('');
  const add = () => {
    if (text.trim()) {
      onAdd(text.trim());
      setText('');
    }
  };
  return (
    <div className="field" style={{ marginTop: 12 }}>
      <label>{label}</label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input className="input" type={type} inputMode={inputMode} placeholder={placeholder} value={text} disabled={disabled}
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && add()} />
        <button className="btn" onClick={add} disabled={disabled || !text.trim()} aria-label="Add"><Icon name="plus" size={18} /></button>
      </div>
    </div>
  );
}

function Contacts({ salon, canEdit }: { salon: string; canEdit: boolean }) {
  const { data, error, loading, stale, reload } = useApi<SalonSettings>('/settings/', { center_id: salon }, true, false);
  const [sms, setSms] = useState<string[]>([]);
  const [owners, setOwners] = useState<string[]>([]);
  const [accounts, setAccounts] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: '' | 'bad' | 'good'; text: string } | null>(null);

  useEffect(() => {
    if (data) {
      setSms(data.sms_numbers);
      setOwners(data.owner_emails);
      setAccounts(data.accounts_emails);
    }
  }, [data]);

  if (!data) return <Waiting error={error} stale={stale} reload={reload} loading={loading} h={260} />;
  const dirty = JSON.stringify([sms, owners, accounts]) !== JSON.stringify([data.sms_numbers, data.owner_emails, data.accounts_emails]);

  async function save() {
    setBusy(true);
    setNote(null);
    try {
      await api<SalonSettings>('/settings/', { method: 'PUT', body: { center_id: Number(salon), sms_numbers: sms, owner_emails: owners, accounts_emails: accounts } });
      setNote({ kind: 'good', text: 'Saved.' });
      reload();
    } catch (err) {
      setNote({ kind: 'bad', text: err instanceof ApiError ? err.message : 'Something went wrong. Try again.' });
    } finally {
      setBusy(false);
    }
  }

  const add = (list: string[], set: (v: string[]) => void, max: number) => (v: string) => {
    if (!list.includes(v) && list.length < max) {
      setNote(null);
      set([...list, v]);
    }
  };

  return (
    <>
      <Card title="Day-end SMS numbers" aside={<span>{sms.length} of 5</span>}>
        <p className="muted" style={{ marginBottom: 10 }}>These numbers get the closing message when {data.salon} closes its day.</p>
        <ChipList values={sms} format={prettyPhone} onRemove={canEdit ? (v) => setSms(sms.filter((x) => x !== v)) : undefined} />
        {canEdit && sms.length < 5 ? <AddRow label="Add a number" placeholder="98765 43210" inputMode="tel" onAdd={add(sms, setSms, 5)} /> : null}
      </Card>
      <Card title="Report e-mails">
        <div className="field"><label>Owner</label><ChipList values={owners} onRemove={canEdit ? (v) => setOwners(owners.filter((x) => x !== v)) : undefined} /></div>
        {canEdit && owners.length < 3 ? <AddRow label="Add an owner e-mail" placeholder="name@example.com" type="email" inputMode="email" onAdd={add(owners, setOwners, 3)} /> : null}
        <div className="field" style={{ marginTop: 18 }}><label>Accounts</label><ChipList values={accounts} onRemove={canEdit ? (v) => setAccounts(accounts.filter((x) => x !== v)) : undefined} /></div>
        {canEdit && accounts.length < 2 ? <AddRow label="Add an accounts e-mail" placeholder="accounts@example.com" type="email" inputMode="email" onAdd={add(accounts, setAccounts, 2)} /> : null}
      </Card>
      {note ? <Notice kind={note.kind}>{note.text}</Notice> : null}
      {canEdit ? <button className="btn primary block" disabled={busy || !dirty} onClick={save}>{busy ? 'Saving…' : 'Save changes'}</button> : null}
    </>
  );
}

/** Today's master PIN of each salon: hidden until asked for, because it unlocks a client's money without their PIN. */
function MasterPinsCard({ salon, salons }: { salon: string; salons: Me['salons'] }) {
  const [open, setOpen] = useState(false);
  const [find, setFind] = useState('');
  const [data, setData] = useState<MasterPins | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Kept in memory only: unlike the other screens, a PIN is never saved on the phone.
  async function show() {
    setOpen(true);
    setError(null);
    try {
      setData(await api<MasterPins>('/master-pins/'));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Something went wrong. Try again.');
    }
  }
  function hide() {
    setOpen(false);
    setData(null);
    setFind('');
  }
  const rows = (data?.pins ?? [])
    .filter((p) => (salon === 'all' ? true : String(p.center_id) === salon))
    .filter((p) => p.center.toLowerCase().includes(find.trim().toLowerCase()));
  return (
    <Card title="Master PIN" aside={<span>{open ? 'Changes every day' : ''}</span>}>
      <p className="muted" style={{ marginBottom: 10 }}>
        Give it to a manager only when a client has forgotten their PIN and you have checked who is asking.
      </p>
      {!open ? (
        <button className="btn block" onClick={show}>Show today's master PIN</button>
      ) : error ? (
        <Notice kind="bad" action={<button className="link" onClick={show}>Try again</button>}>{error}</Notice>
      ) : !data ? (
        <p className="muted">Loading…</p>
      ) : (
        <>
          {salons.length > 4 ? (
            <input className="input" style={{ marginBottom: 10 }} placeholder="Find a salon" value={find} onChange={(e) => setFind(e.target.value)} />
          ) : null}
          {rows.length ? rows.map((p) => (
            <div className="row" key={p.center_id}>
              <div className="name"><b>{p.center}</b><small>{data.date}</small></div>
              <div className="val" style={{ fontSize: 24, letterSpacing: '0.18em' }}>{p.pin}</div>
            </div>
          )) : <p className="muted">No salon found.</p>}
          <button className="link" onClick={hide}>Hide</button>
        </>
      )}
    </Card>
  );
}

export function SettingsScreen({ salon, me, onSignOut, pickSalon }: { salon: string; me: Me; onSignOut: () => void; pickSalon: () => void }) {
  return (
    <Page>
      <Card title="Your account">
        <div className="row" style={{ padding: 0, border: 0 }}>
          <div className="name"><b style={{ fontSize: 17 }}>{me.name}</b><small>{prettyPhone(me.phone) || 'No mobile number on file'}</small></div>
          <button className="btn danger" onClick={onSignOut}><Icon name="logout" size={18} /> Sign out</button>
        </div>
      </Card>
      {me.can_see_master_pins ? <MasterPinsCard salon={salon} salons={me.salons} /> : null}
      {me.can_manage_ai_keys ? <AiKeysCard /> : null}
      {salon === 'all' ? (
        <Notice action={<button className="link" onClick={pickSalon}>Choose a salon</button>}>Contacts are kept for one salon at a time.</Notice>
      ) : (
        <Contacts key={salon} salon={salon} canEdit={me.can_edit_settings} />
      )}
    </Page>
  );
}
