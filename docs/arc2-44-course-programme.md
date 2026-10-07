# Producing the 44-course programme with ARC²

Planning baseline: 2026-10-05. Status: proposed delivery plan; no courses generated,
approved, provisioned or published by this document.

Build ARC as the end-to-end course production and delivery orchestrator: design
the course, generate its materials and assessments, create it in Moodle, and create
the small individual ranges needed for its practical activities. Establish sources,
prove three delivery patterns, then expand through the eight proposed terms.
Named instructors and subject specialists review accuracy and assessment quality;
ARC handles the repeatable generation, publication and provisioning work.

User clarification incorporated: Moodle publication and VM creation are required
product capabilities, not a manual handoff at the end of ARC. The design below
supersedes the earlier engine-only boundary of `arc2-plan-v3.md` for this programme.
This planning document does not itself perform live publication or provisioning.

## 1. Outcome and scope

The starting inventory is 44 proposed courses: 32 labelled Algonquin and 12 labelled
RMC. The stakeholder pathway is three college years followed by eight months at
CTU Kingston with RMC instruction. These are learner programme durations, not the
production schedule. Institutional labels, course codes, term dates and DP mappings
in the current catalogue are not approved programme authority.

Deliver a reviewed course package for every retained course, with full lessons,
practice, assessments, instructor materials, source records, realistic time budgets
and tested LMS delivery. Reconcile overlaps and official outlines before freezing
the final course count. Track all 44 original entries through any merge or rename.

Observable outcomes:

- Students can launch a course, understand its requirements, complete its activities,
  submit evidence, receive feedback and resume their work without unnecessary VMs.
  Where needed, Start lab creates their own small range and returns usable access.
- Instructors can prepare and teach it using facilitator notes, worked solutions,
  marking criteria, expected timings, remediation and tested practical materials.
- Administrators can identify the accepted release, reproduce its package, publish
  it once, recover failed publication and preserve existing student attempts.
- Curriculum owners can trace objectives and proposed qualification mappings to
  approved sources and distinguish learning completion from granted qualification.

Planning scope includes content production and the engineering needed to deliver it.
Full Course Studio UI can follow the first automated delivery pilot; it is
not a prerequisite for starting reviewed content production. Automated Moodle
publication and range lifecycle integration are required for the final outcome. Formal
qualification decisions, institutional accreditation and infrastructure purchases
remain separate.

## 2. Current evidence and implications

| Inspected evidence | Implication |
| --- | --- |
| `content/catalogue/cyber_operator_programme.csv`: 44 rows, all `provenance=unsourced`, `status=proposed`, `qsp_code=QSP-TODO`, empty `duration_hours` | Treat these as the production backlog, not an official syllabus. |
| `content/courses/`: 44 YAML drafts with objectives, topics, one-sentence lab briefs and quizzes | Audit and reuse useful material; a populated file is not evidence of a complete course. |
| C304's draft is `iot-security-foundations.yaml` (`course_code: C304`, titled "IoT Security Foundations"); every other draft is named after its code and catalogue title | Keep `course_code` as the identity; reconcile the title with the catalogue's "IoT & Embedded Device Security" at outline review. |
| `c101-computer-architecture-systems.yaml`: 100 declared hours, six 60-minute modules (the C105, C108 and C208 drafts have the same shape) | Reconcile contact, practice, assessment and independent-study hours; the current metadata accounts for 6 of 100 declared hours. |
| C206 and C207 share the title Incident Response Foundations | Decide whether these are distinct blocks, a sequence or duplication before producing both. |
| Course YAML has no fields for instructor material, rubrics or non-quiz assessment | Extend the format (or carry them in the ARC release) before claiming a complete instructor pack. |
| ARC worktree `.claude/worktrees/arc2-ai-rapid-cyber-8d8a3e`, observed HEAD `3512f61`, forked from an older main (85 commits behind on 2026-10-05) | Engine files exist outside main. Port them onto main rather than merging the old base; their presence does not establish passing tests or delivery readiness. |
| ARC `qa.py` requires every module to have lab text; the manifest requires all seven stage records, and its schema requires critical events, crit validators and injects for every run | Add explicit non-lab activities and tested not-applicable stage handling without bypassing review. |
| `tests/api/test_course_content_ingest.py` asserts a lab in every promoted course file | Relax to range modules only when theory/practical courses are promoted. |
| ARC manifest course-code pattern is `ARC2-…` | Establish stable mapping to existing catalogue identities; avoid creating duplicate courses during promotion. |
| `course_content_ingest.py` accepts absent lab text, creates unpublished lesson/quiz records and updates existing records in place | Some no-lab support exists downstream, but release isolation and safe delivery still need verification. |
| Moodle course-creation work (`local_truenorth` plugin, `moodle_sync.py`) exists only uncommitted in the `moodle-integration-courses-8bc905` worktree, on an older base using python-jose | Port it behind an adapter on main (PyJWT, registry, no vendor branches) rather than starting again. |
| Worker `provisioners/` registry and `BaseProvisioner` expose provisioning, destroy, power and snapshot operations | Reuse the existing backend boundary for small ranges; verify actual backend capabilities on the target host. No live backend was exercised for this plan. |
| `docs/arc2-plan-v3.md` and ARC worktree `docs/arc2-course-studio.md` | Preserve outline/preview reviews and the separate release/publication lifecycle. Resolve plan differences against code and recorded decisions. |
| The main checkout's shared `.venv/bin/python` is a Linux ELF executable and fails with `exec format error` on the Mac | Use a per-worktree native venv (`uv venv .venv --python 3.11`) or a verified suitable host before engine acceptance. No old test count establishes current readiness. |

