# TrueNorth: v1.2.0 release readiness and teaching-workflow delivery

Revision 1 · 2026-10-12 · Proposed execution plan, not a release attestation.

This replaces the execution sequence in the supplied “all 17 modules to stage 6” plan. It preserves the agreed simulator-only v1.2.0 scope and one-course end-to-end content acceptance, corrects the release gates, and explicitly includes all six recommended features. The earlier `codereview1.md` remains historical review material.

## 1. Outcome, scope, and evidence baseline

**Two outcomes:** ship the supported v1.2.0 baseline with truthful per-module evidence, then deliver the six teaching workflows below through named milestones. v1.2.0 release completion and completion of this entire programme are different milestones. None of the six features may disappear into an unowned roadmap entry.

| Milestone | Committed outcome | Exit condition |
|---|---|---|
| R0 — v1.2.0 | Supported core workflows, all applicable staging checks, identity boundaries, and restore/recovery evidence | Section 8 release gates pass for each claimed module. |
| R1 — Teach a class | Class-readiness check and instructor intervention console | F1 and F2 acceptance cases pass in the browser and against real API data. |
| R2 — Explain outcomes | Evidence-linked debrief timeline and unified learning record | F3 and F4 acceptance cases pass, including provenance and identity checks. |
| R3 — Improve the next run | Course revision impact preview and targeted remediation assignments | F5 and F6 acceptance cases pass; all six workflows are delivered. |
| H — Hardware qualification | Live vSphere, networking, consoles, noise, metrics and recovery | Hardware-specific evidence passes; only then remove corresponding simulator-only limitations. |

R1–R3 are included delivery commitments, not v1.2.0 blockers unless they repair an already-promised broken core workflow. Assign their version numbers after R0 scope is frozen. H follows lab availability independently; lack of a live ESXi host does not block R0. Do not promise a release “tomorrow” while identity work, new APIs, or audits remain unfinished.

### Reported evidence versus independently inspected evidence

- The supplied plan reports merges through #152, a signed rc4, and install recap `ok=464, changed=11, failed=0`. Retain the original logs and manifest; do not turn that report into a new verification claim.
- The user previously reported real-Moodle CI covering publication, learner quiz resume/completion, repeat publication, attempted-quiz preservation, tenant-ticket rejection, and HTML sanitization. Preserve these as baseline capabilities and attach current run evidence; do not plan to rebuild them without a regression reason.
- The inspected local checkout is `7120eaa2`; it does not contain the newer staging-acceptance script or all changes referenced by the supplied plan. Reconcile the source revision with the release host in R0 before implementation. Newer branch details below are requirements to verify, not claims established from this checkout.
- Local source does establish release/publication services, student lab sessions, scheduling/capacity, health probes, competency, adaptive learning, Ops Center, scoring and AAR foundations.

### Scope inventory: mandatory before stage promotion

Import the exact 17 module IDs from the current Module Board and acceptance registry. Do not invent names or silently omit modules. The release manifest must contain exactly that set, with no duplicate or unowned entries. A missing inventory blocks the “17/17” claim.

Each module record contains: owner; included SHA/image digest; supported workflows; mandatory check IDs; result; validation environment; declared skips; limitations; monitoring; backup/recovery applicability; evidence links; reviewer. Resolve missing owners before scheduling work.

Keep **release status** (blocked/ready) separate from **validation environment** (unit/integration/staging/simulator/live). “Stage 6 — staging/simulator scope” is valid only when that restricted scope is explicit on the card. A skipped core workflow cannot earn readiness merely because a skip was declared. Unsupported workflows must be clearly disabled or excluded from the stated support contract.

## 2. Ownership, priorities, and dependency order

Assign people to the roles below; roles alone are not completed staffing. Use one integration owner for shared schemas, API routes, task contracts, release state, identity mappings, and generated files. Parallel implementation is appropriate only for disjoint ownership; merge shared-contract changes before dependent consumers.

