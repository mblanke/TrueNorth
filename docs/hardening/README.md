# Hardening (codereview1): where everything is

The overnight run of 2026-10-04/05 worked through the codereview1 plan as a set of PRs on
mblanke/TrueNorth. **Nothing is merged.** Results: `s8-rescore-2026-10-04.md` (3 of 6
areas at 8/10 or above, each with blockers and what closes them).

## PRs, in merge order

Merge top to bottom. When a PR's base merges, retarget it to `main`; CI only runs on PRs
into `main`. **#24 is a draft that must never be merged**: it exists so CI runs on
everything combined (green at `78711f4`).

| # | What | Base |
|---|---|---|
| 12 | ruff format of 45 files; DoD and CI enforce it; ADR 0004 | main |
| 13 | Security catch-up: LTI registration fallback, JWKS rotation, non-root images | main |
| 8 | S0: source manifest and baseline | main |
| 21 | S5b: scoring/xAPI branch; worker SQL on a table mirror (35 → 0); 7 worker schema bugs fixed | main |
| 9 | Course Studio onto main (arc2 tools + Studio) | main |
| 10 | S1a: Studio runs are tenant-owned | #9 |
| 11 | S2a: runner deadline holds whatever the engine does | #9 |
| 15 | S1b: OS sandbox per Studio job (macOS Seatbelt); merges #11 in | #10 |
| 16 | S2b: one runner per runs root; restart recovery | #15 |
| 25 | S1b Linux: bubblewrap backend | #16 |
| 26 | Jobs reach the internet only via an allow-listing proxy | #25 |
| 14 | S2c: wiki edits/restores compare-and-set (wiki + tickets onto main) | main |
| 17 | S3a: range operations, durable, idempotent, serialised | #14 |
| 18 | S3b: worker fencing; lost tasks visible (`no_outcome`, abandon) | #17 |
| 19 | S3c: range UI shows what an operation is doing | #18 |
| 20 | S4a: collision-free network reservations | #19 |
| 29 | S4b: noise engine committed; its management addresses are reservations, not template order (sibling of #22) | #20 |
| 22 | S6: populated-schema upgrade test; CI runs the Postgres tests | #20 |
| 23 | S7: interruption exercise and browser journeys (evidence) | #22 |
| 27 | S8: re-score; rollback rehearsal | #23 |
| 30 | S3d: stop/start power the range's VMs (the `/stop` defect) | #27 |
| 31 | S3a evidence: black-box before-run for "dispatch ignored" | #30 |
| 28 | Fix: scenario exercises carry their objectives (they scored 0/0), found by S7 | main |

Merge conflicts to expect, all resolved already on #24, which shows how to resolve them:
- **#21 vs #18, `worker/tasks.py`:** re-apply the fence on `db_ops.update_range_state`, which already returns the row count. S5b's real-DB tests start from `provisioning`/`destroying`.
- **#13 vs #21, worker `Dockerfile`:** keep both the non-root user and the `scenario_engine` build context.
- **#12 against everything:** re-run `ruff format control-plane/ scenario-engine/ tools/ ai-orchestrator/`. A formatting-only rise in a capped line count follows ADR 0004.
- **#29 vs #28, `routers/exercises.py` import:** keep both, `from .. import range_ops, scenario_objectives`.
- **#29 vs #30, Alembic:** both migrations follow `c0d1e2f3a4b5`; whichever merges second sets its `down_revision` to the other (#24 chains `d1e2f3a4b5c6` after `a9b0c1d2e3f4`).
- **#30 vs #21, worker:** `worker/range_rows.py` reads through `db_ops.range_output_and_backend`; regenerate `tables.py` (`scripts/export_worker_tables.py`) for the new range states.
- **Generated files (`openapi.json`, `schema.d.ts`):** regenerate, never hand-merge: `.venv/bin/python scripts/export_openapi.py`, then `npm run gen:api`.

## What needs you

1. **Runner account (#15).** Create a standard macOS user `arc2runner`, run `claude setup-token` in it, and save the token to `~/.arc2/oauth-token` (mode 600). Steps are in `docs/arc2-course-studio.md`.
2. **Noise engine (#29).** Committed from `.claude/worktrees/network-traffic-noise-tool-8407d2` unchanged; that worktree is untouched and can be cleaned up once #29 is merged. Its Ansible role and vSphere management NIC have never run against a real vCenter.
3. **vSphere (S5a).** Integrating the vmware branch onto reservations and operations needs lab access for evidence; a follow-up task chip is waiting.
4. **Review.** S8 is a self-assessment by the author of most of these changes; a second reviewer should confirm it.

## Evidence files

- `s0-baseline-2026-10-04.md`: branches, worktrees, toolchain, baseline gate.
- `s7-interruption-exercise-2026-10-04.{md,json}`: broker down, worker killed, failing provisioner, ranges and Studio browser journeys.
- `rollback-rehearsal-2026-10-05.md`: main's code on the candidate-upgraded database.
- `s8-rescore-2026-10-04.md`: the scores.