No serving model was inspected or benchmarked. This plan makes no claims about
generation speed, token cost or available model capacity. Confirm the actual served
model, execution environment and allowed source handling during preflight.

## 3. Delivery patterns and provisional inventory

Classify each module by its required activity, then derive course infrastructure.
Do not convert an entire course to VM delivery because one module needs a machine.

| Pattern | Required experience | Default resources |
| --- | --- | --- |
| T: teaching and judgement | Lessons, cases, discussions, quizzes, written/oral work | LMS; no provisioned lab |
| P: practical without a dedicated VM range | Code, supplied logs/PCAPs, simulators, datasets, configuration review | LMS plus local tools or a container/browser workspace |
| R: live range | Configuration, investigation or operations on running systems | Isolated, resettable VM/container/cloud test environment |

P does not mean zero compute. Browser workspaces and containers need resource and
licensing plans. R does not mean a unique topology per course. The table below is a
production hypothesis based on draft titles, not an approved assessment design.
Where live performance is an objective, a file-based substitute cannot silently
replace it. Hardware and cloud requirements must be decided at module design.

| Wave | Code | Draft title | Pattern | Main production decision |
| --- | --- | --- | --- | --- |
| 1 | C101 | Computer Architecture & Systems | P | Simulator exercises and worked traces |
| 1 | C102 | Operating-System Fundamentals | P | Local/container exercises; identify kernel tasks needing a VM |
| 1 | C103 | Intro to Networking | P | Packet captures and network simulation |
| 1 | C104 | Intro to Programming (Python) | P | Executable exercises and independent test cases |
| 1 | C105 | Foundations of Cybersecurity | T | Cases, concepts and decisions; pilot |
| 1 | C106 | Cryptography Basics | P | Worked maths and small experiments |
| 2 | C107 | Secure Software Development | P | Vulnerable sample code, fixes and tests |
| 2 | C108 | Linux System Administration | R | One reusable Linux image; pilot |
| 2 | C109 | Network Services & Protocols | P | Small service containers; validate networking fidelity |
| 2 | C110 | Intro to Risk Management | T | Risk register, trade-offs and briefing |
| 3 | C201 | Network Defense & Firewalls | R | Reusable routed network and firewall topology |
| 3 | C202 | Secure Communications (TLS/VPN) | R | Certificates, tunnels and troubleshooting |
| 3 | C203 | Identity & Access Management | R | Reusable identity environment |
| 3 | C204 | Security Monitoring & SIEM | R | Shared SOC environment and replayable telemetry |
| 3 | C205 | Lab: Hardened Lab Network | R | Integrate the earlier network controls |
| 3 | C206 | Incident Response Foundations | T | Resolve overlap with C207 before outline acceptance |
| 4 | C207 | Incident Response Foundations | P | Evidence-based tabletop; resolve overlap with C206 |
| 4 | C208 | Windows Forensics | P | Supplied evidence and licensed analysis tools; pilot |
| 4 | C209 | Malware Analysis 101 | R | Isolated analysis workstation and approved samples |
| 4 | C210 | Threat Modeling & ATT&CK | T | Threat model and reasoned control selection |
| 4 | C211 | Lab: Simulated Incident Exercise | R | SOC/identity/network reuse and assessable evidence |
| 4 | C212 | Advanced Incident Response | R | Define distinct outcomes from C207 and C211 |
| 5 | C301 | Advanced Threat Hunting | R | Rich telemetry, hypotheses and repeatable investigations |
| 5 | C302 | Red-Team Fundamentals | R | Bounded objectives in the shared isolated network |
| 5 | C303 | Cloud Security (AWS/Azure) | R | Decide provider sandbox versus faithful local exercises |
| 5 | C304 | IoT & Embedded Device Security | P | Firmware/emulation; identify hardware-only objectives; draft titled differently |
| 5 | C305 | Advanced Malware Reverse-Engineering | R | Extend the isolated analysis workstation |
| 5 | C306 | Lab: Full-Scale Adversary Emulation | R | Integrated environment with reproducible reset |
| 6 | C401 | Professional Ethics & Legal Frameworks | T | Jurisdiction-aware cases and specialist review |
| 6 | C402 | Capstone Project Planning | T | Proposal, milestones, feasibility and rubric |
| 6 | C403 | Capstone Implementation | R | Project-dependent environment; approve each scope |
| 6 | C404 | Capstone Assessment & Presentation | T | Portfolio, demonstration and oral defence |
| 7 | RMC C201 | Advanced Network Defense | R | Extend network baseline with advanced faults |
| 7 | RMC C202 | Threat Hunting — Advanced | R | Demonstrate progression beyond C301 |
| 7 | RMC C203 | Cloud Security — Advanced | R | Extend the approved cloud training pattern |
| 7 | RMC C204 | IoT Firmware Reverse-Engineering | P | Toolchain, firmware evidence and reproducible exercises |
| 7 | RMC C205 | Red-Team Operations | R | Team operations, evidence and instructor controls |
| 7 | RMC C206 | Capstone Planning | T | Advanced scope, dependencies and assessment contract |
| 8 | RMC C207 | Advanced Malware Analysis | R | Demonstrate progression beyond C305 |
| 8 | RMC C208 | Incident Response — End-to-End | R | Full response workflow and handover assessment |
| 8 | RMC C209 | Cyber Threat Intelligence | P | Evidence collection, confidence and analytic products |
| 8 | RMC C210 | Cloud-Native Defense | R | Container/orchestration runtime and telemetry |
| 8 | RMC C211 | Advanced IoT Security | R | Confirm hardware/emulation requirements early |
| 8 | RMC C212 | Capstone Execution & Assessment | R | Reuse approved environments; assess individual contribution |

