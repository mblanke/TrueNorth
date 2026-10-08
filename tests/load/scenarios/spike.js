// scenarios/spike.js — Spike test: normal load, a sudden 12x spike, then recovery.

import { sleep } from "k6";
import { defaultThresholds, thresholds } from "../config.js";
import { browse, fixtures, health, rangeLifecycle, stats } from "../helpers/flows.js";

export const options = {
  scenarios: {
    spike: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "30s", target: 100 }, // warm up to normal
        { duration: "1m30s", target: 100 }, // hold normal
        { duration: "30s", target: 1200 }, // SPIKE: 100 -> 1200 in 30 s
        { duration: "2m", target: 1200 }, // hold spike
        { duration: "30s", target: 100 }, // drop back to normal
        { duration: "2m", target: 100 }, // recovery period
        { duration: "30s", target: 0 }, // drain
      ],
      gracefulRampDown: "30s",
    },
  },
  // During the spike, slightly higher latencies are allowed.
  thresholds: thresholds(Object.assign({}, defaultThresholds, { http_req_duration: ["p(95)<1500", "p(99)<3000"] })),
  tags: { testType: "spike" },
};

export function setup() {
  return fixtures();
}

export default function (fx) {
  const roll = Math.random();
  if (roll < 0.3) {
    health();
    stats();
  } else if (roll < 0.65) {
    browse();
  } else {
    rangeLifecycle(fx.templateId);
  }
  sleep(Math.random() + 0.3);
}
