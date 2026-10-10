import { defineConfig, devices } from '@playwright/test';

/**
 * The cmi5 conformance lane (CI job `cmi5`): ADL's CATAPULT Content Test Suite drives
 * TrueNorth's AU runtime in a real browser. Needs the itest stack with web and LRS
 * (ITEST_WEB=1 ITEST_LRS=1 scripts/itest.sh up) and CATAPULT
 * (infra/platform/docker/compose.catapult.yml). Kept out of playwright.config.ts so the e2e
 * lane, which starts no CATAPULT, never collects it.
 */
export default defineConfig({
  testDir: './cmi5',
  fullyParallel: false,
  workers: 1,
  retries: 0, // a conformance verdict is not retried into a pass
  reporter: [['list'], ['junit', { outputFile: 'test-results/cmi5-junit.xml' }], ['html', { open: 'never', outputFolder: 'playwright-report-cmi5' }]],
  timeout: 240_000,
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:14200',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