Wave order follows the draft terms for production organisation only. It does not
establish official prerequisites or DP boundaries. Pilots pull three courses forward
without changing their curriculum placement. Their accepted outputs count toward
the 44, rather than being discarded demonstration courses.

## 4. What counts as a finished course

Each course must have a reviewable production dossier:

1. Identity and sources: stable catalogue identity, release identity, audience,
   source editions/sections, reuse permissions, unresolved gaps and mapping status.
2. Blueprint: measurable objectives, entry knowledge, module sequence and explicit
   dependencies; sourced prerequisites distinguished from suggested preparation.
3. Workload: contact, reading, practice, assessment and independent-study minutes
   accounted for without double counting. Validate the estimate with actual students.
4. Student content: complete explanations, worked examples, diagrams where useful,
   instructions, reference links and accessible alternatives for essential media.
5. Practice: activities aligned to objectives, hints, expected outputs, feedback and
   at least one worked example before independent performance where appropriate.
6. Assessment: objective-to-evidence matrix, formative checks, summative tasks,
   marking rubrics, answer rationale, retest/remediation plan and assessment security.
7. Instructor pack: lesson preparation, facilitation notes, solutions, timing,
   likely misconceptions, marking examples and practical troubleshooting.
8. Practical pack where applicable: pinned tool/image versions, datasets and rights,
   setup/reset instructions, expected observations, resource requirements and teardown.