| Work package | Accountable role | Depends on | Required delivery |
|---|---|---|---|
| R0-A source/audit freeze | Release integrator | Current branch/host inventory | Candidate manifest, 17-module matrix, triaged findings. |
| R0-B correctness and access | API/scoring owner + frontend owner | R0-A | Scoring, Resume, permissions, truthful UI, essential recovery controls. |
| R0-C staging and recovery | Release/operations owner | R0-B candidate | Install, strict acceptance, browser and Moodle loop, full restore drill. |
| M1 Moodle learner loop | LMS owner | Current publishing/pull contracts | Browser SSO → quiz → TrueNorth result; cmi5 evidence if supported. |
| M2 AD qualification | Identity owner | Isolated fixture and credential setup | Federation/role/disable tests; separate environment claim. |
| F4a learning-result contract | Learning-record owner | M1 | Identity, release, attempt, provenance and reconciliation contract. |
| F1 class readiness | Instructor-workflow owner | R0, capacity/lab/release contracts | Instructor-facing preflight report. |
| F2 intervention console | Instructor-workflow owner | R0; F4a for imported results | Cohort view, help requests, interventions. |
| F3 debrief timeline | Evidence/scoring owner | R0, evidence event contract | Correlated, access-controlled outcome timeline. |
| F4b unified record + M3 push | Learning-record/LMS owners | F4a; M3 also needs identity review | Joined evidence record; durable push plus pull reconciliation. |
| F5 revision impact | Course-release owner | R0 release semantics | Pre-publication change/learner impact report. |
| F6 remediation | Curriculum owner | F4 record, F2 assignment workflow | Gap → assigned practice → reassessment. |
| H hardware qualification | Infrastructure owner | Available isolated lab | Real backend lifecycle evidence. |

F1 and F2 can develop together after their shared contracts land. F3 and F4 share evidence identifiers, not ownership of each other's tables. F5 can proceed independently once release semantics are fixed. Plan small PRs for persistence/API, UI, and failure tests; intermediate functionality remains disabled until its full path works.

**Priority rules:** P0/P1 correctness, security and data-loss defects block affected modules. P2 findings block when they break the declared core workflow; otherwise record owner, rationale and target milestone. “Should-fix” must have a concrete impact classification. Finish the running audit and triage it before candidate freeze. A new blocker reopens the candidate, rather than being relabelled a limitation to meet a date.

At R0-A, each owner estimates their slices and names external dependencies. Publish a milestone forecast with confidence and blockers, then revise it from actual validation results. Do not substitute speculative day estimates for these gates.

## 3. R0: establish and verify the supported release

### R0-A — Reconcile and freeze the baseline

1. Confirm main/tag SHA, signed manifest, image digests and staging inventory revision. Account for uncommitted work without resetting active checkouts. Retain CI and installation logs against those identities.
2. Verify running services, actual image digests, API health, ARC² runner heartbeat and Moodle reachability. Treat zero failed installer tasks as installation evidence, not proof of application workflows.
3. Reconfirm each supplied audit finding on the selected source. Record fixed, reproduced, unverified or obsolete; never carry old line numbers forward as proof.
4. Use the current supported Python/Node/browser versions and CI dependency definitions. Run targeted checks and the repository DoD. This Mac's copied Linux interpreter is not a usable validation environment; use a native environment or supported container/runner.
5. Record the 17-module matrix and exact permitted skips before running acceptance. Make an unexpectedly missing test or newly introduced skip fail the gate.

### R0-B — Correctness, permissions, and essential operator workflows

