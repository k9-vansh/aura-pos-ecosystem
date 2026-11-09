/** The app's looks. The first is the default (white and black); the person's choice is kept on their phone. */
export interface Theme {
  id: string;
  name: string;
  /** Three swatches for the picker: page, card, accent. */
  swatch: [string, string, string];
}

export const THEMES: Theme[] = [
  { id: 'classic', name: 'Classic', swatch: ['#ffffff', '#f4f4f5', '#0a0a0a'] },
  { id: 'noir', name: 'Noir', swatch: ['#0a0a0b', '#17171a', '#f5f5f5'] },
  { id: 'champagne', name: 'Champagne', swatch: ['#f8f3ea', '#ffffff', '#b8893b'] },
  { id: 'emerald', name: 'Emerald', swatch: ['#0c1a15', '#12261f', '#d4b56a'] },
  { id: 'rose', name: 'Rosé', swatch: ['#fbf1ef', '#ffffff', '#c4675f'] },
];

const KEY = 'clients.theme';

export function loadTheme(): string {
  try {
    const saved = localStorage.getItem(KEY);
    if (saved && THEMES.some((t) => t.id === saved)) return saved;
  } catch {
    /* private window: the default look */
  }
  return THEMES[0].id;
}

export function applyTheme(id: string) {
  document.documentElement.dataset.theme = id;
  const colour = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', colour || '#ffffff');
}

export function saveTheme(id: string) {
  try {
    localStorage.setItem(KEY, id);
  } catch {
    /* kept for this visit only */
  }
  applyTheme(id);
}
