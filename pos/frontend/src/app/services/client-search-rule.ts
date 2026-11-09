/**
 * When the server will search customers (backend clients/search.py): 3 letters or 4 digits to start, so a search
 * is not sent for fewer. A phone typed with +91, spaces or dashes counts by its digits.
 */
export const CLIENT_SEARCH_HINT = 'Type at least 3 letters or 4 digits to search';

export function canSearchClients(text: string): boolean {
  const typed = (text || '').trim();
  if (!typed) return false;
  if (/^[\d\s+\-().]+$/.test(typed)) return typed.replace(/\D/g, '').length >= 4;
  return typed.split(/\s+/).some(word => /^\d+$/.test(word) ? word.length >= 4 : word.length >= 3);
}
