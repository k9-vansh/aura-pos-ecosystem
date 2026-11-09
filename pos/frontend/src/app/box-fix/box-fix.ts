/**
 * "Box inside a box" safety net for search boxes and other icon fields.
 *
 * The neumorphic look gives a search field a sunken, rounded box. Several screens
 * build that field as a styled wrapper (icon + input) holding an input that is
 * ALSO styled, so the field shows a capsule inside a capsule. Fixing it screen by
 * screen in the stylesheets leaves the next screen's version still broken, so this
 * looks at what is really on screen instead:
 *
 *   an input whose tight wrapper (the input is its only field, there is no label
 *   or text, and it is not much taller than the input) has a box of its own, while
 *   the input has one too, is marked  data-nb="bare".
 *
 * styles.css strips the input's own box when it is marked, so the wrapper is the
 * one field. Inputs that are the only box are never touched.
 */
const FIELD = 'input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="file"])'
  + ':not([type="hidden"]):not([type="color"]):not([type="button"]):not([type="submit"]), select';

const checked = new WeakSet<Element>();
const queue = new Set<Element>();
let scheduled = false;

function alpha(color: string): number {
  if (!color || color === 'transparent') return 0;
  const m = /rgba?\(([^)]+)\)/.exec(color);
  if (!m) return 1;
  const parts = m[1].split(/[,/\s]+/).filter(Boolean);
  return parts.length > 3 ? parseFloat(parts[3]) : 1;
}

/** Does this element draw a visible box: a background, a border or a shadow? */
function hasBox(el: HTMLElement): boolean {
  const cs = getComputedStyle(el);
  if (cs.boxShadow && cs.boxShadow !== 'none') return true;
  if (alpha(cs.backgroundColor) > 0.05) return true;
  if (cs.backgroundImage && cs.backgroundImage !== 'none') return true;
  for (const side of ['Top', 'Right', 'Bottom', 'Left'] as const) {
    const w = parseFloat(cs.getPropertyValue(`border-${side.toLowerCase()}-width`));
    const style = cs.getPropertyValue(`border-${side.toLowerCase()}-style`);
    if (w > 0 && style !== 'none' && style !== 'hidden' && alpha(cs.getPropertyValue(`border-${side.toLowerCase()}-color`)) > 0.05) return true;
  }
  return false;
}

/** The wrapper is "tight": it exists to frame this one field (plus an icon or a clear button). */
function tightWrapper(input: HTMLElement): HTMLElement | null {
  const parent = input.parentElement;
  if (!parent || parent === document.body) return null;
  const fields = Array.from(parent.querySelectorAll(FIELD));
  if (fields.length < 1 || fields.length > 3) return null;
  if (parent.querySelector('label, h1, h2, h3, h4, h5, h6, p, table, ul, ol')) return null;
  if (parent.children.length > 5) return null;
  // A pill holding several fields (a "from [date] to [date]" range): the fields must be
  // its direct children, and it must not hold buttons (that would be a toolbar).
  if (fields.length > 1 && (fields.some(f => f.parentElement !== parent) || parent.querySelector('button, a, [role="button"]'))) return null;
  // A button or link with a WORD on it (Apply, Search) makes this a toolbar, not a field frame.
  if (Array.from(parent.querySelectorAll('button, a, [role="button"]')).some(b => (b.textContent || '').trim().length > 0)) return null;
  // A frame, not a flat container: it has some rounding.
  if (parseFloat(getComputedStyle(parent).borderTopLeftRadius) < 6) return null;
  const rest = (parent.textContent || '').trim();
  if (rest.length > 24) return null;
  const pr = parent.getBoundingClientRect();
  const ir = input.getBoundingClientRect();
  if (!ir.height || !pr.height) return null;
  const tallest = Math.max(...fields.map(f => f.getBoundingClientRect().height));
  // Framing the field(s), not a card or a row that merely contains them.
  if (pr.height > tallest + 24 || pr.width < ir.width - 1) return null;
  return parent;
}

function fix(input: Element) {
  if (!(input instanceof HTMLElement) || checked.has(input) || !input.isConnected) return;
  const ir = input.getBoundingClientRect();
  if (!ir.width || !ir.height) return; // not laid out yet (hidden tab); looked at again when it shows
  checked.add(input);

  const wrapper = tightWrapper(input);
  if (!wrapper) return;
  // Measure the input as the stylesheet leaves it.
  if (input.hasAttribute('data-nb')) input.removeAttribute('data-nb');
  if (hasBox(wrapper) && hasBox(input)) input.setAttribute('data-nb', 'bare');
}

function flush() {
  scheduled = false;
  const items = Array.from(queue);
  queue.clear();
  for (const node of items) {
    if (node.matches?.(FIELD)) fix(node);
    node.querySelectorAll?.(FIELD).forEach(fix);
  }
}

function schedule(node: Element) {
  queue.add(node);
  if (scheduled) return;
  scheduled = true;
  // Wait a moment for the stylesheets to apply; measuring forces the layout itself, so no
  // animation frame is needed (and none would fire in a page that is not on screen).
  setTimeout(flush, 150);
}

export function startBoxFix() {
  schedule(document.body);
  new MutationObserver(records => {
    for (const r of records) {
      r.addedNodes.forEach(n => { if (n.nodeType === 1) schedule(n as Element); });
    }
  }).observe(document.body, { childList: true, subtree: true });

  // A tab or panel being shown: its fields could not be measured while hidden.
  document.addEventListener('click', () => schedule(document.body), { passive: true, capture: true });
}
