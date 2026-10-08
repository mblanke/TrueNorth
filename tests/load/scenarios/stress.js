// scenarios/stress.js — Stress test: 1,200 users, 15 minutes, at the designed capacity.
// Range lifecycles, paged lists, stats, and bursts of event sockets.

import { group, sleep } from "k6";
import { stressThresholds, thresholds } from "../config.js";
import { get } from "../helpers/api.js";
import { fixtures, rangeLifecycle, stats, tenantId } from "../helpers/flows.js";
import { wsSession } from "../helpers/ws.js";

export const options = {
  scenarios: {
    stress_ramp: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "2m", target: 300 },
        { duration: "2m", target: 600 },
        { duration: "3m", target: 1200 },
        { duration: "5m", target: 1200 }, // hold at peak
        { duration: "3m", target: 0 }, // drain
      ],
      gracefulRampDown: "60s",
    },
  },
  thresholds: thresholds(stressThresholds),
  tags: { testType: "stress" },
};

export function setup() {
  return Object.assign(fixtures(), { tenantId: tenantId() });
}

export default function (fx) {
  const roll = Math.random();
  if (roll < 0.35) {
    rangeLifecycle(fx.templateId);
  } else if (roll < 0.6) {
    group("paged lists", () => {
      for (let page = 0; page < 5; page++) get(`/ranges?limit=50&offset=${page * 50}`);
    });
  } else if (roll < 0.8) {
    for (let i = 0; i < 5; i++) {
      stats();
      sleep(0.2);
    }
  } else {
    wsSession(`tenant.${fx.tenantId}`, 5000, 1000);
  }
  sleep(Math.random() * 1.5 + 0.3);
}
