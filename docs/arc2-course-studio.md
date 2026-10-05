# ARC² Course Studio and TrueNorth LMS integration

> **Status:** plan of record, v3 (2026-09-26), validated against the repository the same day
> (§10). It amends the ARC² engine plan v2 (2026-09-24) and separates course *authoring*
> from training *delivery*. Slice A (the authoring engine) is being built on branch
> `claude/arc2-ai-rapid-cyber-8d8a3e`; slices B–F are design only. This document does not
> certify the branch, deploy a Studio UI, validate prerequisites, or publish a course.

## 1. Product boundary

ARC² is the **Course Studio** module. It creates, reviews, validates and packages training.
The LMS delivers approved releases, manages enrolment and attempts, and records learning
outcomes. The two share identity and integration contracts, not ownership of live course
records.

The decision in `docs/moodle-integration.md` stands: Moodle is the student-facing LMS;
TrueNorth supplies curriculum views, content, exercises and integration services. MITE
remains the qualification record of authority. TrueNorth may display recorded qualification
status, but neither generation nor LMS completion grants a qualification.

"Separate module" means separate responsibilities and lifecycle, inside the existing
TrueNorth application and repository. No new deployed service is required.

| Capability | Owner |
| --- | --- |
| Requests, draft content, quizzes, labs, rubrics, revisions | ARC² Course Studio |
| Authoring validation and the two content review gates | ARC² orchestration and authorized reviewers |
| Approved QSP/PO/EO bindings, prerequisites, equivalencies, DP placement | Curriculum governance; ARC² submits proposals |
| Release validation, destination mapping, publication receipts | TrueNorth publishing integration |
| Enrolment, delivery attempts, gradebook and completion decisions | The configured delivery LMS (Moodle) |
| Range execution and evidence production | TrueNorth range and scenario services |
| Learning statements | LRS, correlated to delivery attempts |
| Granted qualifications | MITE |

Students see approved training and their next steps. Instructors create and revise courses.
Administrators configure integrations and publication permissions. Publication and
curriculum approval are capabilities assigned through RBAC, not privileges every instructor
holds. (Commander and event-coordinator views are future roles; see §10.)

The student career-path presentation is an open decision (§10, correction 2). Its
relationship with Moodle is settled in the delivery slice, without creating a second
authoritative enrolment or gradebook.

## 2. The authoring engine (slice A)

Finish the v2 engine as a bounded phase:

- Beau's seven agent names and ownership boundaries.
- Outline acceptance and preview acceptance, feedback routing, hash checks, bounded
  automatic QA retries, and human takeover.
- Staging under `build/arc2/<slug>/` with human promotion. Generation agents never
  provision, commit, import live, or call an external or cloud API.
- cmi5 as the only interoperable package output, alongside the TrueNorth import artefacts
  and `PROMOTE.md`. No SCORM, and no inbound import of Beau's packages, in this phase.
- The existing curriculum eligibility rules, AUTHOR-REQUIRED handling, defang checks,
  separation of instructor material, and manual summative review.
- Framework identifiers stay in the permitted sidecar (`04-artifacts/xapi.json`), outside the
  student package.
- Unresolved XSD validation, provisional publisher identifiers and similar findings are
  explicit outstanding actions. **QA with open human actions is a package candidate, not an
  approved production release.**

v2's "no new API surface" now reads: **no new API surface in the authoring engine phase;
Studio and delivery integration are later phases.** Generation agents never publish; a later,
authorized publishing service does.

## 3. One course journey, two lifecycles

Authoring:

`draft → generating outline → outline review → generating content → QA → preview review → package ready`

v2's feedback and retry transitions still apply. A failed or interrupted run resumes from
its recorded stage state; it does not silently repeat completed work.

Publishing, tracked separately for each destination:

`not requested → validating release → ready for publication → publishing → published`

Validation may return `needs action`; publication may return `failed`, or
`reconciliation required` when the remote result is uncertain. Withdrawal is a recorded
operation on a published release, not deletion of student history.

The two authoring gates are unchanged. **Publish to LMS** is a separate, authorized action
after content acceptance, not another generation stage. The publishing service rechecks the
release digest (§10, correction 4), curriculum approval and destination readiness before it
changes delivery state.

The intended instructor journey, once slices B–E exist:

1. Course Studio submits a request to the authoring API.
2. A durable job records the project and run identity and invokes the engine (§10,
   correction 5: how is not yet decided).
3. Studio shows stage progress and the two reviews, using the same gate operations as the
   CLI. Restarting the browser does not restart generation.
4. An authorized publisher selects an accepted release and a configured LMS destination.
5. The integration validates it, records the publication job, and stages the import.
6. Only successful activation produces a publication receipt and makes the course visible
   to students.
7. Student launch and outcomes use delivery identity and attempt correlation.
8. Instructors receive feedback linked to the release and may start a new draft.

## 4. The release contract between Course Studio and delivery

Keep the internal ARC² manifest for agent coordination. After the engine phase, introduce a
separate, versioned **release descriptor**, so delivery does not depend on agent directories
or prompt conventions. The Package Builder emits a candidate; the release service validates
it and freezes the approved descriptor and artefacts.

| Field group | Required meaning |
| --- | --- |
| Identity | `schema_version`, a stable opaque `studio_course_id` (§10, correction 6), an immutable `release_id`, a display version, the originating project and run IDs |
| Ownership | Tenant and issuer identity, assigned and verified by trusted services, never taken from model output |
| Description | Title, audience, duration, language, entry requirements |
| Artefacts | Package location, release digest, per-file checksums, an explicit student / instructor / runtime audience per file |
| Objectives | Stable objective IDs, source references and versions, proposed versus approved bindings |
| Curriculum proposals | DP and semester placement, prerequisite course IDs, rationale and evidence; the approved curriculum revision is referenced separately |
| Review evidence | Outline and preview accepted digests, reviewer identity and time, QA result, unresolved actions, the applicable curriculum approval |
| Provenance | Source revisions, generator and tool versions, creation time; no credentials and no unnecessary student data |
| Compatibility | Package schema and tested delivery capabilities; target-specific validation stays part of publication |

`release_id` and the release digest are immutable: a changed package is a new release. The
release identity is distinct from publisher activity IRIs and from LMS-issued runtime
activity IDs. v2's publisher-ID rules still apply; never use a runtime activity ID as a stable
course identity, and never change publisher IDs only to encode a display version.

A separate **publication record** holds `publication_id`, destination, course and release
identity, package digest, curriculum revision, publisher, job state, idempotency key, remote
course and activity IDs, timestamps, and structured findings. It is mutable job state with an
audit trail, not part of the immutable package.

Define the CLI and API requests and errors against this contract before wiring any buttons.
The first adapter targets the existing TrueNorth course artefacts; Moodle activation is a
separate adapter. A cmi5 ZIP on its own is not evidence that a destination supports import,
launch, assessment and completion correctly.

## 5. Curriculum and prerequisites are reviewed data

ARC² may propose a relationship; it cannot create an authoritative curriculum rule. Record
course dependencies as explicit prerequisite → dependent edges, each with source, scope and
curriculum revision. Validate the referenced course identities, reject cycles, and tell
prerequisites apart from recommendations and co-requisites. Define supported AND/OR groups
and equivalency rules before accepting any; reject unsupported expressions rather than
silently turning them into stricter rules.

Semester order does not establish a prerequisite. DP placement and eligibility are separate
fields. Completing a course does not necessarily satisfy every skill or qualification
requirement associated with it. **Today's code does derive prerequisites from order; see
§10, correction 1.**

Only approved relationships drive student blocking and prerequisite arrows. Draft proposals
may appear in an instructor preview, clearly labelled. DP 3–5 concepts stay visible as
concepts and excluded from authoritative qualification and readiness calculations until
approved source material exists (`docs/dp3-5-curriculum-proposal.md`).

## 6. Publication, versioning and recovery

- Existing student attempts stay pinned to their enrolled release. Updating a course creates
  a new draft and release; moving existing students needs an explicit, audited migration with
  documented progress and equivalency rules.
- A new release becomes the default for new enrolments only after successful publication.
  Earlier releases keep access and history according to the delivery policy. Withdrawal
  blocks new enrolments without deleting prior evidence.
- Before activation, reject stale approvals, changed package digests, unresolved blocking
  findings, unauthorized curriculum mappings and incompatible destinations.
- A repeated request with the same tenant, destination, release and idempotency key returns
  the existing job. A reused key with different content is a conflict.
