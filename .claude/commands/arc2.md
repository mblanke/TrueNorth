---
description: "ARC² — turn a free-text course request into a staged, QA-checked TrueNorth course bundle plus a cmi5 package under build/arc2/<slug>/. Never commits, applies, provisions or imports."
argument-hint: "[--slug <slug>] <free-text course request> | --resume <slug> [accept | <feedback>]"
---

# /arc2

Orchestrator for the seven ARC² stages. It runs in this thread and is the only place that
launches the `arc2-*` agents (`.claude/agents/arc2-*.md`), runs `merge`/`check`/`gate`/`status`
(`tools/arc2/`) and holds the rework loop. Output: `build/arc2/<slug>/` — `01-blueprint/` to
`05-sensor/` hold the blueprint, course content, range, artifacts and validators; `06-qa/report.md`
is the QA digest; `07-bundle/` holds the cmi5 package and `PROMOTE.md`, the human's instructions
for moving the bundle into the repo. `manifest.json` in the run dir is the only shared state.
Nothing leaves `build/arc2/` (gitignored); promotion is a separate human step.

Request: **$ARGUMENTS**

## Steps

Every command runs from the repo root with `PY=.venv/bin/python` and `PYTHONPATH=tools`.
Below, `$ARC` = `PYTHONPATH=tools $PY -m arc2.check` and `$RUN` = the absolute run dir.
Exit codes: `merge` 1 rejected / 2 STOP; `check` 0 pass / 1 fail / 3 HUMAN-TAKEOVER.

0. **Parse `$ARGUMENTS`.** Empty → print this usage, stop. Otherwise run step 1 first, every
   time, then:
   - `--resume <slug>` alone → `$ARC status $RUN`, stop.
   - `--resume <slug> accept` → outline `pending` → accept at step 4. Preview `pending` and
     stages 1-5 complete → accept at step 6. `qa.result` is `human_takeover` → the takeover
     resume (see Resume). Otherwise → step 3.
   - `--resume <slug> <other text>` → a gate `pending` → that gate's feedback, text verbatim.
     No gate pending and `stages.content-architect.stop_reason` starts `request missing:` →
     append the text as a new line to `$RUN/request.txt`, then step 3. Otherwise refuse and
     print status.
   - `--slug <slug> <request>` → a new request whose slug is chosen by the caller (the Course
     Studio runner does this so it can track the run). `<slug>` must match
     `^arc2-[a-z0-9-]{1,60}$`; otherwise refuse and stop. Step 2 uses it as `SLUG`.
   - Anything else is a new request (step 2). After this command has printed a status, a bare
     `accept` or a free-text reply in the thread counts as `--resume <last slug> …`.
1. **Preflight.** `$PY -c "import sys"` must *run* (a `-x` test is not enough: a venv built
   on another OS passes it and then fails with "exec format error"). If it fails, say so and
   stop: the fix is a native venv (`uv venv .venv --python 3.11`, then install
   `control-plane/api/requirements.txt`, pytest and `ruff==0.16.3`), never a copied one.
   `git rev-parse HEAD`. `git status --porcelain` — warn if dirty; keep the output for step 3.
   `INIT_FLAGS=`; if `ARC2_ENCLAVE=1` then `INIT_FLAGS=--enclave`.
   `PYTHONPATH=tools $PY -c "import arc2.check, arc2.cmi5, pytest"` — stop on ImportError.
2. **Slug and init.** `SLUG` = the `--slug` value if one was given, else
   `arc2-<2-4 lowercase tokens from the request>`;
   `RUN=$PWD/build/arc2/$SLUG`. If `$RUN/manifest.json` exists, say "run already exists;
   resuming it" and go to step 3. Otherwise `mkdir -p build/arc2`, write the request
   verbatim to `build/arc2/$SLUG.request.txt`, then
   `$ARC init $RUN --slug $SLUG --request-file build/arc2/$SLUG.request.txt $INIT_FLAGS`.
