/**
 * The AU side of a cmi5 (Quartz) session, for TrueNorth's AU runtime.
 *
 * A TypeScript port of the knowledge pack's dependency-free AU runtime
 * (docs/xAPI CMI5 Reference/xapi-cmi5-kb/au/cmi5-au.js, the same code ARC² packages as
 * cmi5.js), as docs/moodle-integration.md §2b asks: read the launch parameters, POST once
 * to the fetch URL, read LMS.LaunchData and the learner preferences, then send the
 * cmi5-defined statements with the context template, the cmi5 category, the moveon
 * category where cmi5 requires it, UTC timestamps and durations; respect launchMode and
 * masteryScore; keep completed and passed to once per registration (an AU-owned State
 * document); and leave through returnURL.
 *
 * Talks to the launch's own xAPI endpoint with the session's token only, through
 * `fetch`, never Angular's HttpClient: the TrueNorth sign-in must not reach an LMS's LRS,
 * which may be on another origin (Moodle, PCTE).
 */

export const XAPI_VERSION = '1.0.3';
const EXT = 'https://w3id.org/xapi/cmi5/context/extensions/';
const CAT_CMI5 = { id: 'https://w3id.org/xapi/cmi5/context/categories/cmi5' };
const CAT_MOVEON = { id: 'https://w3id.org/xapi/cmi5/context/categories/moveon' };
const PROGRESS = 'https://w3id.org/xapi/cmi5/result/extensions/progress';
const ADL = 'http://adlnet.gov/expapi/verbs/';
const AU_STATE = 'au.status';
export const LAUNCH_PARAMS = ['endpoint', 'fetch', 'actor', 'registration', 'activityId'] as const;

export type Verb = 'initialized' | 'progressed' | 'completed' | 'passed' | 'failed' | 'terminated';
export type FetchFn = (input: string, init?: RequestInit) => Promise<Response>;

export interface LaunchData {
  contextTemplate: { contextActivities?: Record<string, unknown[]>; extensions?: Record<string, unknown> } & Record<string, unknown>;
  launchMode: 'Normal' | 'Browse' | 'Review';
  moveOn?: string;
  masteryScore?: number;
  launchParameters?: string;
  returnURL?: string;
}

export interface Statement {
  id: string;
  actor: unknown;
  verb: { id: string; display: Record<string, string> };
  object: { objectType: 'Activity'; id: string };
  context: Record<string, any>;
  timestamp: string;
  result?: Record<string, unknown>;
}

export class Cmi5Error extends Error {}

interface AuStatus { completed?: boolean; passed?: boolean }
interface LearnerPreferences { languagePreference?: string; audioPreference?: string }

/** True when the query string carries a cmi5 launch (every parameter present). */
export function isCmi5Launch(search: string): boolean {
  const p = new URLSearchParams(search);
  return LAUNCH_PARAMS.every(k => !!p.get(k));
}

export function isoDuration(ms: number): string {
  const cs = Math.max(0, Math.round(ms / 10));
  return `PT${Math.floor(cs / 100)}.${String(cs % 100).padStart(2, '0')}S`;
}

function uuid(): string {
  return globalThis.crypto.randomUUID();
}

function utcNow(): string {
  return new Date().toISOString(); // always UTC, millisecond precision
}

export class Cmi5Au {
  readonly endpoint: string;
  readonly fetchUrl: string;
  readonly actor: unknown;
  readonly registration: string;
  readonly activityId: string;
  launchData!: LaunchData;
  prefs: LearnerPreferences = {};
  terminated = false;
  private token = '';
  private started = 0;
  private sent = new Set<Verb>();
  private status: AuStatus = {};
  private statusEtag: string | null = null;

  constructor(
    search: string,
    private readonly http: FetchFn = (u, i) => fetch(u, i),
    private readonly storage: Storage | null = Cmi5Au.sessionStore(),
  ) {
    const p = new URLSearchParams(search);
    for (const k of LAUNCH_PARAMS) {
      if (!p.get(k)) throw new Cmi5Error(`cmi5 launch parameter missing: ${k}`);
    }
    this.endpoint = p.get('endpoint')!.replace(/\/?$/, '/');
    this.fetchUrl = p.get('fetch')!;
    this.actor = JSON.parse(p.get('actor')!);
    this.registration = p.get('registration')!;
    this.activityId = p.get('activityId')!;
  }

