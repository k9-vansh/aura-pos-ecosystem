import { test, expect } from '@playwright/test';

test.describe('Authentication Flow', () => {
  test('should successfully log in with valid credentials', async ({ page }) => {
    // Navigate to the app root (will redirect to /login if not authenticated)
    await page.goto('http://localhost:4200/');

    // Assert we are on the login page
    await expect(page).toHaveURL(/.*login/);
    await expect(page.locator('.printer-logo-text')).toContainText(/POS TERMINAL/i);

    // Fill in the login form
    // Credentials come from the environment, never from the repository.
    await page.fill('input[type="email"]', process.env['E2E_USER'] ?? '');
    await page.fill('input[type="password"]', process.env['E2E_PASSWORD'] ?? '');

    // Click the login button
    await page.click('button[type="submit"]');

    // Wait for navigation to the main dashboard or billing screen
    await page.waitForURL(/.*(dashboard|billing|admin\/home)/, { timeout: 10000 });

    // Assert successful login by checking that the page has loaded its layout
    await expect(page.locator('body')).toBeVisible();
  });
});
