# ARC² and TrueNorth LMS integration — proposed plan v3

Date: 2026-09-26. Status: proposed revision, not an implementation report.

This revision amends the supplied “ARC² on TrueNorth — plan v2 (2026-09-24)”.
It separates course authoring from training delivery and defines their handoff.
Keep v2's detailed seven-agent contracts, cmi5 layout, validation rules, gate hashes,
and four engine PRs except where this revision explicitly changes scope or wording.
The v2 resume instructions describe outstanding work; they are not executed by this plan.

## 1. Product boundary and intended outcome

ARC² is the **Course Studio** module. It creates, reviews, validates, and packages
training. The LMS delivers approved releases, manages enrolment and attempts, and
records learning outcomes. The modules share identity and integration contracts,
not ownership of live course records.

The existing decision in `docs/moodle-integration.md` is retained: Moodle is the
student-facing LMS; TrueNorth supplies curriculum views, content, exercises, and
integration services. MITE remains the qualification record of authority. TrueNorth
may display recorded qualification status but neither generation nor LMS completion
grants a qualification automatically.

The agreed TrueNorth student career-path design remains a presentation requirement:
wide DP columns, independent vertical scrolling, semester cards, direct prerequisite
arrows on selection, and a concise detail panel below. Its route/embedding relationship
with Moodle must be settled in the delivery integration slice, without creating a
second authoritative enrolment or gradebook system.

“Separate module” means separate responsibilities and lifecycle. Start inside the
existing TrueNorth application and repository; a new deployed service is not required.

| Capability | Owner |
| --- | --- |
| Requests, draft content, quizzes, labs, rubrics, revisions | ARC² Course Studio |
| Authoring validation and the two content review gates | ARC² orchestration and authorized reviewers |
| Approved QSP/PO/EO bindings, prerequisites, equivalencies, DP placement | Curriculum governance; ARC² submits proposals |
| Release validation, destination mapping, publication receipts | TrueNorth publishing integration |
| Enrolment, delivery attempts, gradebook and completion decisions | Configured delivery LMS; Moodle under the existing decision |
| Range execution and evidence production | TrueNorth range/scenario services |
| Learning statements | LRS, correlated to delivery attempts |
| Granted qualifications | MITE |

Students see approved training and their next steps. Instructors create and revise
courses. Administrators configure integrations and publication permissions. Commanders
see authorized readiness evidence; event coordinators reuse approved training in
collective exercises. Publication and curriculum approval are capabilities assigned
through RBAC, not assumed privileges of every instructor.

## 2. Preserve the current engine work

Finish v2 PRs 1–4 as the bounded authoring engine phase:

- Preserve Beau's seven agent names and ownership boundaries.
- Preserve outline acceptance and preview acceptance, feedback routing, hash checks,
  bounded automatic QA retries, and human takeover.
- Preserve staging under `build/arc2/<slug>/`, human promotion, and no provisioning,
  commits, live imports, or external/cloud calls by generation agents.
- Preserve cmi5 as the only interoperable course-package output, alongside the
  existing TrueNorth import artefacts and `PROMOTE.md`. Do not add SCORM or inbound
  Beau-package import to this phase.
- Preserve the existing curriculum eligibility rules, AUTHOR-REQUIRED handling,
  defang checks, private instructor material separation, and manual summative review.
- Keep framework identifiers in the permitted sidecar, outside the student package.
- Treat unresolved XSD validation, provisional publisher identifiers, and similar
  findings as explicit outstanding actions. QA with human actions is not the same
  as an approved production release.

Change v2's “no new API surface” to **“no new API surface in the authoring engine
phase; Studio and delivery integration are subsequent phases.”** Generation agents
still never publish. A later authorized publishing service handles that operation.

Remove “Nothing is implemented yet” from the Context section. Replace it with a
dated evidence table containing branch/worktree, commit, dirty files, checks run,
and unverified claims. The supplied progress header reports PRs 1, 2 and 4 committed
and PR 3 unfinished; verify that branch before resuming. In the checkout inspected
for this revision (`feat/cyber-operator-curriculum`), `tools/arc2/` is absent. That does
not establish whether the reported ARC² worktree is complete or missing.

## 3. One course journey, two lifecycles

Authoring:

`draft → generating outline → outline review → generating content → QA → preview review → package ready`

Retain v2's feedback and retry transitions. Failed or interrupted runs resume from
recorded stage state; they do not silently repeat completed work.

Publishing, tracked separately for each destination:

`not requested → validating release → ready for publication → publishing → published`

Validation may return `needs action`; publication may return `failed` or
`reconciliation required` when the remote result is uncertain. Withdrawal is a
recorded operation on a published release. It is not deletion of student history.

The two authoring gates remain unchanged. **Publish to LMS** is a separate,
authorized release action after content acceptance; it is not another generation
stage. The publishing service rechecks the accepted package digest, curriculum
approval and destination readiness before changing delivery state.

An instructor journey through the future implementation is:

1. Course Studio submits a request to the authoring API.
2. A durable job records project/run identity and invokes the existing engine.
3. Studio shows stage progress and the two reviews, using the same gate operations
   as the CLI. Restarting the browser does not restart generation.
