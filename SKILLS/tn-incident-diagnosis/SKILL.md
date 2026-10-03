---
name: tn-incident-diagnosis
description: "Diagnose TrueNorth runtime faults across API, queues, services, and infrastructure; use for outages, degraded behavior, and inconsistent status."
---

# Evidence-based incident diagnosis

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `RESUME.md`
- `control-plane/api/app/health.py`
- `control-plane/api/app/tracing.py`
- `infra/platform/docker`
- `docs/operations.md`

## Workflow

1. Define the symptom, affected user action, time window, and expected behavior. Check direct service evidence before trusting a dashboard summary.
2. Identify the first failing hop across browser/proxy, API, database, broker, worker, and external backend. Correlate request or operation IDs.
3. Use dated runbooks as hypotheses. Verify current ports, container mappings, process configuration, and active model identities without printing credentials.
4. Separate missing observability from service failure and environment incompatibility from code defects.
5. Choose the smallest reversible correction supported by evidence. Preserve relevant logs and record how the hypothesis was tested.
6. Stop repeating the same ineffective operation; revise the diagnosis when new evidence contradicts it.

## Verification

Re-run the original user action and a direct health/readiness check; describe whether root cause is confirmed or still inferred.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Symptom-to-cause evidence, scoped remedy, verification, and remaining unknowns.

## Boundary

Start with read-only inspection. Restarting shared services or changing GPU fleet modes needs authorization for that operational action.

