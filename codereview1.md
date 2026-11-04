# TrueNorth code review — current assessment

Date: 2026-11-04

Revision: 3 — replaces the 2026-10-04 review and its implementation plan.

Reviewed baseline: `main` at `eec5390677b58bfece641698ae84d57d2d941126`, two commits after
the `v1.2.0-rc18` tag. The post-tag change is the console-relay test serialization from PR #430.

Remote state: local `main` and `github/main` are at the same commit.

Scope: current application code, tests, CI/release evidence, installer, role guides and the
17-module release contract. This is an engineering review, not an accreditation decision.

## Verdict

**Code engineering grade: 8.5/10 (A-).** The application has crossed the 8/10 line as a
software system. The six concrete defects in the October review are implemented and covered,
the contracts and schema mirrors are gated, CI exercises real PostgreSQL, OpenSearch, Moodle,
cmi5/CATAPULT, Samba AD, Keycloak and browser journeys, and `rc18` has successful CI,
helm-kind and signed-release workflows.

**Release evidence grade: 8.1/10 (B+).** The release candidate is credible, but the repository
still lacks a current rc18 staging/hardware acceptance record. The release notes and module
matrix still describe rc3/rc11 evidence, while current code is rc18.

**Unclassified COTE IOC grade: 6.8/10.** This is deliberately separate from code quality.
Real vCenter builds, physical training assets, load/endurance thresholds, bilingual and
accessibility acceptance, encryption/key-custody decisions, and low-to-high operational proof
are not completed. Simulator and unit evidence cannot earn those points.

### Quality scorecard

| Area | Oct. 4 estimate | Current | Assessment |
|---|---:|---:|---|
| Architecture | 7.0 | **8.8** | Strong adapter boundaries, generated OpenAPI/task/table contracts, 17-module acceptance model, and MOSA ratchet. Some very large central models/schema files remain. |
| Readability and maintainability | 7.0 | **8.1** | Critical invariants are unusually well documented and failure behavior is explicit. Ruff debt is held at 17; several core files remain large and current-state documents drift. |
| Testing | 6.0 | **9.2** | 13,145 non-integration tests collected locally; five CI shards use PostgreSQL and OpenSearch; real-service and browser lanes are extensive. Hardware and full installed-host acceptance remain outside CI. |
| Security | 5.0 | **8.3** | Tenant/RBAC denials, runner confinement, supply-chain scans, append-only audit, sealed credentials and console relay controls are strong. No platform-managed disk/database encryption, KMS/HSM or internal-service mTLS. |
| Operational reliability | 4.0 | **8.4** | Durable range operations, redispatch, generations, leases, fencing, reconciliation, capacity gates, backup/restore and installer validation are substantive. Real-site recovery and alert-delivery evidence is incomplete. |
| Integration readiness | 5.0 | **7.8** | Moodle, cmi5, AD, Keycloak, Greyspace and vSphere simulation are green. Real vCenter, Windows AD/AD CS, physical kits and enclave transfer operations are not yet proven. |
| Documentation and usability | not scored | **7.9** | There is now a coherent install-to-operations guide set for administrators, instructors and Students. The clean-room paper review found unresolved ambiguity, especially in installation and operations. |

The 8.5 engineering grade is not an average that hides a release blocker. Integration and
documentation are below 8 because their missing evidence is real. The fastest honest route to
an all-areas 8+ is in the plan below.

## What changed since the previous review

The October review was a plan against `083aeb75`; current `main` is roughly 2,305 changed
files and 490,000 added lines beyond that baseline, much of it authored course content and
instructor material. The important engineering changes are:

- classes, roll call, onboarding, booking approval, per-session ranges, teams, attendance,
  close-out, Student **My classes**, and events/content calendars;
- Moodle publication and grade return, cmi5 over LTI 1.3, a CATAPULT-judged AU runtime and
  staff deep linking;
- cross-enclave result envelopes, trust keys, controlled-media export/import and replay-safe
  Secret-side ingestion foundations;
- durable range-operation records with retry, generation ordering, worker leases, stale-task
  fencing, reconciliation and operator recovery;
- ARC² tenant ownership, runner deadlines and OS confinement, batch course production and
  local-model fallback;
- persistent collision-free network reservations, range credentials, host-local template
  copies and range-certification evidence;