| Supplied finding / workflow | Minimum acceptance | Scope |
|---|---|---|
| Scoring validators | Known-positive, known-negative, zero-evidence and unavailable-backend cases; unsupported validators rejected before launch. Manual assessment is an explicit instructor action. | R0 blocker if reproduced. |
| Simulated inject evidence | Bound to its exercise/range and labelled synthetic; cannot satisfy a real-execution assertion through an unlabelled fallback. | R0 correctness. |
| Exercise Resume | Paused exercise resumes through the correct endpoint; duplicate requests and invalid states have tested outcomes. | R0 blocker if reproduced. |
| Role menus, deep links and API access | Students see own work/AAR; Observers receive read-only views; range_ops and Instructors get only permitted actions; telemetry access follows permission. Direct API and cross-tenant tests accompany UI tests. | R0 correctness. |
| Fictional readiness screen | Hidden or explicitly isolated as demo content; no mock qualifications shown as real learner readiness. | R0 blocker if reproduced. |
| Self-enrolment | Enrol button on supported course views; eligibility enforced server-side; repeat click is safe; restricted courses remain restricted. | R0 if promised self-service; otherwise deliver with R1. |
| Recovery controls | Operations and reservations visible; retry/abandon/restore show impact and outcome. Force-release requires proof of safety or a quarantined/manual cleanup workflow. No lease is recycled while backend resources may still be active. | Essential R0 operator path. |
| Remaining operator UI | Snapshots/restore, team membership, per-learner progress, Moodle results/pull-now; bulk CSV import and learning-path assign-to-group. Validate authorization, partial failures and audit records. | Essential operations in R0; remaining classroom/bulk conveniences explicitly in R1. |

Each fix needs a failing-before/passing-after regression and current DoD evidence. Preserve existing API contracts or version changes explicitly. Regenerate OpenAPI, Angular types, task schemas and table mirrors where applicable; do not manually merge generated output. Avoid adding raw SQL or external SDK calls outside the established ownership/adaptor boundaries.

C105 cleanup must use a reviewed, idempotent service operation or a narrowly scoped reviewed script. Preview the exact course/quiz records, preserve attempted-quiz history and grades, then verify visibility in both systems. An anonymous scratchpad run bypassing lifecycle rules is not release evidence.

### R0-C — Staging acceptance and per-module proof

- Follow the current staging runbook using the candidate's acceptance script, `--strict --json`, the required admin identity, read-only host configuration mount, and simulator endpoint. Verify option names on the authoritative revision; the local older checkout cannot supply this command today.
- Every declared skip identifies the check, affected workflow/module, reason, supporting alternative evidence, and future validation milestone. Browser rendering skipped in the CLI suite is covered separately by the real browser lane. Hardware skips remain hardware limitations.
- Exercise sign-in and at least one meaningful allowed action as each supported role, plus denied actions and direct-link access. “Page loaded without console errors” is insufficient by itself.
- Prove the M1 Moodle path on staging and in CI. Confirm the cmi5 capability on the selected source, since earlier reports described it as planned and the supplied plan reports it implemented. If included in support scope, test an AU launch and correctly identified statements in the LRS; do not infer PCTE compatibility.
- Use test-host mode only for intentionally synthetic content tests. Also run authorization/approval tests with normal enforcement enabled and verify production configuration rejects test-only automatic acceptance. Test flags must not provide false evidence for normal approval security.

### Content and ARC² acceptance

Prove C101 generation → QA → release → approval → TrueNorth/Moodle delivery, including the TEST CONTENT banner for test-mode automatic acceptance. Record actual serving model/version for the local fallback run, not only `atl-coder` or another alias.

The agreed release content bar is one complete course plus the remaining production batch operating with persisted checkpoints, progress heartbeat, retry/failure visibility and an owner. Derive the eligible course count from the current register; do not hardcode “36 plus 7” without reconciling it. A running process is progress evidence, not proof that unfinished courses passed QA.

Every held course records its hold reason and release authority. Model unavailability may permit an approved fallback; cleared-author, rights, safety, or Standards holds remain until the responsible human clears them. Local generation must not bypass those holds. Preserve C105 as a draft fixture and publish only eligible content.

## 4. Moodle and identity: M1–M3

### M1 — Prove the complete learner loop

**R0 required:** a browser signs a test Student into TrueNorth, follows the SSO ticket into Moodle, opens a page, attempts a quiz, leaves/resumes it, completes it, and sees the grade/completion returned to the correct TrueNorth enrollment. Use isolated test enrollments; preserve pre-existing grades.

Keep CLI/web-service checks as fast integration coverage, clearly labelled as bypassing browser interaction. Add the browser journey to the real-Moodle CI lane and repeat it against the candidate on staging. Include tenant mismatch, expired/reused ticket, unauthorized learner and delayed-result states. Match records by immutable user/platform/release/attempt identity rather than email alone.

