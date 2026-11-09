import { useEffect, type ReactNode } from 'react';
import { Icon } from './Icon';

/** A panel that rises from the bottom on a phone and sits in the middle on a laptop. Esc or a tap outside closes it. */
export function Sheet({ title, onClose, children, right }: { title: string; onClose: () => void; children: ReactNode; right?: ReactNode }) {
  useEffect(() => {
    const key = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, [onClose]);
  return (
    <div className="scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="sheet" role="dialog" aria-modal="true" aria-label={title}>
        <div className="grab" />
        <header>
          <h3>{title}</h3>
          <span style={{ display: 'flex', gap: 8 }} className="no-print">
            {right}
            <button className="round" aria-label="Close" onClick={onClose}>
              <Icon name="close" size={20} />
            </button>
          </span>
        </header>
        {children}
      </div>
    </div>
  );
}
