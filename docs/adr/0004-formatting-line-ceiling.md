# ADR 0004 — A formatting-only change may raise a god-file line ceiling

- Status: accepted
- Date: 2026-10-04
- MOSA pillar: conformance

## Context
`ruff format --check` runs in CI (`lint-python`) but never ran in the Definition of Done,
so 45 files drifted and CI's lint job failed on every branch. Formatting them (commit
`1cb9163`) expands two calls in `control-plane/worker/worker/tasks.py` that end in a
trailing comma into one argument per line. That adds 6 lines to a file whose line count
the MOSA ratchet caps (ADR 0003). No code was added; the other two capped files got
shorter (`models.py` 1867 → 1863, `schemas.py` 1877 → 1875).

## Decision
Raise `lines_worker_tasks_py` from 1585 to 1591 for this formatting-only commit. Do not
compact unrelated code to stay under the old number, because that would hide the cause
in an unrelated diff. Add `ruff format --check` to `scripts/dod.sh` over the same paths as
CI, so formatting can no longer drift and this cannot recur.

## Consequences
- The line ceiling still ratchets down automatically, and new code in `tasks.py` still
  fails the gate.
- Later formatting-only commits that move a capped count need their own ADR. With the
  check now in the DoD, there should be none.
