/**
 * TrueNorth Range — E2E Playwright journeys.
 *
 * Each journey opens one route and asserts that route's own page title (the <h1> inside
 * the shell's <main>, or on a bare page) plus one element that page always renders. The
 * sidebar's group labels are <h2>s ("Overview", "Learn", ...), so a bare `h1, h2` locator
 * matched the navigation instead of the page; titles are found by role and level here.
 *
 * Journeys stay shallow and do not depend on data beyond what the itest stack seeds (the
 * dev tenant and admin). Identity: see support/identity.ts. The API is real; Keycloak's
 * browser endpoints are stubbed, so these prove the SPA and API, not the sign-in itself.
 */
import { test, expect, type Page, type Locator } from '@playwright/test';
import { stubIdentityProvider, type IdentityStub } from '../support/identity';

/** The routed page's title: an <h1> in the shell's main region, matched by name. */
function pageTitle(page: Page, name: string | RegExp): Locator {
  return page.getByRole('main').getByRole('heading', { level: 1, name });
}

/**
 * Skip when the SPA under test was built with `authDisabled: true`: it then treats every
 * visitor as the dev admin and there is no anonymous state to test. Decided after the
 * first page has settled, from whether the adapter asked the stub anything.
 */
function skipIfAuthDisabled(idp: IdentityStub): void {
  test.skip(
    idp.requests === 0,
    'SPA built with authDisabled=true (angular.json has no fileReplacements for environment.prod.ts)',
  );
}

test.describe('Anonymous visitor', () => {
  let idp: IdentityStub;
  test.beforeEach(async ({ context }) => {
    idp = await stubIdentityProvider(context, { signedIn: false });
  });

  test('a guarded page sends the visitor to sign in', async ({ page }) => {
    await page.goto('/dashboard');
    const signIn = page.getByRole('button', { name: /sign in with sso/i });
    await expect(signIn.or(pageTitle(page, 'What are you working towards?'))).toBeVisible();
    skipIfAuthDisabled(idp);
    await expect(page).toHaveURL(/\/login/);
    await expect(signIn).toBeVisible();
    // /login renders without the navigation shell.
    await expect(page.getByRole('navigation', { name: 'TrueNorth workspaces' })).toHaveCount(0);
  });

  test('a lab link without a token asks the student to sign in', async ({ page }) => {
    await page.goto('/labs/00000000-0000-0000-0000-00000000e2e0');
    const signIn = page.getByRole('button', { name: /sign in with sso/i });
    await expect(signIn.or(page.getByRole('heading', { level: 1, name: 'Lab' }))).toBeVisible();
    skipIfAuthDisabled(idp);
    await expect(page).toHaveURL(/\/login\?returnUrl=%2Flabs%2F/);
    await expect(signIn).toBeVisible();
  });
});