9. Delivery evidence: package validation, student/instructor separation, tested launch,
   resume, submission, feedback and completion in the configured LMS.
10. Release evidence: named reviewers, accepted digests, closed blocking findings,
    publication receipt, version notes, maintenance owner and next review trigger.

Do not inflate hours with generated prose or force identical module counts on every
course. Pass marks and retest rules must come from the approved assessment policy,
not the existing draft's default values. Automated quiz success is formative unless
an approved assessment arrangement explicitly establishes otherwise.

## 5. Delivery phases and exit gates

Elapsed windows are provisional, measured from a staffed start with source access.
They overlap where dependencies permit; source and reviewer delays move the dates.

| Phase | Indicative window | Owner | Deliverables and exit evidence |
| --- | --- | --- | --- |
| 0. Baseline and scope | Weeks 1–2 | Programme lead + curriculum lead | 44-entry register; source audit; draft/authoritative distinction; duplicate decisions queued; actual interpreter/model evidence; ARC branch inventory; named owners and reviewer capacity |
| 1. Course production standard | Weeks 2–4 | Instructional designer + assessment lead | Dossier templates, assessment policy, module delivery profiles, workload model, source rules and accepted blueprints for three pilots |
| 2. Engineering foundation | Weeks 2–10 | Engineering lead + LMS integrator | Verified engine; optional range stages; stable identity mapping; automated Moodle publishing; individual range launch/reset/cleanup; restart/retry and approval invalidation evidence |
| 3. Three-course pilot | Weeks 5–12 | Course owners + SMEs | C105, C208 and C108 accepted, taught in small trials, measured and revised; per-pattern effort and student timing baselines |
| 4. Production waves | Earliest week 13 onward | Production lead | Eight wave ledgers; accepted course releases; source/assessment coverage; cross-course review; term-level student trials |
| 5. Programme rehearsal | Last 4–6 weeks, preparation overlaps production | Curriculum + assessment + LMS leads | Representative student journeys, progression review, capstone coherence, instructor calibration, recovery exercises and release decision |
| 6. Maintenance | After each release | Named course owners | Issue triage, source/version review, item analysis, student feedback and controlled revisions |

Phase 0 can proceed with draft sources, but unresolved authoritative inputs must
remain visible. Affected courses can produce exploratory drafts; they cannot pass
the corresponding curriculum/release gate. Release-ready courses need not wait for
every unrelated source gap to close.

Pilot acceptance requires more than a successful ARC run:

- C105 demonstrates a meaningful course with no lab provisioning dependency.
- C208 demonstrates an assessable investigation using supplied evidence, without a
  provisioned target system; instructor and student tools are explicitly accounted for.
- C108 demonstrates a Moodle Start lab action creating a small individual range,
  repeatable reset, observable student actions, reliable assessment and verified cleanup.
- ARC creates all three pilot courses in a Moodle test destination automatically,
  with their sections, content, supported assessment activities and lab links; a
  maintainer does not have to reassemble or upload each course by hand.
- For each, an instructor other than the author can teach from the pack; proposed
  trial size is 3–5 representative students. This is usability/timing evidence, not
  statistical proof of educational effectiveness.
- Reviewers verify every summative item and rubric, resolve critical content errors,
  inspect accessibility and compare actual activity timing with the budget.
- Capture authoring, SME, QA, rework and environment hours separately. Reforecast
  the remaining courses before committing to the full production schedule.

## 6. Engineering slices, dependencies and ownership

The ARC engine (`tools/arc2/`, `tests/arc2/`) is ported from the ARC worktree onto a
branch from main; the ARC branch itself is not merged, because its base is 85 commits
behind. Do not overwrite that worktree's work or assume it is merged into main.

