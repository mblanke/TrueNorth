import { Cmi5Au, Cmi5Error, FetchFn, isCmi5Launch, isoDuration } from './cmi5-au';

const ENDPOINT = 'https://lms.example/xapi/';
const FETCH = 'https://lms.example/fetch/one-time';
const ACTOR = { objectType: 'Agent', account: { homePage: 'https://lms.example', name: 'student-1' } };
const REG = '7c1c7c6a-0a1b-4c2d-8e3f-9a0b1c2d3e4f';
const ACTIVITY = 'https://lms.example/runtime/au/0';
const SESSION = 'b2a1c3d4-0000-4000-8000-000000000001';
const SEARCH = new URLSearchParams({
  endpoint: ENDPOINT, fetch: FETCH, actor: JSON.stringify(ACTOR), registration: REG, activityId: ACTIVITY,
}).toString();

interface Call { method: string; url: string; headers: Record<string, string>; body: any }

/** Just enough of an LMS: one-time fetch, LMS.LaunchData, a 404 preferences profile,
 *  State documents with ETags, and a statements sink. */
class FakeLms {
  calls: Call[] = [];
  statements: any[] = [];
  fetches = 0;
  docs = new Map<string, string>();

  constructor(launch: Record<string, unknown>) {
    this.docs.set('LMS.LaunchData', JSON.stringify({
      contextTemplate: {
        contextActivities: { grouping: [{ id: 'https://publisher.example/au/1' }] },
        extensions: { 'https://w3id.org/xapi/cmi5/context/extensions/sessionid': SESSION },
      },
      ...launch,
    }));
  }

  readonly fetch: FetchFn = async (input, init = {}) => {
    const url = new URL(input);
    const headers = (init.headers ?? {}) as Record<string, string>;
    const method = init.method ?? 'GET';
    this.calls.push({ method, url: input, headers, body: init.body ? JSON.parse(init.body as string) : null });
    if (input === FETCH) {
      this.fetches += 1;
      return json(this.fetches === 1 ? { 'auth-token': 'dG9rZW4=' } : { 'error-code': '1', 'error-text': 'used' });
    }
    expect(headers['Authorization']).toBe('Basic dG9rZW4=');
    expect(headers['X-Experience-API-Version']).toBe('1.0.3');
    const resource = url.pathname.replace('/xapi/', '');
    if (resource === 'statements') {
      this.statements.push(JSON.parse(init.body as string));
      return json([]);
    }
    if (resource === 'agents/profile') return new Response(null, { status: 404 });
    if (resource === 'activities/state') {
      const id = url.searchParams.get('stateId')!;
      if (method === 'PUT') {
        if (this.docs.has(id) ? headers['If-Match'] !== `"${id}"` : headers['If-None-Match'] !== '*') {
          return new Response(null, { status: 409 });
        }
        this.docs.set(id, init.body as string);
        return new Response(null, { status: 204 });
      }
      const doc = this.docs.get(id);
      return doc ? new Response(doc, { status: 200, headers: { ETag: `"${id}"` } }) : new Response(null, { status: 404 });
    }
    return new Response(null, { status: 404 });
  };

