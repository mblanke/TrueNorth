// test-au-node.mjs - end-to-end test of cmi5-au.js in Node 18+ against a real LRS.
// Plays the fetch-URL service itself; expects the LMS launch step to have been done by
// scripts/cmi5-session-sim.sh with LMS_ONLY=1 and FETCH_URL=http://127.0.0.1:8099/fetch.
// Usage: LRS_ENDPOINT=... LRS_KEY=... LRS_SECRET=... LAUNCH_QUERY='endpoint=...&fetch=...' REGISTRATION=... node test-au-node.mjs
import http from 'node:http';
import { createRequire } from 'node:module';
const { Cmi5AU } = createRequire(import.meta.url)('./cmi5-au.js');
const { LRS_ENDPOINT, LRS_KEY, LRS_SECRET, LAUNCH_QUERY, REGISTRATION } = process.env;
const token = Buffer.from(`${LRS_KEY}:${LRS_SECRET}`).toString('base64');
let issued = false;
const srv = http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  if (req.method !== 'POST') { res.statusCode = 405; return res.end('{}'); }
  res.end(JSON.stringify(issued ? { 'error-code': '1', 'error-text': 'The authorization token has already been returned.' } : { 'auth-token': token }));
  issued = true;
}).listen(8099, '127.0.0.1');
const check = (c, m) => { console.log(`${c ? 'PASS' : 'FAIL'}  ${m}`); if (!c) process.exitCode = 1; };
try {
  const au = await new Cmi5AU('?' + LAUNCH_QUERY).start();
  check(au.mode === 'Normal', `launch data read (mode ${au.mode}, masteryScore ${au.masteryScore}, languages ${au.languages.join('/')})`);
  const second = await (await fetch('http://127.0.0.1:8099/fetch', { method: 'POST' })).json();
  check(second['error-code'] === '1', 'second fetch POST returns error-code 1 (one-time URL)');
  check((await au.progress(50))?.verb.display['en-US'] === 'progressed', 'progressed (cmi5-allowed) sent');
  check((await au.complete())?.result.completion === true, 'completed sent with completion=true');
  check((await au.complete()) === null, 'second completed suppressed (once per registration)');
  const s = await au.score(0.9);
  check(s?.verb.display['en-US'] === 'passed' && s.context.extensions['https://w3id.org/xapi/cmi5/context/extensions/masteryscore'] === au.masteryScore, 'passed sent with masteryscore extension');
  check((await au.score(0.2)) === null, 'failed after passed suppressed');
  check((await au.terminate({ redirect: false }))?.result.duration?.startsWith('PT'), 'terminated sent with duration');
  const r = await fetch(`${LRS_ENDPOINT.replace(/\/?$/, '/')}statements?registration=${REGISTRATION}&limit=50`,
    { headers: { Authorization: `Basic ${token}`, 'X-Experience-API-Version': '1.0.3' } });
  const chain = (await r.json()).statements.sort((a, b) => a.timestamp.localeCompare(b.timestamp)).map(x => x.verb.display['en-US']).join(' -> ');
  console.log(chain);
  check(chain === 'launched -> initialized -> progressed -> completed -> passed -> terminated', 'LRS holds the expected verb sequence');
} catch (e) { check(false, e.stack); }
srv.close();
