import { canSearchClients } from './client-search-rule';

describe('canSearchClients (the server searches from 3 letters or 4 digits)', () => {
  it('needs 3 letters or 4 digits', () => {
    expect(canSearchClients('')).toBe(false);
    expect(canSearchClients('as')).toBe(false);
    expect(canSearchClients('asha')).toBe(true);
    expect(canSearchClients('987')).toBe(false);
    expect(canSearchClients('9876')).toBe(true);
  });

  it('counts a phone typed with +91, spaces or dashes by its digits', () => {
    expect(canSearchClients('+91')).toBe(false);
    expect(canSearchClients('98-7')).toBe(false);
    expect(canSearchClients('98 76')).toBe(true);
    expect(canSearchClients('+91 98765 43210')).toBe(true);
  });

  it('lets one long enough word start a search with shorter words narrowing it', () => {
    expect(canSearchClients('asha 98')).toBe(true);
    expect(canSearchClients('as 98')).toBe(false);
  });
});