  verbs(): string[] {
    return this.statements.map(s => s.verb.id.split('/').pop());
  }
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

class MemoryStorage implements Storage {
  private m = new Map<string, string>();
  get length(): number { return this.m.size; }
  clear(): void { this.m.clear(); }
  getItem(k: string): string | null { return this.m.get(k) ?? null; }
  key(i: number): string | null { return [...this.m.keys()][i] ?? null; }
  removeItem(k: string): void { this.m.delete(k); }
  setItem(k: string, v: string): void { this.m.set(k, v); }
}

describe('Cmi5Au', () => {
  async function started(launch: Record<string, unknown> = { launchMode: 'Normal', masteryScore: 0.7, returnURL: 'https://lms.example/back' }) {
    const lms = new FakeLms(launch);
    const storage = new MemoryStorage();
    const au = await new Cmi5Au(SEARCH, lms.fetch, storage).start();
    return { lms, au, storage };
  }

  it('needs every launch parameter', () => {
    expect(isCmi5Launch(SEARCH)).toBeTrue();
    expect(isCmi5Launch('endpoint=x')).toBeFalse();
    expect(() => new Cmi5Au('endpoint=x', async () => json({}))).toThrowError(Cmi5Error, /fetch/);
  });

  it('fetches the token once with POST, reads the launch data and preferences, then sends initialized', async () => {
    const { lms, au, storage } = await started();
    const fetch = lms.calls[0];
    expect(fetch.method).toBe('POST');
    expect(fetch.url).toBe(FETCH);
    expect(storage.getItem(`cmi5-token:${FETCH}`)).toBe('dG9rZW4=');
    expect(lms.calls[1].url).toContain('stateId=LMS.LaunchData');
    expect(lms.calls[2].url).toContain('profileId=cmi5LearnerPreferences');
    expect(lms.verbs()).toEqual(['initialized']);
    const st = lms.statements[0];
    expect(st.actor).toEqual(ACTOR);
    expect(st.object).toEqual({ objectType: 'Activity', id: ACTIVITY });
    expect(st.context.registration).toBe(REG);
    expect(st.context.contextActivities.category).toEqual([{ id: 'https://w3id.org/xapi/cmi5/context/categories/cmi5' }]);
    expect(st.context.contextActivities.grouping).toEqual([{ id: 'https://publisher.example/au/1' }]);
    expect(st.context.extensions['https://w3id.org/xapi/cmi5/context/extensions/sessionid']).toBe(SESSION);
    expect(st.timestamp).toMatch(/Z$/);
    expect(st.result).toBeUndefined();
    expect(au.mode).toBe('Normal');
    expect(au.masteryScore).toBe(0.7);
  });

  it('reuses the token kept for this tab instead of fetching again', async () => {
    const lms = new FakeLms({ launchMode: 'Normal' });
    const storage = new MemoryStorage();
    storage.setItem(`cmi5-token:${FETCH}`, 'dG9rZW4=');
    await new Cmi5Au(SEARCH, lms.fetch, storage).start();
    expect(lms.fetches).toBe(0);
  });

  it('reports a fetch error code', async () => {
    const lms = new FakeLms({ launchMode: 'Normal' });
    lms.fetches = 1; // the token was already handed out
    await expectAsync(new Cmi5Au(SEARCH, lms.fetch, new MemoryStorage()).start()).toBeRejectedWithError(Cmi5Error, /error 1/);
  });

  it('completes once, with the moveon category and a duration, and remembers it', async () => {
    const { lms, au } = await started();
    await au.complete();
    expect(await au.complete()).toBeNull();
    const done = lms.statements.find(s => s.verb.id.endsWith('/completed'));
    expect(done.result.completion).toBeTrue();
    expect(done.result.duration).toMatch(/^PT\d+\.\d\dS$/);
    expect(done.context.contextActivities.category.map((c: any) => c.id)).toContain('https://w3id.org/xapi/cmi5/context/categories/moveon');
    expect(JSON.parse(lms.docs.get('au.status')!)).toEqual({ completed: true });
    expect(au.completed).toBeTrue();
  });

  it('passes against the masteryScore with the masteryscore extension', async () => {
    const { lms, au } = await started();
    await au.score(0.8, { raw: 4, min: 0, max: 5 });
    const passed = lms.statements.find(s => s.verb.id.endsWith('/passed'));
    expect(passed.result.success).toBeTrue();
    expect(passed.result.score).toEqual({ scaled: 0.8, raw: 4, min: 0, max: 5 });
    expect(passed.context.extensions['https://w3id.org/xapi/cmi5/context/extensions/masteryscore']).toBe(0.7);
    expect(await au.score(1)).toBeNull(); // passed once per registration
  });

  it('fails below the masteryScore, once per session', async () => {
    const { lms, au } = await started();
    await au.score(0.6);
    expect(lms.verbs()).toEqual(['initialized', 'failed']);
    expect(lms.statements[1].result.success).toBeFalse();
    expect(await au.score(0.9)).toBeNull();
  });

  it('uses its own pass mark when the LMS sets no masteryScore, and then sends no extension', async () => {
    const { lms, au } = await started({ launchMode: 'Normal' });
    await au.score(0.6, undefined, 0.7);
    const failed = lms.statements[1];
    expect(failed.verb.id).toMatch(/failed$/);
    expect(failed.context.extensions['https://w3id.org/xapi/cmi5/context/extensions/masteryscore']).toBeUndefined();
  });

  it('judges nothing in Browse or Review mode', async () => {
    for (const mode of ['Browse', 'Review']) {
      const { lms, au } = await started({ launchMode: mode });
      expect(await au.complete()).toBeNull();
      expect(await au.score(1)).toBeNull();
      await au.terminate();
      expect(lms.verbs()).toEqual(['initialized', 'terminated']);
    }
  });

  it('reports progress as a cmi5-allowed statement, never after completion', async () => {
    const { lms, au } = await started();
    await au.progress(40.4);
    const p = lms.statements[1];
    expect(p.verb.id).toMatch(/progressed$/);
    expect(p.result.extensions['https://w3id.org/xapi/cmi5/result/extensions/progress']).toBe(40);
    expect(p.context.contextActivities.category).toBeUndefined();
    await au.complete();
    expect(await au.progress(100)).toBeNull();
  });

  it('terminates last, forgets the token and goes to the returnURL', async () => {
    const { lms, au, storage } = await started();
    const to = jasmine.createSpy('redirect');
    await au.terminate({ redirect: to });
    expect(lms.verbs().pop()).toBe('terminated');
    expect(lms.statements.at(-1).result.duration).toMatch(/^PT/);
    expect(to).toHaveBeenCalledWith('https://lms.example/back');
    expect(storage.getItem(`cmi5-token:${FETCH}`)).toBeNull();
    expect(await au.terminate()).toBeNull();
    await expectAsync(au.progress(10)).toBeResolvedTo(null);
  });

  it('formats durations in centiseconds', () => {
    expect(isoDuration(0)).toBe('PT0.00S');
    expect(isoDuration(1234)).toBe('PT1.23S');
    expect(isoDuration(-5)).toBe('PT0.00S');
  });
});
