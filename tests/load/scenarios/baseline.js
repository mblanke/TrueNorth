// scenarios/baseline.js — Baseline regression check: light constant load, strict thresholds.
// Mostly reads, and one range created and deleted per iteration (no provisioning).
//
// Usage: k6 run -e BASE_URL=http://localhost:8081 tests/load/scenarios/baseline.js

import { sleep } from "k6";
import { rangeCreationDuration, thresholds } from "../config.js";
import { del, get, json, post } from "../helpers/api.js";
import { rangeBody, templateBody } from "../helpers/data.js";
import { health } from "../helpers/flows.js";

export const options = {
  scenarios: {
    baseline: { executor: "constant-vus", vus: 10, duration: "1m" },
  },
  thresholds: thresholds({
    http_req_duration: ["p(50)<200", "p(95)<400", "p(99)<800"],
    http_req_failed: ["rate<0.005"],
    checks: ["rate>0.99"],
  }),
  tags: { testType: "baseline" },
};

const LISTS = ["/ranges?limit=20", "/templates?limit=20", "/exercises?limit=20", "/scenarios?limit=20"];

export function setup() {
  const tpl = json(post("/templates", templateBody()));
  if (!tpl) throw new Error("setup: could not create a template");
  return { templateId: tpl.id };
}

export default function (fx) {
  health();
  get(LISTS[Math.floor(Math.random() * LISTS.length)]);

  const created = post("/ranges", rangeBody(fx.templateId));
  rangeCreationDuration.add(created.timings.duration);
  const rng = json(created);
  if (rng && rng.id) del(`/ranges/${rng.id}`, { name: "DELETE /ranges/{id}" });

  sleep(0.5);
}
