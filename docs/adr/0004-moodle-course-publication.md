# ADR 0004 — Courses are published into Moodle by the local_truenorth plugin

- Status: accepted
- Date: 2026-10-05
- MOSA pillar: modular design, designated key interfaces

## Context
The 44-course programme (`docs/arc2-44-course-programme.md`) requires ARC² to create each
accepted course in Moodle automatically: sections, content, assessments and lab links, with
no one rebuilding or uploading a course by hand. Moodle's core web services cannot name
sections or create activities (MDL-37083), and a cmi5 package or an LTI link alone does not
create a course. `docs/moodle-integration.md` (2026-09-23) said "no custom plugin, not now"
and kept catalogue sync as an open decision; the Moodle course-farm work on
`claude/moodle-integration-courses-8bc905` then wrote `local_truenorth`, a plugin that
converges a Moodle course on a TrueNorth payload, authenticated by a ticket signed with the
TrueNorth LTI tool key.

## Decision
1. Courses reach Moodle through `local_truenorth` (`infra/platform/moodle/local_truenorth`,
   Moodle 5.0+, verified on 5.2.3). Its `api.php` takes one signed operation per request:
   `upsert_course`, `describe_course`, `set_visible`, `delete_stage`, `hide_course`.
   No Moodle token is stored in TrueNorth.
2. The plugin is reached only through the `app/moodle_backends` seam (`BaseMoodleBackend`,
   registry keyed on `external_platforms.platform_type`; `fake` for tests). Routers and the
   publishing service never call Moodle directly (ADR 0001).
3. A release is published by a durable job (`app/course_publishing`): stage the whole
   course hidden (`tn-stage:<release>`), verify it from Moodle's own description, then
   converge the live course (idnumber = TrueNorth course id) and delete the stage. The live
   course is untouched until verification passes.
4. Quizzes are native Moodle quizzes in the quiz's own question bank, so Moodle owns the
   attempt, the grade and completion, as `docs/moodle-integration.md` gives Moodle the
   gradebook. Range labs are LTI links to TrueNorth (`resource=lab:<course>:<module>`); the
   launch resolves the student's pinned release, so a new release never re-points a lab.
5. A TrueNorth-owned activity no longer in the release is deleted only if no student has
   used it; otherwise it is hidden with its attempts and grades. A quiz is identified by a
   hash of its questions and pass mark, so changed questions are a new quiz and never
   rewrite one that students have attempted. Correct answers show only once a quiz closes.
6. Trust boundaries (security review, 2026-10-05): every TrueNorth tenant's Moodle trusts
   the one TrueNorth tool key, so a sync ticket carries the tenant (`tid`) and a Moodle
   refuses sync for any tenant but the one it is configured for (`--tenant-id`); the plugin
   only touches courses whose idnumber is a TrueNorth UUID or `tn-stage:<UUID>` and
   categories prefixed `tn-`; authored HTML (pages, question text) is cleaned with Moodle's
   purifier before it is stored and `forceclean` is on.
7. Only a course's accepted release is ever published: a job for a superseded release ends
   superseded, whether retried or resumed, and publications are superseded by release
   version, so Moodle never rolls back.

## Consequences
- Moodle needs the plugin installed; the TrueNorth Moodle image
  (`infra/platform/moodle/Dockerfile`) bakes it in and its hooks install and wire it.
- One Moodle course carries the latest accepted release. Work students completed is
  kept (hidden activities keep their grades), but a student mid-course sees the new
  release's activities from the moment it is activated: per-student version pinning is
  recorded in TrueNorth (`enrollment_release_pins`, used by lab launches), not inside one
  Moodle course. Accepting a release keeps TrueNorth-side work too: unchanged quizzes are
  left alone, a changed quiz that students attempted is retired (not rewritten), dropped
  modules are retired and publication flags are kept.
- The cmi5 package ARC² builds stays a release artefact (formative runtime, quiz keys
  client-side); it is not how Moodle receives a course.
- Live verification: `tests/integration/test_moodle_publish.py` against
  `infra/platform/docker/compose.moodle-test.yml`.
- Results flow back (2026-10-09): the same seam gained `pull_results`. TrueNorth pulls
  completions and quiz grades with a sync ticket; the plugin answers signed by the Moodle's
  LTI site key (verified against the platform's `lti_jwks_url`, bound to the ticket's jti
  and the tenant), and `app/moodle_results` records them idempotently on enrolments,
  module progress and quiz attempts. The schedule runs in the API next to the lab sweep:
  the worker image holds neither the Moodle seam nor the models it writes.
  Runbook: `docs/runbooks/moodle.md` section 4.