| Slice | Dependency | One accountable owner / likely files | Acceptance, migration and recovery |
| --- | --- | --- | --- |
| E0: engine baseline | Phase 0 | Engine owner; ARC tooling/tests and runtime setup | Run existing tests and DoD on actual supported interpreter; record HEAD, dirty files and unresolved actions. No content/schema migration. |
| E1: optional practical delivery | E0 + standard | Engine owner; manifest schema, `check.py`, `qa.py`, agent instructions, command, cmi5 runtime and tests | T/P/R fixtures all package; only R requires range evidence. Explicit not-applicable reason, no fake telemetry. Changed delivery profile invalidates affected approval. Version manifests; reject unsupported versions or migrate copies while retaining originals. |
| E2: identity and release contract | E0; coordinate with E1 | Release owner; release schema, API models/migrations, `course_content_ingest.py` adapter, contract tests | ARC identity maps to existing catalogue course; accepted package immutable; instructor files excluded from student bundle; old enrolled release preserved. Additive persistence, legacy records explicitly labelled, staging rollback tested. |
| E3: automated Moodle publishing | E2 + pilot package | LMS owner; Moodle adapter behind an ABC/registry, publication jobs, result mapping and integration tests | ARC creates course/sections/resources and supported assessments/lab links in a hidden staging course, verifies them, then activates the accepted release. Import/launch/submit/grade/resume/completion tested; duplicate jobs/events deduplicated; tenant/role boundaries enforced. Persist remote IDs and receipts; reconcile interrupted operations. Previous active release remains usable. |
| E3R: individual range delivery | E1/E2; integrate with E3 | Range owner; `worker/provisioners/`, new lifecycle service modules, `routers/ranges.py`, launch integration, contracts/tests | Moodle launch maps to one student/team attempt and immutable lab profile; create only required VMs/networks, health-check access, reset/retry and clean up. Test real backend plus failure cases. Additive session/lease persistence; reconcile resources after worker failure; preserve submitted evidence on teardown. |
| E4: production queue | E1 + pilot measurements | Engine owner; durable run/queue records, batch status and tests | Per-course jobs resume; accepted outline/content reused; bounded retries and human takeover retained; review backlog pauses new generation. No destructive migration; preserve staged runs. |
| E5: Studio UI, if needed for scale | E2/E3/E3R/E4 | UI owner; existing authoring routes, new feature, API/jobs | Same gate logic as CLI; course publication and range status visible; browser refresh does not restart work; permissions tested. Additive schema/API changes, feature flag and CLI fallback. |
| E6: programme evidence | E3 + first wave | Curriculum owner; approved mappings, progress projection and tests | Courses/attempts trace to releases; no qualification inferred from generated material; late/replayed results handled. Version projections and retain source events for rebuild. |

E5 can be deferred if CLI/API orchestration provides automated publication and lab
launch. Manual file promotion is not the completed product. API/worker contract changes
require regenerated contracts and corresponding tests; database changes require
fresh-install and upgrade evidence.

End-to-end journey to prove: instructor request → accepted blueprint → durable ARC
run → QA and accepted preview → immutable candidate release → authorized publishing
job → Moodle activity and attempt → optional individual range provision/access/reset
→ assessed evidence/result → curriculum projection → range cleanup.
The LMS owns delivery/grade decisions under the existing Moodle direction; TrueNorth
supplies its integration and curriculum view. MITE remains qualification authority.

### Automated Moodle publication contract

ARC submits an accepted course release to a durable publishing job. The job owns
Moodle credentials through the configured connector; generation stages emit typed
content and lab profiles rather than directly handling infrastructure credentials.
This is one ARC workflow from the user's perspective, with separate execution services.

The adapter maintains stable mappings for course, section, lesson/resource, assessment,
grade item and external lab activity. Use supported Moodle interfaces, never direct
database writes. During E3, inspect the target Moodle version, enabled services and
plugins and prove the exact creation/import operations available. If required activity
creation needs a Moodle plugin or additional web-service functions, implement and
test that bounded connector capability; do not assume a cmi5 ZIP or LTI launch creates
a complete Moodle course. Decide native quiz/assignment versus packaged runtime by
tested capabilities and assessment needs, retaining existing cmi5 packaging support.

Create hidden staging content, validate required objects and student permissions, then
activate once. Retrying the same release must reuse the publication record and remote
IDs. Interrupted or partial publication is reconciled before retry. Changing a course
creates a new release and follows the pinned-attempt policy; it must not overwrite
in-progress student work. Moodle results and range evidence have explicit ownership
and correlation so neither produces a duplicate grade.

