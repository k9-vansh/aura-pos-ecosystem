import { test, expect } from '@playwright/test';

test.describe('Checkout Flow', () => {
  test.beforeEach(async ({ page }) => {
    // Navigate and login before each test
    await page.goto('http://localhost:4200/');
    // Credentials come from the environment, never from the repository.
    await page.fill('input[type="email"]', process.env['E2E_USER'] ?? '');
    await page.fill('input[type="password"]', process.env['E2E_PASSWORD'] ?? '');
    await page.click('button[type="submit"]');
    await page.waitForURL(/.*(dashboard|billing|admin\/home)/, { timeout: 15000 });
    
    // Ensure we are on the billing page
    if (!page.url().includes('billing')) {
      await page.goto('http://localhost:4200/admin/billing');
    }
  });

  test('should create a new invoice for an anonymous client and add a service', async ({ page }) => {
    // 1. Start a New Invoice
    await page.click('button:has-text("+ New Invoice")');
    
    // 2. Select Anonymous/Walk-in Client
    // We assume there's a button with "Walk-in" or "Anonymous" in the text or title based on billing.html
    await page.click('.btn-anon-client-quick, button[title*="Walk-in"]');

    // 3. Search for a service and add it to cart
    // Use the service search input
    await page.fill('input[placeholder*="Services"]', 'Haircut');
    
    await page.click('.btn-add-item');

    // 4. Verify item is in cart
    await expect(page.locator('.cart-item-row').first()).toBeVisible();
    await expect(page.locator('.cart-total-amount')).toContainText(/Rs/);

    // 5. Open Payment Modal
    await page.click('button:has-text("Checkout"), button:has-text("Pay")');

    // 6. Complete Payment (assume exact cash for simplicity)
    await page.click('button:has-text("Cash")');
    await page.click('button:has-text("Complete Payment"), button:has-text("Confirm")');

    // 7. Verify Success
    await expect(page.locator('.success-modal, text="Invoice Generated"').first()).toBeVisible();
  });
});
