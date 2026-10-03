---
name: tn-scenario-evidence
description: "Connect scenario definitions, injectors, validators, and scoring evidence; use for executable training packages and assessment correctness."
---

# Scenario execution and evidence

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `scenario-engine/scenario_engine`
- `control-plane/api/app/routers/exercises.py`
- `control-plane/worker/worker/tasks.py`
- `tests/scenario_engine`
- `tests/scoring`
- `truenorth-content-pack/truenorth-content/schemas`

## Workflow

1. Trace the actual definition format from stored YAML through parsing, worker payload, injector registry, and validator. Content-pack YAML and runtime YAML may differ; define an explicit adapter.
2. Validate event IDs, timing semantics, required resources, and objective references. Do not silently synthesize successful execution for malformed definitions.
3. Distinguish simulation from real execution in results. Progress increments, sleeps, or a commented dispatch call do not prove an injector ran.
4. Tie assessment outcomes to evidence and rubric criteria. Test missing telemetry, benign noise, malformed evidence, and failed injects; absence of evidence must not become a pass.
5. Verify pause/cancel behavior and replay isolation when working on runtime control. Preserve previous attempt evidence.
6. Read the content pack's own instructions before editing its content. Keep source-owned assessment criteria traceable and required human review explicit.

## Verification

Demonstrate an objective that passes with valid evidence and fails or remains unassessed with missing/incorrect evidence. Label mocked versus real execution.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

Validated execution contract or content changes with an objective-to-evidence mapping and negative-path checks.

## Boundary

A platform implementation task does not authorize running offensive scenarios or provisioning a live range.

