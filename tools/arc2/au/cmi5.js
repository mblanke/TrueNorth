/*
 * cmi5-au.js - minimal cmi5 (Quartz) Assignable Unit runtime. No dependencies.
 * Runs in browsers and in Node 18+ (global fetch). Reference/teaching code: review before production use.
 *
 * Implements: launch-parameter parsing, one-time fetch of the auth token (cached per session in
 * sessionStorage when available), LMS.LaunchData + cmi5LearnerPreferences reads, context-template use,
 * initialized / progressed (cmi5-allowed) / completed / passed / failed / terminated, launchMode,
 * masteryScore, once-per-registration rules (tracked in an AU-owned State document), returnURL.
 */
(function (root) {
  'use strict';
  const XV = '1.0.3';                                   // cmi5 Quartz is bound to xAPI 1.0.3
  const EXT = 'https://w3id.org/xapi/cmi5/context/extensions/';
  const CAT_CMI5 = { id: 'https://w3id.org/xapi/cmi5/context/categories/cmi5' };
  const CAT_MOVEON = { id: 'https://w3id.org/xapi/cmi5/context/categories/moveon' };
  const PROGRESS = 'https://w3id.org/xapi/cmi5/result/extensions/progress';
  const VERB = {
    initialized: 'http://adlnet.gov/expapi/verbs/initialized',
    progressed: 'http://adlnet.gov/expapi/verbs/progressed',
    completed: 'http://adlnet.gov/expapi/verbs/completed',
    passed: 'http://adlnet.gov/expapi/verbs/passed',
    failed: 'http://adlnet.gov/expapi/verbs/failed',
    terminated: 'http://adlnet.gov/expapi/verbs/terminated'
  };
  const AU_STATE_ID = 'au.status';                     // AU-owned State document (never touch LMS.LaunchData)

  function uuid() {
    const c = root.crypto;
    if (c && typeof c.randomUUID === 'function') return c.randomUUID();
    const b = new Uint8Array(16);
    if (c && c.getRandomValues) c.getRandomValues(b); else for (let i = 0; i < 16; i++) b[i] = Math.floor(Math.random() * 256);
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
  }
  function isoDuration(ms) { const cs = Math.max(0, Math.round(ms / 10)); return `PT${Math.floor(cs / 100)}.${String(cs % 100).padStart(2, '0')}S`; }
  function store() { try { return root.sessionStorage || null; } catch (e) { return null; } }

  class Cmi5AU {
    constructor(search) {
      const p = new URLSearchParams(search || (root.location && root.location.search) || '');
      for (const k of ['endpoint', 'fetch', 'actor', 'registration', 'activityId']) {
        if (!p.get(k)) throw new Error(`cmi5 launch parameter missing: ${k}`);
      }
      this.endpoint = p.get('endpoint').replace(/\/?$/, '/');
      this.fetchUrl = p.get('fetch');
      this.actor = JSON.parse(p.get('actor'));
      this.registration = p.get('registration');
      this.activityId = p.get('activityId');
      this.sent = new Set();        // cmi5-defined verbs sent in this session
      this.terminated = false;
    }

    async _token() {
      const key = `cmi5-token:${this.fetchUrl}`, s = store();
      if (s && s.getItem(key)) return s.getItem(key);
      const r = await fetch(this.fetchUrl, { method: 'POST' });           // POST only; GET is not allowed
      const j = await r.json();
      if (j['error-code']) throw new Error(`fetch URL error ${j['error-code']}: ${j['error-text'] || ''}`);
      if (!j['auth-token']) throw new Error('fetch URL returned no auth-token');
      if (s) s.setItem(key, j['auth-token']);
      return j['auth-token'];
    }

    async _xapi(method, resource, params, body, opts = {}) {
      const qs = new URLSearchParams(params || {}).toString();
      const headers = Object.assign({ 'Authorization': `Basic ${this.token}`, 'X-Experience-API-Version': XV }, opts.headers || {});
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      const r = await fetch(this.endpoint + resource + (qs ? `?${qs}` : ''), {
        method, headers, body: body === undefined ? undefined : JSON.stringify(body), keepalive: !!opts.keepalive
      });
      if (opts.allow404 && r.status === 404) return null;
      if (opts.allow403 && r.status === 403) return null;              // LMS may refuse preference writes
      if (!r.ok) throw new Error(`${method} ${resource} -> HTTP ${r.status}: ${await r.text()}`);
      const t = await r.text();
      let out; try { out = t ? JSON.parse(t) : null; } catch (e) { out = t; }
      return opts.withEtag ? { body: out, etag: r.headers.get('ETag') } : out;
    }

    _stateParams(stateId) { return { activityId: this.activityId, agent: JSON.stringify(this.actor), registration: this.registration, stateId }; }

    async start() {
      this.token = await this._token();
      this.launchData = await this._xapi('GET', 'activities/state', this._stateParams('LMS.LaunchData'));
      if (!this.launchData || !this.launchData.contextTemplate) throw new Error('LMS.LaunchData missing or invalid');
      this.prefs = (await this._xapi('GET', 'agents/profile', { agent: JSON.stringify(this.actor), profileId: 'cmi5LearnerPreferences' }, undefined, { allow404: true })) || {};
      await this._loadStatus();
      this.mode = this.launchData.launchMode;
      this.masteryScore = typeof this.launchData.masteryScore === 'number' ? this.launchData.masteryScore : null;
      this.t0 = Date.now();
      await this._send('initialized', { cmi5: true });                   // first statement of the session
      return this;
    }

    get languages() { return (this.prefs.languagePreference || '').split(',').filter(Boolean); }
    get judged() { return this.mode === 'Normal'; }                        // Browse/Review: no completed/passed/failed

    _context(categories, extraExt) {
      const ctx = JSON.parse(JSON.stringify(this.launchData.contextTemplate));  // template values must not be overwritten
      ctx.registration = this.registration;
      ctx.contextActivities = ctx.contextActivities || {};
      if (categories) ctx.contextActivities.category = (ctx.contextActivities.category || []).concat(categories);
      ctx.extensions = Object.assign({}, extraExt || {}, ctx.extensions || {});
      return ctx;
    }

    async _send(verb, { cmi5 = false, moveon = false, result, ext, keepalive = false } = {}) {
      if (this.terminated) throw new Error('session already terminated');
      if (cmi5 && this.sent.has(verb)) throw new Error(`cmi5 verb ${verb} already sent in this session`);
      const cats = cmi5 ? (moveon ? [CAT_CMI5, CAT_MOVEON] : [CAT_CMI5]) : null;
      const stmt = {
        id: uuid(), actor: this.actor,
        verb: { id: VERB[verb], display: { 'en-US': verb } },
        object: { objectType: 'Activity', id: this.activityId },     // runtime activityId, never the publisher ID
        context: this._context(cats, ext),
        timestamp: new Date().toISOString()                           // UTC
      };
      if (result) stmt.result = result;
      await this._xapi('POST', 'statements', null, stmt, { keepalive });
      if (cmi5) this.sent.add(verb);
      return stmt;
    }

    async _loadStatus() {
      const r = await this._xapi('GET', 'activities/state', this._stateParams(AU_STATE_ID), undefined, { allow404: true, withEtag: true });
      this.auStatus = (r && r.body) || {}; this.auEtag = r ? r.etag : null;
    }

    async _saveStatus() {
      // Always send a concurrency header on document writes: some LRSs require it on State even under 1.0.3,
      // and xAPI 2.0 tightens concurrency rules. Create with If-None-Match: *, update with If-Match.
      const headers = this.auEtag ? { 'If-Match': this.auEtag } : { 'If-None-Match': '*' };
      await this._xapi('PUT', 'activities/state', this._stateParams(AU_STATE_ID), this.auStatus, { headers });
      const keep = this.auStatus; await this._loadStatus(); this.auStatus = Object.assign({}, this.auStatus, keep);
    }

    async progress(pct) {                                               // cmi5-allowed statement
      if (this.auStatus.completed) return null;                         // no progress after completion
      return this._send('progressed', { result: { extensions: { [PROGRESS]: Math.max(0, Math.min(100, Math.round(pct))) } } });
    }

    async complete() {
      if (!this.judged || this.auStatus.completed) return null;          // once per registration; Normal mode only
      const s = await this._send('completed', { cmi5: true, moveon: true, result: { completion: true, duration: isoDuration(Date.now() - this.t0) } });
      this.auStatus.completed = true; await this._saveStatus(); return s;
    }

    async score(scaled) {                                               // decides passed/failed against masteryScore
      if (!this.judged || this.auStatus.passed) return null;             // no failed after passed; passed once per registration
      if (this.sent.has('passed') || this.sent.has('failed')) return null;  // at most one of passed/failed per session
      const ok = this.masteryScore === null ? scaled >= 0.5 : scaled >= this.masteryScore;  // 0.5 = AU's own rule when no masteryScore
      const ext = this.masteryScore === null ? undefined : { [EXT + 'masteryscore']: this.masteryScore };
      const s = await this._send(ok ? 'passed' : 'failed', { cmi5: true, moveon: true, ext,
        result: { success: ok, score: { scaled }, duration: isoDuration(Date.now() - this.t0) } });
      if (ok) { this.auStatus.passed = true; await this._saveStatus(); }
      return s;
    }

    async terminate({ keepalive = false, redirect = true } = {}) {
      if (this.terminated) return null;
      const s = await this._send('terminated', { cmi5: true, keepalive, result: { duration: isoDuration(Date.now() - this.t0) } });
      this.terminated = true;
      const st = store(); if (st) st.removeItem(`cmi5-token:${this.fetchUrl}`);
      if (redirect && this.launchData.returnURL && root.location && typeof root.location.assign === 'function') root.location.assign(this.launchData.returnURL);
      return s;
    }
  }

  root.Cmi5AU = Cmi5AU;
  if (typeof module !== 'undefined' && module.exports) module.exports = { Cmi5AU };
})(typeof window !== 'undefined' ? window : globalThis);
