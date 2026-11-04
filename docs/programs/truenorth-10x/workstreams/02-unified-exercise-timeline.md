# Workstream 02 — unified exercise timeline

Status: proposed; begins after 00. Suggested branch: `claude/10x-02-exercise-timeline`.

## Outcome

Give instructors and reviewers one ordered, replayable record of planned injects, scenario
transitions, operator actions, telemetry evidence, scoring and adjudication for an exercise
run. Existing modules become producers and projections; they are not replaced wholesale.

## Contract PR

Define a versioned event envelope: event/run/tenant IDs, monotonic run sequence, event type and
schema version, occurred and ingested times, source/source-event ID, actor kind/ID, execution
mode, correlation/causation IDs, authorization/effect/idempotency references, evidence links,
payload handling marking and integrity fields. Define ordering when clocks disagree and how a
late event is displayed without rewriting history.

## First vertical slice

- Persist an append-only timeline for one existing `ExerciseRun`/scenario path.
- Adapt two sources only: scenario transition/inject plus deterministic scoring evidence.
- Provide cursor-based, tenant-scoped `GET` access with stable ordering and source filters.
- Add an instructor timeline panel that distinguishes planned, observed, calculated, judged
  and failed events and links to existing evidence.
- Add a deterministic replay test that rebuilds the same projection from stored events.

Likely areas: exercise templates/runs, scenario runner, MESL/injectors, scoring/evidence,
classes console, API schema/OpenAPI, a new timeline service/model and Angular exercise view.

## Acceptance

- Duplicate delivery is idempotent and concurrent events receive deterministic order.
- Foreign-tenant access and forged run/source IDs are refused.
- Late/out-of-order events remain visible and do not silently change an official decision.
- Replay from the event log matches the live projection.
- Producer outage and partial event failure are visible, retryable and do not mark the run
  complete early.

## Subsequent slices

Add white-cell/manual events, telemetry references, physical provider events, AAR projection,
signed export and retention/compaction policy. Do not store bulk telemetry in the event table;
store immutable references and hashes.

## Recovery

Dual-write behind a flag, compare old/new projections, then switch reads. Keep existing source
records authoritative until replay parity is proven. Disable consumers without deleting events.

## Skills

Primary: `tn-scenario-evidence`; add `tn-api-contracts`, `tn-job-lifecycle`,
`tn-telemetry-lms`, `tn-schema-migrations` and `tn-angular-delivery`. Shared envelope is owned
by the integrator and is a prerequisite for 04/05.
