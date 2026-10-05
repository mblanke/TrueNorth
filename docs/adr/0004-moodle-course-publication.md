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
   gradebook. Range labs are LTI links to TrueNorth (`resource=lab:<release>:<module>`).
5. A TrueNorth-owned activity no longer in the release is deleted only if no student has
   used it; otherwise it is hidden with its attempts and grades. A quiz is identified by a
   hash of its questions, so changed questions are a new quiz and never rewrite one that
   students have attempted.

## Consequences
- Moodle needs the plugin installed; the TrueNorth Moodle image
  (`infra/platform/moodle/Dockerfile`) bakes it in and its hooks install and wire it.
- One Moodle course carries the latest accepted release. Work students completed is
  kept (hidden activities keep their grades), but a student mid-course sees the new
  release's activities from the moment it is activated: per-student version pinning is
  enforced in TrueNorth (`enrollment_release_pins`), not inside one Moodle course.
- The cmi5 package ARC² builds stays a release artefact (formative runtime, quiz keys
  client-side); it is not how Moodle receives a course.
- Live verification: `tests/integration/test_moodle_publish.py` against
  `infra/platform/docker/compose.moodle-test.yml`.