- Stage local imports transactionally. For remote LMS operations, persist each step and
  reconcile ambiguous results before retrying creation. There is no single database
  transaction across TrueNorth and Moodle.
- A course stays unavailable to students until the required import and activation steps
  complete. A failure keeps the previous active release, with retry and reconcile actions
  and clear ownership. Partial destination IDs are logged for cleanup or recovery.
- Server-side tenant and permission checks, configured destination credentials, validated
  package paths and a constrained preview environment. Generation agents never hold LMS
  publishing credentials.

The current content importer updates course and module records in place and unpublishes the
course (`course_content_ingest.py:384`). Do not attach a Publish button to it; first add a
release-aware staging adapter that preserves active delivery and enrolled students.

## 7. Student experience and results

The LMS launch context correlates tenant, student identity, course release, module,
registration and attempt. Per-destination ID mappings are recorded explicitly. When results
are projected into TrueNorth, the authority's original results and provenance are kept.

Choose one authoritative completion and grade source per activity, and deduplicate by stable
event and attempt identifiers. Phase 1 (LTI AGS) and Phase 2 (cmi5) of
`docs/moodle-integration.md` would otherwise both report. Test replay, late results, wrong
release references and cross-tenant rejection before outcomes feed readiness views.

Client-graded quizzes remain formative. Instructor and manual assessment decisions stay
separate from them. Range telemetry alone does not grant a qualification. ARC² receives
authorized, release-linked feedback and aggregate improvement signals; it needs no
unrestricted student transcripts and no permission to rewrite grades.

**Studio placement:** add **Courses** to the existing Authoring Studio
(`app.routes.ts`, hub `authoring`, beside Ranges, Scenarios, Detections, MESL, Forge and
Content), with project views **Overview, Outline, Preview, Assessment, Lab, Validation**. Raw
files and code live in an advanced view. The seven agents appear as stage status, not as
separate menus. The LMS catalogue and the career path deep-link to the authorized delivery
view.

## 8. Delivery slices

One owner maintains the shared schemas and lifecycle transitions within each slice. Paths are
likely change locations, not claims that the integration code exists.

| Slice | Scope and likely files | Acceptance evidence | Migration and recovery |
| --- | --- | --- | --- |
| **A. Authoring engine** | `tools/arc2/`, the seven `arc2-*` agents, `/arc2`, `tests/arc2/` | Both human gates, hash invalidation, routed rework, three-failure takeover, dry runs A/B, no tracked content changes during a run | No LMS migration; resumable staged runs |
| **B. Release boundary** | Release schema and validator under `tools/arc2/`; API models and schemas; release persistence; adapter tests; the reviewed prerequisite model; new RBAC permissions | Freeze one accepted release; reject changed digests, missing approval and malformed prerequisites; a release cannot be overwritten | Additive migration under `control-plane/api/alembic/`; existing courses become explicit legacy baselines, without invented historical approval |
| **C. Manual delivery pilot** | Release-aware adapter around `course_content_ingest.py`; curriculum and progress integration; `PROMOTE.md`; a Moodle integration runbook (not yet written) | Publish a fixture to an isolated delivery environment; launch a real module; confirm completion and results; old-release students continue | Staged separately; the active release survives a failure; remote receipts reconciled before retry |
| **D. Course Studio** | Angular authoring course feature, `app.routes.ts`, authoring API and jobs on existing worker conventions | Create a request, review the outline, rework the preview, resume after worker or browser restart, show package readiness and outstanding actions | Durable job state; deployed behind a feature flag with the CLI as fallback |
| **E. Publish to LMS** | Publication API and job, destination adapter, RBAC, audit, Studio action | A double click or retry creates one publication; unauthorized requests fail; changed approval blocks activation; an interrupted remote import recovers | Additive job and mapping records; new publications can be switched off without touching student delivery |
| **F. Readiness and improvement loop** | Existing curriculum, course and progress, integration and reporting surfaces | Students see their approved eligibility path and pinned content; event replay produces one outcome; instructor feedback opens a draft without changing the live course | Versioned projections and replay rules; MITE decisions and historical evidence are never rewritten |

Order: A → B → C. D depends on A and B and can use C's manual handoff. E follows C and D. F
follows verified delivery and outcome correlation. Do not automate publication in the UI
before the import and version boundary is proven.

## 9. Verification and open inputs

