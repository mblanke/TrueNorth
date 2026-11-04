# ADR 0020: Instructors judge team exercises

- Status: accepted 2026-11-03 (owner: "there shouldn't be an answer key in team ex";
  "yes, instructor judges team exercises")
- Supersedes: ADR 0005 §1–2 (automatic detection credit) for exercises
- Amends: ADR 0016 (Exercise Studio) stage 5 and the dry run

## Context

ADR 0005 credited a team exercise's detection objective automatically. A Student
submitted a query, and the server compared it with the scenario's query, which acted as
the answer key. Three more automatic paths credited exercise objectives:

- the Student detection endpoint;
- a mock run, which achieved every objective when its timeline ended;
- Exercise Studio's planned stage 5, which writes validators, plus a dry run that proves
  each objective "scores" (not built yet: #332, #334).

In a team exercise, what counts as doing the objective is a judgement about what the team
did, and an instructor (the white cell) makes it. Individual training is different. Course
releases and lab sessions keep their automatic checks: a release is accepted once and
cannot change afterwards.

## Decision

1. **No automatic credit on an exercise objective.** An objective of an exercise (any
   `kind`) is achieved only when an instructor acknowledges it
   (`POST /exercises/{id}/objectives/{ref}/ack`, `objective:ack`). Nothing else marks it
   achieved: not a Student detection, not a validator, not a mock run.
2. **Instructors judge with a record.** The acknowledgement records:
   - who judged;
   - the instructor's note;
   - optionally, the Student detections the instructor relied on (`cites`: ids of
     submissions on that objective in this run).

   An instructor can **withdraw** a judgement while the exercise is open
   (`DELETE /exercises/{id}/objectives/{ref}/ack`). Both re-total the score and are
   audited.
3. **Student detections are evidence, not grades.**
   `POST /exercises/{id}/objectives/{ref}/detections` keeps the ADR 0005 machinery that
   makes a detection trustworthy evidence:
   - participants only;
   - the closed query grammar, with ground-truth labels refused;
   - the exercise window on server ingest time.

   It records the query and how many in-window events it matched, and nothing more: no
   answer key, no precision, no verdict, no credit. Submissions are capped at 20 per
   Student per objective per run, to bound the load on the store. A store outage is 503
   and is not recorded. The Student sees their own submissions; instructors see everyone's
   on the objective they are judging.
4. **The scenario's objective `query` is instructor guidance** on what to look for. It is
   never compared with anything. It stays staff-only (ADR 0005 §5 redaction). Instructors
   can run it themselves in telemetry search. The `opensearch_query` validator name stays
   for now, as the label for "a detection objective".
5. **Mock runs are judged too.** A mock run no longer achieves its objectives. Like a real
   run, it stays running after its timeline until an instructor completes it (or its
   duration runs out).
6. **Exercise Studio (ADR 0016).** Stage 5 writes a **judging guide** per objective
   instead of validators. The guide gives:
   - the measure of effectiveness;
   - the evidence to look for and where it appears (the evidence map);
   - a look-for query when the evidence is telemetry;
   - the points.

   `scoring.no_free_credit` becomes "no objective is credited without an instructor".
   `scoring.validators_known` becomes "every objective has a judging guide". The dry run
   still proves the machinery: the range builds and certifies, the injects fire, and each
   objective's look-for evidence appears in the exemplary player's telemetry inside its
   window and not in the silent player's. It no longer credits objectives, and its
   simulated white cell judges nothing.

## Consequences

- A team exercise's score is the sum of the points of the objectives instructors
  acknowledged. A Student cannot earn or lose team credit through the API.
- The answer-key code in `app/detections/credit.py` (answer key, judge, precision,
  attempts) is removed. The window, grammar and participant checks stay.
- `detection_submissions` keeps its table. New rows have `verdict = "recorded"`.
  `threshold`, `min_precision` and `on_target` stay empty, and old rows keep their values
  as history.
- The Student panel says "Recorded: your query matched N events; your instructor judges the
  objective". There is no achieved/missed, and no attempts countdown, only the 20-submission
  bound.
- Lab sessions and course releases are unchanged.

## Open question

- **The duration clock** (ADR 0005 §6) still completes an exercise when its scenario's
  duration runs out. Judging closes with it. Should the clock instead stop Student
  submissions and leave the exercise open for the instructor to finish judging and
  complete? Until decided, it is unchanged.
