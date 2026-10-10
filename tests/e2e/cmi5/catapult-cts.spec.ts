/**
 * cmi5 conformance of TrueNorth's AU runtime, judged by ADL's CATAPULT Content Test Suite.
 *
 * The CTS (with CATAPULT's player as the LMS) imports the course structure TrueNorth serves
 * for the C105 release (GET /cmi5/releases/{id}/cmi5.xml: every AU URL is TrueNorth's SPA)
 * and launches its AUs into a real browser. Every request the AU makes goes through the
 * CTS, which checks it against the cmi5 requirements and records any it violates; an AU is
 * "conformant" only when it was attempted, became satisfied, and violated nothing.
 *
 * Fails on: any violated requirement in any session, AU 0 not conformant after a passing
 * run, the failing and Browse runs violating anything, or the negative control not being
 * caught (which would mean the checker was not running and every pass was vacuous).
 *
 * Stack: ITEST_WEB=1 ITEST_LRS=1 scripts/itest.sh up (TrueNorth: web on :14200, API on
 * :18081 with AUTH_DISABLED) and compose.catapult.yml (CTS on :13399). docs/cmi5.md.
 */
import { execFileSync } from 'node:child_process';
import { randomUUID } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { APIRequestContext, APIResponse, BrowserContext, Page, expect, request, test } from '@playwright/test';
import { stubIdentityProvider } from '../support/identity';

const ROOT = resolve(__dirname, '../../..');
const BUNDLE = resolve(ROOT, 'content/releases/c105-foundations-d0e83b9f6c1f.tar.gz');
const API = process.env.API_BASE_URL || 'http://localhost:18081';
const CTS = process.env.CATAPULT_CTS_URL || 'http://localhost:13399';
const CTS_USER = { username: 'tn-ci', password: 'tn-ci-catapult-password' };
const CMI5 = 'https://w3id.org/xapi/cmi5/context/categories/cmi5';

type Json = Record<string, any>;

async function ok(resp: APIResponse, what: string): Promise<Json> {
  const text = await resp.text();
  expect(resp.ok(), `${what} => ${resp.status()} ${text.slice(0, 500)}`).toBeTruthy();
  return text ? JSON.parse(text) : {};
}

/** C105 in TrueNorth, accepted (or already there), and its served cmi5.xml. */
async function seedRelease(api: APIRequestContext): Promise<{ id: string; xml: string }> {
  for (const [path, file] of [
    ['/qsp/import-crosswalk', 'truenorth-content-pack/truenorth-content/crosswalk.csv'],
    ['/courses/import-programme', 'content/catalogue/cyber_operator_programme.csv'],
  ]) {
    const buffer = readFileSync(resolve(ROOT, file));
    await ok(await api.post(path, { multipart: { file: { name: 'file.csv', mimeType: 'text/csv', buffer } } }), path);
  }
  let rel = await ok(
    await api.post('/course-releases', {
      multipart: { file: { name: 'c105.tar.gz', mimeType: 'application/gzip', buffer: readFileSync(BUNDLE) } },
    }),
    'upload C105',
  );
  if (rel['state'] === 'candidate') {
    const acks = rel['open_actions'].map((a: Json) => a['id']);
    rel = await ok(await api.post(`/course-releases/${rel['id']}/accept`, { data: { acknowledge_actions: acks } }), 'accept');
  }
  const xml = await api.get(`/cmi5/releases/${rel['id']}/cmi5.xml`);
  expect(xml.status()).toBe(200);
  return { id: rel['id'], xml: await xml.text() };
}

/** The answers of a module's formative quiz, read from the release itself. */
function answers(module: string): string[] {
  const raw = execFileSync('tar', ['-xzOf', BUNDLE, `learner/07-bundle/cmi5/${module}/course-config.json`]);
  return JSON.parse(raw.toString())['quiz']['questions'].map((q: Json) => q['answer']);
}

async function cts(): Promise<APIRequestContext> {
  const anon = await request.newContext({ baseURL: CTS });
  const boot = await anon.post('/api/v1/bootstrap', { data: { firstUser: CTS_USER } });
  expect([200, 204, 400], await boot.text()).toContain(boot.status()); // 400: already bootstrapped
  await anon.dispose();
  const auth = Buffer.from(`${CTS_USER.username}:${CTS_USER.password}`).toString('base64');
  return request.newContext({ baseURL: CTS, extraHTTPHeaders: { Authorization: `Basic ${auth}` } });
}

