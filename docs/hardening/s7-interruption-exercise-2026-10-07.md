# S7 rerun on main (2026-10-07)

The 2026-10-04 exercise ([s7-interruption-exercise-2026-10-04.md](s7-interruption-exercise-2026-10-04.md))
was taken on the pre-R-series candidate. This is the same script,
`scripts/exercise_range_interruptions.py`, run on main at `c4da290` (range operations in
`app/range_ops/`, worker fencing and range leases in `worker/fencing.py`, power tasks in
`worker/power_tasks.py`).

Components: the API in process (lifespan, re-send loop at 2 s); a real Celery worker
(`--pool=solo`, mock provisioner); a throwaway `redis:7-alpine` container on
127.0.0.1:6390; a scratch database on a local, throwaway PostgreSQL 16 (not the dev
stack's), migrated with Alembic. The script removed its container and database. Raw log:
`s7-interruption-exercise-2026-10-07.json`.

| # | Interruption | What happened | As designed? |
|---|---|---|---|
| 1 | none (journey) | provision → `ready` in 2.7 s; stop → `stopped`; start → `running`; stop → `stopped`; destroy → `destroyed`; every operation `succeeded` | yes |
| 2 | broker down | provision **202**, operation `pending` + `broker_unavailable`, range `provisioning`; Redis back → the API's loop sent it (attempt 2), `ready` 12.9 s later | yes |
| 3 | worker killed (SIGKILL) mid-provision | `no_outcome` past the stale threshold; a conflicting destroy **409**; abandon → operation `failed`, range `failed`; **a new provision is refused 409** "a worker is still acting on this range; try again later" | yes, but see below |
| 4 | provisioner fails every attempt | `provisioning` through the retries; the last attempt marks it `failed` (`range_failed: Simulated provision failure`); a healthy worker then provisions it to `ready` | yes |

## What changed since 2026-10-04: scenario 3

On the candidate a new provision (generation 2) followed the abandon at once. On main it
is refused, because the killed task still holds the range's **lease** (`range_leases`,
`worker/fencing.py`, `LEASE_SECONDS = 3600`; added after the 2026-10-05 independent review
to stop a range being built twice). `range_ops.service._check` refuses every action except
destroy while a lease is live, since an abandoned task might in fact still be running.

So after a worker is killed, the operator's options are: wait for the lease to expire (up
to an hour), or destroy the range (accepted; the worker's destroy waits for the lease).
This is the designed trade-off (never two builds of one range), but it is an operability
cost: there is no way to release the lease of a worker known to be dead.
`docs/operations.md` should say so; a follow-up could add an operator "release lease"
action that checks the holder's liveness.

## Also verified on main (same day, same local PostgreSQL)

- `tests/api/test_migration_populated_upgrade.py`: a populated database at `f7a8b9c0d1e2`
  upgrades through the 22 newer migrations without loss, the new code works on old rows,
  and the newer migrations downgrade and re-apply.
- `tests/api/test_dispatch_not_lost_blackbox.py`: provision, destroy, stop and start
  accepted with the broker down all reach it when it is back.
- `scripts/rehearse_rollback.py --old 1a91dda --new c4da290`: 5/5 checks
  (`evidence/rollback-rehearsal-c4da290.jsonl`).
- `scripts/lab/candidate_smoke.py --apply --mock --concurrent`: the lab smoke script runs
  end to end against the current API with the mock provisioner (no vCenter; this is not
  lab evidence).
