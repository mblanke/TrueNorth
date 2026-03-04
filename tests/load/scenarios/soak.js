// scenarios/soak.js — Soak / endurance test: 500 users, 2 hours
// Monitors for memory leaks via response-time degradation and periodic batch provisions.

import http from "k6/http";
import { check, group, sleep } from "k6";
import { SharedArray } from "k6/data";
import { BASE_URL, headers, defaultThresholds, rangeCreationDuration, provisionDuration, batchProvisionDuration } from "../config.js";
import { randomRangePayload, randomTemplatePayload, randomExercisePayload, randomScenarioPayload, uuidv4, generateTemplates } from "../helpers/data.js";
import { checkStatus, checkResponseTime, checkIsJson } from "../helpers/checks.js";

const soakTemplates = new SharedArray("soak_templates", function () {
  return generateTemplates(100);
});

export const options = {
  scenarios: {
    soak: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "5m",    target: 500 },  // ramp up
        { duration: "1h50m", target: 500 },  // steady state
        { duration: "5m",    target: 0   },  // drain
      ],
      gracefulRampDown: "60s",
    },
  },
  thresholds: Object.assign({}, defaultThresholds, {
    // Soak-specific: watch for degradation
    http_req_duration: ["p(95)<600", "p(99)<2000"],
  }),
  tags: { testType: "soak" },
};

export default function () {
  const h = headers();
  const iteration = __ITER;
  const vu = __VU;

  // ── Every 100 iterations per VU, do a batch provision of 50 ranges ──────
  if (iteration > 0 && iteration % 100 === 0) {
    group("Soak: Batch Provision", function () {
      const rangeIds = [];

      // Create 50 ranges
      for (let i = 0; i < 50; i++) {
        const tpl = soakTemplates[Math.floor(Math.random() * soakTemplates.length)];
        const tplRes = http.post(`${BASE_URL}/templates`, JSON.stringify(tpl), { headers: h });
        let templateId;
        try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

        const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
        try {
          const id = res.json().id;
          if (id) rangeIds.push(id);
        } catch (_) {}
      }

      // Batch-provision all created ranges
      if (rangeIds.length > 0) {
        const bStart = Date.now();
        const batchRes = http.post(
          `${BASE_URL}/ranges/batch-provision`,
          JSON.stringify({ range_ids: rangeIds }),
          { headers: h }
        );
        batchProvisionDuration.add(Date.now() - bStart);
        check(batchRes, {
          "batch provision accepted": (r) => r.status >= 200 && r.status < 300,
        });
      }
    });

    sleep(2);
    return;
  }

  // ── Normal mixed workload ───────────────────────────────────────────────
  const roll = Math.random();

  if (roll < 0.45) {
    group("Soak: Read", function () {
      http.get(`${BASE_URL}/ranges`, { headers: h });
      http.get(`${BASE_URL}/templates`, { headers: h });
      http.get(`${BASE_URL}/exercises`, { headers: h });

      const statsRes = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
      checkStatus(statsRes, 200);

      // Track response-time trend for degradation detection
      checkResponseTime(statsRes, 600);
    });
  } else if (roll < 0.75) {
    group("Soak: Create + Provision", function () {
      const tpl = soakTemplates[Math.floor(Math.random() * soakTemplates.length)];
      const tplRes = http.post(`${BASE_URL}/templates`, JSON.stringify(tpl), { headers: h });
      let templateId;
      try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

      const start = Date.now();
      const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      rangeCreationDuration.add(Date.now() - start);

      let rangeId;
      try { rangeId = res.json().id; } catch (_) {}

      if (rangeId) {
        const pStart = Date.now();
        http.post(`${BASE_URL}/ranges/${rangeId}/provision`, null, { headers: h });
        provisionDuration.add(Date.now() - pStart);
      }
    });
  } else {
    group("Soak: Exercise Lifecycle", function () {
      const exRes = http.post(`${BASE_URL}/exercises`, randomExercisePayload(), { headers: h });
      let exerciseId;
      try { exerciseId = exRes.json().id; } catch (_) {}

      if (exerciseId) {
        http.post(`${BASE_URL}/exercises/${exerciseId}/start`, null, { headers: h });
        sleep(0.3);
        http.post(`${BASE_URL}/exercises/${exerciseId}/complete`, null, { headers: h });
      }

      // Health canary — detect degradation
      const healthRes = http.get(`${BASE_URL}/health`, { headers: h });
      checkStatus(healthRes, 200);
      checkResponseTime(healthRes, 200);
    });
  }

  sleep(Math.random() * 3 + 1); // 1–4 s think time for soak
}