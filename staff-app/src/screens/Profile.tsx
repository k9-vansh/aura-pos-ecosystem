import { useState } from 'react';
import { day, phone as prettyPhone } from '../format';
import { loadTheme, saveTheme, THEMES } from '../theme';
import type { Profile } from '../types';
import { Card, Waiting, useLoad } from '../ui/bits';
import { Icon } from '../ui/Icon';

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

function Fact({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><b>{value || '—'}</b></div>;
}

export function ProfileScreen({ onSignOut }: { onSignOut: () => void }) {
  const { data, error, loading, reload } = useLoad<Profile>('/profile/');
  return (
    <>
      {!data ? <Waiting error={error} loading={loading} reload={reload} h={260} /> : (
        <>
          <Card>
            <div className="row" style={{ padding: 0, border: 0 }}>
              <div className="lead">
                <span className="initial big">{data.first_name.charAt(0).toUpperCase()}</span>
                <div className="name"><b style={{ fontSize: 17 }}>{data.name}</b><small>{data.designation}{data.salon ? ` · ${data.salon}` : ''}</small></div>
              </div>
            </div>
            <div className="rates">
              <div><small>Service commission</small><b className="num">{data.commission}%</b></div>
              <div><small>Product commission</small><b className="num">{data.product_commission}%</b></div>
              <div><small>Perk points</small><b className="num">{data.perk_points}</b></div>
            </div>
          </Card>
          <Card title="Your details">
            <div className="kv">
              <Fact label="Your name" value={data.name} />
              <Fact label="Your phone" value={prettyPhone(data.phone)} />
              <Fact label="Your salon" value={data.salon} />
              <Fact label="Salon phone" value={data.salon_phone} />
              <Fact label="Aadhaar" value={data.aadhaar} />
              <Fact label="Staff code" value={data.staff_code} />
              <Fact label="Joined" value={data.joined ? day(data.joined) : ''} />
              <Fact label="E-mail" value={data.email} />
            </div>
            <p className="note">These come from your salon's records. To change any of them, ask your manager.</p>
          </Card>
        </>
      )}
      <LookCard />
      <button className="btn danger block" onClick={onSignOut}><Icon name="logout" size={18} /> Sign out</button>
    </>
  );
}