- content review clocks, signed offline feeds, content porting and substantially expanded
  course/instructor content (104 course YAML files and 97 instructor packs at review time);
- a WebMKS console relay with short-lived grants, re-authorization, bounded dialing, log
  redaction and a server-aware view-only RFB allowlist;
- a broader deterministic noise engine, including web, DNS, mail, file sharing, AD, SSH,
  NTP, ping, RDP negotiation, FTP and IPP status traffic; and
- a clean-room guide set covering prerequisites, install, first-day administration,
  instructors, Students, operations and guide acceptance.

## Previous findings: closure check

| ID | Previous finding | Current status | Current evidence |
|---|---|---|---|
| CR1-01 | ARC² Studio did not enforce tenant ownership | **Closed** | `routers/arc2_studio.py` records/checks `tenant_id`; `tools/arc2/assign_owner.py` handles legacy runs; `test_arc2_studio_tenancy.py` and confinement tests cover foreign runs. |
| CR1-02 | Runner deadline depended on stdout | **Closed** | `tools/arc2/runner.py` uses a monotonic deadline, reader thread, process group and TERM/KILL grace; `test_arc2_runner_deadline.py` covers silent children and cleanup. |
| CR1-03 | Wiki revision update was non-atomic | **Closed** | Compare-and-set revision claims and 409 responses cover edit, metadata and restore conflicts; `test_wiki_concurrency.py` includes a real PostgreSQL row-lock test. |
| CR1-04 | Noise management addresses/VLANs could collide | **Closed** | `network_reservations` has database uniqueness constraints and worker/API mirrors; API, worker, migration and vSphere-simulator tests cover allocation and release. |
| CR1-05 | Range start/stop claimed state despite failed dispatch | **Closed** | `range_ops` accepts durable operations, tracks attempts/status/error and redispatches pending work; API tests cover crash and broker refusal. |
| CR1-06 | Stale power retries could reverse newer intent | **Closed** | Generation fencing, range leases, tombstones and heartbeat/reconciliation tests prevent stale or duplicate side effects. |

No reopened version of these six defects was found in the current source. Their designs are
now among the stronger parts of the application.

## Current findings and risks

These are the actionable items that keep the whole review from an across-the-board 8.5.

### R3-01 — P1: release truth is fragmented and stale

`RESUME.md`, `docs/current-state.md`, `docs/release-notes/v1.2.0.md` and the module matrix
still anchor important claims to rc3, rc11 or rc15. Current code and GitHub are at rc18.
This makes a reviewer choose between code, an old evidence record and a changelog marked
“unreleased.” It is not a runtime defect, but it is a release-control defect.

**Close when:** one rc18 evidence record names the commit and image digests, all 30 CI jobs,
installed-host version, strict acceptance result, staging/browser journeys and explicit H
skips. The current-state and handover documents link to that record instead of copying old
counts.

### R3-02 — P1: real infrastructure proof is still missing

The vSphere backend, host-local copies, range lifecycle and Greyspace path have strong vcsim
coverage, but release documentation still says no real writable vCenter run. Physical GRIMM
TinyTown/car-hacking systems have an architecture plan, not a vendor contract or executed
adapter. Windows AD/AD CS qualification is also outstanding.

**Close when:** the intended work-site candidate builds, certifies, uses, snapshots and tears
down representative enterprise, cloud and OT ranges on real infrastructure; evidence includes
failure cleanup, network isolation and capacity measurements. Vendor discovery then defines a
minimal physical-asset adapter and one supervised TinyTown journey.

### R3-03 — P1: shipped encryption depends too much on the host boundary

The edge and OpenSearch use TLS and sensitive credential columns are sealed, but PostgreSQL,
Redis, MinIO, Keycloak and compose-internal API traffic are plain. TrueNorth configures no
disk encryption, database TDE, MinIO SSE, Vault/KMS/HSM, or FIPS-validated crypto. Backups
protect secrets but not all application data. This is documented accurately in
`docs/security.md`; it remains a material accreditation gap.

**Close when:** an approved threat model assigns encryption controls to platform or host,
the installer verifies those controls, Secret-grade keys have custody/rotation/recovery
procedures, and an acceptance test proves data and backups are protected as designed.

### R3-04 — P2: the excellent browser lane is not yet a protected-branch fact

The current CI run includes a successful real-auth `journeys` job covering persisted user
outcomes, but GitHub issue #197 remains open to make that job a required status check. A green
optional job is evidence; a required job is a release control.

