---
name: tn-release-verification
description: "Make TrueNorth CI and release evidence trustworthy; use for failing gates, skipped integration checks, browser journeys, and release readiness."
---

# Release and regression verification

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `scripts/dod.sh`
- `.github/workflows/ci.yml`
- `tests/integration/conftest.py`
- `tests/e2e/specs/critical-journeys.spec.ts`
- `tests/load`

## Workflow

1. Identify the exact checkout and changed files. Check interpreter architecture, dependency availability, browser setup, and service prerequisites before interpreting failures.
2. Read scripts/dod.sh and CI together; compare their actual commands instead of assuming their comments establish parity.
3. Start required disposable services in CI and wait for readiness. Required integration checks must fail if services are missing rather than silently skip.
4. Test actions and persisted outcomes, not merely page headings. Cover the changed learner, instructor, or administrator journey plus a meaningful failure case.
5. Keep mock, real-service, browser, and real-provisioner evidence distinct. Do not repeat an old passing-test count as today's baseline.
6. Run targeted checks first, then the required DoD gate. If the environment prevents it, report the command and root cause without weakening the gate.

## Verification

Produce actual exit codes and concise results; establish that expected tests ran and that skips/xfails are accounted for.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

A release assessment or CI repair with passed/failed/blocked checks and exact remaining verification.

## Boundary

A green local gate does not authorize deployment. Do not edit markers or baselines to manufacture a pass.

