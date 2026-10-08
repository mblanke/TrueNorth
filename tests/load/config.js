// config.js — Shared k6 configuration for TrueNorth Range load tests
import { Trend, Counter } from "k6/metrics";

// ── Environment ──────────────────────────────────────────────────────────────
// The API itself (the dev stack publishes it on 8081, the itest stack on 18081). Behind
// the web container's nginx use http://host:4200/api: the paths below are the same.
export const BASE_URL = __ENV.BASE_URL || "http://localhost:8081";
export const WS_URL = __ENV.WS_URL || BASE_URL.replace(/^http/, "ws");
// The published contract's base path (docs/interfaces/openapi.json "servers").
export const API_PREFIX = __ENV.API_PREFIX === undefined ? "/api/v1" : __ENV.API_PREFIX;
// An access token, when the target authenticates. Unset against an AUTH_DISABLED stack
// (dev, itest), which acts as its dev admin.
export const AUTH_TOKEN = __ENV.AUTH_TOKEN || "";

// PROFILE=smoke: the CI run (1 VU, 30 s). Smoke thresholds (below) instead of each
// scenario's capacity thresholds, and requests paced under the API's rate limits.
export const PROFILE = __ENV.PROFILE || "full";
export const SMOKE = PROFILE === "smoke";

// Share of each of the API's per-client rate limits one run may use (helpers/api.js).
// The limits are per client address and per 60 s, so scenarios run back to back from one
// host share them: a third each keeps any 60 s window under the limit. 0 = no pacing
// (capacity runs, against a target with RATE_LIMIT_ENABLED=false).
export const RATE_SHARE = __ENV.RATE_SHARE !== undefined ? Number(__ENV.RATE_SHARE) : SMOKE ? 1 / 3 : 0;

// ── Common headers ───────────────────────────────────────────────────────────
export function headers(extra) {
  const h = { "Content-Type": "application/json", Accept: "application/json" };
  if (AUTH_TOKEN) h.Authorization = `Bearer ${AUTH_TOKEN}`;
  return Object.assign(h, extra || {});
}

// ── Custom metrics ───────────────────────────────────────────────────────────
export const rangeCreationDuration = new Trend("range_creation_duration", true);
export const provisionDuration = new Trend("provision_duration", true);
export const rangeReadyDuration = new Trend("range_ready_duration", true);
export const exerciseCompleteDuration = new Trend("exercise_complete_duration", true);
export const batchProvisionDuration = new Trend("batch_provision_duration", true);
export const provisionCounter = new Counter("provisions_enqueued");

// ── Thresholds ───────────────────────────────────────────────────────────────
// Smoke: no failed request, reads under a second, writes under two (p95), every check.
export const smokeThresholds = {
  http_req_failed: ["rate<0.01"],
  "http_req_duration{kind:read}": ["p(95)<1000"],
  "http_req_duration{kind:write}": ["p(95)<2000"],
  checks: ["rate>0.99"],
};

// Default capacity thresholds (smoke, load, soak profiles at full scale).
export const defaultThresholds = {
  http_req_duration: ["p(95)<500", "p(99)<1500"],
  http_req_failed: ["rate<0.01"],
  range_creation_duration: ["p(95)<800"],
  provision_duration: ["p(95)<2000"],
  exercise_complete_duration: ["p(95)<1500"],
  checks: ["rate>0.99"],
};

// Relaxed for peak load (stress, spike).
export const stressThresholds = {
  http_req_duration: ["p(95)<1000", "p(99)<3000"],
  http_req_failed: ["rate<0.02"],
  range_creation_duration: ["p(95)<1500"],
  provision_duration: ["p(95)<5000"],
  exercise_complete_duration: ["p(95)<3000"],
  checks: ["rate>0.98"],
};

// The scenario's own thresholds, or the smoke ones (plus `smokeExtra`) under PROFILE=smoke.
export function thresholds(full, smokeExtra) {
  return SMOKE ? Object.assign({}, smokeThresholds, smokeExtra || {}) : full;
}