/** Play one module in the browser: read every page, answer the quiz, Exit. */
async function play(page: Page, launchUrl: string, picks: string[] | null): Promise<void> {
  await page.goto(launchUrl);
  // Started, or failed to: whichever comes first, and a failure is reported in the AU's words.
  const started = page.getByTestId('au-status').filter({ hasText: /Tracked session/ });
  await expect(started.or(page.getByTestId('au-error')).first()).toBeVisible({ timeout: 60_000 });
  if (await page.getByTestId('au-error').count()) {
    throw new Error(`the AU did not start: ${await page.getByTestId('au-error').innerText()}`);
  }
  const position = page.getByTestId('au-position');
  while (await position.count()) {
    const [at, of] = (await position.innerText()).split('/').map(s => Number(s.trim()));
    await page.getByTestId('au-next').click();
    if (at === of) break;
    await expect(position).toContainText(`${at + 1} /`);
  }
  if (picks) {
    await expect(page.getByTestId('au-quiz')).toBeVisible();
    for (const [i, letter] of picks.entries()) {
      await page.getByTestId(`au-q${i}-${letter}`).locator('input[type="radio"]').check();
    }
    await page.getByTestId('au-submit').click();
    await expect(page.getByTestId('au-result')).toBeVisible();
    console.log(`[au quiz] picked ${picks.join('')}: ${await page.getByTestId('au-result').innerText()}`);
  }
  await expect(page.getByTestId('au-error')).toHaveCount(0);
  await page.getByTestId('au-exit').click();
  await page.waitForURL(/\/api\/v1\/sessions\/\d+\/return-url/, { timeout: 30_000 }); // returnURL followed
}

async function session(ctsApi: APIRequestContext, id: number): Promise<{ violated: string[]; summaries: string[] }> {
  const s = await ok(await ctsApi.get(`/api/v1/sessions/${id}`), 'session');
  const logs = (await ok(await ctsApi.get(`/api/v1/sessions/${id}/logs`), 'session logs')) as unknown as Json[];
  return { violated: s['metadata']['violatedReqIds'], summaries: logs.map(l => String(l['metadata']['summary'])) };
}