**Close when:** `journeys`, the aggregate Python job, integration, supply-chain and contract
gates are verified as required checks on `main`, with a test PR demonstrating that each blocks
merge on failure.

### R3-05 — P2: clean-room documentation is close, not finished

The seven-guide documentation set is a major improvement, but open issues #410–#416 record
paper-dry-run ambiguity. The operations guide has the heaviest concentration: backup VM disk
setup, restricted rsync, host naming, recovery prerequisites, content-port UI wording and
failure text. Hardware-specific switch/firewall commands remain explicitly untested.

**Close when:** those issues are reconciled against code, a person unfamiliar with the install
runs the clean-room protocol, every guessed step is removed, and the resulting PDFs are built
and attached to the candidate release.

### R3-06 — P2: module completion and programme readiness are being conflated

The 17-module matrix is a useful software acceptance contract. It does not prove 70,000-vCPU
capacity, content accreditation, bilingual operation, accessibility, physical asset custody,
or multi-enclave authorization. `docs/cote-unclassified-8.5-plan.md` makes this distinction,
but summary documents still invite the reader to treat “module green” as “COTE ready.”

**Close when:** release evidence, Unclassified IOC evidence and COTE/PSOR traceability are
three separate scorecards with explicit owners, thresholds and non-compensating gates.

### R3-07 — P2: Exercise Template routes are declared twice

`control-plane/web/src/app/app.routes.ts:290-321` repeats both `exercises/templates` route
objects. Angular resolves the first match, so this does not currently create a different user
outcome, but it is silent navigation drift and evidence that route identity is not contract-
checked.

**Close when:** the duplicate declarations are removed and a web contract test fails on two
reachable route definitions with the same normalized path.

## Module assessment

Scores below combine implementation quality and the best current evidence. “Operational
proof” identifies the main reason a module is not higher; it is not a claim that the code is
broken.

| # | Module | Score | Current assessment / next proof |
|---:|---|---:|---|
| 1 | platform-core | 8.8 | Strong tenancy, RBAC, audit, identity and contract gates. Finish encryption/key-custody and current installed-host proof. |
| 2 | web-shell | 8.6 | Production build, role guards, e2e and real-auth journeys pass. Finish accessibility and French journeys. |
| 3 | infra | 7.9 | Installer, Helm, health, metrics, backup and load smoke are substantial. Clean-room recovery, alerts and real-site evidence remain. |
| 4 | scenario-engine | 8.1 | Lifecycle and deterministic scenario evidence are strong. More real host actions/sensors and adversarial failure runs are needed. |
| 5 | ai-arc2 | 8.4 | Ownership, confinement, deadlines, gates and fallback are solid. Production content approval and installed-host model evidence remain. |
| 6 | lms | 9.1 | Real Moodle, LTI 1.3, cmi5/CATAPULT and grade return are unusually well tested. Keep installed Moodle evidence current. |
| 7 | curriculum | 8.7 | QSP/PO/EO/path traceability and expanded programmes are strong. Accreditation/SME acceptance remains a programme gate. |
| 8 | detection | 8.5 | Server-side scoring and real OpenSearch coverage are strong; validate against representative live telemetry. |
| 9 | reporting-aar | 8.5 | Staff/Student access, completion and exports are covered. Validate classified retention/redaction policy. |
| 10 | content | 8.3 | Large, structured course and instructor library with QA/review clocks. Draft status and human instructional validation cap the score. |
| 11 | ranges-labs | 8.1 | Durable lifecycle, capacity, credentials, consoles and certification exist. Real vCenter and endurance evidence are mandatory next. |
| 12 | provisioners | 7.7 | Clean adapter and simulator coverage; real ESXi/vCenter, Windows images and failure cleanup are the missing proof. |
| 13 | telemetry | 8.1 | Ingest/search/authorization and xAPI are sound. Host sensors and real-range evidence remain limited. |
| 14 | scheduling | 8.8 | Classes, approval, calendars, capacity, attendance and close-out form a coherent workflow. Run class-size/concurrency acceptance. |
| 15 | noise-engine | 8.6 | Deterministic plans, target guards, real protocol openings and UI controls are strong. Prove safe sustained operation on a built range. |
| 16 | wiki-helpdesk | 8.4 | Tenant access and concurrency are fixed; browser journey exists. Images/push refinements are non-blocking enhancements. |
| 17 | greyspace | 7.9 | T0 stack and vcsim integration pass. Real network behavior, isolation and workload scale are not yet proven. |

