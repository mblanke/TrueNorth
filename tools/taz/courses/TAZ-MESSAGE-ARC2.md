# Message for the TAZ foreman session (copy and paste over Remote Control)

Everything below the line is the message. It carries no secrets and no paths outside the repo.

---

Foreman: new course style kit on `main` (PR for branch `claude/taz-arc2-style`; pull once it is merged).

1. Pull `main` into your read-only kit clone (`git -C $B/kit pull --ff-only`). Re-install
   the hooks only if `tools/taz/hooks/` changed.
2. Re-read, in full:
   - `tools/taz/courses/AGENTS-courses.md` (new "Read first" section, a stricter lab brief,
     quiz and instructor-pack rules);
   - `tools/taz/courses/ARC2-STYLE.md` (the ARC2 house style, about 390 lines);
   - `tools/taz/courses/FOREMAN.md`, step 3 and the new "When the kit changes" section.
3. From now on the prompt for every course is: the course line, then `AGENTS-courses.md`,
   then `ARC2-STYLE.md`, then the three-file read list for the exemplars in
   `tools/taz/courses/reference/`. Take all of it from a fresh clone of `main`. The coder's
   write scope is unchanged: the course file and its instructor pack, nothing else.
4. `scripts/check_course.py` has new checks: `objectives`, `wording`, `quiz_style`,
   `lab_style`, and a stricter `instructor` (timings equal the module minutes; the answer-key
   notes give `Q1 <letter>` to `Q5 <letter>` matching the quiz). Almost every draft in the repo
   fails `quiz_style` today (answers all keyed `B`, keyed option the longest), so expect to
   re-key and re-write distractors as well as add labs.
5. Re-do every course you have already finished that does not pass the new checker: apply
   its patch in a fresh clone, run `scripts/check_course.py --base origin/main` on it, and
   if it prints any FAIL, run the course again from step 1 of the runbook with the new
   prompt. A course that passes is left alone.
6. Re-send each re-done course as `TAZ course patch: <file> (re-done)`, patch text
   unedited, as before. Do not wait for my reply between courses. Blocked courses are
   still reported as `TAZ course blocked: <file>` with the last checker output.

Reminders: never push, never `pkill -f` or `killall` (stop only your own processes by PID),
no secrets in messages or logs, do not write course content yourself.
