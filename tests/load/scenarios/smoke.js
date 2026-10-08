// scenarios/smoke.js — Smoke test: 10 users, 1 minute. Every journey once per iteration:
// the lists, a range provisioned and torn down, an exercise run to its AAR.

import { sleep } from "k6";
import { defaultThresholds, thresholds } from "../config.js";
import { browse, exerciseLifecycle, fixtures, health, rangeLifecycle, stats } from "../helpers/flows.js";

export const options = {
  vus: 10,
  duration: "1m",
  thresholds: thresholds(defaultThresholds),
  tags: { testType: "smoke" },
};

export function setup() {
  return fixtures();
}

export default function (fx) {
  health();
  browse();
  stats();
  rangeLifecycle(fx.templateId);
  exerciseLifecycle(fx);
  sleep(1);
}
