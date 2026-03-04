// scenarios/load.js — Standard load test: 200 users, 10 minutes
// Mixed workload: 40% reads, 30% range creation, 20% exercise lifecycle, 10% admin/stats

import http from "k6/http";
import { check, group, sleep } from "k6";
import { SharedArray } from "k6/data";
import { BASE_URL, headers, defaultThresholds, rangeCreationDuration, provisionDuration, exerciseCompleteDuration } from "../config.js";
import { randomRangePayload, randomTemplatePayload, randomScenarioPayload, randomExercisePayload, generateTemplates, generateScenarios, uuidv4 } from "../helpers/data.js";
import { checkStatus, checkResponseTime, checkIsJson } from "../helpers/checks.js";

// ── Pre-generated test data (shared across VUs, loaded once) ─────────────────
const templates = new SharedArray("templates", function () {
  return generateTemplates(50);
});

const scenarios = new SharedArray("scenarios", function () {
  return generateScenarios(50);
});

export const options = {
  scenarios: {
    load_test: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m",  target: 50  },  // ramp up
        { duration: "3m",  target: 200 },  // ramp to peak
        { duration: "3m",  target: 200 },  // hold peak
        { duration: "2m",  target: 0   },  // ramp down
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: defaultThresholds,
  tags: { testType: "load" },
};

export default function () {
  const h = headers();
  const roll = Math.random();

  if (roll < 0.40) {
    // ── 40 %  Browse / Read ────────────────────────────────────────────────
    group("Browse Ranges", function () {
      const res = http.get(`${BASE_URL}/ranges`, { headers: h });
      checkStatus(res, 200);
      checkIsJson(res);

      // If results exist, fetch a random one
      try {
        const items = res.json();
        if (Array.isArray(items) && items.length > 0) {
          const item = items[Math.floor(Math.random() * items.length)];
          if (item.id) {
            const detail = http.get(`${BASE_URL}/ranges/${item.id}`, { headers: h });
            checkStatus(detail, 200);
          }
        }
      } catch (_) {}

      const tplRes = http.get(`${BASE_URL}/templates`, { headers: h });
      checkStatus(tplRes, 200);

      const scenRes = http.get(`${BASE_URL}/scenarios`, { headers: h });
      checkStatus(scenRes, 200);

      const exRes = http.get(`${BASE_URL}/exercises`, { headers: h });
      checkStatus(exRes, 200);
    });
  } else if (roll < 0.70) {
    // ── 30 %  Create Ranges ────────────────────────────────────────────────
    group("Manage Ranges", function () {
      // Seed a template first
      const tpl = templates[Math.floor(Math.random() * templates.length)];
      const tplRes = http.post(`${BASE_URL}/templates`, JSON.stringify(tpl), { headers: h });
      let templateId;
      try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

      // Create range
      const start = Date.now();
      const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      rangeCreationDuration.add(Date.now() - start);
      checkStatus(res, 200) || checkStatus(res, 201);

      let rangeId;
      try { rangeId = res.json().id; } catch (_) {}

      if (rangeId) {
        // Provision
        const pStart = Date.now();
        const prov = http.post(`${BASE_URL}/ranges/${rangeId}/provision`, null, { headers: h });
        provisionDuration.add(Date.now() - pStart);
        checkStatus(prov, 200) || checkStatus(prov, 202);

        sleep(1);

        // Destroy
        http.post(`${BASE_URL}/ranges/${rangeId}/destroy`, null, { headers: h });
      }
    });
  } else if (roll < 0.90) {
    // ── 20 %  Exercise Lifecycle ───────────────────────────────────────────
    group("Run Exercises", function () {
      // Create supporting resources
      const tpl = templates[Math.floor(Math.random() * templates.length)];
      const tplRes = http.post(`${BASE_URL}/templates`, JSON.stringify(tpl), { headers: h });
      let templateId;
      try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

      const scn = scenarios[Math.floor(Math.random() * scenarios.length)];
      const scnRes = http.post(`${BASE_URL}/scenarios`, JSON.stringify(scn), { headers: h });
      let scenarioId;
      try { scenarioId = scnRes.json().id; } catch (_) { scenarioId = uuidv4(); }

      // Create range for the exercise
      const rangeRes = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      let rangeId;
      try { rangeId = rangeRes.json().id; } catch (_) { rangeId = uuidv4(); }

      // Create exercise
      const exRes = http.post(`${BASE_URL}/exercises`, randomExercisePayload(scenarioId, rangeId), { headers: h });
      let exerciseId;
      try { exerciseId = exRes.json().id; } catch (_) {}

      if (exerciseId) {
        // Start
        http.post(`${BASE_URL}/exercises/${exerciseId}/start`, null, { headers: h });
        sleep(0.5);

        // Complete
        const cStart = Date.now();
        const comp = http.post(`${BASE_URL}/exercises/${exerciseId}/complete`, null, { headers: h });
        exerciseCompleteDuration.add(Date.now() - cStart);
        checkStatus(comp, 200) || checkStatus(comp, 202);

        // Generate AAR
        http.post(`${BASE_URL}/exercises/${exerciseId}/aar/generate`, null, { headers: h });
      }
    });
  } else {
    // ── 10 %  Admin / Stats ────────────────────────────────────────────────
    group("Admin Operations", function () {
      const health = http.get(`${BASE_URL}/health`, { headers: h });
      checkStatus(health, 200);

      const stats = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
      checkStatus(stats, 200);
      checkIsJson(stats);

      const telemetry = http.get(`${BASE_URL}/telemetry`, { headers: h });
      checkStatus(telemetry, 200);
    });
  }

  sleep(Math.random() * 2 + 0.5); // 0.5–2.5 s think time
}