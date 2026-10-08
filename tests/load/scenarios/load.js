// scenarios/load.js — Standard load test: 200 users, 10 minutes.
// Mixed workload: 40% reads, 30% range lifecycle, 20% exercise lifecycle, 10% health/stats.

import { sleep } from "k6";
import { defaultThresholds, thresholds } from "../config.js";
import { browse, exerciseLifecycle, fixtures, health, rangeLifecycle, stats } from "../helpers/flows.js";

export const options = {
  scenarios: {
    load_test: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m", target: 50 }, // ramp up
        { duration: "3m", target: 200 }, // ramp to peak
        { duration: "3m", target: 200 }, // hold peak
        { duration: "2m", target: 0 }, // ramp down
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: thresholds(defaultThresholds),
  tags: { testType: "load" },
};

export function setup() {
  return fixtures();
}

export default function (fx) {
  const roll = Math.random();
  if (roll < 0.4) {
    browse();
  } else if (roll < 0.7) {
    rangeLifecycle(fx.templateId);
  } else if (roll < 0.9) {
    exerciseLifecycle(fx);
  } else {
    health();
    stats();
  }
  sleep(Math.random() * 2 + 0.5); // 0.5–2.5 s think time
}
