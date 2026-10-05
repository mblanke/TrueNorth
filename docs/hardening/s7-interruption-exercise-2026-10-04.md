# S7: range interruption exercise (2026-10-04)

Run against `hardening/integration-candidate` at `1a1c00d` (main + PRs #8–#22 merged),
with `scripts/exercise_range_interruptions.py`. Components:
- the API in process, with its lifespan and the operation redispatch loop at 2 s;
- a real Celery worker as a separate process (mock provisioner);
- a throwaway `redis:7-alpine` container on 127.0.0.1:6390;
- a scratch database on the dev-stack PostgreSQL 16, migrated with Alembic (no `create_all`).

The dev stack's broker, workers and database were not touched. The script removes its
container and database when it finishes, and none were left behind after this run. The
raw log is `s7-interruption-exercise-2026-10-04.json` (states, operation statuses and
timings; no credentials).

| # | Interruption | What happened | As designed? |
|---|---|---|---|
| 1 | none (journey) | provision 202 → `dispatched` → range `ready` in 2.7 s, operation `succeeded`; destroy 202 → `destroyed`, `succeeded` | yes |
| 2 | broker down (Redis stopped) | provision still **202**, operation `pending` + `broker_unavailable`; 4 s later still pending, range `provisioning` (honest, not lost); Redis back → the API's own loop sent it (attempt 2) and the range was `ready` 13 s later | yes |
| 3 | worker killed (SIGKILL) mid-provision | range `provisioning`, operation `dispatched`; past the stale threshold it reports `no_outcome` and still blocks (a conflicting destroy got **409**); an operator abandoned it → operation `failed`, range `failed`; a new provision (generation 2) reached `ready` | yes |
| 4 | partial provisioning (provisioner fails every attempt) | range stays `provisioning` through the worker's retries (not stamped `failed` on the first attempt); the last attempt marks it `failed` with the provisioner's message (`range_failed: Simulated provision failure`); a healthy worker then provisioned it to `ready` | yes |

Notes:
- **Scenario 3 used abandon, not redelivery.** Celery redelivers an unacknowledged task
  only after the broker's visibility timeout (3600 s in `worker/celery_app.py`), so the
  exercise took the designed recovery path: `no_outcome`, then abandon. If the task had
  been redelivered later, `worker/fencing.py` skips it, because the range is no longer
  `provisioning`.
- **Scenarios not exercised here:** broker loss while a task is running (it comes back
  via redelivery and fencing, covered by unit tests) and a real hypervisor (S5a, lab).

## Browser journey (same day, later)

The candidate's web app (`ng serve` on :4300) ran against the candidate API (uvicorn on
:8092, scratch database `tn_s7_ui`, throwaway Redis on :6390, real Celery worker), driven
in a browser:

| Step | Seen in the browser | Result |
|---|---|---|
| New Range from a template, then Provision | row `created`, then `ready` about 8 s later with no reload (the 5 s polling) | pass |
| Redis stopped, then Provision | row **`provisioning` / "Queued: waiting for the task queue to come back"** (warning colour) | pass |
| Redis and worker back, page left alone | row `ready` about 10 s later | pass |
| Stats strip during the above | stuck at its first-load counts ("created 1, ready 1" next to two ready rows) | **bug, fixed in #19**: the strip now refreshes with the list; re-checked, "ready 3" with 3 ready rows |
| Destroy after a finished provision | **409 "A provision of this range is still in progress"** | **bug, fixed in #17**: production sessions do not autoflush, so `reconcile()` settled the provision only in memory. Regression test runs with autoflush off. Re-checked: "Destroying…", then `destroyed` |

Neither bug showed up in the unit or karma suites; both did in the browser.

## Course Studio journey (same night)

The candidate with Studio enabled (API :8092, web :4300) ran with the **real runner**:
Seatbelt sandbox, egress proxy, runner lock. The engine was a stand-in that behaves like
stage 1 (writes an outline, stops for review), because the runner account and its token
do not exist yet.

| Step | Seen | Result |
|---|---|---|
| New project, describe the course, Send | "Waiting for the runner"; the Pipeline panel says so | pass |
| Runner picks the job up | about 8 s later: "Outline ready: 2 modules. Accept or send feedback.", both modules on the Outline tab, "Accept Outline" offered | pass |
| Job record | `confinement: seatbelt`, `egress: ["api.anthropic.com"]`, the owner's `tenant_id`, `runner` identity, `history_commit` in the runner-owned `_history/<slug>.git`; the job's throwaway home removed | pass |

## Exercise and scoring journey (API, same night)

The candidate with a real Celery worker, a throwaway Redis and a scratch database ran:
range (mock) → scenario `scenario-engine/examples/apt-breach.yaml` → exercise → start
→ AAR.

| Step | Result |
|---|---|
| range provisioned | `ready` |
| scenario created, exercise created, started | 201, 201, 200 → `running`; the worker ran it and the exercise reached `completed` |
| objectives and score | **0 objectives, score 0/0**, although the scenario defines 4 (`detect-phish`, `detect-c2`, `detect-priv-esc`, `incident-report`) |
| AAR generate / read / html | 201 / 200 / 200, but the HTML is 74 bytes |

**Finding (already on main, not introduced by this work):** `Objective` rows are created
only for curriculum-driven exercises (`app/qsp_paths.py`). An exercise started from a
scenario gets none, so the worker's scoring (`run_scenario_v2`, `db_ops.achieve_objective`
by `ref_id`) has nothing to mark and the AAR is empty. The scoring journey is therefore
**not complete**. Follow-up: materialise the scenario's objectives when an exercise is
created (or started), keyed by `ref_id`.

**Fixed in #28** (`app/scenario_objectives.py`): the scenario's objectives are now created
with the exercise. Rerun of the same journey with a real worker: all four objectives
achieved, score **100/100**, and the AAR reports 100% (the HTML summary is intentionally
one line; `report_json` lists each objective).