### Small individual range contract

ARC designs a versioned lab profile for each activity that needs live systems:
objective, topology, approved base-image references, software/configuration, networks,
health checks, student access, evidence checks, resource limits, reset and expiry rules.
Prefer one VM for a single-host task and two or three for client/server or small
network tasks. Larger profiles need an objective-based justification; these are
design defaults, not asserted requirements for every course. No VM profile for T;
P profiles describe their real local/container needs explicitly.

Build reusable, tested base images and apply reproducible per-lab configuration.
Generated image references must resolve to the selected host's image catalogue;
ARC cannot invent a usable template ID. Promote new image builds only after boot,
configuration, access, reset and assessment checks pass. Record licences and versions.

Proposed runtime states: requested → queued → provisioning → ready → active →
expired/completed → cleaning → destroyed, with failed/reconciliation-required states.
Pause/resume and reset are explicit operations where the backend supports them.
Use tenant + student/team + activity release + attempt as the idempotent session key.
Double-click, browser refresh and worker retry must not create a second range.

Launch checks enrolment/access, quotas and profile compatibility, reserves capacity,
creates isolated networks/VMs, waits for actual service readiness, then supplies
short-lived authenticated console or desktop access. Show queue/failure/retry state
inside the student journey. Theory pages remain usable while capacity is unavailable.
Do not label a VM ready merely because a provisioner returned its ID.

Allocate isolated networks per session, prevent cross-student access and apply the
profile's egress policy. Use leases, idle/maximum lifetimes and per-user/concurrent
capacity quotas. Preserve required submissions/evidence before cleanup. Reconcile
tagged resources against session records after crashes; never delete unrelated VMs.
If snapshot restore is unsupported, use a tested rebuild reset or reject the profile.

Validate with two simultaneous students: distinct resources and access; reset one
without affecting the other; retry a failed create without leaked duplicates; expire
one session and verify only its resources disappear. Test late assessment events and
evidence retention after teardown. Measure readiness time and set the launch service
target from pilot measurements rather than promising an unmeasured startup time.

Outline/preview acceptance can authorize a course release; automated publishing and
student lab starts then run within the configured release policy and resource budget.
No per-VM manual approval is part of the intended normal student workflow.

## 7. Production operating model

Assign a course owner and a separate subject reviewer. A coordinator maintains one
register containing original code, retained identity, wave, source readiness, pattern,
hours, owner, reviewer, ARC run, review state, release, publication receipt and blockers.

Use these states: inventoried → source-ready → outline review → producing → technical
QA → subject/assessment review → student trial → release-ready → published. Record
blocked reasons independently so waiting courses are visible. ARC's two existing
human gates remain the engine gates; programme review evidence feeds those gates
and the separate publication decision rather than creating competing approvals.

Start with at most three courses in active production, one per pattern. Increase
to six only after pilot rework and reviewer turnaround are measured. Stop admitting
work when the review queue exceeds available capacity for one week.

Run weekly production triage and a review at each wave boundary. Measure accepted
courses/modules, source and assessment coverage, human hours, rework, reviewer wait,
actual student time, practical reset success and LMS defects. Generated words and
raw package counts are not completion measures. Cross-course review checks repeated
content, unexplained jumps in difficulty and progression between similarly named
college and RMC courses.

Reusable assets should be versioned: Linux workstation, network/firewall topology,
identity lab, SOC/telemetry replay, isolated analysis workstation and approved
cloud/container pattern. Reuse components while retaining course-specific scenarios,
datasets and assessment variants. Keep evidence/answer packs separate from students.

Capacity planning uses the maximum concurrent student sessions and measured resource
usage per profile, not 44 always-running environments. Measure CPU/RAM/storage, boot
and reset time, licences, egress and concurrent assessor access during pilots. Obtain
hardware for IoT objectives only after confirming simulation cannot meet them.

## 8. Staffing, effort and scheduling assumptions

These are bottom-up planning allowances, not estimates established by a benchmark.
They assume existing draft outlines are reusable, substantial text/examples still
need development, and source/licence access can be obtained. Full institutional
syllabi or specialist hardware may materially change both count and effort.

