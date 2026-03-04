// scenarios/batch-provision.js — Batch provisioning stress test
// Create 100 ranges, then batch-provision in groups of 50.
// Target: 500 provisions enqueued in 5 minutes.

import http from "k6/http";
import { check, group, sleep } from "k6";
import { BASE_URL, headers, stressThresholds, rangeCreationDuration, batchProvisionDuration, provisionCounter } from "../config.js";
import { randomRangePayload, randomTemplatePayload, uuidv4 } from "../helpers/data.js";
import { checkStatus } from "../helpers/checks.js";

export const options = {
  scenarios: {
    batch_provision: {
      executor: "per-vu-iterations",
      vus: 10,
      iterations: 10,         // each VU does 10 rounds
      maxDuration: "10m",
    },
  },
  thresholds: Object.assign({}, stressThresholds, {
    batch_provision_duration: ["p(95)<10000"],  // batch can take longer
    provisions_enqueued:      ["count>=500"],    // target: 500 enqueued
  }),
  tags: { testType: "batch-provision" },
};

export default function () {
  const h = headers();
  const BATCH_SIZE = 50;
  const RANGES_PER_ROUND = 100;

  // ── Phase 1: Create ranges ──────────────────────────────────────────────
  const rangeIds = [];

  group("Batch: Create Ranges", function () {
    // Seed a template
    const tplRes = http.post(`${BASE_URL}/templates`, randomTemplatePayload(), { headers: h });
    let templateId;
    try { templateId = tplRes.json().id; } catch (_) { templateId = uuidv4(); }

    for (let i = 0; i < RANGES_PER_ROUND; i++) {
      const start = Date.now();
      const res = http.post(`${BASE_URL}/ranges`, randomRangePayload(templateId), { headers: h });
      rangeCreationDuration.add(Date.now() - start);

      try {
        const id = res.json().id;
        if (id) rangeIds.push(id);
      } catch (_) {}
    }

    check(null, {
      [`created >= ${RANGES_PER_ROUND * 0.9} ranges`]: () => rangeIds.length >= RANGES_PER_ROUND * 0.9,
    });
  });

  // ── Phase 2: Batch provision in groups of BATCH_SIZE ────────────────────
  group("Batch: Provision Groups", function () {
    for (let offset = 0; offset < rangeIds.length; offset += BATCH_SIZE) {
      const batch = rangeIds.slice(offset, offset + BATCH_SIZE);

      const bStart = Date.now();
      const res = http.post(
        `${BASE_URL}/ranges/batch-provision`,
        JSON.stringify({ range_ids: batch }),
        { headers: h }
      );
      batchProvisionDuration.add(Date.now() - bStart);

      const ok = check(res, {
        "batch provision accepted (2xx)": (r) => r.status >= 200 && r.status < 300,
      });

      if (ok) {
        provisionCounter.add(batch.length);
      }

      sleep(0.5);
    }
  });

  // ── Phase 3: Monitor queue via stats ────────────────────────────────────
  group("Batch: Monitor Stats", function () {
    for (let i = 0; i < 5; i++) {
      const statsRes = http.get(`${BASE_URL}/ranges/stats`, { headers: h });
      checkStatus(statsRes, 200);

      try {
        const stats = statsRes.json();
        check(null, {
          "stats has provisioning data": () =>
            stats.hasOwnProperty("provisioning") ||
            stats.hasOwnProperty("queued") ||
            stats.hasOwnProperty("total"),
        });
      } catch (_) {}

      sleep(2);
    }
  });

  sleep(1);
}