v2's engine checks, adversarial review scope, fault injection and DoD commands still apply.
Later slices add contract tests, migration tests and complete browser journeys. Static
package checks do not replace the runtime smoke test: launch, pages and quiz, completion,
pass/fail, termination, reload and duplicate-result handling against the chosen LMS and LRS.
Record what was actually run, and where.

Open inputs: Beau's `course-config.json` and `cmi5.xml` samples; the vendored XSD with its
licence and dependencies; which Moodle cmi5 activity plugin is used and what it has been
tested to do; runtime identity handoff; source-backed curriculum approval. None of these
blocks slice A. Slice B/C must decide how existing records acquire a release identity and
where the student career-path view is reached from Moodle.

## 10. Evidence and corrections (2026-09-26)

### Where slice A stands

| Item | State |
| --- | --- |
| Branch / worktree | `claude/arc2-ai-rapid-cyber-8d8a3e` in `.claude/worktrees/arc2-ai-rapid-cyber-8d8a3e`; also on `origin` |
| Committed | `468b18e` ruff ratchet fix · `da36481` run contract (PR 1) · `3f34e97` cmi5 emission and contract hardening after adversarial review (PR 2) · `c92905a` QA depth (PR 4) |
| PR 3 (committed with this document) | the seven `arc2-*` agents, `/arc2`, the `docs/AGENTS.md` section, `check --dry-run`, and the contract fixes an adversarial review of PR 3 required: step-7 rework, pre-package checks at QA, preview re-open on upstream rework, sha256 digests exempt from `qa.defang` |
| Checks run | DoD green at `c92905a` (826 passed, 1 skipped; ruff 28/28); DoD green on the PR 3 tree (835 passed, 1 skipped; ruff 28/28); `tests/arc2` 112 passed, 1 skipped |
| Not yet verified | an end-to-end `/arc2` run with the real agents (dry runs A and B); XSD validation (schema not vendored); a runtime cmi5 launch against an LMS/LRS |
| Not on the main checkout | `tools/arc2/` is absent on `feat/cyber-operator-curriculum` until this branch merges |

### Confirmed against the repository

- Route hubs `authoring` ("Authoring Studio") and `learning` exist
  (`control-plane/web/src/app/app.routes.ts:57,126`).
- The content importer updates in place and unpublishes (`course_content_ingest.py:384`).
- `Course`, `Enrollment` and `LearningPath` carry a nullable `tenant_id`; `Enrollment` points
  at a course (unique per user and course), so nothing pins a student to a release today.
- DP 3–5 is proposal-only; the catalogue holds `dp_order` 1 and 2.
- The framework sidecar is what the engine does: `xapi.json` stays in `04-artifacts/`, and
  `cmi5.no_framework_tokens` scans the package.

### Corrections

1. **Prerequisites are already derived from course order.** `qsp_paths._linear_prereq`
   (`qsp_paths.py:366`) chains each learning path's courses in order; `course_prereq_edges`
   (`:413`) serves those edges and `routers/qsp.py:810` sends them to the UI as arrows. This
   contradicts §5. Slice B must relabel them as sequence (or stop drawing them) until approved
   edges exist, and migrate `LearningPath.prerequisite_graph` and
   `Course.course_meta["prerequisites"]` (both untyped JSON) into the reviewed edge model, so
   there is one source.
2. **The career-path design is an open decision.** Commit `c5f4a5b` (2026-09-23) replaced
   the DP grid with SVG arrows by a collapsible list per qualification, calling the grid "a
   wall". The 2026-09-26 navigation mockup (not in the repository) brings back wide DP columns,
   semester cards and arrows. Decide before slice C/F; it does not affect slice A.
3. **RBAC has none of the capabilities this plan assumes.** The roles are `admin`,
   `instructor`, `range_ops`, `student` and `observer` (`rbac.py:107-184`); there is no
   commander or event-coordinator role and no course, curriculum-approval or publish
   permission. `POST /courses/import-programme` and `/courses/import-course-content`
   (`routers/courses.py:354,384`) require only a signed-in user. Slice B adds permissions
   such as `course:author`, `curriculum:approve` and `course:publish`. The import endpoints
   should be gated now, as a separate security fix.
4. **There is no accepted package digest yet.** The preview gate hashes the stage inputs
   (`02-content` … `05-sensor` plus stage keys); the package is built after acceptance. Per-file
   sha256s exist in the manifest's `files[]`. Slice B defines the release digest as the
   preview digest plus the sorted `(path, sha256)` list of the package and bundle files, which
   makes `release_id` content-addressed.