3. **Dispatch, then stages 1-5 in order.** On a resume first run `$ARC gate $RUN --verify`:
   exit 1 means accepted inputs were edited by hand and a gate re-opened → `status`, stop.
   Read `$RUN/manifest.json` and go to the first line that matches:
   - `gates.outline.state` is `pending` → step 4.
   - `gates.preview.state` is `pending` and stages 1-5 are all complete → step 6.
   - stages 1-5 all complete and `qa.result` is not `pass` → step 5.
   - stages 1-6 complete, `qa.result` `pass`, preview `accepted`, package-builder not `done`
     → step 7. Package-builder `done` → step 8.
   - Otherwise, for each of content-architect, code-generator, range-engineer,
     artifact-creator, sensor-gateway whose `stages.<name>.state` is not complete (pending, or
     `failed` after a STOP):
     - range-engineer or sensor-gateway, and no module in `$RUN/01-blueprint/outline.yaml` has
       `activity: range` → `$ARC skip $RUN <stage> --reason "no range activity: <module
       activities>"` instead of launching the agent (exit 1 → print stderr, stop), and continue.
     - Agent tool, `subagent_type: arc2-<stage>`, prompt:
       > Stage <N> <stage> of ARC² run <slug>. Run dir: <$RUN>. Read manifest.json first: the
       > keys upstream stages own, request.txt, gates.outline.feedback[], gates.preview.feedback[]
       > entries with routed_to=<stage>, and qa.findings[] with owner_stage=<stage>. Write only
       > <$RUN>/<NN-dir>/fragment.json (your owned keys plus files[]/human_actions[] stamped
       > stage="<stage>") and files under <$RUN>/<NN-dir>/. Refuse with {"stop": "<reason>"}.
       > Never touch manifest.json; never run init/merge/gate, or `check` without `--dry-run`.
       > Enclave: <provenance.enclave>.
     - `$ARC merge $RUN <stage>`. Exit 2: print the STOP reason and `status`, stop. Exit 1:
       print stderr, stop. Never retry a merge silently or edit the fragment to make it pass.
     - `git status --porcelain` must equal step 1's output. A new tracked change → stop and
       report the file and the stage that wrote it.
     - After content-architect merges: `gates.outline.state` `pending` → step 4; otherwise
       continue with code-generator (a rework that changed nothing the outline gate hashes).
       After sensor-gateway merges → step 5.
4. **Outline gate.** `$ARC status $RUN`; print it (it carries the two resume lines) and the
   path `$RUN/01-blueprint/outline.yaml`. STOP the turn. On resume:
   - accept → `$ARC gate $RUN outline accept`, then step 3.
   - feedback → `$ARC gate $RUN outline feedback --text "<text>"`; every stage is pending
     again; step 3, which re-runs content-architect with `gates.outline.feedback[]`.
5. **QA loop.** Agent `arc2-qa-tester` with the step-3 prompt (dir `06-qa`, no owned keys;
   it writes `06-qa/report.md` from `check --dry-run`, which writes nothing), then
   `$ARC merge $RUN qa-tester`, then the recording `$ARC check $RUN --json`.
   - 0 → preview gate already `accepted` on these inputs → step 7; otherwise step 6.
   - 1 → read `.rework_stage` from the JSON. `null`: only the orchestrator owns the failures
     (gates, schema, a crashed check) → print the `fail` findings, stop. `package-builder`
     → step 7. A stage 1-5 → that stage and everything after it are pending again (an
     accepted preview gate is re-opened) → step 3, then back here. `.cycle` is the budget;
     nothing outside this loop re-runs a stage to "try again".
   - 3 → HUMAN-TAKEOVER: `status`, stop. Three counted failures; a human fixes by hand.
6. **Preview gate.** `$ARC status $RUN`; print it, the first 40 lines of
   `$RUN/06-qa/report.md`, and the two resume lines. STOP the turn. On resume:
   - accept → `$ARC gate $RUN preview accept`. Exit 0 → step 7. Exit 1 → the gate's own
     check already recorded `qa` and reset stages: read `qa` from the manifest and route it
     exactly as step 5 routes a `check` exit 1 or 3. Do not launch the qa-tester first.
   - feedback → pick `--route` by ownership: objectives, critical events, course, PO →
     `content-architect`; course text, quiz, pages, config → `code-generator`; range,
     injects, timeline, noise → `range-engineer`; rubric, deliverable, variant, media, xapi →
     `artifact-creator`; validators, scenario, scoring → `sensor-gateway`. Feedback spanning
     two rows → the more upstream one; later stages re-run anyway.
     `$ARC gate $RUN preview feedback --text "<text>" --route <stage>`, then step 3.