## Plan to make every engineering area at least 8.5

### Gate 1 — Make rc18 the single source of release truth

1. Create an rc18 evidence page from workflow run `41912955624`, helm-kind run
   `41912956056` and release run `41917415999`.
2. Record job/test counts from artifacts, image digests, SBOM/audit results and permitted
   skips; do not copy rc11 numbers forward.
3. Update `RESUME.md`, `docs/current-state.md`, the release notes and module matrix to point
   at this record.
4. Close or re-scope stale R0 issues that describe already-merged work.

**Exit:** a reviewer can answer “what commit, what passed, where, and what did not run?” from
one page. Documentation rises above 8.2 and release evidence above 8.4.

### Gate 2 — Finish the clean-room documentation loop

1. Resolve issues #410–#416 against current UI, installer variables and scripts.
2. Run the guide claim tests and render all guide PDFs.
3. Give the bundle to a fresh operator for the clean-room install; log every guess and feed it
   back into the guide.
4. Execute backup restore, alert delivery and upgrade/rollback on that installation.

**Exit:** no undocumented decision or assumed value in the critical install/recovery path;
documentation and operational reliability reach 8.5.

### Gate 3 — Turn CI success into enforced policy

1. Require the aggregate Python, integration, real-auth journeys, supply-chain, MOSA/contracts,
   Angular and install-lint checks on `main`.
2. Use a deliberate failing PR to prove each required check blocks merge.
3. Keep the five-shard completeness/PostgreSQL-skip guard and artifact retention.
4. Add a compact release-evidence generator so job names/counts cannot drift from the matrix.

**Exit:** a merge cannot bypass the evidence that produced the 8.5 code grade.

### Gate 4 — Prove the physical deployment boundary

1. Install the signed candidate on the intended platform and register the real vCenter.
2. Build and certify small representative enterprise, cloud and OT ranges; exercise console,
   snapshot, power, teardown, retry and partial-failure recovery.
3. Measure readiness time, cleanup, resource accounting and isolation; retain artifacts.
4. Complete GRIMM/vendor discovery, implement inventory/reservation/health/evidence first,
   and execute one supervised TinyTown or equivalent journey before automating unsafe actions.

**Exit:** infra, provisioners, ranges, telemetry and Greyspace all have real-environment
evidence and score at least 8.5.

### Gate 5 — Close the security/accreditation delta

1. Approve data classification for Unclassified pass/fail, sealed numeric results and Secret
   identity/grade authority.
2. Select the data-diode interface and test atomic delivery, duplicates, outage, replay,
   corrections, key rotation and quarantined import.
3. Decide and verify host/platform controls for encryption at rest and internal traffic; add
   KMS/HSM integration where the authority requires it.
4. Complete threat modeling and adversarial review for the console relay, exercise execution,
   content port and result transfer.
5. Run English/French and WCAG-focused journeys for Student, instructor and administrator.

**Exit:** security and integration reach 8.5 for the Unclassified IOC scope. Secret remains a
separate deployment/accreditation milestone until tested there.

## The 10× TrueNorth strategy

This is the step beyond “make every module 8.5.” It is a proposed product direction, not a
claim that the capabilities below are implemented. The objective is not ten times as many
screens or courses. It is to make the complete training-to-readiness loop ten times faster,
more credible and more difficult to reproduce.

### Product thesis

TrueNorth should become the **Cyber Mission Readiness Operating System** for DND/CAF:

> Given an approved mission, threat and cohort, TrueNorth identifies the readiness gap,
> prepares people, creates a representative digital/physical environment, conducts the
> exercise, captures evidence, produces a defensible individual/team/unit assessment, assigns
> remediation, proves the improvement and transfers the authoritative result to the correct
> security enclave.

Commercial platforms already join individual labs, team exercises and readiness mapping.
RangeForce describes a `prime → drill → map → upskill` cycle, and Cyberbit markets rapid
exercise setup with SOC-level measurement. TN should not imitate their catalogue race. Its
moat should be sovereign on-prem operation, mission traceability, real infrastructure,
cyber-physical fusion, evidence provenance and multi-enclave delivery.

