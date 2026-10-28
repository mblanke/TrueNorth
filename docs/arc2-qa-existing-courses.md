# ARC² QA over existing courses

TAZ drafts courses without ARC²: a course YAML in `content/courses/` and a staff-only instructor pack in
`content/courses/instructor/`. This harness pushes those through ARC²'s QA stage without regenerating
anything and without a model call. It imports `tools/arc2/qa.py` and `check.py` and changes neither.

## Run it

One course:

```bash
.venv/bin/python scripts/arc2_qa_course.py content/courses/c103-intro-to-networking.yaml
.venv/bin/python scripts/arc2_qa_course.py <course.yaml> --pack <instructor.yaml> --out build/arc2-qa/c103
```

Many courses (quoted globs are expanded by the script):

```bash
.venv/bin/python scripts/arc2_qa_batch.py 'content/courses/c1*.yaml'
.venv/bin/python scripts/arc2_qa_batch.py content/courses/c103-*.yaml content/courses/c104-*.yaml --out build/arc2-qa
```

Exit 0 when nothing blocks, 1 when any course has a blocking finding (`fail`), 2 on bad arguments.
Output goes under `--out` (default `build/arc2-qa/`, ignored by git):

| File | What |
|---|---|
| `<course>/06-qa/report.md` | ARC²'s report: preview digest, objectives (PASS/FAIL), findings, human actions, verdict; then which checks ran, and needs range support |
| `<course>/06-qa/report.json` | the same, machine-readable (`schema: arc2-qa-existing/1`) |
| `<course>/02-content/`, `04-artifacts/instructor/`, `05-sensor/`, `manifest.json` | the staging layout ARC² reads. `manifest.json` is an adapter, not an ARC² run manifest |
| `summary.md`, `summary.json` | batch only: course, checks run, failures, open human actions, needs-range-support count, lab hosts verified per range template |

Held courses (`scripts/check_course.py` `HELD_CODES`) fail `taz.held`; `--no-held` skips that check.
The catalogue code comes from `content/catalogue/production_register.csv` (by file name), else the
course's own `course_code`, else `--catalogue-code`.

## What runs

Every check is listed in the report's Checks table as `ran`, `not_applicable` (with a reason) or `not_run`
(applies, could not run). Nothing passes or fails silently.

ARC² code that runs (imported):

- `arc2.qa.check_course` (`qa.course.*`): publication, provenance, status, `qsp_code`, notes, modules, labs, refs,
  quiz shape, duplicate stems across the library, miscited sources.
- `arc2.qa.check_catalogue_identity`: the course is in the catalogue.
- `arc2.qa.check_engine_scenario`: only when a `lab_range` names a scenario.
- `arc2.qa.check_defang`: over the course file and the instructor pack.
- `arc2.check._check_trace`: `objective.observable_verb` and objective-to-module tracing.
- `arc2.check._check_po`: bound POs exist in the crosswalk and may be bound. A claim by the course itself is not a rival.

Not applicable to a TAZ course, each with its reason in the report: `schema.*`, `stage.*`, `activities.*`,
range evidence, `lab_profile.*`, `trace.file_*`, validator/critical-event/inject/summative/xAPI tracing, `captures.*`,
`gate.*`, `cmi5.*` (no package, no learner HTML pages), the blueprint catalogue row, the inject timeline and
`qa.author_required_marker`, and the comparisons with a blueprint (`qa.course_code_mismatch`,
`qa.course_manifest_mismatch`, `qa.course_objectives_verbatim`, `qa.course.arc2_code`). A TAZ course has no sensor
stage, so there are no validators or sensor map to check.

TAZ-kit checks (`scripts/check_course.py`, as `taz.*`): held, schema, readiness, banner, six modules, `lab_range`
resolution, refs, quiz shape, quiz terms (heuristic), instructor pack. A module declared `activity: theory` or
`practical` is not required to have a lab.

Extra checks (harness, not model-based):

| Check | Fails when |
|---|---|
| `lab.hosts_in_template` | a step lists a host, or its action names a host-shaped word, that the named range template lacks. The hosts that pass are reported per template |
| `lab.host_used` | a step lists a host that neither its own action text nor an earlier step in the module touches. A closing judgement step may list a host used earlier |
| `lab.host_unlisted` (advisory) | an action names a template host the step does not list |
| `lab.needs_range_support` (advisory) | never fails. Each `NEEDS RANGE SUPPORT: <item>` in a pack's `lab_setup` becomes a finding and an open action ("file a range ticket") |
| `lab.range_support_unmarked` (human) | a lab assumes a spare disk, a decoy or planted item, or extra users and `lab_setup` does not list it as needs range support. Confirm the template provides it, or list it |
| `pack.answer_key_matches_quiz` | the pack's answer-key notes do not cite every quiz question as `Q<n> <letter>`, or cite a letter the quiz does not key, or a question that is not there |
| `pack.timings_match` (advisory) | a pack timing differs from the module's `duration_minutes` |
| `content.pass_threshold_agrees` | a module's `pass_threshold` differs from its quiz's (the cmi5 `masteryScore` would disagree) |

## Writing `lab_setup` for the range tickets

In the instructor pack, mark each thing the range cannot yet provide with the phrase `needs range support:`
and end the item with a full stop or semicolon:

```yaml
lab_setup:
  - module: 2
    setup: "Revert the range to baseline. Needs range support: a spare 1 GB disk on ws-01."
```

The batch's `needs_range_support` column counts them; `report.json` lists module, template and item for each.

## Limits

The checks are structural and rule-based. They do not judge whether a keyed answer is true, whether a lab is
pedagogically sound, or whether a step works on the range; those stay with Claude's review and the SME.
`taz.quiz_terms` and the host-use test are heuristics and say so. A pass here does not replace Standards and SME
sign-off; the report always carries that as an open human action while `readiness` is not `sme_signed`.

Tests: `tests/scripts/test_arc2_qa_course.py` (C105's committed ARC² bundle adapted to the TAZ shape is the positive
case; one synthetic bad course per failure type).
