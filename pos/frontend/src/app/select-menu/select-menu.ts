/**
 * Styled drop-down list for every <select> in the app.
 *
 * The browser draws a <select>'s list itself, and it cannot be styled. Here the
 * select stays exactly where it is (so Angular's forms keep working untouched)
 * but its native list is replaced: pressing it opens a list drawn in the same
 * style as the location drop-down. Choosing an entry sets the select and sends
 * the usual `input` and `change` events.
 *
 * Left alone: multi-selects, list boxes (size > 1), disabled selects, and any
 * select marked data-native.
 */
const MENU_CLASS = 'nm-select-menu';

function eligible(el: EventTarget | null): el is HTMLSelectElement {
  if (!(el instanceof HTMLSelectElement)) return false;
  if (el.multiple || el.size > 1 || el.disabled || el.hasAttribute('data-native')) return false;
  return !!el.closest('app-admin');
}

function zoomFactor(): number {
  const z = parseFloat(getComputedStyle(document.documentElement).zoom || '1');
  return Number.isFinite(z) && z > 0 ? z : 1;
}

let menu: HTMLDivElement | null = null;
let owner: HTMLSelectElement | null = null;
let active = -1;

function close() {
  if (menu) { menu.remove(); menu = null; }
  if (owner) { owner.classList.remove('nm-open'); }
  owner = null;
  active = -1;
}

function choose(select: HTMLSelectElement, index: number) {
  if (select.selectedIndex !== index) {
    select.selectedIndex = index;
    select.dispatchEvent(new Event('input', { bubbles: true }));
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }
  close();
  select.focus();
}

function highlight(items: HTMLElement[], next: number) {
  if (!items.length) return;
  active = (next + items.length) % items.length;
  items.forEach((it, i) => it.classList.toggle('nm-active', i === active));
  items[active].scrollIntoView({ block: 'nearest' });
}

function open(select: HTMLSelectElement) {
  close();
  owner = select;
  select.classList.add('nm-open');

  const box = document.createElement('div');
  box.className = MENU_CLASS;
  box.setAttribute('role', 'listbox');

  const items: HTMLElement[] = [];
  const addOption = (opt: HTMLOptionElement, indent: boolean) => {
    const item = document.createElement('div');
    item.className = 'nm-option' + (opt.selected ? ' nm-selected' : '') + (opt.disabled ? ' nm-disabled' : '') + (indent ? ' nm-indent' : '');
    item.textContent = opt.text || ' ';
    item.setAttribute('role', 'option');
    if (!opt.disabled) {
      item.addEventListener('mousedown', e => { e.preventDefault(); choose(select, opt.index); });
      item.addEventListener('mousemove', () => highlight(items, items.indexOf(item)));
      items.push(item);
    }
    box.appendChild(item);
  };
  Array.from(select.children).forEach(child => {
    if (child instanceof HTMLOptGroupElement) {
      const label = document.createElement('div');
      label.className = 'nm-group';
      label.textContent = child.label;
      box.appendChild(label);
      Array.from(child.children).forEach(o => { if (o instanceof HTMLOptionElement) addOption(o, true); });
    } else if (child instanceof HTMLOptionElement) {
      addOption(child, false);
    }
  });

  document.body.appendChild(box);
  menu = box;

  // place it under the select (or above, if there is no room), allowing for page zoom
  const z = zoomFactor();
  const r = select.getBoundingClientRect();
  const width = Math.max(r.width, 170);
  box.style.width = `${width / z}px`;
  const h = box.getBoundingClientRect().height;
  const below = window.innerHeight - r.bottom;
  const top = below >= h + 12 || below >= r.top ? r.bottom + 6 : r.top - h - 6;
  box.style.top = `${Math.max(top, 6) / z}px`;
  box.style.left = `${Math.min(r.left, window.innerWidth - width - 6) / z}px`;

  const selectedItem = items.findIndex(i => i.classList.contains('nm-selected'));
  highlight(items, selectedItem >= 0 ? selectedItem : 0);
  box.dataset['items'] = String(items.length);
  (box as any)._items = items;
}

export function startSelectMenus(): void {
  // open on press; stop the browser's own list
  document.addEventListener('mousedown', e => {
    const t = e.target;
    if (menu && !(t instanceof Node && menu.contains(t))) {
      const wasOwner = t === owner;
      close();
      if (wasOwner) { e.preventDefault(); return; }
    }
    if (e.button === 0 && eligible(t)) {
      e.preventDefault();
      t.focus();
      open(t);
    }
  }, true);

  // keyboard: Enter / Space / Alt+Down open it; arrows, Enter and Esc work inside
  document.addEventListener('keydown', e => {
    if (menu && owner) {
      const items: HTMLElement[] = (menu as any)._items || [];
      if (e.key === 'Escape') { e.preventDefault(); close(); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); highlight(items, active + 1); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); highlight(items, active - 1); return; }
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        const options = Array.from(owner.options).filter(o => !o.disabled);
        if (options[active]) choose(owner, options[active].index);
        return;
      }
      if (e.key === 'Tab') close();
      return;
    }
    if (eligible(e.target) && (e.key === 'Enter' || e.key === ' ' || (e.key === 'ArrowDown' && e.altKey))) {
      e.preventDefault();
      open(e.target as HTMLSelectElement);
    }
  }, true);

  window.addEventListener('resize', close);
  document.addEventListener('scroll', e => {
    if (menu && !(e.target instanceof Node && menu.contains(e.target))) close();
  }, true);
}
