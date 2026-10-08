// scenarios/batch-provision.js — Batch provisioning: 10 VUs each provision a batch of 50
// ranges (POST /ranges/batch-provision), 500 provisions in all, and tear them down.
// On the itest stack the provisioner is the mock.

import { sleep } from "k6";
import { SMOKE, stressThresholds, thresholds } from "../config.js";
import { get, json, post } from "../helpers/api.js";
import { templateBody } from "../helpers/data.js";
import { batchProvision, stats } from "../helpers/flows.js";

// Under PROFILE=smoke a batch of 3: the API allows a client 30 range writes and 5
// batches a minute, and the smoke run uses a third of that.
const BATCH_SIZE = Number(__ENV.BATCH_SIZE || (SMOKE ? 3 : 50));

export const options = {
  scenarios: {
    batch_provision: { executor: "per-vu-iterations", vus: 10, iterations: 1, maxDuration: "10m" },
  },
  thresholds: thresholds(
    Object.assign({}, stressThresholds, {
      batch_provision_duration: ["p(95)<10000"], // a batch can take longer
      provisions_enqueued: ["count>=500"], // 10 VUs x 50
    }),
    { batch_provision_duration: ["p(95)<2000"], provisions_enqueued: ["count>=1"] },
  ),
  tags: { testType: "batch-provision" },
};

export function setup() {
  const tpl = json(post("/templates", templateBody()));
  if (!tpl) throw new Error("setup: could not create a template");
  return { templateId: tpl.id };
}

export default function (fx) {
  batchProvision(fx.templateId, BATCH_SIZE);
  stats();
  get("/ranges?limit=50");
  sleep(1);
}