- RangeForce: <https://www.rangeforce.com/platform>
- Cyberbit: <https://www.cyberbit.com/product/cyber-range/>
- NATO interoperability principle: <https://www.nato.int/en/what-we-do/deterrence-and-defence/interoperability-connecting-forces>

The implementation-ready program control pack is in
[`docs/programs/truenorth-10x/README.md`](docs/programs/truenorth-10x/README.md). It splits
current foundation closure (`F0–F7`) and 10× capability delivery (`00–07`) into
dependency-managed branches, includes a Claude orchestrator prompt and defines a supervised
Taz/GLM-5.3 worker protocol.

### The one demonstration that proves TrueNorth works

The demonstration that defines “10×” is one uninterrupted, replayable journey:

1. A commander selects a mission, unit and readiness date.
2. TN shows the mission tasks, current evidence, confidence and gaps without inventing a
   readiness score for missing evidence.
3. The instructor chooses a proven exercise package and a sanitized digital twin of the
   intended enterprise/cloud/OT environment.
4. TN performs capacity, isolation, image, content, safety and staffing preflight.
5. Approved infrastructure is built; physical assets are reserved and checked.
6. Students complete targeted preparation and join through one task-focused page.
7. The exercise runs with realistic benign traffic, controlled adversary activity, white-cell
   injects and physical effects.
8. The AI director may recommend tempo changes, but every non-preapproved action is explained,
   bounded and approved by a human.
9. TN records technical, process, leadership and teamwork evidence on one live timeline.
10. The timeline becomes an evidence-backed AAR within minutes of ENDEX.
11. TN recommends specific remediation and schedules a shorter validation event.
12. Unclassified retains only approved pseudonymous data and pass/fail; the sealed numeric
    result moves through the approved one-way path to the authoritative Secret record.

If a fresh operator can run this on supported hardware and reproduce the evidence, TN is no
longer merely a broad training application.

### Proposed 10× outcome measures

These are candidate service objectives for owner/operator ratification. Baselines must be
measured before targets are adopted; a target without an environment and workload definition
is not acceptance evidence.

| Outcome | Proposed target |
|---|---|
| Exercise preparation | A proven package can be scheduled for a qualified cohort with no more than 30 minutes of instructor/operator work. |
| Environment readiness | At least 99% of supported, preflight-approved range builds become ready inside the environment-specific SLO. |
| Resource hygiene | Zero unaccounted VMs, networks, grants, credentials or physical-asset holds after an accepted teardown. |
| AAR latency | Draft technical timeline and evidence links available within 15 minutes of ENDEX; human adjudication remains explicit. |
| Evidence coverage | At least 95% of readiness assertions link to source evidence, environment, scorer version, assessor and expiry policy. Missing evidence is shown as unknown. |
| Reproducibility | The same signed exercise package and seed reproduce the planned baseline, while approved adaptive decisions are replayable. |
| Recovery | Broker, worker, API and hypervisor interruption tests meet declared recovery objectives without reversing newer operator intent. |
| Cross-enclave integrity | 100% of accepted result packages are signed, encrypted, schema-valid, idempotent and provenance-linked; duplicate delivery changes nothing. |
| Accessibility/language | Critical Student, instructor, operator and commander journeys pass the approved English/French and accessibility gates. |
| Scale | Capacity is proven in named tiers. The 70,000-vCPU ambition uses federated cells and measured admission control, not an untested single control plane. |

### Six product moats

#### 1. Mission readiness evidence graph

Evolve the existing competency assertions and unit-readiness report into a temporal evidence
graph:

```text
mission → mission task → role → PO/EO → competency → observable behaviour
                                            ↓
person → team → unit → attempt → evidence → assessor/scorer → confidence/expiry
                                            ↓
environment → threat → tool/process → AAR finding → remediation → re-test
```

Every assertion must distinguish observed fact, deterministic calculation, instructor
judgment and AI inference. Preserve the evidence, scoring policy, version, expiry and any
human adjustment. Commanders receive readiness decisions; Students receive understandable
development guidance; instructors receive the gaps they can actually train.

#### 2. Sanitized digital-twin compiler

Create an import pipeline for approved vCenter/NetBox/Terraform/architecture inventories.
It must remove identities, secrets, addresses and unnecessary operational detail, replace
systems with promoted equivalents, retain the behaviours needed for the learning objective,
and produce a reviewable range-template diff. It is a compiler with validation and provenance,
not an AI prompt that silently creates infrastructure.

