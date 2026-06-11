/* Chrome DevTools (CDP) verification harness for the UI overhaul.
 * Drives the locally installed Chrome against the dev server and reports:
 * console errors per route, WebGL context creation/leaks, login-scene FPS,
 * reduced-motion behavior, theme switching, and screenshots for review.
 */
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
].filter(Boolean);

const chromePath = CHROME_CANDIDATES.find((p) => fs.existsSync(p));
if (!chromePath) {
  console.error('FAIL: Chrome not found');
  process.exit(1);
}

const BASE = 'http://localhost:4200';
const SHOT_DIR = 'devtools-shots';
fs.mkdirSync(SHOT_DIR, { recursive: true });

const results = { routes: {}, checks: {} };

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: 'new',
  args: ['--window-size=1600,900', '--force-color-profile=srgb'],
  defaultViewport: { width: 1600, height: 900 },
});

function watchConsole(page, bucket) {
  page.on('console', (msg) => {
    if (msg.type() === 'error') bucket.push(`console.error: ${msg.text()}`);
    else if (msg.type() === 'warning') bucket.push(`console.warn: ${msg.text()}`);
  });
  page.on('pageerror', (err) => bucket.push(`pageerror: ${err.message}`));
}

function isRealProblem(line) {
  // API calls fail by design (no backend running) — ignore those.
  return !/(404|Http failure|ERR_CONNECTION|ECONNREFUSED|proxy|Failed to load resource)/i.test(line);
}

async function visit(page, path, name, settle = 2500) {
  const issues = [];
  const listenerBucket = issues;
  const onConsole = (msg) => {
    if (msg.type() === 'error') listenerBucket.push(`console.error: ${msg.text()}`);
  };
  const onPageError = (err) => listenerBucket.push(`pageerror: ${err.message}`);
  page.on('console', onConsole);
  page.on('pageerror', onPageError);
  await page.goto(`${BASE}${path}`, { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise((r) => setTimeout(r, settle));
  await page.screenshot({ path: `${SHOT_DIR}/${name}.png` });
  page.off('console', onConsole);
  page.off('pageerror', onPageError);
  results.routes[name] = issues.filter(isRealProblem);
}

// ── 1. Route sweep: console errors + screenshots ─────────────────────────
const page = await browser.newPage();
await visit(page, '/login?preview=1', 'login', 4000);
await visit(page, '/dashboard', 'dashboard');
await visit(page, '/telemetry', 'telemetry');
await visit(page, '/topology-3d', 'topology-3d');
await visit(page, '/range-designer', 'range-designer');
await visit(page, '/training', 'training');
await visit(page, '/my-progress', 'my-progress');

// ── 2. Login scene FPS (rAF count over 3s) ───────────────────────────────
await page.goto(`${BASE}/login?preview=1`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2000));
const fps = await page.evaluate(
  () =>
    new Promise((resolve) => {
      let frames = 0;
      const start = performance.now();
      const tick = () => {
        frames++;
        if (performance.now() - start < 3000) requestAnimationFrame(tick);
        else resolve(Math.round((frames / (performance.now() - start)) * 1000));
      };
      requestAnimationFrame(tick);
    }),
);
results.checks.loginFps = fps;

// WebGL context present on login canvas?
results.checks.loginWebgl = await page.evaluate(() => {
  const c = document.querySelector('canvas.hero-canvas');
  return !!c && c.width > 0;
});

// ── 3. WebGL context leak: login <-> dashboard x3 ────────────────────────
const leakWarnings = [];
const leakListener = (msg) => {
  if (/too many active webgl contexts/i.test(msg.text())) leakWarnings.push(msg.text());
};
page.on('console', leakListener);
for (let i = 0; i < 3; i++) {
  await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 800));
  await page.goto(`${BASE}/login?preview=1`, { waitUntil: 'networkidle2' });
  await new Promise((r) => setTimeout(r, 1200));
}
page.off('console', leakListener);
results.checks.webglLeakWarnings = leakWarnings.length;

// ── 4. Theme switching re-tints (CSS var changes + persistence) ──────────
await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1500));
const themeCheck = await page.evaluate(async () => {
  const accentOf = () => getComputedStyle(document.body).getPropertyValue('--accent').trim();
  const before = accentOf();
  const buttons = [...document.querySelectorAll('.theme-btn')];
  buttons[1]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  await new Promise((r) => setTimeout(r, 300));
  const after = accentOf();
  const stored = localStorage.getItem('tn-theme');
  buttons[0]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  return { before, after, changed: before !== after, stored };
});
results.checks.theme = themeCheck;

// ── 5. Reduced motion: content must not be stuck invisible ───────────────
const rmPage = await browser.newPage();
await rmPage.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
await rmPage.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1500));
results.checks.reducedMotion = await rmPage.evaluate(() => {
  const cards = [...document.querySelectorAll('.stat-card')];
  const visible = cards.filter((c) => parseFloat(getComputedStyle(c).opacity) > 0.9);
  return { statCards: cards.length, visibleCards: visible.length };
});
await rmPage.screenshot({ path: `${SHOT_DIR}/dashboard-reduced-motion.png` });

// Login under reduced motion: static frame, card visible.
await rmPage.goto(`${BASE}/login?preview=1`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2500));
results.checks.reducedMotionLogin = await rmPage.evaluate(() => {
  const card = document.querySelector('.hero-card');
  return card ? parseFloat(getComputedStyle(card).opacity) : -1;
});
await rmPage.screenshot({ path: `${SHOT_DIR}/login-reduced-motion.png` });
await rmPage.close();

// ── 6. Route transition leaves no transform on .app-content ──────────────
await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 200));
await page.evaluate(() => {
  document.querySelector('a[href="/range-designer"]')?.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
});
await new Promise((r) => setTimeout(r, 2500));
results.checks.contentTransformAfterNav = await page.evaluate(() => {
  const el = document.querySelector('.app-content');
  return el ? getComputedStyle(el).transform : 'missing';
});

// ── 7. Network: three.js chunk only on login/topology ────────────────────
const netPage = await browser.newPage();
const requested = [];
netPage.on('request', (req) => requested.push(req.url()));
await netPage.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2500));
const threeOnDashboard = requested.some((u) => /three/i.test(u));
requested.length = 0;
await netPage.goto(`${BASE}/login?preview=1`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2500));
const chunksOnLogin = requested.filter((u) => /chunk-.*\.js/.test(u)).length;
results.checks.threeOnDashboard = threeOnDashboard;
results.checks.lazyChunksFetchedOnLogin = chunksOnLogin;
await netPage.close();

await browser.close();
console.log(JSON.stringify(results, null, 2));
