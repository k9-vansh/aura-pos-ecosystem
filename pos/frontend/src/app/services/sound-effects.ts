import { SoundService, SoundName } from './sound.service';

/**
 * Gives the whole app sound without touching any screen: one set of listeners on
 * the document picks the sound from what was pressed.
 *
 *  - buttons, links, rows you can open  -> soft click
 *  - tabs and menu items                -> a higher tick
 *  - checkboxes, radios, switches       -> two-note toggle
 *  - a dialog or panel appearing/going  -> open / close sweep
 *
 * Success and error toasts play their own sounds from ToastService, and the till
 * plays the scan, cash and print sounds itself.
 */
const PRESSABLE = [
  'button', 'a[href]', '[role="button"]', '[role="tab"]', '[role="menuitem"]', '[role="option"]',
  'summary', 'input[type="button"]', 'input[type="submit"]', 'input[type="checkbox"]',
  'input[type="radio"]', 'label[for]', 'select', '.tab-btn', '.po-item', '.smart-item-row',
  'tr[style*="cursor"]', '.nav-item', '.nav-link', '.sidebar-item',
].join(',');

const TABLIKE = '[role="tab"], [role="menuitem"], .tab-btn, [class*="tab-btn"], [class*="-tab"], .nav-item, .nav-link, .sidebar-item, a[href]';
const OVERLAY = /(^|[\s_-])(modal|overlay|backdrop|dialog|drawer|popup|flyout)([\s_-]|$)/i;

function disabled(el: Element): boolean {
  return (el as HTMLButtonElement).disabled === true
    || el.getAttribute('aria-disabled') === 'true'
    || !!el.closest('[data-no-sound]');
}

export function startSoundEffects(sound: SoundService) {
  // The browser only allows sound after a gesture; the first touch wakes the engine.
  const wake = () => sound.unlock();
  document.addEventListener('pointerdown', wake, { capture: true, passive: true });
  document.addEventListener('keydown', wake, { capture: true, passive: true });

  document.addEventListener('pointerdown', (e: PointerEvent) => {
    if (e.button !== 0 || !sound.enabled) return;
    const target = e.target as Element | null;
    const el = target?.closest?.(PRESSABLE);
    if (!el || disabled(el)) return;
    // Checkboxes and radios sound on `change` instead (so a cancelled press stays silent).
    if (el.matches('input[type="checkbox"], input[type="radio"], label[for]')) return;
    sound.play(el.matches(TABLIKE) ? 'tab' : 'click');
  }, { capture: true, passive: true });

  // Keyboard presses on a focused button or link
  document.addEventListener('keydown', (e: KeyboardEvent) => {
    if ((e.key !== 'Enter' && e.key !== ' ') || e.repeat || !sound.enabled) return;
    const t = e.target as Element | null;
    if (t?.matches?.('button, a[href], [role="button"], [role="tab"]') && !disabled(t)) {
      sound.play(t.matches(TABLIKE) ? 'tab' : 'click');
    }
  }, { capture: true, passive: true });

  document.addEventListener('change', (e: Event) => {
    const t = e.target as HTMLElement | null;
    if (!t || !sound.enabled) return;
    if (t.matches('input[type="checkbox"], input[type="radio"]')) sound.play('toggle');
    else if (t.matches('select')) sound.play('tab');
  }, { capture: true, passive: true });

  // Dialogs and panels: sound when one is added to / removed from the page.
  const isOverlay = (n: Node): boolean => {
    if (n.nodeType !== 1) return false;
    const cls = (n as Element).getAttribute('class');
    return !!cls && OVERLAY.test(cls);
  };
  new MutationObserver(records => {
    if (!sound.enabled) return;
    let opened = false;
    let closed = false;
    for (const r of records) {
      for (let i = 0; i < r.addedNodes.length && !opened; i++) opened = isOverlay(r.addedNodes[i]);
      for (let i = 0; i < r.removedNodes.length && !closed; i++) closed = isOverlay(r.removedNodes[i]);
      if (opened && closed) break;
    }
    if (opened) sound.play('open');
    else if (closed) sound.play('close');
  }).observe(document.body, { childList: true, subtree: true });
}