#### 3. Exercise operating system and safe AI director

Unify the MESL, scenario runner, live class view, Ops Center, telemetry, scoring and AAR around
one versioned event timeline. Add three explicit execution modes:

- `scripted`: accredited, deterministic and fully preapproved;
- `adaptive`: selection only from approved actions and policy bounds; and
- `generative`: proposals held at a human approval gate.

The director can operate NPC email/helpdesk/leadership personas, recommend interventions and
summarize evidence. It cannot provision, attack, grade, cross an enclave boundary or alter an
official record merely because a model requested it. Every action has an authorization,
idempotency key, evidence trail, timeout, kill switch and replay record.

#### 4. Virtual, cloud and physical fusion

Introduce a vendor-neutral Physical Training Asset Provider for TinyTown, car-hacking and
future ICS/IoT systems. Inventory, reserve, preflight, access, health, evidence, reset,
release, custody and quarantine are core operations. A vendor may declare start/reset as a
manual instructor step. TN must display that truth instead of simulating automation.

#### 5. Enclave-native content and results

Complete the content-port and result-transfer foundations as one governed product experience:
signed release bundles, enclave-local keys and identity, data-diode/controlled-media adapters,
quarantine, deterministic validation, replay protection, corrections and non-sensitive
receipts. Treat Secret as the authoritative identity/grade store and Unclassified as a
pseudonymous training system under the approved data-classification decision.

#### 6. Federation and extension SDK

Publish stable, capability-declared contracts for hypervisors, clouds, physical assets,
telemetry sources, injectors, validators, LMS/LRS systems and cross-domain transports. Ship a
simulator, conformance suite, signed package format and compatibility policy. Federation must
allow a larger event to use several independently governed TN cells without sharing databases,
keys or administrative trust.

### Flagship exercise portfolio

Do not add another large wave of draft courses until three end-to-end exercises are proven by
human SMEs on real environments.

| Exercise | What it must combine | Readiness decision it supports |
|---|---|---|
| **Northern Shield** | Enterprise AD compromise, email/helpdesk NPCs, endpoint and network telemetry, SIEM/EDR, lateral movement, containment, leadership and communications | Can this team detect, understand, contain and communicate an enterprise intrusion? |
| **Cloud Breaker** | Hybrid identity, Azure-like cloud, containers, CI/CD, IaC, cloud logging and recovery | Can this team defend and restore a hybrid-cloud mission system? |
| **TinyTown Blackout** | Enterprise IT, OT/HMI, TinyTown, vehicle/CAN component, safety gates, physical effects and sensor evidence | Can technical and command teams manage a cyber-physical incident without creating unsafe effects? |

Each package must be bilingual, accessible, versioned, content-reviewed, threat-mapped,
capacity-sized, safe to reset, portable between approved enclaves and accompanied by an
instructor rehearsal and scoring calibration pack.

### Delivery slices

Each slice delivers usable behaviour and retains a safe recovery path. One owner controls
shared OpenAPI, event, task and database contracts.

#### X0 — Credibility and product hygiene

**Outcome:** a buyer can trust the candidate before seeing a new feature.

- Reconcile rc18 release truth, clean-room guides and hardware evidence as Gates 1–4 above.
- Remove the duplicate `exercises/templates` and `exercises/templates/:id` route declarations
  in `control-plane/web/src/app/app.routes.ts:290-321`; add a route-uniqueness contract test.
- Establish role-task usability baselines and measure build/AAR/operator effort.
- Freeze new top-level navigation and unreviewed content expansion until the north-star
  journey has an owner and an acceptance harness.

**Contracts/files:** `app.routes.ts`, navigation permissions, release evidence, guide claim
tests. **Migration:** none. **Acceptance:** unique routes, current evidence page, clean-room
reader completes the supported journey without guessing. **Recovery:** route redirects retain
old deep links; no persisted data changes.

#### X1 — Readiness evidence ledger

**Outcome:** a Student, instructor and commander see the same facts at the appropriate level,
with provenance rather than a mysterious score.

- Add append-only observations/assertions, evidence references, scoring-policy versions,
  assessor type, confidence, validity window, supersession and classification markings.
- Project current competency, detection, attendance, course, exercise and AAR outcomes into
  the ledger without rewriting their source records.
- Make readiness queries tenant/unit scoped and explain every roll-up.