| Pattern | Initial human effort per course | Included work |
| --- | --- | --- |
| T | 60–140 hours | Design, author/edit, SME and assessment review, trial and fixes |
| P | 100–220 hours | T work plus reproducible evidence/tools and practical validation |
| R | 160–360 hours | P work plus environment, instrumentation, reset and live assessment |

Count the profiles in the inventory when maintaining this estimate. Current counts:
8 T, 12 P and 24 R = 5,520–12,400 course-production hours. Reserve a further
1,000–2,000 hours for shared engineering/assets, automated Moodle/range integration,
curriculum coordination and programme rehearsal, avoiding double counting
course-specific work. Add 20% contingency for rework and unresolved design:
approximately **7,824–17,280 human hours** in total. AI execution cost and reviewer
waiting time are separate. Pilot evidence replaces these allowances; do not treat the
range as a fixed-price quote.

Suggested staffed team: programme/curriculum lead (0.5 FTE), instructional designer
(1), content developers (2), pooled specialist/assessment reviewers (1–1.5), ARC/LMS
engineer (1), range engineer (0.5–1) and QA/accessibility support (0.5). Some people may
cover multiple roles, but independent assessment review still needs a second person.
This is about 6.5–7.5 FTE with different availability across phases.

At 25 focused production hours per FTE-week, this supplies roughly 160–190 hours/week.
The effort envelope implies approximately **42–108 working weeks**, before extended
source/approval delays. Use **12–26 months as a provisional programme planning range**
with this team, not a promise. The first three accepted pilots target weeks 8–12 if
sources, engine and LMS prerequisites hold. Publish usable waves progressively.
A smaller team lengthens the schedule; generation concurrency cannot replace SME
review capacity. Budget labour as the sum of role-hours × agreed rates, plus measured
model compute, licensed content/tools and lab infrastructure; no rates are assumed.

After the pilot, calculate remaining effort from observed accepted-module effort,
course complexity and reuse, then schedule against the scarcest role. Report both
the likely date and a risk-adjusted date. If capacity is insufficient, reduce the
initial release to the first college year rather than thinning all 44 courses.

## 9. Decisions and risks to retire early

| Decision/risk | Owner and deadline | Action if unresolved |
| --- | --- | --- |
| Official outlines, source editions, reuse rights and qualification authority | Curriculum lead, phase 0 | Mark affected objectives blocked; retain draft status; continue unrelated sourced work |
| C206/C207 overlap and advanced-course progression | Curriculum + subject leads, before affected outlines | Preserve both backlog identities; defer duplicate generation |
| C304 draft title differs from the catalogue | Curriculum lead, before wave 5 | Keep the catalogue code as identity; record the accepted title at outline review |
| Real teaching hours and term workload | Instructional designer, phase 1/pilot | Rebuild activity budget; do not inherit placeholder hours |
| Named SMEs and approval turnaround | Programme lead, week 2 | Reduce active queue and reforecast schedule |
| Supported interpreter and actual serving model | Engineering lead, preflight | Repair/choose a supported environment before claiming engine readiness |
| Moodle package/runtime and assessment compatibility | LMS lead, E3 | Keep accepted candidates staged; resolve adapter/runtime gaps before student release |
| Moodle destination, connector permissions, hypervisor host/image catalogue and resource budget | LMS + range leads, E3/E3R | Implement against fixtures; obtain concrete deployment configuration before live verification |
| Language, accessibility and student prerequisites | Curriculum lead, phase 1 | Record assumed English-first baseline; estimate translation/equivalence separately |
| Windows/cloud/tool licences, IoT hardware and permitted training samples | Range + procurement owners, pilot/design | Choose legitimate alternatives that preserve objectives or keep affected tasks blocked |
| Source material handling and model suitability | Source owner + engineering lead, preflight | Keep restricted sources in their permitted environment; do not transfer them by assumption |
| Source/tool changes after publication | Course owner, each release | Set risk-based reviews and version triggers; preserve prior student releases |

## 10. First ten working days and next implementable slice

Days 1–2: create the 44-entry production register, identify owners and source gaps,
verify ARC branch/runtime state and baseline its actual tests. Preserve user changes.

