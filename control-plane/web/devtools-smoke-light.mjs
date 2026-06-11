/* CDP smoke for the Great White North light theme. */
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const chromePath = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find((p) => fs.existsSync(p));

const BASE = process.argv[2] || 'http://localhost:4200';
const SHOTS = 'devtools-shots';
const browser = await puppeteer.launch({ executablePath: chromePath, headless: 'new', defaultViewport: { width: 1600, height: 900 } });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
page.on('console', (m) => {
  if (m.type() === 'error' && !/Failed to load resource|Http failure/.test(m.text())) errors.push(m.text());
});

// Select the light theme (4th swatch) on the dashboard.
await page.goto(`${BASE}/dashboard`, { waitUntil: 'networkidle2', timeout: 60000 });
await new Promise((r) => setTimeout(r, 1500));
const picked = await page.evaluate(() => {
  const buttons = [...document.querySelectorAll('.theme-btn')];
  buttons[3]?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  return buttons.length;
});
await new Promise((r) => setTimeout(r, 800));
await page.screenshot({ path: `${SHOTS}/light-dashboard.png` });

const themeState = await page.evaluate(() => ({
  bodyClass: document.body.className,
  accent: getComputedStyle(document.body).getPropertyValue('--accent').trim(),
  bg: getComputedStyle(document.body).getPropertyValue('--bg-primary').trim(),
  colorScheme: document.documentElement.style.colorScheme,
  stored: localStorage.getItem('tn-theme'),
}));

await page.goto(`${BASE}/curriculum-forge`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1800));
await page.screenshot({ path: `${SHOTS}/light-curriculum-forge.png` });

await page.goto(`${BASE}/login?preview=1`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 3500));
await page.screenshot({ path: `${SHOTS}/light-login.png` });

await page.goto(`${BASE}/range-designer`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 2000));
await page.screenshot({ path: `${SHOTS}/light-range-designer.png` });

await browser.close();
console.log(JSON.stringify({ swatches: picked, themeState, errors }, null, 2));
