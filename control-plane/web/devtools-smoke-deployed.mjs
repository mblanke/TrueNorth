/* Smoke test against the Docker-deployed production build. */
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const chromePath = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
].find((p) => fs.existsSync(p));

const browser = await puppeteer.launch({ executablePath: chromePath, headless: 'new', defaultViewport: { width: 1600, height: 900 } });
const page = await browser.newPage();
const errors = [];
page.on('pageerror', (e) => errors.push(e.message));
page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

const shots = 'devtools-shots';
await page.goto('http://localhost:4200/login?preview=1', { waitUntil: 'networkidle2', timeout: 60000 });
await new Promise((r) => setTimeout(r, 4000));
await page.screenshot({ path: `${shots}/deployed-login.png` });

await page.goto('http://localhost:4200/dashboard', { waitUntil: 'networkidle2', timeout: 60000 });
await new Promise((r) => setTimeout(r, 3000));
await page.screenshot({ path: `${shots}/deployed-dashboard.png` });
const url = page.url();

await browser.close();
console.log(JSON.stringify({ finalUrl: url, errors: errors.filter(e => !/Failed to load resource|Http failure/.test(e)) }, null, 2));
