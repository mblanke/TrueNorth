# ADR 0010: Deleting what history depends on is refused (409); retire it instead

- Status: accepted 2026-10-11 (owner decision for v1.2.0 release readiness)
- Date: 2026-10-11
- Numbering: 0008 is left free for an open branch (the formatting line-ceiling ADR), so
  this is 0010.
- Code: `control-plane/api/app/delete_guard.py` (`refuse_if`, `commit_delete`) and the
  DELETE handlers that use it. Tests: `tests/api/test_delete_conflicts.py` (its own SQLite
  engine with foreign keys enforced, so a forgotten child table fails as it would on
  PostgreSQL), `tests/api/test_delete_policy.py` (no new NULL-on-delete).

## Context

PostgreSQL refuses to delete a row a foreign key still points at. SQLite, the unit-test
engine, does not enforce foreign keys unless asked. So a DELETE handler that only did
`db.delete(parent); db.commit()` passed its tests and returned an unhandled 500 in
production the first time the parent was in use (templates with ranges, courses with
enrollments, LTI platforms with launches, OUs with users).

There are three ways to make such a delete "work", and two of them destroy history:

1. cascade: the children go with the parent. A Student's enrollment, progress and grades
   disappear because someone tidied the course list;
2. NULL-on-delete (`ondelete="SET NULL"`, or a handler nulling the column): the children
   stay but no longer say what they belonged to. A progress record or a release that points
   at nothing cannot be audited, re-graded, or explained to an accreditor;
3. refuse: the parent stays while anything that records history refers to it.

TrueNorth's records feed accreditation (QSP/CFITES, cmi5/xAPI statements, AARs), so the
history must stay intact and attributable.

## Decision

1. **A delete that would orphan history is refused with 409**, and the message names
   what blocks it (`refuse_if(query, "Course has {n} enrollment(s) and is kept as part of
   the Students' records")`). History is anything with value of its own: Student records
   (enrollments, module progress, attempts, scores, competency assertions, detections),
   what was released or published (course releases, publications), what was built from
   the parent (ranges from a template), and scheduled events still reserving it.
2. **The path for a record that is no longer wanted is retire / archive, not delete.**
   What exists today: a course is unpublished (authoring state) or, for a spine
   placeholder, marked `retired` and hidden from catalogues (`qsp_paths`,
   `course_content_ingest._remove_placeholder`); users are soft-deleted
   (`SoftDeleteMixin`); platforms, hypervisor connections, AI backends and routes, nations,
   coalitions and zone policies carry `is_active`; a scheduled event is cancelled. A new
   history-bearing entity gets such a state before it gets a DELETE endpoint.
3. **Pure dependents go with their parent, explicitly.** A child with no value of its own
   (an LTI nonce of a deregistered platform, a group membership, a template's PO map, a
   module's content links) is deleted by the handler, in code, before the parent, and the
   handler's docstring says so. No new `ondelete="CASCADE"` on a table that records
   history.
4. **No NULL-on-delete.** No new foreign key uses `ondelete="SET NULL"`, and no handler
   nulls a reference to let a delete through. `tests/api/test_delete_policy.py` holds the
   list of the sites that predate this ADR and fails on any other.
5. **`commit_delete` is the backstop**: a reference the handler does not know about yet
   (a table added later) rolls back and answers 409 instead of letting the
   `IntegrityError` escape as a 500.

## Existing behaviour this records

`tests/api/test_delete_conflicts.py` covers every guarded handler with an in-use case
and a plain case: AI backends and hypervisor connections (dependents deleted), storage
appliances (409 with volumes), OUs (409 with sub-OUs, security groups or teams), groups (memberships
deleted), LTI platforms (409 with Student activity or a publication; nonces deleted),
templates (409 with a range or a reserving event), courses (409 with enrollments,
releases, publications, module progress or a reserving event), scenarios (409 when booked
or used by an exercise), learning paths (409 when a registration names them), and an
unknown reference (409, not 500). `test_placeholder_with_booking_is_retired_not_deleted`
shows the retire path.

## Exceptions that predate this ADR (not to be copied)

These null a reference today. They keep the child row and its denormalised name, so the
history is still readable, but they are the pattern this ADR rules out for new code; each
should move to "refuse + retire" when its area is next changed:

| Where | What is nulled | Why it was done |
|---|---|---|
| `scheduler/service.py` `detach()` (template, course, scenario, range deletes) | `scheduled_events.<link>_id` of **finished** events | finished events are history but did not block the delete |
| `routers/courses.py` `delete_course` | `quizzes.module_id` | quizzes outlive the course's modules |
| `scenario_runs/models.py` `ScenarioExecution` | `scenario_id`, `range_id` (`ondelete="SET NULL"`) | an execution keeps `scenario_name` for its report |

Related, and also to be revisited: `inject_records` (inject evidence) cascades from
`exercises` and `scenario_executions` (`ondelete="CASCADE"`), so deleting an exercise
takes its inject evidence with it.

## Consequences

- Operators see 409 with a reason instead of a 500, and know what to retire or cancel.
- Lists grow: retired rows stay. Catalogues hide them (`include_retired=false`).
- A new DELETE endpoint needs a test in `test_delete_conflicts.py` under enforced foreign
  keys, and a new history-bearing entity needs a retire/archive state first.