7. **Package.** Agent `arc2-package-builder` with the step-3 prompt (dir `07-bundle`),
   `$ARC merge $RUN package-builder`, then the recording `$ARC check $RUN --json`.
   - 0 → `$ARC status $RUN`, step 8.
   - 1 → `.rework_stage` `package-builder` → step 7 again (`merge` accepts stage 7 while QA
     is failing only for its own findings; each counted failure spends the cycle budget).
     A stage 1-5 → the check has reset it and re-opened the preview gate → step 3, then
     step 5, step 6 (the human accepts again), step 7. `null` → print the `fail` findings,
     stop.
   - 3 → HUMAN-TAKEOVER: `status`, stop.
8. **Finish.** Print the final status, the open `human_actions` (they block promotion, not
   the run) and the path `$RUN/07-bundle/PROMOTE.md`. Stop. Promotion is the human's step.
   The package is a candidate, not an approved release (`docs/arc2-course-studio.md`).

## Hard rules

- **Never** commit, `terraform apply`, `forge.py provision`, import into the API, or call an
  external API — not in this thread, not through an agent.
- Every write lands under `build/arc2/<slug>/` (gitignored). A tracked change after any
  stage stops the run; report it, do not revert it.
- Never edit `manifest.json` by hand and never let an agent do so: only `merge`, `check` and
  `gate` write it. Agents write `fragment.json` and their own files; nothing else.
- Never launch a stage out of order or past a pending gate. `merge` refuses both; do not
  work around the refusal.
- Three counted QA failures end the run for a human. Do not reset `qa.cycle`, re-init under
  a new slug, or edit a fragment to make `check` pass.
- Agent rules live in `.claude/agents/arc2-*.md`, which point at
  `.claude/agents/scenario-engineer.md` and
  `truenorth-content-pack/truenorth-content/CLAUDE.md`; the contract is `tools/arc2/`
  (`check.py`, `cmi5.py`, `manifest.schema.json`). Do not restate either in a prompt; point
  at them. A STOP of `QSP-READ-REQUIRED` means `ARC2_ENCLAVE` was not 1: say so and stop;
  never paraphrase a QSP to get past it.
- This command is the only caller of the Agent tool, of init/merge/gate and of a recording
  `check`; agents may run the read-only `status` and `check --dry-run`. An agent that asks
  you to run one of the others on its behalf gets a STOP, not a favour.

## Resume

"Complete" means `done`, or `not_applicable` for the two range stages of a run with no range
module. `manifest.json` records where a run stopped: `stages.<name>` (`state`
pending/done/failed/not_applicable,
`attempts`, `stop_reason`), `gates.outline` and `gates.preview` (`state` n/a/pending/accepted/
feedback, `feedback[]` with `round` and `routed_to`, `rework_count`) and `qa` (`result`,
`cycle` 0-3, `rework_stage`, `findings[]`). `status` reads it and prints a `next` line.

- `/arc2 --resume <slug>` prints status and stops. Nothing runs.
- `/arc2 --resume <slug> accept` accepts the pending gate (outline first). With no gate
  pending after a STOP the human has fixed by hand, it continues at step 3.
- **Takeover resume.** After a HUMAN-TAKEOVER, `accept` relaunches no agent: the human's
  fixes are on disk. For each stage 1-5 that is not complete, in order, `$ARC merge $RUN
  <stage>` on the fragment the human left (exit 1 or 2 → print it, stop); then step 5.
  `qa.cycle` stays at 3 until a pass, so one more failure hands the run back again.
- `/arc2 --resume <slug> <text>` is feedback for the pending gate. With no gate pending it
  is refused and the status printed, except after a `request missing:` STOP, where the text
  is appended to `request.txt` and the run continues (step 0).
- Re-running `/arc2` with a request that derives an existing slug resumes that run (step 2).
