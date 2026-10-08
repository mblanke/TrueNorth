// scenarios/ci-gate.js — Read-only gate: the lists every page opens, under 30 s.
// Usage: k6 run -e BASE_URL=http://localhost:8081 tests/load/scenarios/ci-gate.js

import { sleep } from "k6";
import { thresholds } from "../config.js";
import { get } from "../helpers/api.js";
import { health } from "../helpers/flows.js";

export const options = {
  stages: [
    { duration: "5s", target: 5 }, // ramp up
    { duration: "15s", target: 10 }, // steady
    { duration: "5s", target: 0 }, // ramp down
  ],
  thresholds: thresholds({
    http_req_duration: ["p(95)<400", "p(99)<1000"],
    http_req_failed: ["rate<0.01"],
    checks: ["rate>0.99"],
  }),
  tags: { testType: "ci-gate" },
};

export default function () {
  health();
  get("/ranges?limit=5");
  get("/templates?limit=5");
  get("/scenarios?limit=5");
  get("/threat-intel/feeds");
  get("/detection-rules?limit=5");
  sleep(0.5);
}
