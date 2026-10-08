// helpers/flows.js — The user journeys the scenarios mix: what the UI does, through the
// published API. Each checks what it gets back; none invents data it did not receive.
import { check, group, sleep } from "k6";
import {
  batchProvisionDuration,
  exerciseCompleteDuration,
  provisionCounter,
  provisionDuration,
  rangeCreationDuration,
  rangeReadyDuration,
} from "../config.js";
import { del, get, json, post } from "./api.js";
import { exerciseBody, rangeBody, scenarioBody, templateBody } from "./data.js";

const READY_TIMEOUT_S = Number(__ENV.READY_TIMEOUT_S || 60);

/**
 * For setup(): a template and a scenario, and one range to run exercises on (a range
 * with exercises on record is kept for their history, so they share this one).
 */
export function fixtures() {
  const tpl = json(post("/templates", templateBody()));
  const scn = json(post("/scenarios", scenarioBody()));
  const rng = tpl && json(post("/ranges", rangeBody(tpl.id)));
  if (!tpl || !scn || !rng) throw new Error("setup: could not create the template, scenario and range");
  return { templateId: tpl.id, scenarioId: scn.id, exerciseRangeId: rng.id };
}

/** For setup(): the signed-in user's tenant (the dev admin's, on an AUTH_DISABLED stack). */
export function tenantId() {
  const me = json(get("/auth/me"));
  const id = me && me.user && me.user.tenant_id;
  if (!id) throw new Error("setup: GET /auth/me returned no tenant");
  return id;
}

export function health() {
  const res = get("/health");
  check(res, { "health status ok": (r) => (json(r) || {}).status === "ok" });
}

/** The lists the dashboard opens, and one range's detail. */
export function browse() {
  group("browse", () => {
    const ranges = json(get("/ranges?limit=20")) || [];
    check(ranges, { "ranges is a list": (r) => Array.isArray(r) });
    if (ranges.length) get(`/ranges/${ranges[Math.floor(Math.random() * ranges.length)].id}`, { name: "GET /ranges/{id}" });
    get("/templates?limit=20");
    get("/scenarios?limit=20");
    get("/exercises?limit=20");
  });
}

export function stats() {
  const res = get("/ranges/stats");
  check(res, { "stats has total_ranges": (r) => typeof (json(r) || {}).total_ranges === "number" });
}

/** Poll a range until it is `wanted` (or failed, or the timeout). Returns whether it got there. */
export function waitForState(rangeId, wanted) {
  const deadline = Date.now() + READY_TIMEOUT_S * 1000;
  let state = null;
  while (Date.now() < deadline) {
    state = (json(get(`/ranges/${rangeId}`, { name: "GET /ranges/{id}" })) || {}).state;
    if (state === wanted || state === "failed") break;
    sleep(1);
  }
  return check(state, { [`range reaches ${wanted}`]: (s) => s === wanted });
}

/** Create a range, provision it (mock provisioner on the itest stack), tear it down, delete it. */
export function rangeLifecycle(templateId) {
  group("range lifecycle", () => {
    const created = post("/ranges", rangeBody(templateId));
    rangeCreationDuration.add(created.timings.duration);
    const rng = json(created);
    if (!rng || !rng.id) return;

    const prov = post(`/ranges/${rng.id}/provision`, null, { expect: [202], name: "POST /ranges/{id}/provision" });
    provisionDuration.add(prov.timings.duration);
    if (prov.status !== 202) return;
    provisionCounter.add(1);
    const accepted = Date.now();
    if (!waitForState(rng.id, "ready")) return;
    rangeReadyDuration.add(Date.now() - accepted);

    const destroy = post(`/ranges/${rng.id}/destroy`, null, { expect: [202], name: "POST /ranges/{id}/destroy" });
    if (destroy.status === 202 && waitForState(rng.id, "destroyed")) {
      del(`/ranges/${rng.id}`, { name: "DELETE /ranges/{id}" });
    }
  });
}

/** An exercise on the shared range: create, start, complete, generate its AAR. */
export function exerciseLifecycle(fx) {
  group("exercise lifecycle", () => {
    const ex = json(post("/exercises", exerciseBody(fx.exerciseRangeId, fx.scenarioId)));
    if (!ex || !ex.id) return;
    const started = post(`/exercises/${ex.id}/start`, null, { expect: [200], name: "POST /exercises/{id}/start" });
    check(started, { "exercise running": (r) => (json(r) || {}).state === "running" });
    const done = post(`/exercises/${ex.id}/complete`, null, { expect: [200], name: "POST /exercises/{id}/complete" });
    exerciseCompleteDuration.add(done.timings.duration);
    check(done, { "exercise completed": (r) => (json(r) || {}).state === "completed" });
    const aar = post(`/exercises/${ex.id}/aar/generate`, null, { name: "POST /exercises/{id}/aar/generate" });
    check(aar, { "aar is for this exercise": (r) => (json(r) || {}).exercise_id === ex.id });
  });
}

/** Create `n` ranges, provision them in one batch, wait for all, tear them down. */
export function batchProvision(templateId, n) {
  group("batch provision", () => {
    const ids = [];
    for (let i = 0; i < n; i++) {
      const created = post("/ranges", rangeBody(templateId));
      rangeCreationDuration.add(created.timings.duration);
      const rng = json(created);
      if (rng && rng.id) ids.push(rng.id);
    }
    if (!ids.length) return;
    const res = post("/ranges/batch-provision", { range_ids: ids }, { expect: [202] });
    batchProvisionDuration.add(res.timings.duration);
    const out = json(res) || {};
    if (!check(out, { "batch dispatched every range": (o) => o.dispatched === ids.length })) return;
    provisionCounter.add(ids.length);
    const ready = ids.filter((id) => waitForState(id, "ready"));
    for (const id of ready) post(`/ranges/${id}/destroy`, null, { expect: [202], name: "POST /ranges/{id}/destroy" });
    for (const id of ready) if (waitForState(id, "destroyed")) del(`/ranges/${id}`, { name: "DELETE /ranges/{id}" });
  });
}
