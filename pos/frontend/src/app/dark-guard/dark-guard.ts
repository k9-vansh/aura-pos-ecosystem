/**
 * Dark mode safety net.
 *
 * Some elements carry a light background that no stylesheet rule reaches, and in
 * Dark Mode their white text then disappears into them. This watches the page
 * while Dark Mode is on and tags any element whose background is a light colour
 * (or a gradient of light colours); a rule in styles.css turns those dark.
 * The invoice receipt is paper and is left alone.
 */
const SKIP = 'svg, canvas, img, video, picture, .classic-receipt-paper, .classic-receipt-paper *, [data-keep-light]';
const RGB = /rgba?\((\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\)/g;

function isLightColour(value: string): boolean {
  RGB.lastIndex = 0;
  const m = RGB.exec(value);
  if (!m) return false;
  const alpha = m[4] === undefined ? 1 : parseFloat(m[4]);
  return alpha > 0.5 && +m[1] > 205 && +m[2] > 205 && +m[3] > 205;
}

function isLightGradient(value: string): boolean {
  if (!value || value.indexOf('gradient') === -1) return false;
  const colours = value.match(RGB);
  if (!colours || !colours.length) return false;
  return colours.every(c => isLightColour(c));
}

function fix(el: Element) {
  if (!(el instanceof HTMLElement) || el.matches(SKIP)) return;
  const cs = getComputedStyle(el);
  if (isLightColour(cs.backgroundColor) || isLightGradient(cs.backgroundImage)) {
    el.classList.add('dk-fix');
  }
}

function scan(root: Element) {
  fix(root);
  root.querySelectorAll('*').forEach(fix);
}

export function startDarkGuard(): void {
  const body = document.body;
  let pending = new Set<Element>();
  let timer: ReturnType<typeof setTimeout> | null = null;

  const flush = () => {
    timer = null;
    if (body.getAttribute('data-theme') !== 'dark') { pending = new Set(); return; }
    const batch = pending;
    pending = new Set();
    batch.forEach(el => { if (el.isConnected) scan(el); });
  };
  const queue = (el: Element) => {
    pending.add(el);
    if (!timer) timer = setTimeout(flush, 350);
  };

  new MutationObserver(records => {
    if (body.getAttribute('data-theme') !== 'dark') return;
    for (const r of records) {
      r.addedNodes.forEach(n => { if (n.nodeType === 1) queue(n as Element); });
      if (r.type === 'attributes' && r.target !== body && r.target instanceof Element) queue(r.target);
    }
  }).observe(body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });

  // switching to Dark Mode: check everything already on the page
  new MutationObserver(() => {
    if (body.getAttribute('data-theme') === 'dark') queue(body);
  }).observe(body, { attributes: true, attributeFilter: ['data-theme'] });

  if (body.getAttribute('data-theme') === 'dark') queue(body);
}
