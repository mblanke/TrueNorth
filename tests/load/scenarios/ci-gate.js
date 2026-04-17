// k6-ci-gate.js — Lightweight CI pipeline smoke test with strict thresholds.
// Usage: k6 run tests/load/k6-ci-gate.js --env BASE_URL=http://localhost:8080
//
// Designed to run in < 30 seconds with pass/fail thresholds for CI gates.

import http from "k6/http";
import { check, sleep } from "k6";
import { BASE_URL, headers } from "../config.js";

export const options = {
  stages: [
    { duration: "5s",  target: 5 },   // ramp up
    { duration: "15s", target: 10 },   // steady
    { duration: "5s",  target: 0 },    // ramp down
  ],
  thresholds: {
    http_req_duration: ["p(95)<400", "p(99)<1000"],
    http_req_failed:   ["rate<0.01"],
    checks:            ["rate>0.99"],
  },
};

export default function () {
  // Health check
  const health = http.get(`${BASE_URL}/health`, { headers: headers() });
  check(health, {
    "health 200":    (r) => r.status === 200,
    "health < 200ms": (r) => r.timings.duration < 200,
  });

  // List ranges
  const ranges = http.get(`${BASE_URL}/ranges?limit=5`, { headers: headers() });
  check(ranges, {
    "ranges 200":    (r) => r.status === 200,
    "ranges < 400ms": (r) => r.timings.duration < 400,
  });

  // List templates
  const templates = http.get(`${BASE_URL}/templates?limit=5`, { headers: headers() });
  check(templates, {
    "templates 200":    (r) => r.status === 200,
    "templates < 400ms": (r) => r.timings.duration < 400,
  });

  // List scenarios
  const scenarios = http.get(`${BASE_URL}/scenarios?limit=5`, { headers: headers() });
  check(scenarios, {
    "scenarios 200":    (r) => r.status === 200,
    "scenarios < 400ms": (r) => r.timings.duration < 400,
  });

  // Threat intel feeds (new V2 endpoint)
  const feeds = http.get(`${BASE_URL}/threat-intel/feeds`, { headers: headers() });
  check(feeds, {
    "feeds 200": (r) => r.status === 200,
  });

  // Detection rules (new V2 endpoint)
  const rules = http.get(`${BASE_URL}/detection-rules?limit=5`, { headers: headers() });
  check(rules, {
    "rules 200": (r) => r.status === 200,
  });

  sleep(0.5);
}
