/**
 * TrueNorth Range — E2E Playwright tests
 *
 * Critical user journeys:
 * 1. Login → Dashboard
 * 2. Template → Range creation → Provision
 * 3. Scenario → Exercise → Score
 * 4. Telemetry → Detection rules
 * 5. Admin → Users → Teams
 */
import { test, expect } from '@playwright/test';

test.describe('Critical Journey: Dashboard', () => {
  test('should load dashboard after login redirect', async ({ page }) => {
    await page.goto('/');
    // Should redirect to dashboard (or login if Keycloak is configured)
    await expect(page).toHaveURL(/dashboard|login/);
  });

  test('should display navigation sidebar', async ({ page }) => {
    await page.goto('/dashboard');
    // Check for key navigation items
    const nav = page.locator('mat-sidenav, nav, [role="navigation"]');
    await expect(nav.first()).toBeVisible({ timeout: 10_000 });
  });
});

test.describe('Critical Journey: Range Lifecycle', () => {
  test('should display ranges list', async ({ page }) => {
    await page.goto('/ranges');
    await expect(page.locator('h1, h2').first()).toContainText(/range/i);
  });

  test('should display templates list', async ({ page }) => {
    await page.goto('/templates');
    await expect(page.locator('h1, h2').first()).toContainText(/template/i);
  });

  test('should load range designer', async ({ page }) => {
    await page.goto('/range-designer');
    // The designer canvas should be present
    await expect(page.locator('.designer-layout, .joint-paper, [class*="designer"]').first())
      .toBeVisible({ timeout: 10_000 });
  });
});

test.describe('Critical Journey: Exercises', () => {
  test('should display exercises list', async ({ page }) => {
    await page.goto('/exercises');
    await expect(page.locator('h1, h2').first()).toContainText(/exercise/i);
  });

  test('should display scoring page', async ({ page }) => {
    await page.goto('/scoring');
    await expect(page.locator('h1, h2').first()).toContainText(/scor|aar/i);
  });
});

test.describe('Critical Journey: Detection Editor', () => {
  test('should load detection editor page', async ({ page }) => {
    await page.goto('/detection-editor');
    await expect(page.locator('h1, h2').first()).toContainText(/detection/i);
  });
});

test.describe('Critical Journey: Competency', () => {
  test('should display competency framework', async ({ page }) => {
    await page.goto('/competency');
    await expect(page.locator('h1, h2').first()).toContainText(/competency/i);
  });
});

test.describe('API Health', () => {
  test('should return healthy API status', async ({ request }) => {
    const resp = await request.get('/api/health');
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.status).toBe('ok');
  });
});
