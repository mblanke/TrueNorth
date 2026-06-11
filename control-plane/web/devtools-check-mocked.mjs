/* CDP verification with a mocked API: exercises the 3D topology viewer and
 * telemetry charts with realistic data, no backend required. */
import puppeteer from 'puppeteer-core';
import fs from 'node:fs';

const CHROME_CANDIDATES = [
  process.env.CHROME_BIN,
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  `${process.env.LOCALAPPDATA}/Google/Chrome/Application/chrome.exe`,
].filter(Boolean);
const chromePath = CHROME_CANDIDATES.find((p) => fs.existsSync(p));

const BASE = 'http://localhost:4200';
const SHOT_DIR = 'devtools-shots';

const RANGE_ID = '11111111-1111-1111-1111-111111111111';
const ranges = [{ id: RANGE_ID, name: 'Demo Enterprise Range', state: 'running', created_at: new Date().toISOString() }];

const mkCell = (id, type, label, x, y, extra = {}) => ({
  id, type: 'standard.Rectangle', position: { x, y }, size: { width: 120, height: 60 },
  nodeType: type, nodeData: { label, ...extra },
});
const mkLink = (s, t, i) => ({ id: `link-${i}`, type: 'standard.Link', source: { id: s }, target: { id: t } });

const diagram = {
  cells: [
    { id: 'zone-1', type: 'standard.Rectangle', position: { x: 60, y: 60 }, size: { width: 520, height: 360 }, nodeType: 'subnet', nodeData: { label: 'Corp LAN 10.0.0.0/24' } },
    { id: 'zone-2', type: 'standard.Rectangle', position: { x: 660, y: 60 }, size: { width: 340, height: 280 }, nodeType: 'dmz', nodeData: { label: 'DMZ 172.16.0.0/24' } },
    mkCell('dc-1', 'dc', 'DC01', 140, 120, { ip: '10.0.0.10', os: 'winserver2022', vcpu: 4, ram_mb: 8192 }),
    mkCell('srv-1', 'server', 'FILES01', 320, 120, { ip: '10.0.0.20', os: 'ubuntu2204', vcpu: 4, ram_mb: 8192 }),
    mkCell('ws-1', 'workstation', 'WS-ALICE', 140, 300, { ip: '10.0.0.101', os: 'win11', vcpu: 2, ram_mb: 4096 }),
    mkCell('ws-2', 'workstation', 'WS-BOB', 320, 300, { ip: '10.0.0.102', os: 'win11', vcpu: 2, ram_mb: 4096 }),
    mkCell('fw-1', 'firewall', 'PFSENSE', 540, 200, { ip: '10.0.0.1', os: 'pfsense', vcpu: 2, ram_mb: 2048 }),
    mkCell('web-1', 'server', 'WEB01', 720, 140, { ip: '172.16.0.10', os: 'ubuntu2204', vcpu: 2, ram_mb: 4096 }),
    mkCell('kali-1', 'kali', 'ATTACKER', 880, 260, { ip: '172.16.0.66', os: 'kali', vcpu: 2, ram_mb: 4096 }),
    mkCell('sw-1', 'switch', 'SW-CORE', 320, 210),
    mkLink('dc-1', 'sw-1', 1), mkLink('srv-1', 'sw-1', 2), mkLink('ws-1', 'sw-1', 3),
    mkLink('ws-2', 'sw-1', 4), mkLink('sw-1', 'fw-1', 5), mkLink('fw-1', 'web-1', 6),
    mkLink('kali-1', 'web-1', 7),
  ],
};

const now = Date.now();
const telemetryHits = Array.from({ length: 120 }, (_, i) => ({
  _source: {
    timestamp: new Date(now - i * 45_000).toISOString(),
    event_type: ['process_create', 'network_connect', 'dns_query', 'auth_failure'][i % 4],
    hostname: ['WS-ALICE', 'WS-BOB', 'DC01', 'WEB01', 'FILES01'][i % 5],
    source_ip: `10.0.0.${100 + (i % 6)}`,
    process_name: ['powershell.exe', 'chrome.exe', 'svchost.exe'][i % 3],
  },
}));

const browser = await puppeteer.launch({
  executablePath: chromePath,
  headless: 'new',
  defaultViewport: { width: 1600, height: 900 },
});

const page = await browser.newPage();
const issues = [];
page.on('console', (m) => { if (m.type() === 'error') issues.push(m.text()); });
page.on('pageerror', (e) => issues.push(e.message));

await page.setRequestInterception(true);
page.on('request', (req) => {
  const url = req.url();
  const respond = (body) => req.respond({
    status: 200, contentType: 'application/json', body: JSON.stringify(body),
  });
  if (/\/api\/ranges\/.+\/diagram/.test(url)) return respond({ range_id: RANGE_ID, diagram_json: diagram });
  if (/\/api\/ranges(\?|$)/.test(url)) return respond(ranges);
  if (/\/api\/telemetry/.test(url)) return respond({ hits: { hits: telemetryHits } });
  if (/\/api\//.test(url)) return respond([]);
  return req.continue();
});

// 3D topology with data
await page.goto(`${BASE}/topology-3d?range=${RANGE_ID}`, { waitUntil: 'networkidle2', timeout: 60000 });
await new Promise((r) => setTimeout(r, 4500));
await page.screenshot({ path: `${SHOT_DIR}/topology-3d-mocked.png` });
const webgl = await page.evaluate(() => {
  const c = document.querySelector('.canvas-wrap canvas');
  return !!c && c.width > 0;
});

// Telemetry charts with data
await page.goto(`${BASE}/telemetry`, { waitUntil: 'networkidle2' });
await new Promise((r) => setTimeout(r, 1500));
await page.evaluate(() => {
  const sel = document.querySelector('mat-select');
  sel?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
});
await new Promise((r) => setTimeout(r, 600));
await page.evaluate(() => {
  document.querySelector('mat-option')?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
});
await new Promise((r) => setTimeout(r, 400));
await page.evaluate(() => {
  const btns = [...document.querySelectorAll('button')];
  btns.find((b) => b.textContent?.includes('Search'))?.click();
});
await new Promise((r) => setTimeout(r, 2500));
await page.screenshot({ path: `${SHOT_DIR}/telemetry-mocked.png` });
const chartCount = await page.evaluate(() => document.querySelectorAll('.chart canvas').length);

await browser.close();
console.log(JSON.stringify({ webgl3d: webgl, telemetryCharts: chartCount, consoleIssues: issues }, null, 2));
