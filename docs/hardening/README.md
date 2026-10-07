# Hardening (codereview1): evidence and tools

The overnight run of 2026-10-04/05 worked through the codereview1 plan as PRs #8–#40 on
an integration candidate (`hardening/integration-candidate`, draft #24). **None of those
PRs merged.** Their product code re-landed on main as the R-series (#55–#68) and later
PRs (#71–#87). On 2026-10-07 the still-useful tests, scripts and evidence were carved onto
main; this folder holds the evidence.

Files dated 2026-10-04/05 are **historical**: each opens with a note saying what it
describes. Files dated 2026-10-07 were produced on main.

## Evidence

| File | What | Recorded on |
|---|---|---|
| `s0-baseline-2026-10-04.md` | branches, worktrees, toolchain, baseline gate, S0 defect list | main `083aeb7` |
| `s7-interruption-exercise-2026-10-04.{md,json}` | broker down, worker killed, failing provisioner; ranges, Studio and scoring browser journeys | candidate `1a1c00d` |
| `s7-interruption-exercise-2026-10-07.{md,json}` | the same script on main, with a stop/start journey; what changed (range leases) | main `c4da290` |
| `rollback-rehearsal-2026-10-05.md` | code rollback on an upgraded, populated database; rerun on main 2026-10-07 | candidate `5270cdf`, `8d93ab1`; main `c4da290` |
| `s8-rescore-2026-10-04.md` | rubric scores of the candidate (author, then audited) | candidate |
| `handoff-2026-10-05.md` | S5a, lab run, runner account, deferred work, independent review | candidate `8d93ab1` |
| `evidence/` | raw logs: rollback rehearsals, Seatbelt confinement run; lab smoke output lands here | |

## Tests and scripts on main

| Path | What | Needs |
|---|---|---|
| `tests/api/test_migration_populated_upgrade.py` | a populated database at `f7a8b9c0d1e2` upgrades to head without loss, the new code runs on old rows, the newer migrations downgrade and re-apply | `TEST_POSTGRES_ADMIN_URL` (CI's test-python sets it) |
| `tests/api/test_dispatch_not_lost_blackbox.py` | provision/destroy/stop/start accepted with the broker down reach it when it is back (child-process API, HTTP only) | nothing |
| `scripts/exercise_range_interruptions.py` | S7 exercise: real Celery worker (mock provisioner), throwaway Redis, scratch database | Docker, `TEST_POSTGRES_ADMIN_URL` |
| `scripts/rehearse_rollback.py` | code rollback rehearsal between two refs | `TEST_POSTGRES_ADMIN_URL` |
| `scripts/lab/candidate_smoke.py` | runbook §5 smoke test on vCenter; `--apply --mock` rehearses it without a lab | **live lab** for real evidence: vCenter credentials, Docker, `TEST_POSTGRES_ADMIN_URL` |

None of these touches a dev stack or lab unless you point it there: each creates and drops
its own scratch database, and the scripts' Redis is a throwaway container on its own port.
`TEST_POSTGRES_ADMIN_URL` may name a local throwaway server; it must be reachable over
TCP (or a URL the Alembic config can take: a `?host=/socket/dir` query does not survive
`configparser` interpolation).

## Not carved

- `.git-blame-ignore-revs` and ADR 0004 "formatting line ceiling" (#12): the formatting
  commit `1cb9163` is not in main's history (main was never bulk-formatted; `ruff format
  --check` still reports 73 files), so there is no revision to ignore and the ADR's
  decision has nothing to apply to. Redo both if main is bulk-formatted.
