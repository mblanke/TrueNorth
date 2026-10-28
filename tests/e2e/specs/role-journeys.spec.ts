/**
 * R0-B role journeys: for each role, one page it may open and one it may not, by deep
 * link, plus what its menu offers.
 *
 * What this lane proves and what it does not (see support/identity.ts):
 * - The SPA is the production build. Keycloak's browser endpoints are stubbed, and
 *   GET /api/auth/me is answered here with the role under test, so the route guards, the
 *   menu and the buttons are the ones that role gets.
 * - The API runs with AUTH_DISABLED=true and answers every other call as the dev admin.
 *   So these journeys prove the web app's role behaviour, not the API's: the API refusing
 *   each role (direct calls, cross-tenant) is proven by tests/api/test_r0b_role_access.py.
 */
import { test, expect, type Page, type Locator } from '@playwright/test';
import { stubIdentityProvider } from '../support/identity';

type Role = 'student' | 'observer' | 'range_ops' | 'instructor';

function pageTitle(page: Page, name: string | RegExp): Locator {
  return page.getByRole('main').getByRole('heading', { level: 1, name });
}

function menu(page: Page): Locator {
  return page.getByRole('navigation', { name: 'TrueNorth workspaces' });
}

/** Answer /auth/me as a registered account with `role`; returns how often it was asked. */
async function signInAs(page: Page, role: Role): Promise<{ calls: number }> {
  const seen = { calls: 0 };
  await page.route('**/api/auth/me', async (route) => {
    seen.calls += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'registered',
        user: {
          id: `00000000-0000-0000-0000-0000000e2e${role.length}0`,
          email: `${role}@truenorth.local`,
          display_name: `E2E ${role}`,
          role,
          onboarding_state: 'complete',
        },
      }),
    });
  });
  return seen;
}

/**
 * Skip when the SPA under test was built with `authDisabled: true`: it never asks
 * /auth/me and treats every visitor as the dev admin. Decided once the first page has
 * settled, since identity resolves after the adapter's silent sign-in.
 */
async function skipIfAuthDisabled(page: Page, me: { calls: number }): Promise<void> {
  await page.waitForLoadState('networkidle');
  test.skip(me.calls === 0, 'SPA built with authDisabled=true: every visitor is the dev admin');
}

/** A guard that refuses sends the visitor to the dashboard. */
async function expectRefused(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(pageTitle(page, 'What are you working towards?')).toBeVisible();
}

test.describe('Role journeys (R0-B)', () => {
  test.beforeEach(async ({ context }) => {
    await stubIdentityProvider(context, { signedIn: true });
  });

  test('Student: opens their exercises without staff controls; telemetry is refused', async ({ page }) => {
    const me = await signInAs(page, 'student');
    await page.goto('/exercises');
    await skipIfAuthDisabled(page, me);
    await expect(pageTitle(page, 'Exercises')).toBeVisible();
    await expect(page.getByRole('button', { name: /new exercise/i })).toHaveCount(0);
    await expect(menu(page).getByRole('link', { name: 'Authoring Studio' })).toHaveCount(0);
    await expect(menu(page).getByRole('link', { name: 'Scoring & AAR' })).toBeVisible();
    await expectRefused(page, '/telemetry');
  });

  test('Observer: watches the Ops Center; Authoring is refused', async ({ page }) => {
    const me = await signInAs(page, 'observer');
    await page.goto('/ops-center/select');
    await skipIfAuthDisabled(page, me);
    await expect(pageTitle(page, 'Ops Center')).toBeVisible();
    await expect(menu(page).getByRole('link', { name: 'Telemetry' })).toBeVisible();
    await expectRefused(page, '/authoring/scenarios');
  });

  test('Range ops: opens Ranges in the Authoring Studio; Scoring is refused', async ({ page }) => {
    const me = await signInAs(page, 'range_ops');
    await page.goto('/authoring/ranges');
    await skipIfAuthDisabled(page, me);
    await expect(page).toHaveURL(/\/authoring\/ranges$/);
    await expect(pageTitle(page, 'Ranges')).toBeVisible();
    const tabs = page.getByRole('navigation', { name: 'Authoring Studio sections' });
    await expect(tabs.getByRole('link', { name: 'Content' })).toBeVisible();
    await expect(tabs.getByRole('link', { name: 'Scenarios' })).toHaveCount(0);
    await expectRefused(page, '/scoring');
  });

  test('Instructor: opens Classes; approvals (an admin approves Request access) and the admin directory are refused', async ({ page }) => {
    const me = await signInAs(page, 'instructor');
    await page.goto('/classes');
    await skipIfAuthDisabled(page, me);
    await expect(pageTitle(page, 'Classes')).toBeVisible();
    await expect(menu(page).getByRole('link', { name: 'Approvals' })).toHaveCount(0);
    await expectRefused(page, '/approvals');
    await expectRefused(page, '/users');
  });

  test('Nobody is shown the fictional previews unless the deployment turns them on', async ({ page }) => {
    const me = await signInAs(page, 'student');
    await page.goto('/learning/previews');
    await skipIfAuthDisabled(page, me);
    await expect(page).toHaveURL(/\/learning\/career-path$/);
    await expect(page.getByRole('link', { name: /previews/i })).toHaveCount(0);
  });
});