Days 3–4: audit the three pilot drafts, check institutional/source alignment, map
objectives to evidence and build realistic student-hour budgets. Resolve delivery
profiles and identify licensed tools or missing inputs.

Days 5–6: draft the standard dossier and three blueprints. Hold outline review with
the course owner, SME and assessment lead; record specific feedback and accepted versions.

Days 7–8: implement and test E1's smallest vertical slice: a theory-only module can
pass QA and package with explicit no-range status; changing it to a range activity
requires environment evidence and invalidates the relevant accepted gate.

Days 9–10: draft the first complete C105 module and private instructor pack, rehearse
its assessment, specify the Moodle publishing adapter and C108 one-VM profile, and
exercise available isolated integration fixtures. Keep actual automated publication
and provisioning acceptance outstanding until E2/E3/E3R are ready and tested.

The next implementable slice is **E0 plus the E1 theory-only fixture**, alongside
the production register and pilot blueprints. Its success is an inspectable accepted
pattern for no-VM course production, not a bulk run of 44 unreviewed packages.

Execution note (2026-10-05): the build carried out from this plan goes beyond the
first slice. It covers E0–E3R and E5 as stacked branches from main, the three pilots
produced end to end, and the remaining 41 courses as unreviewed drafts. Drafts go
through outline (accepted per wave), content and QA only: no instructor pack, sensors,
range build or package. The register is `content/catalogue/production_register.csv`.
Decisions taken on 2026-10-05: C206 is IR concepts and process (T) and C207 an
evidence-based tabletop (P) that builds on it; C304 is drafted from
`iot-security-foundations.yaml` under the catalogue title. Draft status and SME review
remain as described above.

## 11. Verification of this plan

Checks performed on 2026-10-05: all 44 inventory codes match the catalogue exactly,
with no duplicates; 8 T / 12 P / 24 R profile counts and both effort ranges recompute
correctly. `bash scripts/dod.sh` passed (exit 0) in a per-worktree
Python 3.11 venv: 1342 passed, 1 skipped, 3 xfailed; web checks not run.
No live Moodle or range validation was performed for this planning change.

Implementation verification must subsequently include engine tests, T/P/R fixtures,
approval invalidation, interrupted-run recovery, release/import rollback, tenant and
role rejection, private-material exclusion, automated Moodle creation/activation,
two-student isolated range lifecycle and cleanup, LMS attempts/results and appropriate
web checks, followed by the repository DoD. Classroom trials remain required separately
from software test success.

Primary local references: `content/catalogue/cyber_operator_programme.csv`,
`content/courses/`, `control-plane/api/app/course_content_ingest.py`,
`control-plane/api/app/routers/courses.py`, `control-plane/web/src/app/app.routes.ts`,
`docs/lms-development-design.md`, `docs/moodle-integration.md`, `docs/arc2-plan-v3.md`,
and the ARC worktree's `tools/arc2/`, `tests/arc2/`, `.claude/commands/arc2.md` and
`docs/arc2-course-studio.md`. Older status/effort claims are not used as estimates.

## 12. Production status (2026-10-07)

The per-course state is `content/catalogue/production_register.csv`; run directories are
`build/arc2/<slug>/` (git-ignored). All 44 outlines were accepted by the programme owner.

| Outcome | Count | Courses |
| --- | --- | --- |
| Pilot: released, published to the test Moodle, student walk-through passed | 3 | C105 (T), C108 (R; live lab needs vCenter), C208 (P) |
| Draft: content generated, draft QA pass | 34 | the rest, except below |
| Partial draft: defensive modules written, the rest AUTHOR-REQUIRED | 3 | C305, C306, RMC C207 |
| Held for a cleared author (AI drafting stopped by safety checks) | 4 | C302, RMC C202, RMC C205, RMC C211 |

Drafts went through outline, content and QA only: no instructor pack, sensors, range build
or package, and nothing is SME-reviewed. Each run lists its open human actions; the common
ones are the defaulted qualification (ALJQ for DP 1, TEMP67 for DP 2), reference gaps, SME
verification of commands that could not be run here, and the range build for R courses.
Labs are sized for realism (up to 20 VMs per student). Offensive and evasion content is never
drafted by ARC²; it is left to a cleared Standards author.