  private static sessionStore(): Storage | null {
    try {
      return globalThis.sessionStorage ?? null;
    } catch {
      return null;
    }
  }

  get mode(): LaunchData['launchMode'] {
    return this.launchData.launchMode;
  }

  /** Normal mode only: Browse and Review send no completed/passed/failed. */
  get judged(): boolean {
    return this.mode === 'Normal';
  }

  get masteryScore(): number | null {
    return typeof this.launchData.masteryScore === 'number' ? this.launchData.masteryScore : null;
  }

  get completed(): boolean {
    return !!this.status.completed;
  }

  get passed(): boolean {
    return !!this.status.passed;
  }

  /** Fetch the token (once; kept for this tab so a reload keeps the session), read the
   *  launch data and preferences, and send `initialized`. */
  async start(): Promise<this> {
    this.token = await this.authToken();
    const launchData = await this.xapi('GET', 'activities/state', this.stateParams('LMS.LaunchData'));
    if (!launchData || typeof launchData !== 'object' || !('contextTemplate' in launchData)) {
      throw new Cmi5Error('LMS.LaunchData is missing or has no contextTemplate');
    }
    this.launchData = launchData as LaunchData;
    const prefs = await this.xapi(
      'GET', 'agents/profile', { agent: JSON.stringify(this.actor), profileId: 'cmi5LearnerPreferences' }, undefined,
      { allow404: true },
    );
    this.prefs = (prefs as LearnerPreferences | null) ?? {};
    await this.loadStatus();
    this.started = Date.now();
    await this.send('initialized', { cmi5: true });
    return this;
  }

  /** cmi5-allowed progress (0-100), never after completion. */
  async progress(percent: number): Promise<Statement | null> {
    if (this.status.completed || this.terminated) return null;
    const pct = Math.max(0, Math.min(100, Math.round(percent)));
    return this.send('progressed', { result: { extensions: { [PROGRESS]: pct } } });
  }

  /** `completed`, once per registration, Normal mode only. */
  async complete(): Promise<Statement | null> {
    if (!this.judged || this.status.completed || this.sent.has('completed')) return null;
    const st = await this.send('completed', {
      cmi5: true, moveon: true, result: { completion: true, duration: isoDuration(Date.now() - this.started) },
    });
    this.status.completed = true;
    await this.saveStatus();
    return st;
  }

  /** `passed` or `failed` against the masteryScore (the AU's own `fallback` when the LMS
   *  sets none). At most one per session; never `failed` after `passed`; `passed` once. */
  async score(scaled: number, raw?: { raw: number; min: number; max: number }, fallback = 0.5): Promise<Statement | null> {
    if (!this.judged || this.status.passed || this.sent.has('passed') || this.sent.has('failed')) return null;
    const mastery = this.masteryScore;
    const ok = scaled >= (mastery ?? fallback);
    const score: Record<string, number> = { scaled: Math.round(scaled * 10000) / 10000, ...(raw ?? {}) };
    const st = await this.send(ok ? 'passed' : 'failed', {
      cmi5: true,
      moveon: true,
      ext: mastery === null ? undefined : { [EXT + 'masteryscore']: mastery },
      result: { success: ok, score, duration: isoDuration(Date.now() - this.started) },
    });
    if (ok) {
      this.status.passed = true;
      await this.saveStatus();
    }
    return st;
  }

  /** `terminated` (last statement of the session), then returnURL when there is one. */
  async terminate(opts: { keepalive?: boolean; redirect?: (url: string) => void } = {}): Promise<Statement | null> {
    if (this.terminated) return null;
    const st = await this.send('terminated', {
      cmi5: true, keepalive: opts.keepalive, result: { duration: isoDuration(Date.now() - this.started) },
    });
    this.terminated = true;
    this.storage?.removeItem(this.tokenKey);
    if (opts.redirect && this.launchData.returnURL) opts.redirect(this.launchData.returnURL);
    return st;
  }

  // -- internals ------------------------------------------------------------------------------
  private get tokenKey(): string {
    return `cmi5-token:${this.fetchUrl}`;
  }