Grade-return acceptance uses a documented configured sync interval: after a successful scheduled or explicit pull, the result is visible, with source timestamp and last-sync status. Induced outage shows stale/error status; recovery reconciles without duplication. A cmi5 AU, if supported, yields correctly correlated LRS statements; LTI grade passback and Moodle-to-TrueNorth grade retrieval are distinct directions and both must be labelled accurately.

### M2 — Qualify directory-backed sign-in

Build an isolated Samba AD/LDAPS fixture with test CA, groups and accounts, reusable in CI. Do not join it to the production domain or change staging's sole identity provider during ordinary release verification. Preserve a tested local administrator recovery path.

Test student/instructor/admin group mappings, correct tenant binding, nested-group policy where supported, untrusted CA rejection, disabled accounts, group removal, session/token invalidation policy, and downstream Moodle SSO identity. Define and verify the maximum disable/revocation propagation time from the configured synchronization/token policy. Email collision must not grant staff privileges or silently rebind an account.

The operator selects the test host and supplies the bind credential through the vault. Store public CA material separately from CA private keys. Pass the identity review before enabling the profile. Record “Samba-tested” separately from “Windows AD/AD CS qualified”; document work-domain cutover and rollback. M2 blocks R0 only if directory authentication is explicitly in the R0 supported profile; otherwise it is an owned qualification deliverable before that profile is advertised.

### M3 — Add durable Moodle events without weakening reconciliation

Included in R2 with F4, rather than silently expanding R0. The existing successful pull path remains authoritative reconciliation during rollout.

- Provision a tenant/platform-bound service client with narrow event permission. Validate issuer, audience, signature, expiry, client identity, platform binding and scope server-side; never trust a tenant ID supplied only in the event body.
- The Moodle plugin durably queues grade/completion events with bounded retry/backoff and dead-letter visibility. A transactional API inbox acknowledges only persisted events. Unavailability must not lose a grade update.
- Use a uniqueness key including authenticated source/platform and event ID. An identical duplicate succeeds without a second effect; the same ID with changed content is rejected and audited. Expired or unauthorized authentication is rejected independently of legitimate duplicate delivery.
- Reconcile ordering, regrades, deletions and teacher overrides. Use source revision/authoritative retrieval when ordering is ambiguous; arrival time alone cannot let an old event or delayed poll overwrite a newer grade. Record raw source value, scale and normalized value.
- Generate/rotate client secrets through the installer, with no credential values in logs or reports. Test rotation and revocation, and retain pull fallback while push is disabled.
- CI demonstrates a result arriving without a pull, duplicate handling, out-of-order updates, outage/restart recovery and tenant denial. Regenerate contracts and require identity/adversarial review before merge.

## 5. Six explicit feature delivery contracts

These are required R1–R3 deliverables. Extend existing modules and shared clients; do not create six disconnected dashboards. Every feature requires tenant/role tests, truthful loading/error/stale states, keyboard access, an actual browser journey and evidence on its delivery candidate. “Done” means persistence → API → UI → outcome works, not merely that a screen exists.

### F1 — Instructor class-readiness check (R1)

**User outcome:** select a course release, cohort size and start time; see whether the class can run and exactly what blocks it.

**Build:** combine existing scheduling capacity, golden-image availability, lab profiles/probes, Moodle publication, identity/launch configuration and required service health. Report each check's status, explanation, owner, observation time and source. Distinguish measured host capacity from configured fallback totals. Capacity estimates are not reservations. Unknown data is not green.

**First PRs:** aggregation contract and read-only API; instructor form/report with links to fixes; stale/inaccessible dependency tests. Persist the report inputs and observations for audit. Invalidate/re-run when release, cohort size, inventory or configuration changes. An optional representative lab build is a separately authorized later enhancement, not a hidden side effect of checking readiness.

**Acceptance F1-A:** ready fixture passes; missing image, insufficient capacity and unpublished Moodle course identify exact blockers; unreachable backend reports unknown; checks are tenant-scoped and create no VMs/reservations. A changed course release cannot reuse an old green result. Instructor can run and understand the report from the browser.