**Contracts/files:** new focused `app/readiness_graph/` module; existing
`classes/readiness.py`, `routers/competency.py`, detection/AAR services,
`worker/tasks.py::auto_assess_competency`, generated OpenAPI and the readiness/progress web
features. **Migration:** new append-only tables and idempotent backfill, with source IDs unique.
**Acceptance:** two-tenant denial matrix; one learner-to-unit trace; expired/conflicting/missing
evidence; deterministic replay; CSV/PDF/API parity. **Recovery:** old readiness report remains
read-only behind a feature flag until ledger parity is signed off.

#### X2 — Unified live exercise timeline

**Outcome:** instructors operate one live event surface and receive an evidence-linked AAR.

- Define a versioned exercise-event envelope for injects, telemetry milestones, console/range
  state, white-cell decisions, participant actions, safety events and scoring evidence.
- Fold the class live view and Ops Center into role-specific projections of the same timeline.
- Bookmark/adjudicate during the event; generate the draft AAR from those immutable events.

**Contracts/files:** scenario-run/event modules, WebSocket channels, `classes/console_router.py`,
Ops Center/live-class components, AAR services and event schemas. **Migration:** event stream
and bookmark/adjudication tables. **Acceptance:** pause/resume/reconnect, duplicate and
out-of-order events, role redaction, Student isolation, ENDEX-to-AAR target and browser journey.
**Recovery:** event ingestion is append-only; projections are rebuildable; existing Ops Center
stays selectable until parity.

#### X3 — Sanitized digital-twin compiler

**Outcome:** an architect turns an approved source inventory into a reviewable training range
without transferring operational secrets.

- Build source adapters, normalization, sanitization policy, component substitution and a
  range-template compiler.
- Require human review of the source-to-template diff and prove that secrets/identifiers cannot
  reach content bundles, logs or model prompts.
- Compile only to existing validated range-template and provisioner contracts.

**Contracts/files:** new `app/digital_twins/`, range-template schemas/designer, content port,
network inventory, golden-image catalogue and worker provisioner contracts. **Migration:**
import jobs, mappings and provenance. **Acceptance:** seeded imports, malicious source fields,
secret scan, stable output, unsupported-component refusal and build on vcsim then real vCenter.
**Recovery:** source artifacts are encrypted/quarantined with retention; generated templates
are drafts and can be deleted without touching the source system.

#### X4 — Constrained AI exercise director

**Outcome:** an instructor can safely increase exercise realism and reduce white-cell load.

- Introduce policy-bounded action registry, recommendation engine, approval queue, executor,
  kill switch and replay.
- Start with NPC communications and timing recommendations; add technical actions only after
  adversarial review and a proven sandbox.
- Keep scoring advisory until a human accepts an evidence-linked adjudication.

**Contracts/files:** new `app/exercise_director/`, AI backend contracts, scenario engine,
worker task contract, MESL/exercise templates and live timeline UI. **Migration:** director
sessions, proposals, approvals and action receipts. **Acceptance:** prompt injection, stale
approval, duplicate dispatch, model outage, policy escape, kill switch, replay and audit
redaction. **Recovery:** feature off by default; scripted exercises work with AI absent.

#### X5 — Physical asset provider and TinyTown pilot

**Outcome:** an instructor reserves, checks, uses and safely returns a physical kit alongside a
virtual range.

- Implement generic capability declarations before a GRIMM-specific adapter.
- Begin with inventory, reservation, health, custody and evidence; keep unsafe start/reset
  manual until the vendor interface and safety authority approve automation.
- Make physical capacity participate in class approval and preflight.

**Contracts/files:** new `app/physical_assets/`, scheduler/capacity, class sessions,
`exercise_templates` physical delivery, evidence timeline and an enclave-local gateway.
**Migration:** asset, capability, reservation, custody, calibration and quarantine tables.
**Acceptance:** reservation races, failed health, manual step, emergency stop, evidence clock
alignment, safe reset and custody handback. **Recovery:** quarantine the asset without losing
exercise history; virtual exercise remains operable without the adapter.

#### X6 — One-way result and content operations

**Outcome:** approved packages move between independent test enclaves without creating a
general network trust path.

- Finish trust-key administration, outbox/quarantine operations, diode/media adapters,
  receipts, correction workflow, key rotation and operator dashboards.
