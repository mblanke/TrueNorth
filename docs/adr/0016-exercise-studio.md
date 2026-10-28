# ADR 0016: Exercise Studio, a staged generator for collective exercises

- **Status:** proposed 2026-10-28 (design only; the owner decisions in "Owner decisions" are settled, the open questions are not).
- **Date:** 2026-10-28
- **Code:** none. This ADR, the glossary terms and `docs/exercise-studio.md` are the whole change. The build is the tickets in "Delivery".
- **Builds on:** ARC² (`docs/arc2-course-studio.md`), ADR 0012 (golden templates), ADR 0013 (range certification), ADR 0014 (classes, content clock), ADR 0005 (detection credit), the content port (#324, `docs/content-port.md`) and the exercise template record (branch `claude/exercise-template-record`, see "The exercise template record").

## Context

ARC² turns a free-text course request into a staged, QA-checked bundle that a human promotes. It is the only generator TrueNorth has that is reviewed, resumable and sandboxed. Exercises have no such thing:

- **Exercise Forge** (`app/routers/exercise_forge.py`, `ForgedExercise`) makes a one-shot scenario and exercise from threat-intelligence indicators. Nothing reviews it, it produces no instructor material, and it never proves the result runs.
- `POST /exercises/{id}/mesl/generate` asks the AI orchestrator for MESL serials for one exercise on one range.
- Scenarios (`content/scenarios/*`), inject packs (`content/inject-packs/*`) and range templates (`content/ranges/*`) are written by hand.
- A collective exercise's objectives and MESL belong to one `Exercise` on one `Range`. There is no reusable, portable "exercise" (`docs/content-port.md`: "TrueNorth has no exercise template record"). The in-flight template record changes that.

The owner wants "the same thing as ARC² but for exercises". This ADR designs it. It is also the first generator whose output can contain things that act on machines (live injects), so it has a safety section that ARC² never needed.

What ARC² gives us, and how this design uses it:

| ARC² mechanic | Where it lives | Exercise Studio |
|---|---|---|
| Seven stage agents, each owning manifest keys and a `NN-*/` directory | `.claude/agents/arc2-*.md` | Seven mirrored agents plus one optional (see "Agent roster") |
| `/arc2` orchestrator: the only caller of agents, `init`/`merge`/`gate`/`check` | `.claude/commands/arc2.md` | `/exstudio`, same shape |
| `manifest.json` as the only shared state; fragments merged by the orchestrator | `tools/arc2/check.py`, `manifest.schema.json` | `tools/exstudio/`, its own schema |
| Outline gate and preview gate, input hashes, re-open on edit | `tools/arc2/check.py` | Same, plus a dry-run digest in the preview hash |
| Three counted QA failures, then human takeover | `check` exit 3 | Same, with a rule for what counts (see "Failure and human takeover") |
| Runner and OS sandbox (Seatbelt, bubblewrap), egress proxy, model modes | `tools/arc2/runner.py`, `confine.py`, `egress.py`, `netbridge.py`; `install/roles/tn_arc2` | The same runner and sandbox, a second job `kind` |
| Tenant-owned runs, 404 for another tenant's | `app/routers/arc2_studio.py`, `arc2_studio_schemas.py` | `app/routers/exercise_studio.py`, same ownership code path |
| Agents never commit, provision, import live or call an external service | `/arc2` "Hard rules" | Same, word for word |
| AUTHOR-REQUIRED instead of tradecraft; defang patterns | `tools/arc2/qa.py` `DEFANG_PATTERNS` | Same, extended (see "What the generator must never produce") |

## Owner decisions (settled)

Recorded verbatim from the owner's brief of 2026-10-28.

1. The owner wants "the same thing as ARC² but for exercises".
2. **Exercises and courses are separate.** Exercises are COLLECTIVE training (teams, roles, a shared range); courses are INDIVIDUAL training. Exercise Studio never attaches to course modules or touches `lab_range`.
3. **New ranges too.** From a free-text request it picks an existing range template, or drafts a new one. A drafted range is unusable until its golden images exist and the range reaches Certified. Golden images are built and proven at the work site and ported, so a draft lists the images it needs and which exist.
4. **Instructors request from the web app, and an admin approves and promotes.** Nothing goes live until a human promotes it. Generation agents never provision, commit, import live, or call external services (as with ARC² now).
5. **Inject safety, "all of the above":**
   - the generator may draft both defanged pre-captured injects and live scripted attacks;
   - anything live is clearly flagged;
   - it runs only on a range certified isolated (S4/S6);
   - it needs a cleared admin to approve it separately from the rest;
   - live scripts run only inside the range, via the existing guest-operations or injector path;
   - decide and document what the generator must never produce (e.g. anything targeting systems outside the range, or persistence that survives teardown).
6. **Automatic dry run.** Before an admin sees an exercise, QA builds the range on a free certified range (queued behind real classes via the capacity gate from #311), plays the injects on their timeline with simulated players, checks that every objective can be scored, then tears it down. The admin sees a pass/fail run report. That run is the evidence for "proven" in the content port.
7. Outputs: an exercise template (per the in-flight record), scenario, inject timeline (MESL), validators and scoring, roles/teams, instructor pack, debrief/AAR template, and an optional range-template draft. Exports through the content port as a signed bundle. If your design can't reach this point, say so and why.
8. Reuse ARC²'s mechanics: the staged `build/<...>/` layout, outline acceptance then preview acceptance, hash checks, bounded QA retries, human takeover, the runner and sandbox, and the models rule (no Chinese models except GLM; local-model mode on TAZ allowed).

9. **Participant sizing (added 2026-10-28, owner requirement relayed by the coordinator).** The studio asks the instructor or event planner how many people are taking part, and in which roles: for example analysts, red team, blue team, intel team, and other roles the exercise needs. That sizing decides how many VMs get created and of what kind. It covers intake (counts per role plus free text, with follow-up questions when sizing is missing or does not fit the idea), sizing rules (each role maps to a seat pattern from the exercise template's role definition, with a per-role VM profile; shared infrastructure scales with team size), range output (the chosen or generated range is scaled to the sizing), a capacity check before the dry run and before promotion (needs N VMs, X vCPU, Y GB; the site can or cannot fit it, and the biggest team size that fits), re-sizing (a later headcount change scales the range again, and approved bookings go back to approval), and the link from role counts to the class roll call and the team split (#299).

## Decision

### 1. One module, its own engine, shared sandbox

Exercise Studio is a module of the existing application, next to Course Studio under the Authoring hub. No new deployed service.

- **Engine:** `tools/exstudio/` (manifest schema, `check`, `merge`, `gate`, `status`, bundle builder), agents `.claude/agents/exstudio-*.md`, orchestrator `.claude/commands/exstudio.md`. Run directories live under `build/exstudio/<slug>/` (gitignored), slug `^exs-[a-z0-9-]{1,60}$`.
- **Do not refactor ARC² in this project.** ARC² is on the GA path (ADR 0014 / deployability plan). `tools/exstudio/` imports only the content-agnostic sandbox primitives (`arc2.confine`, `arc2.egress`, `arc2.netbridge`) and copies, then adapts, the gate, merge and hash code. A later ticket may extract a shared `studio_core` once both have shipped. The cost of the copy is drift; it is listed under Risks.
- **Runner:** the existing runner gains a job `kind` (`arc2` by default, `exercise`). Both kinds use the same queue discipline, flock, confinement and egress rules, each with its own runs root (`ARC2_RUNS_DIR` and `EXSTUDIO_RUNS_DIR`) and its own group-readable directories. Adding `kind` is the only change to ARC² code, it defaults to today's behaviour, and it gets an adversarial review.
- **API:** `app/routers/exercise_studio.py` under `/exercise-studio/...` with a status route that always answers and run routes that 404 when `EXERCISE_STUDIO_ENABLED` is off (ARC² §12). Pydantic schemas in `exercise_studio_schemas.py`; the section's code goes in its own package (`app/exercise_studio/`), never in `models.py`, `schemas.py` or `worker/tasks.py` (ADR 0003).
- **Off by default.** `tn_exercise_studio_enabled: false`. Live attacks have a second switch (`tn_exercise_studio_live: false`) that cannot be turned on while `RANGE_ISOLATION_CHECK` is `warn` (see section 6).

### 2. Where it runs

Generation, the dry run and the proven check all happen at the **origin site**, the one that has the hypervisor, the golden images and the certified ranges (today: the work site). The destination site only imports a signed bundle through the content port. Consequences:

- Generation uses the runner's model modes (`ARC2_MODE`, ARC² §13). At a CUI site that means `local` mode against the TAZ gateway; `subscription` mode sends exercise text to the model provider and is allowed only where that is acceptable (the ARC² rule, unchanged).
- The dry run needs a hypervisor. A development machine without one can generate an exercise and see the static QA, never a dry-run pass.

### 3. Exercise Studio never touches courses

- No Studio output has a `course_id`, a module, `lab_range`, a lab profile, a cmi5 package or a `module_content` link. `exstudio.check` fails the run on any of them (`separation.course_keys`), and the promotion importer refuses them again.
- Studio writes no `Course`, `Module`, `Lesson`, `Quiz` or `CourseRelease` row, and the content clock's "module to scenario" edge is never created by a promoted exercise.
- A scenario Studio creates is an exercise scenario. A course may not reference it unless a person later links it by hand through the existing course tools, which is not Studio's act.
- Individual labs stay with ARC² and `lab_sessions`. The Studio's request form has no "individual" option; a request that reads as individual training is answered with a STOP that says so (stage 1 refusal, `request: individual training`).

### 4. Request to promoted exercise (the flow)

```
Instructor: New exercise request (web: free text + participants per role)  ->  run queued, owned by the tenant
01 blueprint   exercise-architect   -> [NEEDS INPUT: follow-up sizing questions, answered in the web app]
                                    -> OUTLINE GATE (requester or admin accepts; outline shows the sizing)
02 scenario    scenario-writer
03 range       range-engineer       (pick, or draft) + scale rules + captured inject timeline
               CAPACITY CHECK #1    (platform code: needs N VMs, X vCPU, Y GB; fits or biggest size that fits)
03b live       live-inject-engineer (only when the request allows live and the site allows it)
04 pack        pack-writer
05 scoring     scoring-engineer
06 qa          qa-tester: static checks  ->  [LIVE APPROVAL, if any live action]  ->  DRY RUN  ->  run report
                                         ->  PREVIEW GATE (admin accepts the exercise and the report)
07 bundle      bundle-builder       -> bundle + PROMOTE.md
Admin: Promote (separate click, server-side, audited; capacity check #2 recorded)  ->  records exist in the tenant
Later: instructor sets the real headcount on a booking  ->  re-render, re-check, booking back to approval
Origin site: proven (computed)  ->  content port export (signed bundle)
```

Stage order is enforced by `merge`, as in ARC² (`merge` refuses an out-of-order stage and a stage past a pending gate).

### 5. Agent roster

Seven agents mirror ARC²'s seven, with exercise-specific work. An eighth exists only when live attacks are allowed, because live material is the highest-risk output and needs its own tool list, prompt rules and approval path.

| # | ARC² agent | Exercise Studio agent | Owns | Writes |
|---|---|---|---|---|
| 1 | content-architect | `exstudio-exercise-architect` | `request`, `exercise` (title, audience, duration, difficulty, phases), `objectives[]` with MoE and competency refs, `teams[]`, `roles[]` (each with count min/default/max, `seat_pattern`, `vm_profile`: section 7a), `sizing` (requested participants per role), `questions[]` (follow-ups for the requester), `constraints` | `01-blueprint/outline.yaml`, `request.md`, `fit.md` |
| 2 | code-generator | `exstudio-scenario-writer` | `scenario` (situation, storyline, adversary or fault narrative, assumptions), `mesl.serials[]` (phase, scenario time, delivery method cyber/white_cell/email/radio/physical/opfor, from-cell, to-participant, expected action, MoE, objective ref), white-cell and OPFOR scripts | `02-scenario/scenario.yaml`, `mesl.yaml`, `cells/*.md` |
| 3 | range-engineer | `exstudio-range-engineer` | `range` (mode `reuse` or `draft`, nodes-to-roles table, `images_needed[]` each with `exists`, `enabled`, `promoted_build`), `scale` (the scale rules: section 7a), `injects` (captured timeline only) | `03-range/range.yaml` (draft only), `scale.yaml`, `timeline.yaml`, `captures/*`, `README.md` |
| 3b | (none) | `exstudio-live-inject-engineer` (optional) | `live` (a sequence of live-action registry calls per live inject, with targets, limits and abort conditions) | `03-range/live/*.yaml` |
| 4 | artifact-creator | `exstudio-pack-writer` | `pack` (instructor pack, per-role briefs for Students, white-cell run sheet, hot-wash questions), `debrief` (AAR template: sections, prompts per objective, evidence to attach) | `04-pack/*` |
| 5 | sensor-gateway | `exstudio-scoring-engineer` | `scoring` (per objective: validator, params, points, window, evidence class), `evidence_map` (objective to inject/serial to expected telemetry), `scenario.yaml` for the engine | `05-scoring/*` |
| 6 | qa-tester | `exstudio-qa-tester` | none (writes `06-qa/report.md` from `check --dry-run`; does not run the dry run, see section 9) | `06-qa/report.md`, `check.json` |
| 7 | package-builder | `exstudio-bundle-builder` | `bundle` (the exercise template payload and its parts, laid out by destination), `PROMOTE.md` | `07-bundle/*` |

Rules that carry over unchanged from ARC²: an agent reads `manifest.json` first, writes only its own `NN-*/` directory and `fragment.json`, returns `{"stop": "<reason>"}` rather than improvise, never runs `init`/`merge`/`gate` or a recording `check`, and returns a summary of ten lines or fewer. Shared content rules stay in `.claude/agents/scenario-engineer.md`; the Studio's agents point at it and do not restate it.

The pack-writer and scoring-engineer run in this order because scoring changes what the pack may promise. Stage 5 depends on stage 3's timeline (an objective with no inject or serial that can produce its evidence fails `trace.objective_has_evidence`).

### 6. Injects: captured by default, live by exception

Every cyber serial in the MESL maps to an inject on the timeline. There are two kinds, and the checker, not the model, decides which an inject is.

| | Captured inject | Live inject |
|---|---|---|
| What it is | Pre-captured, defanged evidence the analyst observes: pcap, IDS alert, log lines, a seeded file, a ticket. Delivered by the existing simulated injectors (`execution_mode: simulated`) and Greyspace breadcrumbs. | A scripted action that runs on range machines through guest operations (`vsphere_api.run_in_guest`) or a live injector, and produces real telemetry. |
| Who writes it | stage 3 | stage 3b only |
| Form | The ARC² timeline shape (`id, t_offset_min, objective_id, attack_technique, critical, critical_event_id, description`), `pcapgen` captures | A **sequence of calls to the live action registry**, never free-form shell or PowerShell (section "What the generator must never produce", item 1) |
| Flag | none | `live: true` set by `exstudio.check` from the action's class in the registry. A model cannot declare an inject non-live. |
| Needs | the range exists | Live approval, an Isolated range, the kill switch, the site switch |

**Live action registry.** Live actions are reviewed code in the repository, behind an adapter ABC and registry (ADR 0001: `app/exercise_studio/live_actions/`, one module per action, plus the guest-script bodies in the worker as the isolation probes are). Each action declares:

- `id`, a parameter schema (typed, bounded: counts, rates, durations, node ids), and the operating systems it supports;
- `targets`: only node ids of the range template; the checker rejects any other target;
- `effect_class`: `observable_only` (generates logs and traffic), `state_change_reversible` (creates a user, drops a file, changes a setting that a snapshot revert undoes) or `disruptive` (stops a service). `disruptive` needs the instructor to arm it in Ops Center at run time;
- `max_rate`, `max_duration`, `max_targets`, and the abort condition it honours;
- `cleanup`: what a revert-to-baseline must restore, and a dry-run check that proves it did.

The generator picks actions and parameters; it writes no executable text. Where an objective needs behaviour the registry lacks, the stage records an `AUTHOR-REQUIRED` human action for a cleared author to add a registry action by pull request, exactly as ARC² asks a cleared author to supply real sanitised telemetry. This is a deliberate narrowing of decision 5 ("live scripted attacks"): the script is declarative, and the vocabulary is reviewed code. See open question Q3.

**Run-time conditions for any live inject.** All must hold when the executor is about to fire it, checked every time, not once:

1. `tn_exercise_studio_live` is on, and `RANGE_ISOLATION_CHECK` is `enforce`. In `warn` mode a range that fails isolation stays up, and a live attack on it could escape. `enforce` waits for the network team's decision (ADR 0013 section 4), so **live is unavailable until that decision is made** (ticket D0). Captured-only exercises are not affected.
2. The range's newest certification record verdict is `Certified for Students` and its isolation verdict is `Isolated`, and an isolation re-probe (`POST /ranges/{id}/certification/isolation-check` path) younger than `live_reprobe_max_age` (default 15 minutes) returned Isolated. A weaker verdict, a drift `fail`, or a stale probe holds the inject (`held: isolation`), never fires it.
3. The inject's script digest equals the digest the cleared admin approved (Live approval, section 8).
4. The exercise is `running` and not paused. Pausing, completing or cancelling stops live injects at the next event, as `run_scenario_v2` already does; Ops Center has a **Stop live** control that also reverts nothing but halts all live injects at once.
5. Targets exist in the range's recorded inventory (ADR 0013 section 5 identities), not merely by name.

The inject log (`inject_records`) records `execution_mode: "live"` and the script digest. The existing doc's rule "every injector is `simulated` except `greyspace_breadcrumb`" gains a second exception, to be updated in `docs/scenario-inject-execution.md` by the live substrate ticket.

**Flagging.** A live inject is flagged in the outline preview, the preview gate, the run report, the promoted exercise's page, the instructor pack's front page, the class booking (a collective session on a live exercise says so in the calendar), Ops Center, and the AAR. Students are told before the exercise starts that live activity occurs inside the range (the rules of use already cover range activity; the briefing repeats it).

### 7. New ranges: pick or draft

The range-engineer first looks for an existing range template whose nodes cover the roles the exercise needs, preferring one whose latest build is Certified (the plan hash equals the template's current YAML, as in the content port's "Range certified"). Otherwise it drafts.

- A **range-template draft** is a normal range-template YAML, validated against `scenario-engine/schemas/template.schema.json`, naming only catalogue ids. It is stored on promotion with a draft state: **unusable** (a range cannot be created from it and a booking cannot name it) until every image it names is an enabled golden template with a promoted build at this site, and a range built from it reaches Certified.
- The draft lists `images_needed[]`. For each: catalogue id, whether the id exists in `content/catalogue/vm_iso_catalogue.csv`, whether it is `enabled=yes`, and whether this site has a promoted build. An id that does not exist is a **request for a new image**: the draft names it, describes the role, and the stage adds a human action (`ask: build`, `who: template builder at the work site`). The generator never invents a catalogue id that is absent from the catalogue as if it existed, never enables one, and never builds or edits an image (ADR 0012).
- The state moves `draft -> images_ready -> certified` on evidence, by the existing machinery: image promotion (ADR 0013 section 2) and the range certification record (section 6). Nobody sets it by hand.
- The dry run of an exercise on a draft range is **deferred** (section 9). The exercise promotes as Draft and cannot become Proven until the dry run has run on a certified build of the range.
- Passwords: a drafted template declares accounts with `generate`; typed-in passwords appear only for scenario accounts whose weak password is the lesson, flagged `scenario: true` and ported redacted (content port, "Every account password is removed").

### 7a. Sizing: roles, seats and scaling (owner decision 9)

**Intake.** The request form has, besides the free text, a table of **participants per role** (role name, count) and the structured helpers (duration, difficulty, live allowed). A role can be one of the common ones (analyst, red team, blue team, intel team, white cell, observer) or free text. The exercise-architect reads the table and the text together. When sizing is missing, or does not fit the idea, it does not guess: it writes `{"stop": "needs_input: sizing"}` plus `questions[]` (the same mechanism as ARC²'s `request missing:` STOP; the answer is appended to `request.txt` through the existing reply route and the stage re-runs). The run shows state **Needs input** with the questions in the web app. It is not a QA failure and spends no budget. The checks that trigger a question are deterministic (`intake.*`, below), so the model cannot skip them:

- a red-vs-blue idea with no red role or no blue role;
- a role the MESL's expected actions need (`from_cell` / `to_participant`) that has zero participants;
- a count below the role's minimum or above its maximum;
- no count at all for a role, or a total of 0;
- a role whose seat pattern needs a minimum team (for example "intel team" shared pattern with one person).

**Role definition.** Each role in the exercise template carries:

| Field | Meaning |
|---|---|
| `id`, `label`, `team` | The role and the team it belongs to (teams and roles are separate: a team has roles) |
| `count` | `min`, `default`, `max` participants |
| `seat_pattern` | `per_participant` (each participant has their own machines), `shared_per_team` (k machines for the team, however many people), `shared_per_n` (one machine per N participants) or `fixed` (a set number regardless) |
| `vm_profile[]` | The machines a seat gets: `profile_id`, `catalogue_id` (an enabled golden template, or a listed image need), `os`, `role_in_range` (workstation, SIEM console, attack workstation, server), `per` (participant, team, n:<N>), `access` (console, ssh, rdp), `accounts` |

Examples the architect starts from (it adapts them; the numbers come from the catalogue, never from the model):

| Role | Pattern | Profile |
|---|---|---|
| analyst | per_participant | SIEM console plus a workstation |
| red team | per_participant (or shared_per_team for a small team) | attack workstation |
| blue team | per_participant for defender workstations; shared_per_team for the servers they defend | defender workstation or server |
| intel team | per_participant | analyst workstation |
| white cell | fixed | control console (not a Student seat; instructors) |

**Shared infrastructure** (targets, domain controllers, sensors, file and mail servers, gateways) is not a role. It is declared in `scale.yaml` as **rules** that read the sizing: `per_segment` (one sensor per network segment), `step` tables (targets: 1 to 10 blue seats uses 1 server group, 11 to 25 uses 2, and so on; DCs: 1 for up to 50 domain users, 2 up to 200), `per_team`, and `fixed`. The model writes the rules; code computes the result.

**One deterministic renderer.** `tools/exstudio/sizing.py` (pure, no database, imported by the checker, the API and the worker, and tested against fixtures) provides `render(base_template, scale_rules, headcount) -> concrete_template_yaml` and `demand(concrete) -> {vms, vcpu, ram_mb, disk_gb, by_profile}`. The VM resources come from the catalogue row or the node in the base template; the renderer rejects a profile it cannot resolve (`sizing.profile_resolves`). Networks get CIDRs that hold the rendered machines (a segment of 300 hosts does not render into a /24; `sizing.cidr_fits`), unique names and ordered ids so renders are stable and diffable. Rendering the same inputs always gives the same YAML and hash.

**Range output: how a template is scaled.**
- *A drafted range* comes out already sized. Its YAML carries a `scale:` block (the rules above) and a base layout; the base layout is the **minimum render**. Stage 3 writes a concrete render at the requested sizing and one at the minimum. Draft promotion stores the parametric template once, flagged draft (section 7), and the concrete templates are derived from it.
- *An existing range template* is never edited by the Studio and is not assumed to be parametric (the existing hand-written templates have fixed counts). The range-engineer writes a **scale overlay** (`scale.yaml`) that names, for each role and rule, which existing node to clone (by node `role`), what to add when none fits (a profile from the catalogue, which may become an `images_needed` entry), and the step tables. The renderer applies the overlay to the unchanged template and produces a **derived range template**: an ordinary `Template` row, tenant-owned, hidden from the Create list, recording `derived_from` (base template id and hash), the overlay hash and the sizing, built from that concrete YAML. This uses only variants that are real templates, so certification, plan hashes, teardown inventory and the content clock all see an ordinary template, and no new provisioner behaviour is needed.
- Why not only fixed variants (small, medium, large)? A fixed set does not fit "17 analysts and a 4-person red team". Why not only parametric counts? Existing templates are not parametric and ranges are certified from concrete YAML. The overlay plus renderer gives parametric sizing with concrete, certifiable output.

**Capacity check.** Two points, same code: after stage 3 (so the instructor learns early, before the dry run spends anything), and at Promote (recorded on the promoted record). It is also run again at booking and at resize (below). Inputs: the concrete render at the requested sizing, `capacity.supply(db)` (site supply) and the capacity gate's free capacity over the window (`dry_run_max_minutes` for the first check; the session window at booking). The report reads, in the web app and in the run report:

> Needs 62 VMs, 148 vCPU, 396 GB RAM, 5.1 TB disk. The site can fit it (free now: 210 vCPU, 640 GB, 8 TB; peak booked in the next 14 days leaves 120 vCPU, 410 GB, 6 TB). / The site cannot fit it: short by 28 vCPU. The biggest team size that fits is 38 participants (analyst 14, red 6, blue 14, intel 4), found by scaling the headcount down at the same role proportions until the render fits, never below the roles' minimums.

Two verdicts are reported, because they differ: *fits the site* (against total supply, so "this exercise can ever run here") and *fits now* (against the gate's free capacity over the window, so "can it run in this window"). A "does not fit the site" at the first check does not stop generation (the exercise may be moved to another site by the port) but blocks the dry run at that sizing, which then falls back to the minimal size (below) and says so. A "does not fit now" only queues.

**Dry run size.** Decided: the dry run builds the **minimal representative size**, not the requested one. Minimal = every role at its `count.min`, but never fewer than two participants for any shared pattern, every distinct profile present at least once, and every shared-infrastructure rule at its lowest step. Reasons: the dry run proves machinery (the range builds and certifies, injects fire, objectives score, teardown is clean); building 60 VMs to prove that costs capacity that classes need; and the dry run is a hard-capped 90-minute window. The scaling itself is proven another way: stage 3's checker renders the template at the minimum, the requested sizing, **and every step boundary and the maximum**, validates each against the template schema, CIDR fit and name uniqueness, and computes demand for each (`sizing.render_all`). An admin may ask for a **full-size dry run** (the requested sizing) when the first capacity check says it fits now; it is queued the same way and optional. The run report states the sizing built, the sizing requested, the VM count of each, and "full-size not built" when that is the case. A later full-size certification still happens for every real class range, because every deployment gets its own certification record (ADR 0013 section 6) and the live conditions check the actual range.

**Teams and roster (#299).** Role counts feed the class, the roll call and the team split:

- At booking a collective session on a promoted exercise (ADR 0014 range mode `collective`), the session gets a **sizing record** (`exercise_sizing`: per role count, with source `planned` or `roll_call`). The default comes from the roll call (the number of Students with a valid entry) distributed over roles in the template's proportions; the lead edits any role's count within `[min, max]`. A headcount that differs from the roll call is shown ("roll call has 23 Students; roles have 21 seats") and is not an error.
- The team split (`classes/teams.py`, #299) today splits the roll call round-robin into balanced teams. For an exercise with roles it becomes **role-aware**: teams and roles come from the template, each Student is assigned to a role (the lead can move them, as now, before the session starts), surplus Students are listed as unassigned or as observers (the lead chooses), and seats not filled are flagged. Balanced round-robin remains for exercises with no roles. A role assignment is a new column on the team membership (`role_id`), part of S13.
- A Student sees only their own team and role and the machines of their seat. A role-aware split never lets a Student onto a seat of another team (the existing `authorize` and `member_target` rules apply, and the range's accounts are filtered by seat as credentials already are by role).

**Re-sizing.** The instructor can change the headcount later (on the request before promotion, or on the session after it).
- Before promotion: the outline gate re-opens if the sizing changed (the sizing is part of the outline hash) and stage 3 runs again; the dry run is stale.
- After promotion, on a session: the sizing record changes, the derived template is re-rendered and the demand recomputed. If the demand changed, the **session's booking goes back to approval** (`pending_approval`; it holds no capacity, range or instructor until re-approved, per ADR 0014 section 14), the approvers see old and new demand side by side, and the instructor sees why. If the demand is unchanged (for example one analyst fewer but the same step) nothing else happens. Reapproval checks conflicts and capacity as an approval does.
- Class ranges already built are not resized in place. A resize after the range is built, and before a session that uses it, tears the range down and rebuilds it from the new render before that session (ADR 0014 section 9-10 lifecycle), or is refused in the last provisioning lead window with the reason; no VMs are added to a running exercise.
- A resize within the template's min to max and its already-checked steps needs no new dry run; the evidence stays valid because the template's content digest has not changed. A change that needs a new role or profile is an edit to the template and makes the dry-run evidence stale.

### 8. Approval, promotion and who may do what

New RBAC permissions (names are proposals; the code owner confirms them in the first ticket):

| Permission | Roles by default | Allows |
|---|---|---|
| `exercise_studio:request` | instructor, admin | Create a run; read and reply to the tenant's own runs; accept the **outline** gate on a run you requested |
| `exercise_studio:read` | instructor, admin, range_ops | List and read the tenant's runs and run reports |
| `exercise_studio:approve` | admin | Accept the **preview** gate; **Promote**; take over a run |
| `exercise_studio:approve_live` | nobody by default; granted by the platform admin to named admins | **Live approval** (below) |

- **Outline gate:** the requesting instructor, or an admin. It is cheap and about intent.
- **Preview gate:** an admin with `exercise_studio:approve`. The preview digest covers stages 2 to 5, the live scripts (if any), and the **dry-run report digest** (section 9), so editing anything after acceptance, or re-running the dry run with a different result, re-opens the gate.
- **Live approval (Gate L):** a separate act by a cleared admin, on a separate screen that shows only the live scripts as text: each action, its targets, parameters, limits and cleanup, with the digest. It is not part of the preview gate and the preview approver cannot give it by accepting. Rules: (a) the approver holds `exercise_studio:approve_live`; (b) the approver is **not** the requester and **not** the preview approver (two-person, see Q2); (c) it needs MFA step-up within the last 15 minutes; (d) it is per script digest, so any change to a live inject invalidates it; (e) it is audit-logged with the digests; (f) it expires with the preview digest and after 30 days. "Cleared" has no representation in RBAC today. v1 models it as the permission above plus an attestation text the approver signs on the approval screen; the owner decides what "cleared" means in practice (Q1).
- **Promote:** a separate server-side action by the preview approver after stage 7 (it records the capacity report for the template's default sizing, section 7a, and shows it on the confirm screen). It reads the stage-7 bundle by digest (bounded reads, no links followed, the `release.py` approach), validates it again with the same code the API's own routes use, and in one transaction creates in the **requesting tenant**: the exercise template record, the scenario, the validators/scoring, the roles/teams, the instructor pack and debrief template parts, and (if drafted) the draft range template. Nothing is scheduled, booked, started or provisioned by promotion. Every created record carries `provenance: studio:<slug>@<preview digest>`.
- An instructor sees their runs, the outline, the report and the promoted template. An instructor does not see other tenants' runs (404), and does not see the live approval screen unless they hold the permission.
- Auto-accepting gates (ARC²'s test-host `auto_accept`) is **not offered** for Exercise Studio, not even on a test host, because live material and the dry run's evidence must not be a machine's own acceptance.

### 9. The dry run

The dry run is **platform code, not an agent**. Agents are sandboxed and cannot reach a hypervisor, the API or telemetry, and must not be able to forge the result. It runs as a worker task (`worker/exercise_studio_dry_run.py`, queue `provision`, a Redis lock per run) triggered by the API when static QA passes (and Live approval is in place, if the exercise has live injects). Its record is written by the worker to a table (`exercise_dry_runs`, in `app/exercise_studio/`), never to the run directory, and the report is derived from the table.

**Input:** a snapshot of the candidate (the digest of stages 2 to 5 plus live scripts) taken by the API into a private store the dry run reads. If the run directory changes afterwards, the digest no longer matches and the report is marked stale.

**Steps**

1. **Admit.** The dry run asks the capacity service (`app/capacity/`, gate from #311) for the rendered range's size (from `sizing.demand`, the same number the capacity report shows) over a fixed window (`dry_run_max_minutes`, default 90) and queues behind every approved class and booking and every running range. It starts only when all 15-minute slices fit. A class approval or a self-serve lab that needs the capacity wins by being admitted first; the dry run is never preempted once running (its window is short and counted as a running range), and it is never admitted into capacity a class has been approved to hold. After `dry_run_max_wait` (default 7 days) it ends `blocked: capacity`, not failed.
2. **Build.** A range from the exercise's range template **rendered at the minimal representative size** (section 7a; the full-size render only when an admin asked for a full-size dry run and the capacity check says it fits now), tagged `purpose=studio_dry_run`, tenant-owned, on the provisioner the site uses, through the normal provisioning path (ADR 0009), with the lease and the maximum lifetime set to the window. For a template with no build at this site, or with images missing: `blocked: images` (section 7), nothing is built.
3. **Certify.** The build must reach `Certified for Students` with the isolation verdict `Isolated` (ADR 0013 sections 4 and 6). A lower verdict stops the run: `failed: range_not_certified` with the record id and reasons. This is the "free certified range" of decision 6: a range built from the template and certified, not a borrowed class range. Real classes' ranges are never used.
4. **Seat simulated players.** Create a synthetic roster for the teams and roles in the exercise, one per seat of the rendered size: `dryrun-<run>-<team>-<n>` Students in the requesting tenant, flagged `synthetic`, with generated credentials that are sealed and never shown. They are excluded from every roster, leaderboard, AAR, class list and analytics view, and from the content port's "used with passing evidence" (their effect is accounted for by the dry-run evidence itself, section 10).
5. **Play.** Start an exercise from the template on that range with a **compressed clock** (default: the whole timeline in at most 20 minutes; `t` offsets and scoring windows scale together; a serial with a human delivery method (white cell, email, radio, physical) is delivered by the simulated white cell as a recorded delivery). Every captured inject goes through the normal dispatch seam; every approved live inject goes through the live executor under all conditions in section 6. The players act through the same API routes a Student uses (detection submission, deliverable upload, manual ack by the simulated white cell), driven by the evidence map.
6. **Two player policies.** `exemplary` acts on every expected action at its serial; every objective must be achieved. `silent` acts on nothing; no objective may be achieved. A validator that credits `silent`, or one that `exemplary` cannot satisfy inside its window, fails the objective (this is ADR 0005's rule turned into a test).
7. **Check.** Per objective: scored by `exemplary`, not by `silent`, evidence present, window respected. Per inject: fired / held / skipped / failed with reasons. Per live inject: the cleanup check passed after the range was reverted to its baseline. Per serial: a delivery was recorded.
8. **Tear down.** Destroy the range by the independently identified inventory (ADR 0013 section 5) and record teardown `pass`. Failure cleanup runs through the maintenance-window bypass (section 7 of ADR 0013). The synthetic roster is disabled, then deleted by the soft-delete policy (ADR 0010). A dry run that cannot confirm teardown `pass` ends `failed: teardown` and raises a range-ops alert; it can never report `pass`.
9. **Report.** The worker writes the run report: pass/fail, per-objective and per-inject tables, the range certification record id and `record_sha256`, the plan hash, the exercise's content digest, the build ids of the golden templates used, timings, the policies, and a verdict. The report's digest is part of the preview hash. The admin sees it before and while deciding.

**Outcomes** (one of): `pass`, `fail` (content-attributable, with the stage that owns each finding), `blocked: capacity`, `blocked: images`, `blocked: infrastructure` (hypervisor down, provisioning error not caused by the template), `deferred: draft range`, `not_run: live not approved`. Only `pass` lets the preview gate be accepted for an exercise that is to be Proven. An admin may accept the preview of a `deferred` or `not_run` exercise knowing it will promote as **Not proven**; the chip says why.

**What a dry run does not prove.** That people can learn from the exercise, that the instructor pack is good, or that the scoring is fair to humans. It proves the machinery: the range builds and certifies, the injects fire, the objectives are scorable and unforgeable by a silent player, and the range tears down.

### 10. Proven and the content port

- The content port's **Proven** is computed from three conditions: range certified, used with passing evidence, not stale. This ADR adds to the **second** condition for the new `exercise_template` item type: a **dry-run pass** whose exercise digest, range plan hash and golden-build ids still match the current template and its range's current certification is passing evidence, in addition to a closed class or a completed exercise at 70% or better. It is computed on every display and never stored as "proven". Owner decision 6 makes this the evidence; the chip shows its tier (**Proven by dry run** or **Proven in use**) so that nobody mistakes a machine pass for a taught class. See Q5.
- The evidence covers the template within its min to max role counts and checked steps (section 7a); a resize inside them does not make it stale. An edit to the exercise template, its scenario, its scoring or its range template changes a digest or a plan hash and makes the dry-run evidence stale at once, as an edited range template already makes its certification stale.
- **Export:** the content port gains the item type `exercise_template` with the parts of its record and its dependencies: the scenario, the range template (reuse or drafted), the validators and scoring, the roles and teams, the instructor pack, the debrief template, and the dry-run summary (not the synthetic Students). The existing rules apply unchanged: refused (409) if anything selected or depended on is not Proven, signed with the content-port key, strict JSON, no Students, no credentials, passwords redacted, no tenant ids. The golden-template builds the certified range ran are listed in the bundle; the images themselves travel by the OVF and build-record procedure.
- **Import** lands the exercise **not yet proven here**, as every imported item does: the destination's range must reach Certified, and the exercise must be used or dry-run there. The import runs its own dry run through the same service if the destination site enables the Studio's dry-run service; otherwise it needs a class. A ported exercise whose live inject digests are present arrives with the live flag set and **no approval**: Live approval is never carried by a bundle.
- Where this cannot be reached: an exercise on a **drafted** range exports only after (1) the images exist and are promoted at the work site, (2) the range certifies, (3) the deferred dry run passes. Until then it is Draft and the port refuses it, correctly. See "Can the design reach decision 7?".

### 11. Artefact layout

```
build/exstudio/<slug>/
  request.txt                      written by the runner before the job starts
  manifest.json                    only merge/check/gate write it
  01-blueprint/   outline.yaml  request.md  fit.md
  02-scenario/    scenario.yaml  mesl.yaml  cells/*.md  fragment.json
  03-range/       timeline.yaml  captures/<inject>.yaml|.pcap|.summary.json
                  range.yaml            (mode draft only)
                  live/<inject>.yaml    (stage 3b only)
                  README.md  fragment.json
  04-pack/        instructor_pack.md  role_briefs/*.md  whitecell_runsheet.md  debrief.yaml  fragment.json
  05-scoring/     scoring.yaml  evidence_map.yaml  scenario_engine.yaml  validators/*.md  fragment.json
  06-qa/          report.md  check.json  fragment.json     (+ dry-run summary copied back by the API, read-only for agents)
  07-bundle/      exercise_template.json  parts/*  PROMOTE.md  fragment.json
build/exstudio/_studio/<slug>.json   tenant ownership (API writes; never in the sandbox)
build/exstudio/_queue/ _jobs/ _history/   runner-owned, as ARC²
```

The dry-run record is not in this tree. It is in the database, tied to the run's digest.

### 12. QA checks

`exstudio.check` (deterministic, no model) runs before and after each stage, as `arc2.check` does. Findings carry `check`, `severity`, `owner_stage`, `message`, `path`.

| Group | Check | Fails when | Owner |
|---|---|---|---|
| schema | `schema.valid` | manifest fails `tools/exstudio/manifest.schema.json` | the stage that wrote it |
| trace | `trace.objective_has_serial` | an objective no MESL serial references | scenario-writer |
| trace | `trace.objective_has_evidence` | an objective with no inject or serial that can produce its evidence, or no scoring row | scoring-engineer |
| trace | `trace.serial_has_expected_action` | a serial with delivery `cyber` and no expected action or MoE | scenario-writer |
| trace | `trace.inject_serial_resolve` | an inject with no serial, or a cyber serial with no inject (and not human-delivered) | range-engineer |
| trace | `trace.critical_inject` | a critical event with no critical inject | range-engineer |
| trace | `trace.role_has_node` | a role with a console or machine duty and no node in the range | range-engineer |
| trace | `trace.team_roles` | a team with no role, or a role in no team | exercise-architect |
| range | `range.template_valid` | draft YAML fails the template schema, or reuse names a template that does not exist | range-engineer |
| range | `range.images_listed` | an image used is missing from `images_needed[]`, or marked existing when the catalogue says not | range-engineer |
| range | `range.images_exist_flag` | `exists`, `enabled` or `promoted_build` disagree with the catalogue and this site's registered builds | range-engineer |
| range | `range.no_site_names` | a datacenter, cluster, datastore, vCenter, site VLAN name or site address | range-engineer |
| inject | `inject.defang` | any `DEFANG_PATTERNS` hit, plus the Studio additions, in any text or YAML under the run | owner of the file |
| inject | `inject.captured_only` | a stage-3 inject calls a live action or contains a command line | range-engineer |
| inject | `inject.noise_floor` | too few plausible-noise items for the difficulty (ARC²'s rule) | range-engineer |
| live | `live.registry_only` | a live inject uses anything but registry actions and typed parameters | live-inject-engineer |
| live | `live.targets_in_range` | a target not a node id of the range template | live-inject-engineer |
| live | `live.limits` | rate, duration, target count or effect class above the action's maxima | live-inject-engineer |
| live | `live.flag_derived` | the manifest's `live` flag differs from the flag derived from the registry (the check recomputes it) | live-inject-engineer |
| live | `live.cleanup_declared` | a state-changing action without a cleanup check | live-inject-engineer |
| live | `live.requires_approval` | a live inject present and no Live approval for its digest (a finding, not a failure, until the QA step needs it) | orchestrator |
| scoring | `scoring.validators_known` | a validator not in the engine registry, or `params` outside its schema | scoring-engineer |
| scoring | `scoring.no_free_credit` | a plain detection objective credited by the worker (ADR 0005); detection objectives must be judged on a submission | scoring-engineer |
| scoring | `scoring.answer_key_hidden` | a query or key that references `tn_ground_truth`, or an answer key in a Student-visible file | scoring-engineer |
| scoring | `scoring.points_sum` | points do not sum to `max_score` | scoring-engineer |
| pack | `pack.instructor_only` | instructor material (answer keys, white-cell scripts, live scripts) under a role brief or Student-visible path | pack-writer |
| pack | `pack.debrief_covers_objectives` | a debrief template with no prompt for an objective | pack-writer |
| separation | `separation.course_keys` | any `course`, `module`, `lab_range`, lab profile, cmi5 or `module_content` key | orchestrator |
| tenancy | `tenancy.no_ids` | any tenant id, user id, email, credential or real address in model output | owner of the file |
| identity | `identity.no_framework_tokens` | NICE/DCWF/CSF tokens outside the permitted sidecar (ARC²'s rule) | owner of the file |
| intake | `intake.sizing_present` | a role has no count, a total of 0, or no sizing at all (question, not failure) | exercise-architect |
| intake | `intake.roles_fit_idea` | the idea needs a role the sizing lacks (red-vs-blue with no red), or a MESL `from_cell`/`to_participant` names a role with no participants (question, not failure) | exercise-architect |
| intake | `intake.counts_in_bounds` | a count outside the role's min and max | exercise-architect |
| sizing | `sizing.profile_resolves` | a VM profile whose catalogue id, vCPU, RAM or disk cannot be resolved from the catalogue or the base template | range-engineer |
| sizing | `sizing.render_all` | the render at the minimum, the requested sizing, any step boundary or the maximum fails the template schema, name uniqueness or ordering | range-engineer |
| sizing | `sizing.cidr_fits` | a segment's CIDR cannot hold its rendered machines (plus gateways and reservations) | range-engineer |
| sizing | `sizing.shared_infra_scales` | a shared-infrastructure rule that does not grow as the sizing crosses its step (a single sensor for two segments, one DC for 300 users) | range-engineer |
| sizing | `sizing.every_role_seated` | a role with no seat pattern or no machine, or a profile used by no role or rule | range-engineer |
| sizing | `sizing.base_untouched` | the overlay edits the existing range template instead of deriving from it | range-engineer |
| capacity | `capacity.reported` | no capacity report for the current sizing and digest (at the dry-run step and at Promote) | orchestrator |
| gates | `gate.*` | accepted inputs edited after acceptance (hash mismatch) | orchestrator |
| dry run | `dryrun.present` | no dry-run record for the current digest, when one is required to proceed | orchestrator |
| dry run | `dryrun.pass` | outcome is `fail`; each finding names its owner stage | per finding |
| dry run | `dryrun.stale` | the report's digest differs from the candidate's | orchestrator |

Static checks run in the sandbox as ARC²'s do. The `dryrun.*` checks read the database record through the API at the time the orchestrator asks (the orchestrator in the Studio's case is the runner's resume step plus the API; see "Failure and human takeover"), never from a file an agent could write.

### 13. What the generator must never produce

This list binds all eight agents, is enforced by `exstudio.check` and by the live executor at run time, and is repeated at the top of every agent's rules. A finding against it is `severity: block`, is never auto-retried silently, and counts as a QA failure.

1. **Executable text of any kind that the registry did not author.** No shell, PowerShell, batch, Python, macro, script block, one-liner, encoded run (`-enc`, base64/hex runs), reverse shell, or `Invoke-*`/`/dev/tcp`-style content. Live behaviour is registry calls with typed parameters. The existing `DEFANG_PATTERNS` apply to every file, and `AUTHOR-REQUIRED` stands in for anything that needs real tradecraft.
2. **Anything that targets a system outside the range.** No address, CIDR, hostname or URL that is not (a) a node or network of the range template, (b) a Greyspace name, or (c) a reserved documentation name (`example.*`, `.test`, `.invalid`, `.example`, RFC 5737/3849 addresses). Never the management network, the build network, the depot, vCenter, ESXi hosts, DNS or NTP servers of the site, the control plane, Student consoles or any real internet host. A live action's `targets` are node ids, resolved at run time against the range's recorded inventory.
3. **Persistence or effect that outlives teardown or a baseline revert.** Nothing that writes to a hypervisor, datastore, vCenter object, snapshot, golden template, host-local template copy, VM-tools or guest-operations configuration, firmware, shared storage or the depot; nothing that must survive a snapshot revert; nothing scheduled to run after the exercise ends. Each state-changing action declares cleanup, and the dry run proves the revert removed it.
4. **Self-propagation and unbounded effect.** No worm-like spread beyond declared targets; no unbounded loops, rates or durations; no denial of service on shared infrastructure; every live action carries `max_rate`, `max_duration` and `max_targets`.
5. **Real malware, working weaponised exploits, real stolen data or real credentials.** No functional ransomware (no real encryption with a key anyone holds), no samples, no exploit chains for unpatched or in-the-wild vulnerabilities, no real personal data, no real organisation, brand or person as victim, nothing derived from classified or CUI material unless the run is an enclave run (`ARC2_ENCLAVE=1`) and the source rules of the content pack allow it.
6. **Anything that touches TrueNorth itself.** No call to the TrueNorth API, the control plane, the telemetry store's write path, the range credentials store, the guest-operations channel as an attack path, or the Student console. No instruction to disable the isolation probes, the certification, the kill switch or the audit log.
7. **Its own approval.** It may not set `live`, `approved`, `proven`, `certified`, `promoted`, a gate's state, a dry-run result or a human action's status to a passing value; the checker derives or ignores them.
8. **Image or catalogue claims it cannot back.** No catalogue id invented as if it existed, no `enabled` image claimed that is not, no build, edit or promotion of a golden template, no credential, key or secret in any file.
9. **A live inject without disclosure.** A live inject missing from the front page of the instructor pack, the run report or the role briefs' notice.
10. **Course coupling.** No link into courses (section 3).
11. **Student-visible leakage.** No answer key, ground-truth label, scoring query, live script or white-cell script in a Student-visible file.

If a request can only be satisfied by something on this list, the stage writes `{"stop": "..."}` or an `AUTHOR-REQUIRED` action; it does not approximate.

### 14. Models

The ARC² models rule applies: Claude (`subscription`) or an Anthropic-compatible gateway (`local`, TAZ). No Chinese models except GLM. The runner records `served_model` for every step; for `kind: exercise` it **checks that name against a deny pattern after each step** (Qwen, DeepSeek, MiniMax, MiMo, Kimi/Moonshot, Yi, Baichuan, InternLM, ERNIE and similar; GLM is exempt as the owner decided) and fails the job closed on a match, flagging the run. The pattern list lives in the repository and changes only by ADR. Wording of the exception needs the owner's confirmation (Q4).

## Security and tenancy model

- **Ownership:** every run belongs to the tenant recorded in `build/exstudio/_studio/<slug>.json` at creation (exclusive create). Every route resolves the slug through the owner check; another tenant's run is a 404 and nothing is queued or appended. Admins are tenant-scoped; the platform admin reads across tenants only where `docs/security.md` allows it. Runs with no owner are listed to nobody. (ARC² section 11, same code.)
- **Request text is untrusted.** It may not contain `--slug` or `--resume` (422), is length-bounded, and drives an engine whose tools include arbitrary Python, so the same OS sandbox applies: write only the run directory, read the repository except `.env*`, no other run, no `_studio/`, no sockets, egress only to the model endpoint.
- **Run files are untrusted output.** Reads are bounded (2 MB per file, 200 MB per bundle), YAML goes through `safe_yaml` (no aliases), nothing follows a link, and paths are validated one component at a time with `O_NOFOLLOW`.
- **The dry run is the platform's, not the agents'.** The agent sandbox has no route to the hypervisor, the API or the database. The record cannot be written by a job. The preview hash binds the report.
- **Dry-run objects are tenant-owned and labelled.** The range, the synthetic Students and the exercise carry the requesting tenant and `synthetic`/`purpose` markers, are filtered out of all human-facing views, and cannot be enrolled, booked or exported.
- **Live approval** is a separate permission, a separate screen, a separate person, per-digest, MFA step-up, audit-logged (ADR 0011 integrity applies).
- **Promotion is the only door** from `build/exstudio/` into the database, and it is a human act through code that revalidates. Studio code has no import path into `Course` tables.
- **Bundles:** signed with the content-port key; purpose-separated context line; nothing carries Students, credentials, tenant ids or approvals.
- **Audit:** `exercise_studio.request`, `.gate_accept`, `.live_approve`, `.dry_run_*`, `.promote`, `.takeover` are audit rows with digests.

## Failure and human takeover

- **Counted failure:** a `check` failure whose cause is content: a static finding or a dry-run `fail`. Three counted failures end the run for a human (`qa.result: human_takeover`), as ARC². The budget covers static and dry-run failures together, so a flapping exercise cannot loop through range builds.
- **Not counted:** missing information (`Needs input`: the run waits for the requester's answers to the architect's sizing questions) and infrastructure. `blocked: capacity`, `blocked: infrastructure`, a runner crash, a model outage (`FALLBACK_ERRORS`), and a refused merge due to ordering. These leave the run in a `waiting` state with a reason and a Retry action for `exercise_studio:approve` or range ops. After two infrastructure retries of the same step the run goes to takeover too, so nothing retries forever.
- **Dry run cost:** a failed dry run costs a range build, so after a content `fail` the stage rework runs static QA first, and the dry run starts again only on a static pass.
- **Takeover:** exactly ARC²'s semantics. The run directory is the human's to fix by hand (download, edit, restore through the admin CLI `scripts/exstudio_takeover.py`), then `resume accept` merges the fragments the human left, runs static QA, and goes on. `qa.cycle` stays at 3 until a pass. A takeover never skips the dry run, never carries over a previous report (digest mismatch), and never carries Live approval.
- **Resumability:** all state is `manifest.json` plus the runner's job records; a restarted browser, API or runner does not restart generation. A dry run in progress when the worker dies is resumed or torn down by its lock and lease: the lease's maximum lifetime guarantees teardown, and a janitor marks the record `failed: abandoned` and reconciles inventory.
- **Cancel:** the requester or an admin can cancel a run. Cancelling during a dry run tears the range down through the same teardown path.
- **Rejection:** the admin can reject at the preview gate with feedback routed to a stage by ownership (ARC²'s routing table, with the Studio's rows), which re-runs that stage and everything after and re-opens the gate.

## Can the design reach decision 7?

Yes, with five conditions, each stated as a constraint rather than hidden.

1. **The exercise template.** The record is in flight and has no commits or PR of its own as of 2026-10-28. The Studio needs from it: a stable id and version, a content digest over its parts, references to a scenario and a range template, objectives with MoE, MESL serials, teams and roles, free parts for the instructor pack and debrief template, a provenance field, and an "instantiate" operation a collective class session can use. Ticket S7 maps these to the real record. Anything the record lacks is stored by Studio as typed parts keyed by the record id and digest (`exercise_template_parts`), never by changing the record's shape from this module.
2. **The debrief/AAR template.** Today the AAR is generated per run (`aar_report.py`), with no template concept. The Studio's debrief template is data (sections, prompts, evidence to attach) that the AAR generator can read when present. Ticket S5 adds that reader; without it the template is delivered as a document in the instructor pack.
3. **Content port.** `exercise_template` is a new item type. It needs #324 merged and the template record merged. Ticket S8.
4. **Live attacks.** They cannot ship until `RANGE_ISOLATION_CHECK=enforce` is decided and turned on (ADR 0013 section 4). Everything else ships without them. D0 asks the owner for the decision; S10 and S11 wait for it.
5. **Drafted ranges.** An exercise on a drafted range cannot be Proven, and therefore cannot be exported through the port, until images exist and the range certifies at the work site. The design says so and shows it as "Draft" with the list of what is missing; it does not weaken the port's rule.

## Risks

| Risk | Mitigation |
|---|---|
| Live scripts escape the range | Registry-only actions; targets by node id from the recorded inventory; isolation Isolated and re-probed; `enforce` required; kill switch; separate approval; the never list; adversarial review of S10 and S11 |
| Isolation `enforce` is not decided | Live is gated on it; captured-only ships first |
| A prompt-injected request makes the engine misbehave | The ARC² sandbox; bounded reads; the checker is deterministic; the dry run is outside the sandbox; no auto-accept |
| Dry-run evidence is overrated as proof | Tier on the chip; two-policy test only proves machinery; the AAR and real classes still produce "used" evidence |
| Dry runs starve classes | Capacity gate admission against whole windows, short lifetime, never preempt, max wait, `purpose` label and alarm if more than N per day |
| Orphaned dry-run ranges | Lease with hard lifetime, inventory-identified teardown, janitor, range-ops alert |
| Drift between `tools/arc2` and `tools/exstudio` | Copy, not fork; a convergence ticket after GA; shared sandbox primitives only |
| The template record's shape differs from this design | S7 maps; typed parts as a fallback; no change to the record from the Studio |
| Cleared-admin concept is unspecified | Permission plus attestation in v1; owner decision D0 |
| Synthetic users leak into analytics or evidence | `synthetic` filter in the same places `participants()` already centralises; tests per view; excluded from port evidence |
| A model deny-list is evaded by an alias | Check the served model name; the router config is pinned by ADR; fail closed on unknown names for `kind: exercise` when the list is non-empty |
| Time compression changes behaviour (a rate-based detection never trips) | The dry run proves machinery, not tuning; compression is a recorded parameter; detections in the evidence map must state their window in scenario-time |
| Sizing renders wrongly at a scale nobody built (a CIDR too small, a rule that does not step) | Deterministic renderer; `sizing.render_all` renders min, requested, every step boundary and max; the dry run builds the minimum only, so full-size problems are caught statically, and each real class range certifies on its own build |
| A resize silently takes capacity from approved classes | Any demand change sends the booking back to approval; approvers see old and new demand; the capacity gate checks again |
| Role-aware teams break #299's balanced split | Balanced split stays for exercises without roles; role-aware code is a new path with its own tests |
| Headcount is guessed by the model | The model asks (Needs input); counts come from the requester or the roll call; the renderer computes VMs |
| Scope creep into courses | `separation.course_keys` in the checker and the importer; no course tables in the module's imports (MOSA check) |
| ADR 0003 ratchets | New code in `app/exercise_studio/`, `worker/exercise_studio_dry_run.py`, `tools/exstudio/`; none in `models.py`, `schemas.py`, `worker/tasks.py`; no raw SQL in the worker beyond the existing seam; no new `HttpClient` in Angular `features/` |

## Open questions

Each has a resolution I would adopt if forced to answer today.

| # | Question | If forced |
|---|---|---|
| Q1 | What does "cleared admin" mean in TrueNorth? RBAC has no clearance. | A named list of admins holding `exercise_studio:approve_live`, granted by the platform admin, with a signed attestation on each approval and MFA step-up. Revisit if the programme has a formal clearance register. |
| Q2 | Must the live approver differ from the preview approver and the requester? | Yes, both. Two-person for live is cheap and the downside of one person approving everything is the whole risk. Relax later by ADR, not silently. |
| Q3 | Are live "scripted attacks" declarative sequences of reviewed registry actions, or may the generator write free-form guest scripts? | Declarative only in v1 (section 6). Free-form is `AUTHOR-REQUIRED` for a cleared human to add to the registry by PR. This narrows decision 5; the owner confirms. |
| Q4 | The models rule: "no Chinese models except GLM". The owner's standing rule elsewhere also excludes GLM pending a decision, and `CLAUDE.md` lists glm-5.2 and Qwen as write-capable. | Follow the brief for the Studio: GLM allowed, Qwen and the rest denied, deny list in the repository. Confirm the exception in the D0 ticket and align `CLAUDE.md`. |
| Q5 | Does a dry-run pass alone make an exercise Proven, or is it a weaker tier than a closed class? | It counts as the owner decided, shown as a distinct tier, **Proven by dry run**, with the export bundle carrying the tier so the destination can require use. |
| Q6 | Does the dry run use time compression, and is compressed-time scoring honest? | Yes, default whole timeline in 20 minutes, recorded; the dry run proves machinery only. Add real-time soak for live exercises only if the owner asks. |
| Q7 | Where does a hosted model get exercise text on a CUI site? | `local` mode on TAZ at CUI sites (ARC²'s rule). The site enables `subscription` knowingly. The Studio shows which engine ran each stage. |
| Q8 | Who owns promoted templates? Courses and exercises have no owner; the calendar is the record (glossary: Teaches/runs). | The requesting tenant owns the record; `created_by_run` provenance names the requester; "run" permission stays the calendar's. No new ownership concept. |
| Q9 | Is the dry run required for exercises using only existing, certified ranges and captured injects, or optional? | Required to be Proven; an admin may promote without it as Not proven, as for a deferred run. |
| Q10 | Should Exercise Forge be retired or become a Studio entry? | Leave Forge alone; revisit after the Studio ships. Mark the two clearly apart in the UI and glossary. |
| Q11 | Can a ported exercise's dry-run evidence be re-checked at the destination without a hypervisor? | No. The destination needs its own certification and use. The port says "Proven at origin" only. |
| Q12 | Should live injects be allowed in a class exercise at all, or in dry run and rehearsal only for v1? | Allowed with all conditions, off by default per exercise: an instructor arms live for a session at booking. If the owner prefers, ship rehearsal-only first by making S11's class arming a separate flag. |
| Q13 | How should a role count be chosen when the roll call and the planned roles disagree? | The lead edits role counts; the roll call only proposes the default; a disagreement is shown, never blocked. Seats are what size the range, not the roll call. |
| Q14 | Does a smaller headcount also go back to approval? | Yes in v1: any change of demand does, because the held window and the rendered template changed. Revisit with a one-click "approve reduction" if instructors find it heavy. |
| Q15 | Is the Proven dry-run evidence valid for sizes the dry run did not build? | Yes, within the template's min to max and its checked steps, because the digest is unchanged and the scaling is checked statically; each real range still certifies on its own build. Say so on the chip. |
| Q16 | Is there an upper bound on participants per exercise? | The site's capacity and tenant quota decide (no VM cap on labs, per the owner's earlier rule); the schema keeps a sanity bound (500 participants) that an admin can raise by setting. |
| Q17 | Can a resize after the range is built add VMs in place? | No. Rebuild before the next session, or refuse in the provisioning lead window. In-place growth is a provisioning feature the platform does not have. |
| Q18 | Which numbers feed "free capacity" before a booking exists? | Both: total supply (can this ever run here) and the gate's free capacity over the window (can it run now), reported separately. |

## What this changes

- **ARC² code:** one additive change to the runner (`kind`), nothing else.
- **Content port:** a new `exercise_template` item type and a new evidence source (`dry_run`) for the "used with passing evidence" condition. The port's doc line "Exercise templates are not ported" changes when S8 lands.
- **Scenario inject execution:** a second `execution_mode: "live"` and a live executor; `docs/scenario-inject-execution.md` is updated by S10.
- **Range templates:** a draft state that refuses range creation and booking, and derived (sized) templates with a `derived_from` link.
- **Classes:** a session sizing record; a role-aware team split (`classes/teams.py`, #299) and a role on the team membership; any change of demand returns a session's booking to approval.
- **AAR:** an optional debrief template reader.
- **RBAC:** four permissions.

It changes nothing about courses, cmi5, Moodle, lab sessions or the career-path views.

## Delivery

One issue and one PR each. Blocking edges are on the issues (GitHub dependencies, plus a `Blocked by` line). All carry the label R1. Slices marked AR need `adversarial-reviewer` before merge (CLAUDE.md: auth, tenancy, provisioning); any slice touching a range also gets `range-reviewer`. Every slice passes the gate (`bash scripts/dod.sh`); anything with a UI also runs `DOD_WEB=1`.

| Id | Issue | Slice | Blocked by | AR |
|---|---|---|---|---|
| D0 | TBD | Owner decisions: cleared admin, live approver separation, isolation `enforce`, GLM wording, free-form scripts | none | n/a |
| S1 | TBD | Tracer: request to outline gate, end to end (web request form with participants per role, API, runner kind, sandbox, model deny list, architect agent with the Needs input sizing loop and `intake.*` checks, tenancy) | this ADR merged | yes |
| S2 | TBD | Scenario, MESL, delivery cells mapped to roles (stage 2) | S1 | no |
| S3 | TBD | Reuse an existing range, captured inject timeline, defang checks, image inventory (stage 3) | S2 | range-reviewer |
| S4 | TBD | Scoring and evidence map (stage 5), static QA report, preview gate (admin) | S3 | no |
| S5 | TBD | Instructor pack, role briefs, debrief template (stage 4), AAR template reader | S4 | no |
| S6 | TBD | Dry run: capacity admission, build at minimal size, certify, simulated players, report, teardown, hash into preview | S4, S12, #311, ADR 0013 S4 to S6 | yes |
| S7 | TBD | Bundle, Promote, exercise template record mapping, separation checks | S5, S6, the template-record PR | yes |
| S8 | TBD | Proven by dry run and the content port item type `exercise_template` | S7, #324 | yes |
| S9 | TBD | Drafted range templates: images needed and exists, draft state refuses creation and booking, deferred dry run | S3, S6, S7 | yes |
| S10 | TBD | Live substrate: action registry, executor, isolation re-probe condition, kill switch, flag off | S6, D0 | yes |
| S12 | TBD | Sizing: role definitions, seat patterns, scale rules, deterministic renderer, derived templates, capacity report ("needs N VMs..., biggest size that fits") | S3 | no (range-reviewer) |
| S13 | TBD | Resize and roster: session sizing record, re-approval on change, role-aware team split and role on membership (#299) | S7, S12, #299 | yes |
| S11 | TBD | Live generation: stage 3b agent, `live.*` checks, Live approval gate, flagging everywhere, class arming | S7, S10 | yes |

Slices that must be adversarially reviewed: S1 (runner, tenancy), S6 (dry run: capacity, provisioning, teardown, synthetic users), S7 (promotion importer), S8 (extends the port's import trust boundary), S9 (provisioning gating), S13 (booking approval and team assignment across students), S10 and S11 (live attacks). S2, S4, S5 and S12 are content, UI and pure-computation slices and take the ordinary `tn-change-review`; S3 and S12 take `range-reviewer`.