**Foundation:** scheduling router, lab-session probes, golden images, course-publication services. **Recovery:** disable aggregation UI without changing source records; retain reports as historical observations.

### F2 — Instructor intervention console (R1)

**User outcome:** see who needs help, who is waiting for infrastructure, and what assistance has already been provided.

**Build:** one cohort view with learner activity, latest meaningful progress, lab condition, result-sync freshness, explicit help requests, assigned instructor and intervention history. Learners can request help; instructors acknowledge, assign, annotate and resolve. Differentiate infrastructure failure, disconnection, unanswered help and learning difficulty. Do not infer incompetence from idle time alone.

**First PRs:** help-request/intervention model and API; cohort aggregation from existing enrollment/lab/result sources; learner request control and instructor queue; role/browser tests. Reuse per-learner progress, team membership and group-assignment UI from R1. Persist author, timestamps and state changes. Decide staff-only versus learner-visible notes explicitly.

**Acceptance F2-A:** request → acknowledge → assigned intervention → resolution works for a class; reconnect/repeated clicks do not duplicate requests; infrastructure outage has a distinct status; concurrent acknowledgement has one owner; students cannot view other learners or staff-only notes. Retain a clear reassignment path if an instructor disconnects.

**Foundation:** Ops Center, enrollments/teams, lab sessions, progress services. **Recovery:** preserve open requests and history; feature-disable the aggregation without losing requests. Automated assistance suggestions are optional after the explicit workflow works.

### F3 — Evidence-linked debrief timeline (R2)

**User outcome:** select an exercise/objective and understand what happened, which evidence supports the result, and what is missing.

**Build:** correlate MESL injects, telemetry, learner annotations, interventions and scoring decisions using tenant/exercise/range/objective/attempt IDs. Store source time and ingestion time; expose clock uncertainty and delayed events. Distinguish planned injects from executed actions and synthetic from observed evidence. Link source evidence and applicable scenario/course/rule versions to the assessment.

**First PRs:** event/evidence contract and indexed query; synchronized timeline within scoring/AAR; objective drill-down and debrief export; delayed/duplicate/authorization tests. Reuse evidence references rather than copying entire sensitive logs. Define retention and a tombstone/checksum policy for expired evidence so old reports do not silently claim it remains available.

**Acceptance F3-A:** a known exercise reconstructs inject → observed event → student action → assessment; each decision resolves to supporting evidence or explicitly states absence; duplicate/late events are handled deterministically; untrusted notes render safely; students see only their allowed debrief. Define a representative event-volume fixture and pass its latency budget before release.

**Foundation:** telemetry/event stores, MESL, annotations, scoring, AAR and F2 interventions. **Recovery:** preserve original events and decisions; rebuilding an index cannot rewrite grades. Full video/keystroke recording is outside this feature.

### F4 — Unified learning record (R2; contract begins before R1)

**User outcome:** see Moodle learning, practical evidence and instructor assessment against the same objectives, with clear provenance and synchronization state.

**Build:** define authoritative mappings for tenant, learner, enrollment, course release, activity, attempt and PO/EO or competency. Retain source IDs, timestamp, raw score/scale, normalization, outcome and provenance. Keep course completion, demonstrated competence and formal qualification approval as separate facts. Only an authorized assessor can create or revoke formal sign-off.

**First PRs:** F4a source mapping and append/audit semantics using the working pull path; joined learner/instructor record and objective drill-down; M3 durable events; corrections/disputes and reconciliation visibility. Reuse current competency assertions and progress records; justify any new storage instead of building a competing gradebook. Version mappings when release content changes.

**Acceptance F4-A:** one learner's Moodle quiz, lab evidence and instructor sign-off map correctly; a second tenant with the same email remains isolated; attempts and pinned releases remain distinct; duplicate results do not double-count; regrade/revocation preserves history; invalid scales are rejected; completion cannot silently grant a qualification. Pull/push disagreements are visible and reconciled according to documented source precedence.

**Foundation:** M1/M3, course releases, enrollment pinning, QSP, competency assertions, lab evidence. **Recovery:** imports can be replayed idempotently; keep source receipts and mapping versions. Disabling push leaves pull and existing records intact.

