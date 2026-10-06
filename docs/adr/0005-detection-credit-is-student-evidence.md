# ADR 0005: Detection credit comes from what the Student did

- Status: accepted 2026-10-06 (credit model, instructor-only ack, exercise stays live for its duration)
- Date: 2026-10-06
- Builds on: PR #6 (`worker/detection.py`, shipped behind `DETECTION_SCORING=off`), PR #52
  (telemetry ingest needs `telemetry:write`, which Students do not hold)

## Context

PR #6 scores a detection objective as achieved when the scenario's Lucene query
(`objectives[].params.query`) matches at least `min_hits` events in `range-<range_id>`.
Those queries describe **the attack** (`ParentImage:*WINWORD.EXE AND Image:*powershell.exe`),
so the inject's own telemetry satisfies them. A Student who does nothing scores full marks.
Nothing bounds the search in time either, and forge reuses the tenant's first range, so an
earlier exercise's telemetry also counts.

Checked against current code on 2026-10-06, the scoring problem is wider than that:

- **Students can award themselves objectives today.** `POST /exercises/{id}/objectives/{ref}/ack`
  needs `exercise:complete`, which the Student role holds (`rbac.py`). The evidence string
  comes from the request body. That endpoint also looks the objective up without `get_owned`,
  so it is cross-tenant as well. This applies on main and on PR #6.
- **The answer key is served to Students.** `GET /exercises/{id}/scenario-detail` returns the
  full timeline and each objective's `evidence`, and that evidence includes the query once
  the objective is scored.
- **The ingest time is whatever the sender says.** `POST /telemetry/{range}/events` keeps a
  client-supplied `@timestamp`. A time window on `@timestamp` therefore relies on whatever
  every sender reports.
- **A real-backend run ends when its timeline ends.** `run_scenario_v2` sleeps through the
  timeline (injects are not dispatched yet) and then calls `complete_exercise`, so a Student
  gets seconds, not the scenario's `duration_minutes`.
- Exercises have no Student owner. `total_score` belongs to the exercise, which is the
  team's score.

## Decision

### 1. The scenario query is the answer key, not the grade

`objectives[].params.query` stays in the content but changes meaning: it is the
**ground truth**, the events that make up the attack for this objective. On its own it
never achieves anything.

### 2. A detection is achieved when a Student's submitted detection finds the attack

A Student submits a detection for an objective during the exercise:
`POST /exercises/{id}/objectives/{ref}/detections` with `{query}`, written in Lucene or as a
Sigma rule from the detection-rule editor and compiled to Lucene. The server evaluates it
with service credentials against `range-<range_id>`, restricted to the **exercise window**
(see 3):

- `GT` = events matched by the ground-truth query in the window
- `S` = events matched by the Student's query in the window
- **Achieved** when `|S ∩ GT| >= threshold` (`min_hits`, at least 1) and the precision
  `|S ∩ GT| / |S| >= min_precision` (default 0.5, settable per objective).

The precision floor is what stops `*` or `Image:*` from passing. The intersection is
computed by OpenSearch: `S` AND `GT` as a single `bool` query, compared with the count for `S`.

Every attempt is stored, whether it passes or fails, in a new table `detection_submissions`
(exercise, objective ref, Student user id, query, submitted_at, the counts, matched event
ids, verdict). Only the API writes to this table, and it writes only from the authenticated
request plus its own evaluation. Students cannot insert, update or backdate rows. On an
achieved objective, `objectives.evidence` points at the submission
(`{"source":"student_detection","submission_id":…,"student":…}`). The points go to the
exercise, as they do today, and the evidence names the Student.

Attempts per objective are capped (default 5) so the query cannot be brute-forced. The
instructor can see every attempt.

### 3. The exercise window is server time

- Ingest (API `POST /telemetry/...` and worker `ingest_telemetry_batch`) stamps
  `event.ingested` with the server's own clock and overwrites any value the sender sent.
  `@timestamp` stays the sensor's claim, for display only.
- The window is `exercise.started_at <= event.ingested <= (completed_at or now)`.
  Telemetry from before this change has no `event.ingested`, so it is excluded, and so is
  telemetry from earlier exercises on a reused range.

### 4. Only instructors acknowledge objectives

`/ack` moves to a new permission, `objective:ack` (instructor, range admin, admin; not
Student). The lookup goes through `get_owned`. Manual objectives and instructor overrides
keep working; the evidence records the instructor.

### 5. Students do not see the answer key

`scenario-detail` and the scenario read endpoints strip `params` and the
`evidence.query` / `events` fields for callers without `scenario:write`, and Students see
the timeline only as the narrative fields.

### 6. A real-backend exercise stays live for its duration

On a non-mock backend, `run_scenario_v2` no longer completes the exercise when the timeline
ends. The exercise stays `running` until the instructor completes it or `duration_minutes`
passes, whichever comes first; a beat task completes overdue exercises. Submissions are
accepted only while it is `running`. The worker's in-loop `DetectionScorer.score()` goes
away; scoring happens when a submission arrives.

### 7. Turning it on

`DETECTION_SCORING` keeps gating the new endpoint until 1–6 are built and tested on
Postgres. After that the flag is removed, because there is nothing left to switch off:
without a Student submission, nothing is credited.

## Not in this decision (later ADRs)

- **Alert triage credit.** No alert pipeline exists yet; when one does, an alert the
  Student acknowledges with a correct disposition becomes a second evidence source,
  stored the same way.
- **Deliverable credit** (IOC list or report checked against the rendered scenario
  variables). `validate_deliverable` reads a local path today and is not safe to expose.
- **Per-Student scores** within a team exercise (needs an exercise–Student relation).

## Consequences

- Detection objectives measure the Student. An idle Student scores zero on every
  detection objective.
- Content authors keep writing the same queries, but they must also hold up as ground
  truth (specific enough that the precision floor means something) and be fully rendered
  (no `{{ }}`; see the scenario `variables:` block).
- New table and migration (`detection_submissions`), a new permission, an ingest change and
  an OpenAPI change (new endpoint, `scenario-detail` shape for Students). New code goes in a
  `detection` section module, not `models.py` / `schemas.py` / `tasks.py` (ADR 0003).
- Mock runs are unchanged: they still auto-achieve.
