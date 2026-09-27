---
name: tn-change-review
description: "Review a TrueNorth patch for concrete regressions and unmet acceptance criteria; use before merging or when an independent review is requested."
---

# Independent change review

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `tests/api`
- `tests/worker`
- `control-plane/api/app/tenancy.py`
- `scripts/dod.sh`

## Workflow

1. Read the requested outcome, diff, surrounding code, and direct callers. Review the final implementation, not only its author's explanation.
2. Trace realistic failure scenarios: unauthorized access, silent dispatch loss, stale retries, empty evidence, broken redirects, and misleading UI state where relevant.
3. Distinguish a demonstrated defect, an untested risk, and a style preference. Rank by user impact and provide precise file locations.
4. Check that test assertions would fail on the old bug and are not merely mirroring implementation. Do not infer behavior from test names.
5. Verify that claims about deployment, real backends, or performance have matching evidence.
6. Do not manufacture findings to fill a quota; explicitly state when no blocking issue was found and what was not exercised.

## Verification

Reproduce findings with bounded local checks when feasible. Preserve the patch while reviewing unless edits are explicitly requested.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Prioritized actionable findings with trigger, consequence, location, and suggested correction, followed by verification limits.

## Boundary

Reviewing a change does not authorize applying it, merging, or deploying.

