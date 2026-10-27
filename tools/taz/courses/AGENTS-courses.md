# Rules for drafting one course

You are bringing **one** TrueNorth course file up to the "technically proven" bar in a
disposable clone. The checker's output decides the outcome, not your summary. The prompt
names the course file (`content/courses/<file>.yaml`) and a suggested range template.

## What you may change

1. **Only two files:** the course file the prompt names, and its instructor pack
   `content/courses/instructor/<same file name>`. The write gate refuses everything else.
   Never touch app code, tests, scripts, range templates, scenarios or
   `content/catalogue/references.yaml`.
2. **Edit, never Write, an existing file over 150 lines.** Course files are 380+ lines; a
   whole-file Write is refused and would drop every line you did not re-emit. Use Write
   only to create the instructor pack, which does not exist yet.
3. **Read the whole course file before your first edit.** Read it again after any
   context compaction before you edit it again.

## What the course needs

4. **Keep the banner and the draft notes.** The leading comment lines (the
   "Unsourced ... not validated by Standards" banner), `provenance: unsourced`,
   `status: draft`, `is_published: false` and every entry in `source.notes` stay exactly
   as they are.
5. **Set `readiness: draft`** at the top level (next to `status`). Never set
   `technically_proven` or `sme_signed`: Claude sets `technically_proven` after it has
   validated your patch, and only SME/Standards sign-off sets `sme_signed`.
6. **Pick the range template whose hosts fit the labs.** Read
   `content/ranges/<id>/template.yaml` for the suggested template and at least one
   alternative. Every module gets a `lab_range`:
   ```yaml
   lab_range:
     template: small-enterprise          # a directory under content/ranges/
     scenario: ransomware-lite           # optional; must run on the same template
     steps:
       - hosts: [jump-box]
         action: Log on to jump-box over SSH and open a terminal.
       - hosts: [jump-box, dc-01]
         action: From jump-box, query dc-01 for the domain password policy and record it.
   ```
   - `hosts` are node `id`s from that template's `nodes:` list. **Use only hosts that
     exist.** Do not invent a host, and do not name one in `action` text that the template
     lacks. The checker reads every word shaped like a host name (`dc01`, `ws-01`).
   - A scenario is optional. If you give one, its `scenario.yaml` `range_template` must be
     the same template.
   - Write 3 to 8 concrete, ordered steps per module that a Student can follow on that
     range. Keep the module's `lab:` brief and make it agree with the steps.
7. **Cite only refs in `content/catalogue/references.yaml`.** Never invent a source, a
   title, a URL or a standard number. If no listed reference fits, keep the existing refs.
8. **Quizzes:** each module keeps exactly 5 questions with options lettered `A) ` to `D) `
   in order and `answer` one of A to D. No question may repeat another in the course.
   Each question must use at least one key term from its module's objectives or topics
   (the checker uses a simple word-overlap heuristic). If a question fails, fix the
   question or add the missing term to a topic it genuinely belongs to; do not pad topics
   with unrelated words. Check that every keyed answer is actually correct.
9. **Instructor pack:** create `content/courses/instructor/<same file name>` with:
   ```yaml
   course_code: C103                     # same as the course file
   audience: staff_only
   overview: <what the course does, how the six modules and labs fit together>
   timings:                              # one per module ordinal
     - {module: 1, minutes: 60, plan: "15 teach, 35 lab, 10 quiz"}
   answer_key_notes:                     # one per module ordinal
     - {module: 1, notes: "Q1 B: ... Q2 ...; the distractors test ..."}
   lab_setup:                            # one per module ordinal
     - {module: 1, setup: "Revert the range to baseline; check jump-box reaches dc-01."}
   common_faults:                        # at least one
     - {module: 1, fault: "...", remedy: "..."}
   ```
   It is staff-only: answer keys and lab setup live here, never in the course file.
10. **Words:** say "Student" for a person taking a course. Never "Member" or "Learner".

## Done means the checker passes

11. Run the checker and read every line:
    ```bash
    .venv/bin/python scripts/check_course.py --base origin/main content/courses/<file>.yaml
    ```
    Fix every FAIL and run it again until the first line reads `PASS`.
12. **Never claim done without the passing output.** End with a short report: the
    template you chose and why, what you changed, and the last checker output pasted
    verbatim. If you cannot make it pass, start the report with `BLOCKED:` and say which
    checks still fail and why.