test.describe('CATAPULT CTS: TrueNorth AU runtime', () => {
  let tn: APIRequestContext;
  let ctsApi: APIRequestContext;
  let release: { id: string; xml: string };
  let courseId: number;
  let context: BrowserContext;

  test.beforeAll(async ({ browser }) => {
    tn = await request.newContext({ baseURL: API });
    release = await seedRelease(tn);
    ctsApi = await cts();
    const course = await ok(
      await ctsApi.post('/api/v1/courses', { headers: { 'Content-Type': 'text/xml' }, data: release.xml }),
      'CTS import of the served cmi5.xml',
    );
    courseId = course['id'];
    expect(course['metadata']['aus'].length).toBe(6);
    context = await browser.newContext();
    await stubIdentityProvider(context, { signedIn: true });
    // What the AU did, in the job log: its console, and every request that failed or was refused.
    context.on('console', m => console.log(`[au console:${m.type()}] ${m.text()}`));
    context.on('requestfailed', r => console.log(`[au request failed] ${r.method()} ${r.url()} ${r.failure()?.errorText}`));
    context.on('response', r => {
      if (r.status() >= 400) console.log(`[au response] ${r.status()} ${r.request().method()} ${r.url()}`);
    });
  });

  test.afterAll(async () => {
    await context?.close();
    await tn?.dispose();
    await ctsApi?.dispose();
  });

  async function newTest(name: string): Promise<number> {
    const t = await ok(
      await ctsApi.post('/api/v1/tests', {
        data: { courseId, actor: { objectType: 'Agent', account: { homePage: 'https://cts.truenorth.test', name } } },
      }),
      'CTS test registration',
    );
    return t['id'];
  }

  async function launch(testId: number, auIndex: number, launchMode?: string): Promise<{ id: number; launchUrl: string }> {
    const s = await ok(
      await ctsApi.post('/api/v1/sessions', { data: { testId, auIndex, ...(launchMode ? { launchMode } : {}) } }),
      'CTS launch',
    );
    expect(new URL(s['launchUrl']).pathname).toBe(`/au/releases/${release.id}/${auIndex}`);
    return { id: s['id'], launchUrl: s['launchUrl'] };
  }

  test('passing, failing and Browse sessions are conformant', async () => {
    const testId = await newTest(`tn-ci-${Date.now()}`);
    const page = await context.newPage();

    const pass = await launch(testId, 0);
    await play(page, pass.launchUrl, answers('mod_001'));
    const wrong = answers('mod_002').map(a => (a === 'A' ? 'B' : 'A'));
    const fail = await launch(testId, 1);
    await play(page, fail.launchUrl, wrong);
    const browse = await launch(testId, 2, 'Browse');
    await play(page, browse.launchUrl, null);

    for (const [name, s] of [['pass', pass], ['fail', fail], ['browse', browse]] as const) {
      const { violated, summaries } = await session(ctsApi, s.id);
      expect(violated, `${name}: violated cmi5 requirements`).toEqual([]);
      expect(summaries.join('\n'), `${name}: the AU's statements reached the CTS`).toContain('/initialized');
      expect(summaries.join('\n')).toContain('/terminated');
      expect(summaries.some(m => m.includes('LMS Launch Data retrieved'))).toBeTruthy();
      expect(summaries.some(m => m.includes('Learner Preferences'))).toBeTruthy();
    }
    expect((await session(ctsApi, pass.id)).summaries.join('\n')).toContain('/passed');
    expect((await session(ctsApi, fail.id)).summaries.join('\n')).toContain('/failed');
    expect((await session(ctsApi, browse.id)).summaries.join('\n')).not.toMatch(/\/(completed|passed|failed)/);

    const verdict = await ok(await ctsApi.get(`/api/v1/tests/${testId}`), 'CTS verdict');
    const aus = verdict['metadata']['aus'] as Json[];
    expect(aus[0]['result'], 'AU 0 after a passing run').toBe('conformant');
    expect(aus[1]['result'], 'AU 1 after a failing run: attempted, not satisfied, nothing violated').toBe('pending');
    expect(verdict['metadata']['result']).not.toBe('non-conformant');
    await page.close();
  });

  test('negative control: the CTS catches a non-conformant AU', async () => {
    // Speak to the CTS as a broken AU would: a cmi5-defined statement before initialized.
    const testId = await newTest(`tn-ci-control-${Date.now()}`);
    const s = await launch(testId, 3);
    const q = new URL(s.launchUrl).searchParams;
    const au = await request.newContext();
    const token = (await (await au.post(q.get('fetch')!)).json())['auth-token'];
    expect(token, 'fetch URL returned a token').toBeTruthy();
    const headers = { Authorization: `Basic ${token}`, 'X-Experience-API-Version': '1.0.3' };
    const endpoint = q.get('endpoint')!.replace(/\/?$/, '/');
    const agent = q.get('actor')!;
    const stateQ = new URLSearchParams({ stateId: 'LMS.LaunchData', activityId: q.get('activityId')!, agent, registration: q.get('registration')! });
    const launchData = await (await au.get(`${endpoint}activities/state?${stateQ}`, { headers })).json();
    await au.get(`${endpoint}agents/profile?${new URLSearchParams({ agent, profileId: 'cmi5LearnerPreferences' })}`, { headers });
    const premature = await au.post(`${endpoint}statements`, {
      headers: { ...headers, 'Content-Type': 'application/json' },
      data: {
        id: randomUUID(),
        actor: JSON.parse(agent),
        verb: { id: 'http://adlnet.gov/expapi/verbs/completed', display: { en: 'completed' } },
        object: { objectType: 'Activity', id: q.get('activityId') },
        result: { completion: true, duration: 'PT1S' },
        context: {
          ...launchData['contextTemplate'],
          registration: q.get('registration'),
          contextActivities: {
            ...launchData['contextTemplate']['contextActivities'],
            category: [{ id: CMI5 }, { id: 'https://w3id.org/xapi/cmi5/context/categories/moveon' }],
          },
        },
        timestamp: new Date().toISOString(),
      },
    });
    expect(premature.status()).toBeGreaterThanOrEqual(400);
    const { violated } = await session(ctsApi, s.id);
    expect(violated).toContain('9.3.0.0-4');
    await au.dispose();
  });
});