- Prove Unclassified persistence contains no readable numeric grade or identity mapping under
  the approved model.

**Contracts/files:** existing `result_transfer`, `content_port`, signing, installer roles,
audit export and new administration UI. **Migration:** additive package/receipt state only.
**Acceptance:** lost/duplicate/out-of-order media, destination outage, malware rejection,
wrong enclave/key, replay, supersession, rotation and log/support-bundle inspection.
**Recovery:** transport-neutral outbox permits controlled media when a diode is unavailable;
no reverse connection is assumed.

#### X7 — Federation and scale cells

**Outcome:** TN coordinates large exercises across independently governed cells and proves
capacity tiers on the path toward 70,000 vCPU.

- Define a federation manifest, cell capabilities, signed event exchange, time synchronization,
  admission, health and degraded-mode rules.
- Keep identity, secrets, databases and administrative control local to each cell.
- Aggregate only approved event/readiness products at the coordinating layer.

**Contracts/files:** extension SDK, federation service, capacity model, event schemas,
observability and install profiles. **Migration:** federation peers/trust and event cursors.
**Acceptance:** mixed versions, partition, clock skew, duplicate events, cell loss, overload,
cross-tenant/enclave denial and tiered load/endurance reports. **Recovery:** a cell continues or
ends safely according to the exercise plan when the coordinator is unavailable.

### Portfolio rules

- Spend roughly 70% of near-term capacity on proof, reliability, UX and security; 20% on the
  readiness/timeline differentiators; no more than 10% on unproven experiments.
- No new feature earns “implemented” without a role journey and an observable outcome.
- No simulator result is promoted to hardware evidence.
- No AI output becomes an official grade or infrastructure action without declared authority.
- No vendor SDK enters core domain code; adapters declare unsupported capabilities honestly.
- No fourth flagship exercise starts until the first three pass instructor rehearsal,
  teardown, AAR and scoring calibration.
- Do not optimize one control plane for 70,000 vCPU. Prove cell economics and federation first.
- Prefer deleting, consolidating or hiding weak surfaces over adding another navigation entry.

### The next implementable slice

Start with **X0**, then deliver the smallest vertical part of **X1**:

1. Remove the duplicate exercise-template routes and add the uniqueness test.
2. Publish the rc18 evidence record and capture baseline operator/instructor timings.
3. Write the readiness-ledger ADR and classification model with the privacy/security authority.
4. Persist one append-only evidence type from a completed class result.
5. Show that evidence, source and expiry on the existing unit-readiness page.
6. Prove tenant/unit denial, supersession and deterministic rebuild on PostgreSQL.

That slice creates visible commander value without committing to a premature universal score,
AI director or federation architecture.

## Verification record for this review

- Source identity: local `main` and local `github/main` resolve to
  `eec5390677b58bfece641698ae84d57d2d941126`; tag `v1.2.0-rc18` remains at
  `74b8e2d02fc8a06d0fee156bec3b971574d34037`.
- GitHub Actions at the tagged commit: **CI 30/30 jobs successful**, helm-kind successful and
  Release successful. That evidence is not relabelled as a run for the two-commit-newer head.
- CI includes Python shards, PostgreSQL/OpenSearch, integration acceptance, Angular/Karma,
  production builds, e2e, real-auth browser journeys, Moodle, cmi5/CATAPULT, Samba AD,
  Keycloak, Greyspace, vcsim, load smoke, supply-chain, installer lint and contract gates.
- Local interpreter: native arm64 Python 3.11.16 in `.venv`.
- Local DoD: the ratchets and generated-contract checks passed, then the sandboxed run reached
  965 passes and 21 environmental skips before eleven console tests failed because the sandbox
  refused their loopback socket bind (`PermissionError: [Errno 1]`). The run was stopped after
  confirming that common cause rather than spending the remainder of the suite repeating it.
  Re-running both affected files with loopback permission gave **125/125 passes**. The default
  DoD intentionally excludes integration and web checks; those are represented by the current
  GitHub run above. No new `.dod-pass` is claimed for this documentation-only change.
- Worktree note: four unrelated, untracked Terraform/Start-Here documents existed before this
  review and were not modified.

### Evidence limits

No claim in this review upgrades simulator evidence to real hardware, CI to an installed-host
run, a draft course to accredited content, or an encryption design to an authorization. Open
GitHub issues are used as backlog evidence, not automatically treated as reproduced defects.