### F5 — Course revision impact preview (R3; may develop during R2)

**User outcome:** before acceptance/publication, see what changes and how existing learners and assessments are affected.

**Build:** compare candidate with the active release: objectives, pages/files, quiz questions, lab profile/resources, prerequisites and human-action acknowledgements. Show affected enrollment counts, pinned releases, attempted quizzes that must be retained, and future-enrollment behavior. Make removed or unmapped objectives explicit. Publication remains an explicit action with its own authorization.

**First PRs:** deterministic release comparison/impact API; preview in Course Studio; acceptance/publication precondition linking the reviewed candidate and active-release digests; attempt-preservation and stale-preview tests. Reuse immutable bundles, hashes and publication records.

**Acceptance F5-A:** fixtures cover additions, removals, changed questions and increased lab requirements; attempted quiz/grade history survives publication; existing enrollment pins do not move silently; no instructor-only material leaks into learner views; a concurrent release change invalidates the preview and requires refresh. Computing a preview changes no course state.

**Foundation:** release services, bundle metadata, enrollment pinning, Moodle publication. **Recovery:** preserve prior immutable releases; follow supported publication recovery rather than editing old bundles in place.

### F6 — Targeted remediation assignments (R3)

**User outcome:** move from a specific failed objective to assigned practice, reassessment and a visible evidence-backed outcome.

**Build:** maintain explicit objective-to-practice mappings with prerequisite/release constraints. Instructor selects or approves a recommendation, assigns it to a learner/group, sets a due date and reassessment criterion, then reviews the result. Track assigned/in-progress/awaiting-assessment/completed/cancelled states. Use deterministic mappings first; AI suggestions must cite the gap and never auto-certify the learner.

**First PRs:** assignment/mapping model and API; instructor assignment and learner task views; reassessment link to F4 evidence; group/idempotency/permission tests. Reuse adaptive-learning recommendations and skill-gap APIs instead of adding another recommendation engine.

**Acceptance F6-A:** failed objective → instructor-approved practice → learner completion → fresh assessment → updated evidence works end to end; repetition of the same request does not duplicate assignments; the original failure remains in history; course completion alone cannot mark the skill mastered; inaccessible/retired practice is detected; cancelled assignments do not issue stale reminders.

**Foundation:** adaptive learning, QSP/competency, F4 learning record and F2 instructor workflow. **Recovery:** retain assignment history; recommendations can be disabled without deleting active assignments or assessment evidence.

## 6. Data safety, identity, observability and recovery

**Migrations:** additive schema first, compatible readers/consumers next, resumable verified backfill, then new writes. Test empty installation and upgrade from a populated prior PostgreSQL schema with automatic table creation disabled. Preserve identities, grades, release pins, intervention records and assessment history. Specify forward repair or backup restore when downgrade is unsafe. Version queued messages if consumers change.

**Asynchronous operations:** durable acceptance before success response; explicit operation/source IDs; duplicate-safe consumption; bounded retry; stale-operation rejection; reconciliation for unknown remote outcomes. Serialize conflicting range actions and never assume lease expiry means an in-flight hypervisor action ended. Recovery UI must expose consequences, not present abandon as successful cleanup.

**Backup drill:** cover the applicable TrueNorth and Moodle databases, Moodle data files, release/course artifacts, ARC² runs/state, installer configuration and necessary keys through the approved secret backup mechanism. Document consistent snapshot order. Restore to an isolated environment that cannot send real notifications, consume production jobs or provision live resources.

**Restore acceptance:** restored users open a course and its files, existing learner results and attempted quiz history survive, release pins resolve, and interrupted work is safely classified/reconciled. Exercise both partial ARC² exclusion and full restoration, explicitly recording what each proves. Record backup age/data loss and recovery duration against operator-approved RPO/RTO budgets set before the drill. An archive listing or successful database import is not enough.

**Monitoring:** load alert rules with `promtool` and induce representative failures to prove delivery. Cover stale runner/dispatcher heartbeat, oldest pending publication/result event, grade-sync lag, lab cleanup failures, resource reservations and failed backups. Attach tenant/platform/operation correlation IDs without private course content, tokens or credentials. Link each alert to a recovery action and owner.

