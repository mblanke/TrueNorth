// scenarios/spike.js — Spike test
// Normal load → sudden 12x spike → recovery monitoring

import http from "k6/http";
import { check, group, sleep } from "k6";
import { BASE_URL, headers, defaultThresholds, rangeCreationDuration } from "../config.js";
import { randomRangePayload, randomTemplatePayload, uuidv4 } from "../helpers/data.js";
import { checkStatus, checkIsJson } from "../helpers/checks.js";

export const options = {
  scenarios: {
    spike: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 100  },  // warm up to normal
        { duration: "1m30s", target: 100 },  // hold normal
        { duration: "30s", target: 1200 },  // SPIKE — 0 → 1200 in 30s
        { duration: "2m",  target: 1200 },  // hold spike
        { duration: "30s", target: 100  },  // drop back to normal
        { duration: "2m",  target: 100  },  // recovery period
        { duration: "30s", target: 0    },  // drain
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: Object.assign({}, defaultThresholds, {
    // During spike we allow slightly higher latencies
    http_req_duration: ["p(95)<1500", "p(99)<3000"],
  }),
  tags: { testType: "spike" },
};

export default function () {
  const h = headers();
  const roll = Math.random();

  if (roll < 0.30) {
    group("Spike: Health + Stats", function () {
      const health = http.get(`${BASE_URL}/health`, { headers: h });
      checkStatus(health, 200);

      const stats = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
      checkStatus(stats, 200);
      checkIsJson(stats);

      const telemetry = http.get(`${BASE_URL}/telemetry`, { headers: h });
      checkStatus(telemetry, 200);
    });
  } else if (roll < 0.65) {
    group("Spike: List + Read", function () {
      const res = http.get(`${BASE_URL}/ranges`, { headers: h });
      checkStatus(res, 200);

      try {
        const items = res.json();
        if (Array.isArray(items) && items.length > 0) {
          const item = items[Math.floor(Math.random() * items.length)];
          if (item.id) {
            http.get(`${BASE_URL}/ranges/${item.id}`, { headers: h });
          }
        }
      } catch (_) {}

      http.get(`${BASE_URL}/templates`, { headers: h });
      http.get(`${BASE_URL}/exercises`, { headers: h });
    });
  } else {
    group("Spike: Create + Provision", function () {
      const tplRes = http.post(`${BASE_URL}/templates`, randomTemplatePayload(), { headers: h });
      let templateId;
      try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

      const start = Date.now();
      const rangeRes = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      rangeCreationDuration.add(Date.now() - start);

      check(rangeRes, {
        "range created under spike": (r) => r.status >= 200 && r.status < 300,
      });

      let rangeId;
      try { rangeId = rangeRes.json().id; } catch (_) {}

      if (rangeId) {
        http.post(`${BASE_URL}/ranges/${rangeId}/provision`, null, { headers: h });
        sleep(0.5);
        http.post(`${BASE_URL}/ranges/${rangeId}/destroy`, null, { headers: h });
      }
    });
  }

  sleep(Math.random() * 1 + 0.3);
}