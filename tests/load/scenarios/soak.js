// scenarios/soak.js — Soak / endurance test: 500 users, 2 hours. Watches for degradation
// (memory, connection pools) over a steady mixed workload, with a periodic batch provision.

import { sleep } from "k6";
import { SMOKE, defaultThresholds, thresholds } from "../config.js";
import { batchProvision, browse, exerciseLifecycle, fixtures, health, rangeLifecycle, stats } from "../helpers/flows.js";

export const options = {
  scenarios: {
    soak: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "5m", target: 500 }, // ramp up
        { duration: "1h50m", target: 500 }, // steady state
        { duration: "5m", target: 0 }, // drain
      ],
      gracefulRampDown: "60s",
    },
  },
  // Soak-specific: watch for degradation.
  thresholds: thresholds(Object.assign({}, defaultThresholds, { http_req_duration: ["p(95)<600", "p(99)<2000"] })),
  tags: { testType: "soak" },
};

// Every BATCH_EVERY-th iteration of a VU provisions a batch of ranges.
const BATCH_EVERY = SMOKE ? 5 : 100;
const BATCH_SIZE = SMOKE ? 2 : 50;

export function setup() {
  return fixtures();
}

export default function (fx) {
  if (__ITER > 0 && __ITER % BATCH_EVERY === 0) {
    batchProvision(fx.templateId, BATCH_SIZE);
    sleep(2);
    return;
  }
  const roll = Math.random();
  if (roll < 0.45) {
    browse();
    stats();
  } else if (roll < 0.75) {
    rangeLifecycle(fx.templateId);
  } else {
    exerciseLifecycle(fx);
    health();
  }
  sleep(Math.random() * 3 + 1); // 1–4 s think time
}