**Test safety and secrets:** use dedicated test tenants/courses, reviewed idempotent cleanup and test-fixture labels. Keep content automation distinct from normal approvals. Handle credentials through the installer/vault; never reproduce secret values in plans, logs or evidence. Rotate the credentials identified in the supplied plan using a coordinated dependency update, verify consumers, then remove superseded temporary credential files. Do not delete the only recoverable credential before replacement is verified.

## 7. Hardware qualification and explicit exclusions

H is deferred until an isolated supported vSphere environment is available. It is not opportunistically added to R0 halfway through validation. Confirm then-current licensing, supported versions, VCSA sizing, capacity, service-account privileges and connectivity before setup; the older plan's evaluation-license and memory assumptions are not prerequisites to rely on without checking.

Operator supplies credentials through approved controls. Validate a Linux template with tools, optional Windows template, supported standard-switch configuration, datastore and isolated networks. Test build/destroy, start/stop, snapshots/restore, console access, gs-core guest configuration size, noise deployment, real metrics and cleanup after failure. Lift only the specific hardware skips proven by these results. Register the self-hosted lab CI runner separately if ongoing hardware regression coverage is intended.

The following remain explicitly outside R0 and outside the six new feature MVPs unless separately scoped: live host injectors; Proxmox/Hyper-V parity; non-vSphere console; Windows noise agent; TAXII/STIX; external calendar push; Greyspace T2+ and breadcrumb submissions/storage-NIC enhancements; ARC² Observe/Access; broad Astra-style redesign; PCTE-specific handoff; full MOSA module splits; remaining QSP/golden-image/storage/kit UI; Kubernetes production deployment; disk/network usage accounting. Existing API functionality is not proof these user workflows are delivered.

Track real-vSphere scheduler measurements under H; until then, F1 labels configured capacity estimates. Keep TN-network AD qualification distinct from the Samba fixture. Repository privacy, registry credentials and stale PR/worktree cleanup are separately coordinated maintenance tasks; preserve dirty work and do not close a PR merely to tidy a board.

## 8. Candidate promotion and per-module release gates

**Gate A — source and scope:** exact 17-module manifest, supported environments, frozen audit triage, owner/reviewer assignments, compatible dependencies and all required fixes merged. Current limitations are explicit; missing tests are not rewritten as limitations to obtain a pass.

**Gate B — code evidence:** targeted regressions and current repository DoD pass on the candidate. Relevant frontend build/test/browser checks, real-Moodle lane, PostgreSQL migration/concurrency checks and broker recovery tests pass. Identity/LTI and recovery checks are mandatory where those capabilities ship. A zero-failure install does not waive any of these.

**Gate C — staging:** signed candidate installed with zero failed tasks; image digests match the manifest; strict acceptance has zero failures and only scope-valid predeclared skips; required separate browser/per-module proofs pass; restore and monitoring drills satisfy their budgets. Verify that test-only acceptance paths are not enabled in the normal supported profile.

**Gate D — release:** promote the tested source and artifact set using the established signing/release process; do not retag an old candidate or substitute rebuilt images without verification. If the final release produces different artifact digests, validate those artifacts before attesting. Upgrade staging to the final tag and retain its final acceptance record.

Any code, plugin, installer, image, migration, dependency or behavior-affecting configuration change after validation produces a new candidate and re-runs affected checks, regardless of whether it came from step 2, a Moodle feature, UI work or a late audit. The final candidate always gets the full supported staging suite. Pure evidence-document edits must be identified separately; if the release process rebuilds artifacts for them, the new digests still require verification.

Per check, record `PASS`, `FAIL`, `BLOCKED`, `NOT RUN`, or reviewed `NOT APPLICABLE`, with commit/digest, configuration identity, environment versions, exact command, timestamp, result artifact and reviewer. Skips never count as passing workflows. No new 8.5/10 application grade is claimed by writing this plan.

