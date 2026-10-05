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
