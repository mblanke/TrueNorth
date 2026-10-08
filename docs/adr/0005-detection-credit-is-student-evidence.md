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
`POST /exercises/{id}/objectives/{ref}/detections` with `{query}` in Lucene, under the new
permission `detection:submit` (Student, instructor, admin). Sigma rules from the
detection-rule editor follow once a Sigma-to-Lucene compiler is in the repo; the editor
only validates Sigma today. The server evaluates the query with service credentials
against `range-<range_id>`, restricted to the **exercise window** (see 3):

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

Attempts are capped per Student per objective (`max_attempts`, default 5) so the key cannot
be brute-forced. A store outage returns 503, is stored as `unscored`, and does not use an
attempt. A Student sees the verdict, how many events their query matched, and how many
attempts are left. Staff who may edit scenarios (`scenario:update`) see every attempt,
including how many matched events were the attack and the precision
(`GET /exercises/{id}/detections`). Built in `app/detections/` and
`app/routers/detections.py`.

### 3. The exercise window is server time

- Ingest stamps `truenorth.ingested_at` with the server's own clock and drops anything
  the sender put under `truenorth`. That covers the API's search backend
  (`OpenSearchBackend.ingest`) and the worker's `ingest_telemetry_batch`
  (`worker/telemetry.py`). It is not ECS `event.ingested` because many feeds send `event`
  as a string, and the two would clash in the mapping. `@timestamp` stays the sensor's
  claim, for display only.
- The window is `exercise.started_at <= truenorth.ingested_at <= (completed_at or now)`.
  Telemetry from before this change has no `truenorth.ingested_at`, so it is excluded, and
  so is telemetry from earlier exercises on a reused range.

### 4. Only instructors acknowledge objectives

`/ack` moves to a new permission, `objective:ack` (instructor, range admin, admin; not
Student). The lookup goes through `get_owned`. Manual objectives and instructor overrides
keep working; the evidence records the instructor.

### 5. Students do not see the answer key

For callers without `scenario:update` (Students, observers), `GET /scenarios/{id}` returns
the briefing YAML: no `variables:`, no objective `params`, and timeline entries cut to
their narrative fields (`t`, `phase`, `name`, `title`, `description`, `narrative`).
`scenario-detail` and `GET /exercises/{id}/objectives` do the same, and they drop
`query` / `events` / `index` / `threshold` from evidence (older scorer evidence carried
them). Built in `app/detections/redaction.py`. The AAR is left as it is: it is the debrief,
read after the exercise.

### 6. A real-backend exercise stays live for its duration

On a non-mock backend, `run_scenario_v2` no longer completes the exercise when the timeline
ends. The exercise stays `running` until the instructor completes it or `duration_minutes`
(or `duration_min`) passes, whichever comes first. The beat task `close_overdue_exercises`
(`worker/exercise_clock.py`, every minute) completes overdue exercises. It measures wall
time since start, so paused time counts. An exercise whose scenario sets no duration stays
running until an instructor completes it. Submissions are accepted only while it is
`running`. The worker's in-loop `DetectionScorer` is gone (`worker/detection.py` deleted);
scoring happens when a submission arrives. (Amended at the merge with the scenario engine,
2026-10-07: the run loop is `worker/exercise_run.py`, the overdue close runs in the API
(`app/exercise_completion.py`), and `scenario_engine` stays in the worker image because
inject dispatch uses its injectors, not for scoring.)

### 7. Turning it on

The `DETECTION_SCORING` flag is removed. There is nothing left to switch off: without a
Student submission, nothing is credited.

## Changes after the adversarial review (2026-10-06)

The review of the first build found four blockers. All are fixed in the same branch:

- **Self-award and forged telemetry.** Objective ack is instructor-only (PR #58), and
  telemetry ingest needs `telemetry:write` on a range in the caller's tenant (PR #52).
  Both PRs are merged into this branch.
- **The answer key in the briefing.** The briefing is now an allowlist of keys. The
  incident-response drill's own tasks named the indicators its objectives ask for; they now
  refer to "the … you identified".
- **Keys that never match on real OpenSearch.** `telemetry/pipelines/bootstrap.py`
  installs a `range-*` template. It maps strings to `keyword` with a `.text` sub-field, and
  sets `index.final_pipeline` to `truenorth-ingested-at`. That pipeline stamps
  `truenorth.ingested_at` (`date_nanos`) on every document from every writer, including
  Filebeat (`range-<id>-<date>`) and Logstash (now `range-<id>-<date>` when the event
  carries `range_id`). It also drops any `truenorth` field the sender sent, flat or nested.
  Credit searches `range-<id>,range-<id>-*`. `tests/api/test_detection_opensearch.py`
  checks every shipped key against a real OpenSearch with an event it should find. That
  test found `detect_usb`'s key did not parse; it is fixed.

Also fixed:

- **Attempt cap race.** The attempt is reserved, as a `pending` row under a lock on the
  objective row, before judging.
- **Closing while judging.** Credit is applied under a lock on the exercise row, and only
  while the exercise is running; otherwise the attempt is recorded as `closed`.
- **Replays.** Attempts count per run (`window_start >= started_at`), and `/run` clears
  the last run's evidence.
- **Queries that do not parse.** These return 422 and use no attempt. Unparseable queries
  and store outages are capped at 20 per Student per objective.
- **Search backend.** `SEARCH_BACKEND=null` is an outage, not "nothing matched".
- **Staff detections.** These are recorded as `staff_detection`, not Student evidence.
- **Worker start.** `start_exercise` in the worker no longer moves `started_at` or reopens
  a closed exercise.
- **Participants.** Both `/complete` and the clock assess, grade and send xAPI for every
  Student who submitted a detection in the run, plus whoever closed it.

Left as they are:

- Mock runs still auto-achieve every objective. A mock range has no telemetry, so it is a
  simulation, not a graded exercise.
- ~~Students keep `exercise:start` and `exercise:complete`~~ Reversed by the security
  sweep (M3, 2026-10-08): Students hold neither, so they cannot run, close or replay a
  team exercise, and `/run` on a completed or cancelled exercise needs `reset=true`
  (a replay clears every objective). Lab sessions have their own lifecycle.
- The answer key is read from the scenario at submission time, so editing a running
  exercise's scenario changes its key.
- There is no Student-to-exercise relation, so any Student in the tenant may submit on a
  running exercise.

## Changes after the security sweep (2026-10-08, H4)

Inject telemetry was stamped with flat labels (`inject_action`, `exercise_id`,
`truenorth_simulated`, `event.module: truenorth.inject`, `threat.technique.id`,
`inject_target`) and a Student's detection ran as raw Lucene `query_string`. A Student
who queried `inject_action:<action>` matched exactly the inject's events, at full
precision, and was credited without detecting anything.

- **The Student's query is a closed grammar** (`app/search_backends/detection_query.py`):
  `field:value`, quoted values, wildcards anywhere in a value, `field:*`, numeric
  `field:>N`, `field:(a OR b)`, and `AND` / `OR` / `NOT` / parentheses. Every term names a
  field (no free text, which searched every field); no regex, fuzzy, proximity, boosts or
  `_`-prefixed fields; no ground-truth label field (`tn_ground_truth.*`, `inject_*`,
  `truenorth*`, `exercise_id`, `threat.technique.*`, `threat.tactic.*`, `event.module`
  values matching `truenorth*`). Outside it: 422, before an attempt is reserved. The parsed
  query is re-serialised with every value escaped, so the store sees only what was checked.
  Every shipped answer key is expressible in it (a test parses them all).
- **The labels moved out of the observable fields.** The scenario engine writes them under
  one object, `tn_ground_truth` (`exercise_id`, `inject_action`, `simulated`, `module`,
  `action`, `message`, `technique_id`, `target`). The `range-*` template maps it
  `enabled: false`: kept in `_source` for staff, never indexed, so no query matches it, a
  Student's least of all. A summary event (an injector with no telemetry of its own) is
  now only `@timestamp`, `event.kind`, `range_id`, `tenant_id` and the labels.
- **Answer keys are written on observable fields.** A key that names a label is
  `NotScorable` (409 on submit), because it can never match. No shipped key did.
- **Telemetry search hides the labels from non-staff.** `GET /telemetry/{range}/search`
  refuses label field terms (422) and strips labels from each hit's `_source` for callers
  without `scenario:update`.

Residual: indices created before this change keep the old flat labels (searchable by
staff; refused and hidden for everyone else), and their documents fall outside any new
exercise window. An index that existed before the template change maps
`tn_ground_truth` dynamically until it is recreated; the grammar still refuses it.

## Open questions

- **TODO (security sweep M3, 2026-10-08, owner decision):** detection submissions are
  not restricted to an exercise's participants. Any Student in the tenant holding
  `detection:submit` can submit on any running exercise and earn (or spend) its
  detection credit, because there is no Student-to-exercise relation (see the last
  bullet of the review changes above). Left unchanged on purpose until the owner decides
  whether to add that relation and gate submissions on it; tracked here so it is not lost.

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