5. **The engine cannot yet run from a server job.** The seven agents are Claude Code
   subagents driven by `/arc2` in an interactive session; a Celery task cannot launch them,
   and the content-pack rule (no external or cloud API; CUI stays on-box) rules out a cloud
   model in the enclave. Before slice D, choose one of:
   - a headless harness (`claude -p` or the Agent SDK) against an on-premises model (Taz); or
   - porting the stages to `ai-orchestrator`, which already has `/ai/course-generate`
     (unauthenticated) and ollama, openai and anthropic backends.
6. **Name collision.** The release descriptor's stable ID is `studio_course_id`, because the
   manifest's `cmi5.course_id` is already the publisher IRI.

### Smaller notes

- Files carry a `kind` but no `audience`; instructor material is separated by directory name
  only (`cmi5.no_instructor_content`). Slice B adds `audience`.
- The Moodle integration runbook named in slice C does not exist yet; only the design
  (`docs/moodle-integration.md`) does.

## 11. Run ownership (2026-10-04)

A Studio run belongs to the tenant recorded as `tenant_id` in `<runs>/_studio/<slug>.json`.
`POST /arc2/runs` records the caller's tenant on the run and on every queued job. Every
other route resolves the slug through the owner check, so another tenant's run is a 404 and
nothing is queued or appended to its chat. Admins are tenant-scoped as well, following
`app/tenancy.py`. Slugs share one namespace on disk; a new run claims its slug with an
exclusive create, so it can never overwrite another run's metadata.

Runs with no recorded owner are listed to nobody. That covers runs started from Claude Code
and runs created before this change. Assign them once, after deploying, then check that the
"unowned after" count is 0:

```bash
PYTHONPATH=tools .venv/bin/python -m arc2.assign_owner --tenant <tenant-uuid> --all-unowned --dry-run
PYTHONPATH=tools .venv/bin/python -m arc2.assign_owner --tenant <tenant-uuid> --all-unowned
```

The tool is idempotent. It refuses to move a run that another tenant already owns, and it
never edits `manifest.json`. Tests: `tests/api/test_arc2_studio_tenancy.py`,
`tests/arc2/test_assign_owner.py`.

Request and feedback text may not contain `--slug` or `--resume`. The API returns 422 and
the runner fails the job, because that text is placed on `/arc2`'s argument line after
the run's own slug. `package.zip` serves only files that resolve inside the caller's run.

**Runner isolation (S1b).** Request and feedback text from any tenant drives the engine,
and its tools include arbitrary Python, so Claude Code's `--allowedTools` rules are not a
boundary. The runner runs the whole `claude` process tree for each job in an OS sandbox
where file writes are denied by default (`tools/arc2/confine.py`: Seatbelt via `sandbox-exec` on macOS, bubblewrap on Linux):

