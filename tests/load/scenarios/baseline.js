// scenarios/baseline.js — CI gate: quick baseline regression check
// Light load, strict thresholds — fails the build if performance degrades
//
// Usage: k6 run --env BASE_URL=https://localhost scenarios/baseline.js

import http from "k6/http";
import { check, group, sleep } from "k6";
import { BASE_URL, headers, rangeCreationDuration } from "../config.js";
import { randomRangePayload, generateTemplates, uuidv4 } from "../helpers/data.js";
import { checkStatus, checkResponseTime, checkIsJson } from "../helpers/checks.js";

export const options = {
  scenarios: {
    baseline: {
      executor: "constant-vus",
      vus: 10,
      duration: "1m",
    },
  },
  thresholds: {
    // CI-gate thresholds — tighter than normal load test
    http_req_duration: ["p(50)<200", "p(95)<400", "p(99)<800"],
    http_req_failed:   ["rate<0.005"],  // 0.5% error rate max
    checks:            ["rate>0.99"],   // 99% checks pass
  },
  tags: { testType: "baseline" },
};

export default function () {
  const h = headers();

  group("Health check", () => {
    const res = http.get(`${BASE_URL}/health`, { headers: h });
    check(res, {
      "health 200": (r) => r.status === 200,
      "health < 100ms": (r) => r.timings.duration < 100,
      "health has status": (r) => r.json("status") === "ok",
    });
  });

  group("List endpoints", () => {
    const endpoints = ["/ranges", "/templates", "/exercises", "/scenarios"];
    const ep = endpoints[Math.floor(Math.random() * endpoints.length)];
    const res = http.get(`${BASE_URL}${ep}`, { headers: h });
    check(res, {
      [`${ep} status 200`]: (r) => r.status === 200,
      [`${ep} < 400ms`]: (r) => r.timings.duration < 400,
      [`${ep} is JSON`]: (r) => r.headers["Content-Type"]?.includes("json"),
    });
  });

  group("Range creation", () => {
    const payload = JSON.stringify(randomRangePayload());
    const res = http.post(`${BASE_URL}/ranges`, payload, { headers: h });
    rangeCreationDuration.add(res.timings.duration);
    check(res, {
      "create 201": (r) => r.status === 201,
      "create < 500ms": (r) => r.timings.duration < 500,
    });
  });

  sleep(0.5);
}

export function handleSummary(data) {
  // Output JSON summary for CI parsing
  const passed = !data.root_group?.checks?.some(c => c.fails > 0);
  const summary = {
    timestamp: new Date().toISOString(),
    test: "baseline",
    passed,
    metrics: {
      "p50_ms": data.metrics?.http_req_duration?.values?.["p(50)"],
      "p95_ms": data.metrics?.http_req_duration?.values?.["p(95)"],
      "p99_ms": data.metrics?.http_req_duration?.values?.["p(99)"],
      "error_rate": data.metrics?.http_req_failed?.values?.rate,
      "total_requests": data.metrics?.http_reqs?.values?.count,
    },
  };

  return {
    "stdout": JSON.stringify(summary, null, 2) + "\n",
    "tests/load/results/baseline-latest.json": JSON.stringify(summary, null, 2),
  };
}
