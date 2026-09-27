---
name: tn-epic-planning
description: "Turn a substantial TrueNorth feature or redesign into sequenced, testable delivery slices; use before cross-layer implementation."
---

# Epic planning

Work from the repository root. Apply this workflow only to the requested scope.
Read the applicable repository instructions and relevant source before editing.

## Start here

- `control-plane/web/src/app/app.routes.ts`
- `control-plane/api/app/routers`
- `control-plane/worker/worker/tasks.py`
- `docs/current-state.md`

## Workflow

1. Translate the request into observable outcomes for learners, instructors, and administrators where relevant. Record the user's accepted choices and avoid reopening them.
2. Trace one complete journey through UI, API, job, persistence, and outcome. Use current source for behavior; dated status documents are leads, not proof.
3. Identify dependencies and define slices that produce usable behavior. For each slice name affected contracts, likely files, acceptance evidence, migration needs, and recovery approach.
4. Keep proposed design separate from implemented behavior. Work through authorized implementation slices when requested; a plan is not completion of a build request.
5. Use one owner for shared routes and schemas. Delegate only when authorized and useful; give each participant non-overlapping ownership, input artifacts, and a return contract.

## Verification

Validate the dependency order and whether every promised user outcome has an observable acceptance check. Identify unavailable environments without inventing pass results.
For implementation, follow the repository DoD and report any blocked checks honestly.

## Return

A prioritized slice plan with acceptance criteria, dependencies, file ownership, and the next implementable slice.

## Boundary

Planning does not authorize deployment, infrastructure provisioning, or changes outside the user's requested scope.