4. An authorized publisher selects an accepted release and configured LMS destination.
5. The integration validates it, records the publication job, and stages the import.
6. Only successful activation produces a publication receipt and student visibility.
7. Student launch and outcomes use delivery identity and attempt correlation.
8. Instructors receive feedback linked to the release and may create a new draft.

## 4. Release contract between Course Studio and delivery

Keep the internal ARC² manifest for agent coordination. Introduce a separate,
versioned release descriptor after the engine phase so delivery does not depend on
agent directories or prompt conventions. Package Builder emits a candidate;
the release service validates and freezes the approved descriptor and artefacts.

Proposed contract fields:

| Field group | Required meaning |
| --- | --- |
| Identity | `schema_version`, stable opaque `course_id`, immutable `release_id`, display version, originating project/run IDs |
| Ownership | Tenant and issuer identity, assigned and verified by trusted services rather than accepted from model output |
| Description | Title, audience, duration, language, entry requirements |
| Artefacts | Package location, digest, per-file checksums, explicit student/instructor/runtime audience classification |
| Objectives | Stable objective IDs, source references and versions, proposed versus approved bindings |
| Curriculum proposals | DP/semester placement, course prerequisite IDs, rationale, and evidence references; approved curriculum revision referenced separately |
| Review evidence | Outline/preview accepted digests, reviewer identity and time, QA result, unresolved actions, applicable curriculum approval |
| Provenance | Source revisions, generator/tool versions, creation time; no credentials or unnecessary student data |
| Compatibility | Package schema and tested delivery capabilities; target-specific validation remains part of publication |

`release_id` and digest are immutable: a changed package requires a new release.
The release identity is distinct from publisher activity IRIs and LMS-issued runtime
activity IDs. Retain v2's publisher-ID rules; do not use a runtime activity ID as a
stable course identity or change publisher IDs solely to encode a display version.

Keep a separate publication record: `publication_id`, destination, course/release
identity, package digest, curriculum revision, publisher, job state, idempotency key,
remote course/activity IDs, timestamps, and structured findings. It is mutable job
state with an audit trail, not part of the immutable package.

Define CLI/API requests and errors against this contract before wiring buttons.
The first adapter targets the existing TrueNorth course artefacts; Moodle activation
is a separate adapter capability. A cmi5 ZIP by itself is not evidence that either
destination supports import, launch, assessment, and completion correctly.

## 5. Curriculum and prerequisites are reviewed data

ARC² may propose a relationship but cannot create an authoritative curriculum rule.
Record course dependencies explicitly as prerequisite-to-dependent edges with
source, scope and curriculum revision. Validate referenced course identities,
reject cycles, and distinguish prerequisites from recommendations and co-requisites.
Define supported AND/OR groups and equivalency rules before accepting them; reject
unsupported expressions rather than silently converting them into stricter rules.

Semester order does not establish a prerequisite. DP placement and eligibility
are separate fields. Course completion does not necessarily satisfy every skill
or qualification requirement associated with that course.

Only approved curriculum relationships drive student blocking and prerequisite
arrows. Draft proposals can appear in an instructor preview with clear labels.
DP3–DP5 concepts remain visible as concepts and excluded from authoritative
qualification and readiness calculations until approved source material exists.

The current mockup's arrows are illustrative, not validated prerequisites. Do not
copy that graph into production curriculum data.

## 6. Publication, versioning and recovery

- Existing student attempts remain pinned to their enrolled release. Updating a
  course creates a new draft/release; switching existing students requires an explicit,
  audited migration and documented progress/equivalency rules.
- A new release becomes the default for new enrolments only after successful
  publication. Preserve access and history for previous releases according to the
  delivery policy. Withdrawal blocks new enrolments without deleting prior evidence.
- Reject stale approvals, changed package digests, unresolved blocking findings,
  unauthorized curriculum mappings and incompatible destinations before activation.
- Repeated requests with the same tenant, destination, release and idempotency key
  return the existing job. A reused key with different content is a conflict.
- Stage local imports transactionally. For remote LMS operations, persist each step
  and reconcile ambiguous results before retrying creation. Do not promise a single
  database transaction across TrueNorth and Moodle.
- Keep a course unavailable to students until required import and activation steps
  complete. Failure preserves the previous active release; provide retry/reconcile
  actions with clear ownership. Log partial destination IDs for cleanup or recovery.
- Use server-side tenant and permission checks, configured destination credentials,
  validated package paths and a constrained preview environment. Generation agents
  receive no LMS publishing credentials.

The current content importer updates existing course/module records and marks them
unpublished. Do not attach a Publish button directly to that importer: first add a
release-aware staging adapter that preserves active delivery and enrolled students.

## 7. Student experience and results integration

The LMS launch context correlates tenant, student identity, course release, module,
registration and attempt. Record per-destination ID mappings explicitly. Keep
the authority's original results and provenance when projecting them in TrueNorth.

