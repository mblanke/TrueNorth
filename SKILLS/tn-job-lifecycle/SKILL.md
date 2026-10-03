---
name: tn-job-lifecycle
description: "Repair Celery, provisioning, snapshot, and exercise state transitions; use for stuck jobs, duplicate effects, retries, or premature completion."
---

# Reliable background operations

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/api/app/celery_client.py`
- `control-plane/worker/worker/tasks.py`
- `control-plane/worker/worker/provisioners/base.py`
- `control-plane/worker/worker/provisioners/results.py`
- `tests/worker`

## Workflow

1. Draw the legal states and identify which process owns each transition. Trace API commit, broker dispatch, worker execution, backend result, and notification.
2. Confirm every backend method exists and its result matches the worker contract. Treat unsupported capabilities explicitly instead of exposing doomed actions.
3. Distinguish dispatch accepted, execution started, and operation completed. Propagate dispatch failures or persist recoverable pending work.
4. Account for late acknowledgements, retries, process crashes, and duplicate deliveries. Use operation IDs or equivalent deduplication and conditional state updates.
5. Sequence scenario execution after range readiness. Cancellation, pause, destroy, and stale retry paths must not overwrite newer terminal states.
6. Validate backend result status and preserve snapshot identifiers needed for restore. Report partial failure without inventing success.

## Verification

Use fake backends and broker failures to cover timeout, duplicate delivery, stale operation, partial result, and recovery. Real backend validation remains a separately identified requirement.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

An operation/state contract, implementation, and failure-path tests that prove transitions and side effects.

## Boundary

Local tests are authorized by a code task; live VM creation, restore, deletion, or deployment requires task-specific authorization.

