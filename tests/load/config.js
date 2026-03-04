// config.js — Shared k6 configuration for TrueNorth Range load tests
import { Trend, Rate, Counter } from "k6/metrics";

// ── Environment ──────────────────────────────────────────────────────────────
export const BASE_URL  = __ENV.BASE_URL  || "http://localhost:8080";
export const WS_URL    = __ENV.WS_URL    || "ws://localhost:8080";
export const AUTH_TOKEN = __ENV.AUTH_TOKEN || "dev-test-token";

// ── Common headers ───────────────────────────────────────────────────────────
export function headers(extra) {
  const h = {
    "Content-Type":  "application/json",
    "Accept":        "application/json",
    "Authorization": `Bearer ${AUTH_TOKEN}`,
  };
  return Object.assign(h, extra || {});
}

// ── Custom metrics ───────────────────────────────────────────────────────────
export const rangeCreationDuration   = new Trend("range_creation_duration",   true);
export const provisionDuration       = new Trend("provision_duration",        true);
export const exerciseCompleteDuration = new Trend("exercise_complete_duration", true);
export const batchProvisionDuration  = new Trend("batch_provision_duration",  true);
export const wsMessageLatency        = new Trend("ws_message_latency",        true);
export const httpErrors              = new Rate("http_errors");
export const provisionCounter        = new Counter("provisions_enqueued");

// ── Default thresholds ───────────────────────────────────────────────────────
export const defaultThresholds = {
  http_req_duration:          ["p(95)<500", "p(99)<1500"],
  http_req_failed:            ["rate<0.01"],
  range_creation_duration:    ["p(95)<800"],
  provision_duration:         ["p(95)<2000"],
  exercise_complete_duration: ["p(95)<1500"],
  http_errors:                ["rate<0.01"],
};

// ── Aggressive (stress) thresholds ───────────────────────────────────────────
export const stressThresholds = {
  http_req_duration:          ["p(95)<1000", "p(99)<3000"],
  http_req_failed:            ["rate<0.02"],
  range_creation_duration:    ["p(95)<1500"],
  provision_duration:         ["p(95)<5000"],
  exercise_complete_duration: ["p(95)<3000"],
  http_errors:                ["rate<0.02"],
};