Update `docs/release-notes/v1.2.0.md`, CHANGELOG and runbooks on the authoritative branch. Link staging JSON, install recap, Moodle/browser proof, content record, backup/restore evidence and known limitations. Close #51 only after verifying its stated resolution; retain the supplied requirement for explicit user approval before closing #92.

Update each Module Board card from its own evidence. Publish “17/17 at stage 6” only if all 17 independently meet the declared support contract. Otherwise publish the honest count and remaining blockers. Update associated status/memory notes from that evidence; do not use notes as the source of truth.

## 9. Plan-quality acceptance and next actions

The requested 8.5/10 target is a **plan-quality target**, assessed separately from release readiness. Use five dimensions scored 0–2: current scope/evidence; priorities/dependencies; executable ownership; measurable acceptance; recovery/release control. Half-points are allowed, each dimension must score at least 1.5, and the total must reach 8.5. Source reconciliation and owner assignment remain outstanding execution prerequisites, not hidden assumptions. A reviewer should downgrade the plan if any committed feature lacks an owner, test, dependency or recovery path.

**Author's assessment of this revision: 9.0/10 for plan design**, subject to independent review. Scope/evidence earns 1.5/2 because the newer release evidence still needs reconciliation; ownership earns 1.5/2 because accountable roles are defined but people and forecasts are not assigned. Priorities/dependencies, measurable acceptance, and recovery/release controls each earn 2/2 for explicit written contracts. These scores assess the document's design, not the correctness of unexecuted implementations or actual release readiness.

| Review concern / recommendation | Explicit coverage |
|---|---|
| Stage 6 overstating simulator evidence | Section 1 module records; section 8 per-module gates. |
| Tomorrow deadline versus new feature scope | R0/R1/R2/R3/H milestones and owner-based forecasting. |
| Candidate rebuilt only for acceptance-script fixes | Section 8 invalidates evidence after any behavioral change. |
| Browser bypass, Moodle ordering and authentication | M1 real browser path and M3 durable event/reconciliation contract. |
| Database-only restore proof | Section 6 full application restore and recovery budgets. |
| Running batch / local model bypassing holds | Section 3 content acceptance and hold authority. |
| Audit never frozen / ambiguous live-hardware scope | R0-A audit freeze and independent H qualification. |
| All six requested features | F1–F6: owned milestones, source foundations, first PRs, acceptance and recovery. |

**Next actions, in order:** reconcile the newer release branch and evidence; assign owners and populate the exact 17-module matrix; finish/triage the audit; land bounded R0 fixes; prove the supported candidate; then execute R1–R3 in their dependency order. Hardware work proceeds when its prerequisites exist. This document authorizes no deployment, credential change, PR closure or live resource creation by itself.

### Completion checklist

- [ ] R0 source, owners and exact module matrix reconciled; audit triage frozen.
- [ ] Scoring, Resume, permissions, truthful status and essential recovery regressions pass.
- [ ] M1 browser/pull loop and applicable cmi5 proof pass on the selected release.
- [ ] Restore, alerts, migrations, strict acceptance and final artifact evidence pass.
- [ ] Board promotion reflects evidence per module; limitations remain visible.
- [ ] F1 readiness and F2 intervention workflows delivered (R1).
- [ ] F3 debrief and F4 unified record, including M3 delivery, delivered (R2).
- [ ] F5 revision impact and F6 remediation delivered (R3).
- [ ] M2 directory qualification delivered before advertising that profile.
- [ ] H evidence recorded before removing live-hardware limitations.

### Validation of this document

On 2026-10-12, document checks passed for all six feature contracts and acceptance IDs, nine main sections, unique headings, balanced fences and whitespace. The source/recommendation mapping above was reviewed for scope and sequencing. Only this new plan file was created; the original attachment, historical review plan and unrelated user files were preserved.

`bash scripts/dod.sh` was attempted and stopped before tests with `ruff not installed in .../.venv/bin/python`. Inspection confirmed that interpreter is an x86-64 Linux ELF executable on this Mac, consistent with the previously reproduced executable-format failure. No application gate pass, staging verification, implementation, release promotion or feature completion is asserted here. Re-run application gates in the reconciled supported environment during R0.
