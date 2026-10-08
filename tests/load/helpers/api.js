// helpers/api.js — HTTP calls against the published API (docs/interfaces/openapi.json),
// tagged for thresholds and paced under the API's rate limits.
import http from "k6/http";
import { check, sleep } from "k6";
import { API_PREFIX, BASE_URL, RATE_SHARE, headers } from "../config.js";

// The API's per-client limits: requests per 60 s sliding window, per client address and
// bucket (control-plane/api/app/middleware.py: _DEFAULT_ROUTE_LIMITS, RATE_LIMIT_DEFAULT,
// _UNLIMITED_PATHS). First match wins; "/ranges" is a prefix, so a range's provision and
// destroy count against POST /ranges too.
const WINDOW_MS = 60000;
const ROUTE_LIMITS = [
  ["POST", "/ranges/batch-provision", 5],
  ["POST", "/ranges", 30],
  ["GET", "", 200],
];
const DEFAULT_LIMIT = 100;
const UNLIMITED = ["/health"];

function bucketOf(method, path) {
  if (UNLIMITED.some((p) => path.startsWith(p))) return null;
  for (const [m, prefix, limit] of ROUTE_LIMITS) {
    if (method === m && (prefix === "" || path.startsWith(prefix))) return [`${m}:${prefix}`, limit];
  }
  return ["default", DEFAULT_LIMIT];
}

// This VU's own requests in the last window, per bucket. It waits rather than collect
// a 429: a rate-limited request is a failure, and the smoke run tolerates none.
const sent = {};

function pace(method, path) {
  if (!(RATE_SHARE > 0)) return;
  const bucket = bucketOf(method, path.split("?")[0]);
  if (!bucket) return;
  const [key, limit] = bucket;
  const budget = Math.max(1, Math.floor(limit * RATE_SHARE));
  const q = sent[key] || (sent[key] = []);
  for (;;) {
    const now = Date.now();
    while (q.length && q[0] <= now - WINDOW_MS) q.shift();
    if (q.length < budget) {
      q.push(now);
      return;
    }
    sleep((q[0] + WINDOW_MS - now) / 1000 + 0.05);
  }
}

/**
 * One API call. `path` is relative to the API prefix ("/ranges"). Options:
 *   expect  statuses that count as success (default 200)
 *   name    metric name for paths with ids ("GET /ranges/{id}")
 *   kind    "read" | "write" (default: GET is a read)
 */
export function request(method, path, body, opts) {
  const o = opts || {};
  const expect = o.expect || [200];
  const name = o.name || `${method} ${path.split("?")[0]}`;
  const kind = o.kind || (method === "GET" ? "read" : "write");
  pace(method, path);
  const res = http.request(method, `${BASE_URL}${API_PREFIX}${path}`, body == null ? null : JSON.stringify(body), {
    headers: headers(),
    tags: { name, kind },
    responseCallback: http.expectedStatuses(...expect),
  });
  check(res, { [`${name} ${expect.join("|")}`]: (r) => expect.includes(r.status) });
  return res;
}

export const get = (path, opts) => request("GET", path, null, opts);
export const post = (path, body, opts) => request("POST", path, body, Object.assign({ expect: [201] }, opts));
export const del = (path, opts) => request("DELETE", path, null, Object.assign({ expect: [204] }, opts));

/** The JSON body, or null. */
export function json(res) {
  try {
    return res.json();
  } catch (_) {
    return null;
  }
}
