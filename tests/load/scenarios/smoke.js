// scenarios/smoke.js — Smoke test: 10 users, 1 minute
// Quick validation that all endpoints respond and a basic CRUD flow works.

import http from "k6/http";
import { check, group, sleep } from "k6";
import { BASE_URL, headers, defaultThresholds, rangeCreationDuration } from "../config.js";
import { randomRangePayload, randomTemplatePayload, randomScenarioPayload, randomExercisePayload } from "../helpers/data.js";
import { checkStatus, checkIsJson } from "../helpers/checks.js";

export const options = {
  vus: 10,
  duration: "1m",
  thresholds: defaultThresholds,
  tags: { testType: "smoke" },
};

export default function () {
  const h = headers();

  // ── Health check ───────────────────────────────────────────────────────────
  group("Health Check", function () {
    const res = http.get(`${BASE_URL}/health`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Create template ────────────────────────────────────────────────────────
  let templateId;
  group("Create Template", function () {
    const res = http.post(`${BASE_URL}/templates`, randomTemplatePayload(), { headers: h });
    checkStatus(res, 200) || checkStatus(res, 201);
    checkIsJson(res);
    try { templateId = res.json().id; } catch (_) {}
  });

  // ── List templates ─────────────────────────────────────────────────────────
  group("List Templates", function () {
    const res = http.get(`${BASE_URL}/templates`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Create scenario ────────────────────────────────────────────────────────
  let scenarioId;
  group("Create Scenario", function () {
    const res = http.post(`${BASE_URL}/scenarios`, randomScenarioPayload(), { headers: h });
    checkStatus(res, 200) || checkStatus(res, 201);
    try { scenarioId = res.json().id; } catch (_) {}
  });

  // ── List scenarios ─────────────────────────────────────────────────────────
  group("List Scenarios", function () {
    const res = http.get(`${BASE_URL}/scenarios`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Create range ───────────────────────────────────────────────────────────
  let rangeId;
  group("Create Range", function () {
    const start = Date.now();
    const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
    rangeCreationDuration.add(Date.now() - start);
    checkStatus(res, 200) || checkStatus(res, 201);
    try { rangeId = res.json().id; } catch (_) {}
  });

  // ── List ranges ────────────────────────────────────────────────────────────
  group("List Ranges", function () {
    const res = http.get(`${BASE_URL}/ranges`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Get specific range ─────────────────────────────────────────────────────
  if (rangeId) {
    group("Get Range", function () {
      const res = http.get(`${BASE_URL}/ranges/${rangeId}`, { headers: h });
      checkStatus(res, 200);
      check(res, { "range id matches": (r) => { try { return r.json().id === rangeId; } catch (_) { return false; } } });
    });
  }

  // ── Provision range ────────────────────────────────────────────────────────
  if (rangeId) {
    group("Provision Range", function () {
      const res = http.post(`${BASE_URL}/ranges/${rangeId}/provision`, null, { headers: h });
      checkStatus(res, 200) || checkStatus(res, 202);
    });
  }

  // ── Range stats ────────────────────────────────────────────────────────────
  group("Range Stats", function () {
    const res = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Exercise lifecycle ─────────────────────────────────────────────────────
  let exerciseId;
  group("Create Exercise", function () {
    const res = http.post(`${BASE_URL}/exercises`, randomExercisePayload(scenarioId, rangeId), { headers: h });
    checkStatus(res, 200) || checkStatus(res, 201);
    try { exerciseId = res.json().id; } catch (_) {}
  });

  group("List Exercises", function () {
    const res = http.get(`${BASE_URL}/exercises`, { headers: h });
    checkStatus(res, 200);
  });

  if (exerciseId) {
    group("Start Exercise", function () {
      const res = http.post(`${BASE_URL}/exercises/${exerciseId}/start`, null, { headers: h });
      checkStatus(res, 200) || checkStatus(res, 202);
    });

    sleep(0.5);

    group("Complete Exercise", function () {
      const res = http.post(`${BASE_URL}/exercises/${exerciseId}/complete`, null, { headers: h });
      checkStatus(res, 200) || checkStatus(res, 202);
    });

    group("Generate AAR", function () {
      const res = http.post(`${BASE_URL}/exercises/${exerciseId}/aar/generate`, null, { headers: h });
      checkStatus(res, 200) || checkStatus(res, 202);
    });
  }

  // ── Telemetry ──────────────────────────────────────────────────────────────
  group("Telemetry", function () {
    const res = http.get(`${BASE_URL}/telemetry`, { headers: h });
    checkStatus(res, 200);
  });

  // ── Destroy range ──────────────────────────────────────────────────────────
  if (rangeId) {
    group("Destroy Range", function () {
      const res = http.post(`${BASE_URL}/ranges/${rangeId}/destroy`, null, { headers: h });
      checkStatus(res, 200) || checkStatus(res, 202);
    });
  }

  sleep(1);
}