  private async authToken(): Promise<string> {
    const kept = this.storage?.getItem(this.tokenKey);
    if (kept) return kept;
    const r = await this.http(this.fetchUrl, { method: 'POST', credentials: 'omit' }); // POST only (cmi5 8.2)
    const body = (await r.json()) as Record<string, string>;
    if (body['error-code']) throw new Cmi5Error(`fetch URL error ${body['error-code']}: ${body['error-text'] ?? ''}`);
    if (!body['auth-token']) throw new Cmi5Error('fetch URL returned no auth-token');
    this.storage?.setItem(this.tokenKey, body['auth-token']);
    return body['auth-token'];
  }

  private stateParams(stateId: string): Record<string, string> {
    return { activityId: this.activityId, agent: JSON.stringify(this.actor), registration: this.registration, stateId };
  }

  private context(categories: { id: string }[] | null, ext?: Record<string, unknown>): Record<string, any> {
    // The template's values are copied, never overwritten (cmi5 10.2.1).
    const ctx = JSON.parse(JSON.stringify(this.launchData.contextTemplate)) as Record<string, any>;
    ctx['registration'] = this.registration;
    ctx['contextActivities'] = ctx['contextActivities'] ?? {};
    if (categories) {
      ctx['contextActivities']['category'] = [...(ctx['contextActivities']['category'] ?? []), ...categories];
    }
    ctx['extensions'] = { ...(ext ?? {}), ...(ctx['extensions'] ?? {}) };
    return ctx;
  }

  private async send(
    verb: Verb,
    opts: { cmi5?: boolean; moveon?: boolean; result?: Record<string, unknown>; ext?: Record<string, unknown>; keepalive?: boolean } = {},
  ): Promise<Statement> {
    if (this.terminated) throw new Cmi5Error('the session is terminated');
    if (opts.cmi5 && this.sent.has(verb)) throw new Cmi5Error(`${verb} was already sent in this session`);
    const categories = opts.cmi5 ? (opts.moveon ? [CAT_CMI5, CAT_MOVEON] : [CAT_CMI5]) : null;
    const st: Statement = {
      id: uuid(),
      actor: this.actor,
      verb: { id: ADL + verb, display: { en: verb } },
      object: { objectType: 'Activity', id: this.activityId }, // the launch activityId, never the publisher id
      context: this.context(categories, opts.ext),
      timestamp: utcNow(),
    };
    if (opts.result) st.result = opts.result;
    await this.xapi('POST', 'statements', null, st, { keepalive: opts.keepalive });
    if (opts.cmi5) this.sent.add(verb);
    return st;
  }

  private async loadStatus(): Promise<void> {
    const r = await this.xapi('GET', 'activities/state', this.stateParams(AU_STATE), undefined, { allow404: true, withEtag: true });
    const got = r as { body: AuStatus | null; etag: string | null } | null;
    this.status = got?.body ?? {};
    this.statusEtag = got?.etag ?? null;
  }

  private async saveStatus(): Promise<void> {
    // Always a concurrency header: lrsql refuses a State PUT over a document without one.
    const headers: Record<string, string> = this.statusEtag ? { 'If-Match': this.statusEtag } : { 'If-None-Match': '*' };
    const keep = { ...this.status };
    await this.xapi('PUT', 'activities/state', this.stateParams(AU_STATE), keep, { headers });
    await this.loadStatus();
    this.status = { ...this.status, ...keep };
  }

  private async xapi(
    method: string,
    resource: string,
    params: Record<string, string> | null,
    body?: unknown,
    opts: { allow404?: boolean; withEtag?: boolean; keepalive?: boolean; headers?: Record<string, string> } = {},
  ): Promise<unknown> {
    const qs = params ? new URLSearchParams(params).toString() : '';
    const headers: Record<string, string> = {
      Authorization: `Basic ${this.token}`,
      'X-Experience-API-Version': XAPI_VERSION,
      ...(opts.headers ?? {}),
    };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const r = await this.http(this.endpoint + resource + (qs ? `?${qs}` : ''), {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      keepalive: !!opts.keepalive,
      credentials: 'omit',
    });
    if (opts.allow404 && r.status === 404) return null;
    if (!r.ok) throw new Cmi5Error(`${method} ${resource}: HTTP ${r.status} ${await r.text()}`);
    const text = await r.text();
    let out: unknown = null;
    try {
      out = text ? JSON.parse(text) : null;
    } catch {
      out = text;
    }
    return opts.withEtag ? { body: out, etag: r.headers.get('ETag') } : out;
  }
}