test.describe('Signed in', () => {
  test.beforeEach(async ({ context }) => {
    await stubIdentityProvider(context, { signedIn: true });
  });

  test.describe('Critical Journey: Dashboard', () => {
    test('root redirects to the dashboard', async ({ page }) => {
      await page.goto('/');
      await expect(page).toHaveURL(/\/dashboard$/);
      await expect(pageTitle(page, 'What are you working towards?')).toBeVisible();
      await expect(page.getByRole('region', { name: 'Choose your next task' })).toBeVisible();
    });

    test('shows the navigation sidebar', async ({ page }) => {
      await page.goto('/dashboard');
      const nav = page.getByRole('navigation', { name: 'TrueNorth workspaces' });
      await expect(nav).toBeVisible({ timeout: 10_000 });
      await expect(nav.getByRole('link', { name: 'Dashboard' })).toHaveAttribute('aria-current', 'page');
    });
  });

  test.describe('Critical Journey: Range Lifecycle', () => {
    test('ranges list', async ({ page }) => {
      await page.goto('/ranges');
      await expect(page).toHaveURL(/\/authoring\/ranges$/);
      await expect(pageTitle(page, 'Ranges')).toBeVisible();
      await expect(page.getByRole('button', { name: /new range/i }).first()).toBeVisible();
    });

    test('templates open the content catalogue', async ({ page }) => {
      await page.goto('/templates');
      await expect(page).toHaveURL(/\/authoring\/content$/);
      await expect(pageTitle(page, 'Content')).toBeVisible();
    });

    test('range designer', async ({ page }) => {
      await page.goto('/range-designer');
      await expect(page).toHaveURL(/\/authoring\/ranges\/designer$/);
      await expect(page.locator('.designer-layout, .joint-paper, [class*="designer"]').first())
        .toBeVisible({ timeout: 10_000 });
    });
  });

  test.describe('Critical Journey: Exercises', () => {
    test('exercises list', async ({ page }) => {
      await page.goto('/exercises');
      await expect(pageTitle(page, 'Exercises')).toBeVisible();
      await expect(page.getByRole('button', { name: /new exercise/i }).first()).toBeVisible();
    });

    test('scoring and AAR', async ({ page }) => {
      await page.goto('/scoring');
      await expect(pageTitle(page, 'Scoring & After Action Review')).toBeVisible();
      await expect(page.getByRole('combobox', { name: 'Select Exercise' })).toBeVisible();
    });
  });

  test.describe('Critical Journey: Detection Editor', () => {
    test('detection editor', async ({ page }) => {
      await page.goto('/detection-editor');
      await expect(page).toHaveURL(/\/authoring\/detections$/);
      await expect(pageTitle(page, 'Detection Rule Editor')).toBeVisible();
      await expect(page.getByRole('button', { name: /new rule/i }).first()).toBeVisible();
    });
  });

  test.describe('Critical Journey: Competency', () => {
    test('competency framework', async ({ page }) => {
      await page.goto('/competency');
      await expect(page).toHaveURL(/\/learning\/competency$/);
      await expect(pageTitle(page, 'Competency Framework')).toBeVisible();
      await expect(page.getByRole('tab', { name: 'My Profile' })).toBeVisible();
    });
  });

  test.describe('Stage 4 modules', () => {
    test('labs: an unknown lab says so', async ({ page }) => {
      // Bare route (no shell, no <main>), so the title is found on the page itself.
      await page.goto('/labs/00000000-0000-0000-0000-00000000e2e0');
      await expect(page.getByRole('heading', { level: 1, name: 'Lab' })).toBeVisible();
      await expect(page.getByText(/could not be found|has expired/)).toBeVisible({ timeout: 10_000 });
    });

    test('ops center: exercise selector', async ({ page }) => {
      await page.goto('/ops-center/select');
      await expect(pageTitle(page, 'Ops Center')).toBeVisible();
      await expect(page.getByRole('navigation', { name: 'Exercise pages' })).toBeVisible();
    });

    test('telemetry explorer', async ({ page }) => {
      await page.goto('/telemetry');
      await expect(pageTitle(page, 'Telemetry Explorer')).toBeVisible();
      await expect(page.getByRole('textbox', { name: 'Query' })).toBeVisible();
      await expect(page.getByRole('button', { name: /search/i })).toBeVisible();
    });

    test('content catalogue', async ({ page }) => {
      await page.goto('/authoring/content');
      await expect(pageTitle(page, 'Content')).toBeVisible();
      await expect(page.getByRole('button', { name: /new template/i }).first()).toBeVisible();
    });

    test('integrations', async ({ page }) => {
      await page.goto('/integrations');
      await expect(pageTitle(page, 'Integrations')).toBeVisible();
      await expect(page.getByRole('tab', { name: 'Connected Platforms' })).toBeVisible();
    });

    test('my progress', async ({ page }) => {
      await page.goto('/my-progress');
      await expect(page).toHaveURL(/\/learning\/progress$/);
      await expect(pageTitle(page, 'My Progress')).toBeVisible();
      await expect(page.getByText('Training Hours')).toBeVisible();
    });
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