Avoid duplicate grades when both LTI and xAPI report one activity: choose an
authoritative completion/grade source for each activity and deduplicate by stable
event/attempt identifiers. Test replay, late results, incorrect release references
and cross-tenant rejection before using outcomes in readiness views.

Client-graded quizzes remain formative. Instructor/manual assessment decisions stay
separate from those checks. Range telemetry alone does not grant qualification.
ARC² receives authorized release-linked feedback and aggregate improvement signals;
it has no need for unrestricted student transcripts or permission to rewrite grades.

Studio placement: add **Courses** under the existing Authoring Studio, with project
views **Overview, Outline, Preview, Assessment, Lab, Validation**. Keep raw files/code
in an advanced view. Show the seven agents as stage status rather than separate menus.
The LMS catalogue and career path deep-link to the authorized course delivery view.

## 8. Delivery slices and acceptance evidence

One owner maintains shared schemas and lifecycle transitions within each slice.
Paths below are likely change locations, not claims of existing integration code.

| Slice | Scope and likely files | Acceptance evidence | Migration and recovery |
| --- | --- | --- | --- |
| A. Complete authoring engine | Existing ARC² worktree: `tools/arc2/`, seven agent files, `/arc2`, `tests/arc2/`; v2 PRs 1–4 | Both human gates, hash invalidation, routed rework, three-failure takeover, dry runs A/B, no tracked content changes during a run | No LMS migration; resumable staged runs. Preserve v2 review findings and fixes |
| B. Release boundary | Proposed release schema/validator under `tools/arc2/`, API models/schemas, release persistence and adapter tests | Freeze one accepted release; reject changed digests, missing approval and malformed prerequisites; same release cannot be overwritten | Additive migration under `control-plane/api/alembic/`; map existing courses to explicit legacy baselines without inventing historical approval |
| C. Manual delivery pilot | Release-aware adapter around `course_content_ingest.py`, curriculum/progress integration, `PROMOTE.md`, Moodle integration runbook | Publish a fixture to an isolated delivery environment; launch a real module; confirm completion/result handling and old-release student continuity | Stage separately; preserve active release on failure; reconcile remote receipts before retry |
| D. Course Studio | Proposed Angular authoring course feature, `app.routes.ts`, authoring API/jobs using existing worker conventions | Create request, review outline, rework preview, resume after worker/browser restart, show package readiness and outstanding actions | Durable job state and migration if needed; deploy behind a feature flag with CLI fallback |
| E. Publish to LMS | Publication API/job, destination adapter, RBAC, audit and Studio action | Double click/retry creates one publication; unauthorized requests fail; changed approval blocks activation; interrupted remote import recovers | Additive job/mapping records; turn off new publications independently of student delivery |
| F. Readiness and improvement loop | Existing curriculum, course/progress, integration and authorized reporting surfaces | Student sees approved eligibility path and pinned content; event replay produces one outcome; instructor feedback opens a draft without changing live course | Versioned projections and replay rules; no rewriting MITE decisions or historical evidence |

Dependency order: A → B → C; D depends on A and B and can use C's manual handoff;
E follows C and D; F follows verified delivery/outcome correlation. Do not start with
UI automation of publication before the import/version boundary is proven.

## 9. Verification and unresolved inputs

Retain v2's engine checks, adversarial review scope, fault injection and DoD commands.
For later slices, add contract tests, migration tests and complete browser journeys.
Static package checks are not a substitute for the runtime smoke: test launch,
pages/quiz, completion, pass/fail, termination, reload and duplicate result handling
against the chosen LMS/LRS environment. Record what was actually run and where.

Open inputs remain: Beau's config/XML samples, vendored XSD and licence/dependencies,
the chosen Moodle activity support and its tested capabilities, runtime identity
handoff, and source-backed curriculum approval. These do not prevent finishing A.
The B/C design must select how existing records acquire release identity and where
the student career-path view is reached from Moodle.

Next implementable slice: verify the reported ARC² worktree and finish v2 PR 3
review/gates, then the engine dry runs. In parallel planning terms only, specify the
B release schema and approved-prerequisite contract. Do not merge, push, provision
or publish merely because those actions appear in the pasted resume instructions.

## Local evidence used for this revision

- `control-plane/web/src/app/app.routes.ts`: separate `authoring` and `learning`
  hubs already exist; extend them rather than inventing a competing top-level shell.
- `control-plane/api/app/routers/courses.py`: existing course and content/programme
  import surfaces; these are not an ARC² release publication contract.
- `control-plane/api/app/course_content_ingest.py`: in-place imports and unpublished
  draft state require isolation from active student releases.
- `control-plane/api/app/models.py`: existing Course, Enrollment and LearningPath
  data provide integration points, not proof of immutable release support.
- `control-plane/worker/worker/tasks.py`: existing worker/job patterns to assess for
  durable authoring and publication work; no new job is implemented by this plan.
- `docs/moodle-integration.md`: recorded Moodle/MITE architecture decision and
  planned integration gaps; dated implementation claims require fresh verification.

This revision changes planning documentation only. It does not certify the ARC²
branch, deploy a Studio UI, validate the mockup prerequisites, or publish a course.