| While a job runs | Allowed |
|---|---|
| Write `build/arc2/<slug>/`, `build/arc2/<slug>.request.txt`, the job's own home | yes |
| Write anything else: other runs, `_studio/` (ownership), `_queue/`, `_jobs/`, `_history/`, the repository, the runner account's home (`~/.claude`, `~/.gitconfig`, shell profiles, launch agents), shared temp | no |
| Read the repository (except `.env*`) and the `claude` installation | yes |
| Read other runs, `_studio/`, `_queue/`, `_jobs/`, anything else in the runner's home (other jobs' sessions, `~/.ssh`, `~/.docker`), including through symlinks | no |
| Read `build/arc2/` or `.claude/worktrees/` anywhere in the repository (other checkouts' runs) | no |
| Signal processes outside the sandbox | no |
| Unix sockets (Docker) and localhost (API, Redis, Postgres) | no, except DNS and the local model fallback's port |
| Internet (the model API) | yes |

A job can still read the original arguments and environment of any process in the same
account (`sysctl KERN_PROCARGS2`); Seatbelt has no rule for it. So neither may hold
anything worth reading: the runner re-executes itself with a scrubbed environment (its own
`ARC2_*` settings, `RUNNER_ENV` and the `ARC2_JOB_ENV` names), and prompts go to `claude`
on stdin, never on its command line. The API bounds every read of a run file (2 MB; a
package at 200 MB), refuses YAML aliases (a few hundred bytes of nested aliases expand to
gigabytes), and lists a run's pages without following links.

Each job gets a fresh `HOME` and `CLAUDE_CONFIG_DIR` (its Claude sessions, memory and temp
files), deleted when it ends. It also gets an allow-listed environment (`JOB_ENV` plus
`ARC2_JOB_ENV`) and `--setting-sources project --strict-mcp-config`, so no user hooks,
settings or MCP servers load. Its `Write`/`Edit` tool rules name only its own run.

The course history is a runner-owned git directory, `build/arc2/_history/<slug>.git`, with
the run as its work tree. Step transcripts sit beside it. Git runs with hooks, fsmonitor
and global/system config disabled, and inside the same sandbox, so it can read the run but
write only the history. A run folder that has been replaced by a link is not recorded.
Older runs keep their in-run `.git`, which is now ignored.

The API reads run files one path component at a time with `O_NOFOLLOW`. A link anywhere in
a run, including the run folder itself, is never followed, even if it is swapped in while
the read is in progress.

The runner fails closed. With `ARC2_CONFINE=auto` (the default) and no sandbox it exits
with status 2 and runs nothing. `ARC2_CONFINE=none` runs jobs unconfined with the runner's
own config and environment, and is for single-tenant hosts only. Each job record carries
`confinement`.

**One runner per runs root (S2b).** A runner holds an exclusive `flock` on
`<runs>/_runner.lock` for as long as it runs. A second runner exits with status 3 and
recovers nothing, so a live runner's jobs are never failed by someone else's startup.
The kernel releases the lock however the holder dies. On startup the lock holder
recovers: a job claimed but not yet recorded (`_jobs/<id>.claiming`) goes back on the
queue, because it never started. A job left `running` is failed, because it did start
and resuming it blind is unsafe. Every job record names its runner (`runner`: pid, host,
start time). This is a single-host design; several hosts would need leases, which are not
needed today.

**Runner account (one-time setup, by an administrator).** A confined job cannot use the
operator's interactive Claude login, so the runner signs in with a long-lived token. Run it
under a dedicated macOS account, so that this token and the account's home are all a job
could ever reach:

1. Create a standard user, for example `arc2runner` (System Settings → Users & Groups).
   Do not make it an administrator, and do not add it to the `docker` group.
2. As that user, check out the repository, create the venv (`uv venv .venv --python 3.11`,
   `uv pip install -r requirements-test.txt`), install Claude Code, then run
   `claude setup-token`. Save the token to `~/.arc2/oauth-token` and `chmod 600` it.
   `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY` in the runner's environment work too.
3. Give it the runs folder that the API reads (`build/arc2`, or `ARC2_RUNS_DIR` for both),
   writable by `arc2runner` and readable by the API.
4. Start it as that user with `make arc2-runner`. It prints `confinement: seatbelt`. If it
   cannot find a token, it warns that confined jobs cannot sign in.

A job can still read its own token from its environment and send it out over the
internet. Use a token for this account only, and revoke it if a run looks hostile.

Evidence:
- `tests/arc2/test_arc2_confinement.py` runs a hostile fake engine through `runner.main`.
  It covers own run/home writes, other runs, `_studio`, `_queue`, `_history`, the repo, the
  runner's home, `.env`, symlinks, a localhost service, a Unix socket, signalling the
  runner, the runner's environment, and git hooks and config planted for the snapshot.
  Everything outside the job is denied. Linked run folders are not recorded.
- A real `claude -p` starts under the profile and keeps all its state in the job's home.
  Without a token it stops at "Not logged in".
- An adversarial review of the first version reproduced the hook, link, socket and home
  escapes that this version closes.

Still open:
- Linux (bubblewrap, `confine.Bubblewrap`) shares the network namespace: localhost services and filesystem sockets stay reachable. Block the runner account's loopback traffic on the host, e.g. `iptables -A OUTPUT -o lo -m owner --uid-owner arc2runner -j REJECT`, and keep it out of the `docker` group.
- The internet stays open, so the token can be exfiltrated (see above).
- `/arc2` always uses `<repo>/build/arc2`, so `ARC2_RUNS_DIR` must point there for the
  engine, runner and API to agree.
- A descendant that calls `setsid()` survives the job's process-group kill. It stays
  confined to that run.
