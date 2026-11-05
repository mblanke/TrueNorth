# TrueNorth whole-product closure and 10x delivery program

Status: proposed program plan; capabilities are not implemented merely because they appear here.
Owner: TrueNorth product owner.
Program integrator: Claude Code, unless the owner assigns another integrator.
Optional worker: Taz running GLM-5.3 after the served model and tool path are verified.

## Why this is a control pack, not one giant branch

The program has two lanes. The **foundation closure lane (`F0–F7`)** closes what is currently
broken, stale, unproven or blocked by operational evidence. The **10x capability lane
(`00–07`)** builds readiness, exercise, digital-twin, AI, physical, enclave and federation
capabilities. One giant branch would hide dependencies, create conflicting migrations and
make review nearly impossible. This directory lets one integrator manage both lanes while
workers read only the section they need.

The unit of delivery is one independently reviewable pull request. The integrator owns shared
contracts, migration order, `STATUS.yaml` and merge sequencing. Workstream agents never edit
`STATUS.yaml` and never assume another proposed workstream has landed.

## Product promise

Given an approved mission, threat and cohort, TrueNorth should identify the readiness gap,
prepare the people, create a representative digital and physical environment, conduct the
exercise, capture evidence, produce a defensible individual/team/unit assessment, assign
remediation, prove the improvement and transfer the authoritative result to the correct
security enclave.

This does not mean adding a universal readiness score. Missing evidence remains unknown.
Observed facts, deterministic calculations, instructor judgments and AI inferences remain
distinguishable and traceable to their sources.

## The one demonstration that proves TrueNorth works

The finished program must make this one uninterrupted journey reproducible:

1. A commander selects a mission, unit and required-readiness date.
2. TrueNorth shows applicable mission tasks, evidence, confidence, expiry and gaps without
   converting missing data into a passing score.
3. An instructor selects an approved exercise package and a reviewed, sanitized digital twin.
4. TrueNorth preflights capacity, isolation, images, content, safety, staff and physical assets.
5. Approved infrastructure is built and physical assets are reserved and checked.
6. Students complete targeted preparation and join from one task-focused workspace.
7. The exercise runs with benign traffic, controlled adversary activity, white-cell injects
   and, where approved, physical effects.
8. An AI director may recommend or select only policy-approved actions. Anything outside the
   approved envelope waits for a named human decision.
9. Technical, process, leadership and teamwork evidence appears on one versioned timeline.
10. That timeline becomes a source-linked draft AAR within the agreed service objective.
11. TrueNorth assigns focused remediation and schedules a shorter validation event.
12. Unclassified retains only approved pseudonymous/pass-fail data; the sealed authoritative
    result moves through the approved one-way process to Secret.

## Existing foundations to extend, not rebuild

Source inspection at `eec5390677b58bfece641698ae84d57d2d941126` (two commits after the
v1.2.0-rc18 tag) found working foundations for class close-out and pass/fail results, unit
readiness, competency assertions, exercise runs, scenario evidence, content porting, result
transfer, telemetry/LMS integrations, range lifecycle jobs and a `physical` exercise delivery
type. It also found a current Ansible Moodle stage, installer variables and an installed-host
runbook; Moodle's remaining foundation work is proof and reconciliation, not rebuilding the
installer from an older claim. Each packet requires its agent to recheck current source before
work.

## Program outcomes by role

| Role | Observable outcome |
|---|---|
| Student | One preparation/run/remediation workspace; clear pass/fail and development guidance; no exposure to commander-only or cross-enclave data. |
| Instructor | Reusable packages, preflight, live control, evidence-linked adjudication and rapid AAR drafting without spreadsheet reconciliation. |
| Range operator | Capacity and dependency checks, truthful lifecycle state, explicit physical/manual steps, safe retry and complete teardown accounting. |
| Commander | Mission-task gaps, source provenance, confidence/expiry and change over time; unknown stays unknown. |
| Security/admin | Tenant and enclave boundaries, signed exchanges, audit evidence, retention controls and deterministic recovery. |

## Proposed outcome measures

These targets require owner/operator ratification and named test environments. They are not
claims about the current release.

| Outcome | Candidate acceptance target |
|---|---|
| Exercise preparation | Schedule a previously proven package for a qualified cohort with at most 30 minutes of instructor/operator work. |
| Environment readiness | At least 99% of supported, preflight-approved builds become ready inside the environment-specific SLO. |
| Resource hygiene | Zero unaccounted VMs, networks, grants, credentials or physical-asset holds after an accepted teardown. |
| AAR latency | Draft timeline and evidence links within 15 minutes of ENDEX; human adjudication remains explicit. |
| Evidence coverage | At least 95% of readiness assertions link to evidence, environment, policy/scorer version, assessor and expiry policy. |
| Reproducibility | A signed package and seed reproduce the planned baseline; adaptive decisions remain replayable. |
| Cross-enclave integrity | Every accepted package is signed, encrypted, schema-valid, idempotent and provenance-linked; duplicates change nothing. |
| Accessibility/language | Critical Student, instructor, operator and commander journeys pass the approved accessibility and English/French gates. |
| Scale | Capacity is proven in named tiers; the 70,000-vCPU ambition uses federated cells and measured admission control. |

## Architecture rules

1. **Evidence before inference.** No readiness assertion exists without provenance and a
   missing-data state.
2. **One timeline, many projections.** Scenario, inject, telemetry, scoring and AAR views use
   a common event envelope; they do not create competing clocks.
3. **Control is policy bounded.** AI proposes; deterministic authorization decides what may
   run. Every effect has an idempotency key, timeout, audit event and kill path.
4. **Providers tell the truth.** A physical vendor may require a manual start/reset. TrueNorth
   displays that state instead of pretending it automated the device.
5. **Enclaves are separate trust domains.** Unclassified never becomes a shadow personnel
   database. Exact transfer direction and diode mechanics remain deployment decisions.
6. **Scale through cells.** No single control plane is assumed to schedule 70,000 vCPU.
7. **Contracts move first.** One named owner controls schemas, event envelopes and Alembic
   ordering before parallel implementation begins.
8. **Compatibility is explicit.** Deep links, published OpenAPI, package versions and old
   records require a stated migration/deprecation path.

## Dependency graph and delivery waves

```text
foundation closure
F0 release truth and scorecards
   ├── F1 clean-room install/operations ── F3 real infrastructure and identity proof
   ├── F2 protected CI and release controls
   ├── F4 security and accreditation controls
   ├── F5 existing integration compatibility
   ├── F6 bilingual and accessibility acceptance
   └── F7 content and operational validation (also uses F1 + F6)

10x capability
00 credibility baseline
   ├── 01 readiness evidence ledger ──┐
   ├── 02 unified exercise timeline ──┼── 04 safe AI director
   ├── 03 digital-twin compiler ──────┤
   └── 06 enclave exchange ───────────┤
                   02 ────────────────┴── 05 physical asset provider
              01 + 02 + 03 + 05 + 06 ─── 07 federation and scale cells
```

- **Closure wave 0 — establish truth:** F0 plus 00. Do not plan against stale release notes.
- **Closure wave 1 — parallel closure:** F1, F2, F4, F5 and F6 proceed with distinct owners.
- **Closure wave 2 — external proof:** F3 and F7 run on named environments with named human
  authorities. An unavailable environment is a blocker, not a simulated pass.
- **Capability wave A — freeze contracts:** ADR/contract PRs for 01, 02 and 06.
- **Capability wave B — parallel foundations:** 01, 02, 03 and 06 may run in separate worktrees after
  their shared contracts and migration order are accepted.
- **Capability wave C — controlled effects:** 04 and 05 begin only after the timeline/effect envelope is
  stable.
- **Capability wave D — federation:** 07 proves admission, isolation and recovery using completed cell
  contracts. It does not mask unfinished lifecycle behavior with more hardware.

All workstreams may be tracked at once. Only dependency-ready slices may modify code. A 10x
feature may be developed while a foundation proof waits for hardware, but it cannot be called
release-ready when one of its listed foundation gates remains open.

[`CURRENT-GAP-MAP.md`](CURRENT-GAP-MAP.md) routes every current review finding and published
v1.2 limitation to a closure packet. F0 revalidates that map against source before tickets are
created, because a stale limitation is not permission to rebuild behavior that already exists.

## Foundation closure index

| ID | Packet | Primary skills/agents | Closure result |
|---|---|---|---|
| F0 | [Release truth and scorecards](workstreams/F0-release-truth-scorecards.md) | docs handoff, release verification | One current evidence record; release, IOC and COTE scorecards stay separate. |
| F1 | [Clean-room install and operations](workstreams/F1-clean-room-install-operations.md) | docs handoff, release verification, incident diagnosis | A fresh operator installs, upgrades, backs up/restores and proves alerts, including optional Moodle. |
| F2 | [Protected CI and release controls](workstreams/F2-protected-ci-release-controls.md) | release verification, change review | Required checks demonstrably block a failing PR and release evidence is generated from runs. |
| F3 | [Real infrastructure and identity proof](workstreams/F3-real-infrastructure-identity.md) | release verification, job lifecycle, tenant auth | Real vCenter, Windows AD/AD CS and installed Moodle evidence is captured on named systems. |
| F4 | [Security and accreditation controls](workstreams/F4-security-accreditation-controls.md) | tenant auth, release verification, change review | Approved encryption/key/handling design is implemented and acceptance-tested. |
| F5 | [Existing integration compatibility](workstreams/F5-existing-integration-compatibility.md) | telemetry/LMS, API contracts, release verification | Every carried release limitation is reproduced, fixed or explicitly retained with a test. |
| F6 | [Bilingual and accessibility acceptance](workstreams/F6-bilingual-accessibility.md) | Angular delivery, navigation design, release verification | Critical role journeys pass approved English/French and accessibility gates. |
| F7 | [Content and operational validation](workstreams/F7-content-operational-validation.md) | curriculum traceability, scenario evidence, release verification | Approved content and representative exercises produce human-reviewed evidence. |

## 10x capability index

| ID | Packet | Primary skills/agents | First independently useful result |
|---|---|---|---|
| 00 | [Credibility baseline](workstreams/00-credibility-baseline.md) | change review, release verification | Duplicate route removed; route uniqueness and evidence baseline enforced. |
| 01 | [Readiness evidence ledger](workstreams/01-readiness-evidence-ledger.md) | curriculum traceability, API, migrations, Angular | Existing class pass/fail result becomes a provenance-linked readiness fact. |
| 02 | [Unified exercise timeline](workstreams/02-unified-exercise-timeline.md) | scenario evidence, API, job lifecycle | One exercise run exposes replayable events from scenario and scoring sources. |
| 03 | [Digital-twin compiler](workstreams/03-digital-twin-compiler.md) | API, tenant auth, job lifecycle | Sanitized Terraform inventory produces a reviewable draft template diff. |
| 04 | [Safe AI director](workstreams/04-safe-ai-director.md) | scenario evidence, tenant auth, telemetry/AI | AI proposes a bounded inject; a human approves it; replay proves what happened. |
| 05 | [Physical asset provider](workstreams/05-physical-asset-provider.md) | job lifecycle, tenant auth, scenario evidence | A manual TinyTown/car-lab reservation and reset completes truthfully. |
| 06 | [Enclave exchange](workstreams/06-enclave-exchange.md) | telemetry/LMS, tenant auth, release verification | Signed, idempotent pass/fail export/import with pseudonymous identity and receipt. |
| 07 | [Federation and scale cells](workstreams/07-federation-scale-cells.md) | job lifecycle, release verification, incident diagnosis | Scheduler admits/rejects work using measured cell capacity and preserves tenant isolation. |

## Claude + Taz operating model

Claude is the program integrator. It reads this file, `STATUS.yaml`, the selected packet and
only the skills/source paths named by that packet. It may assign bounded tickets to agents or
Taz. It does not paste the full program into every worker prompt.

Taz/GLM-5.3 is well suited to repository indexing, test generation, bounded code slices,
static review, documentation drift checks and long test runs. Before it receives write access,
record the exact model ID/revision, quantization, vLLM version, tool-call parser, chat template,
context/concurrency and GPU topology. Run the fixed evaluation cases in
`SKILLS/tn-local-model-evaluation/references/repo-evaluation.md`; an alias or attractive patch
is not qualification. Use a disposable worktree, synthetic data and the same DoD. Taz does
not approve its own output and does not operate live infrastructure or learner records.
Use the exact conversation and evidence envelope in
[`TAZ-WORKER-PROTOCOL.md`](TAZ-WORKER-PROTOCOL.md).

Recommended four-lane maximum:

1. Claude integrator: contracts, decisions, merge order and final acceptance.
2. Implementation worker A: one ready workstream.
3. Implementation worker B or Taz/GLM-5.3: a non-overlapping ready workstream.
4. Independent reviewer/release agent: tests, security boundary and unsupported-claim check.

More concurrent agents usually increase shared-file conflicts and review cost. One owner must
hold migrations and another named owner must hold common API/event contracts; initially these
should both be the integrator.

## Token-efficient session protocol

At the start of a session, read only:

1. `CLAUDE.md` and `docs/AGENTS.md`;
2. this file (first session) or its workstream/dependency sections (later sessions);
3. `STATUS.yaml`;
4. one assigned workstream packet;
5. the one or two matching skills and the packet's source pointers.

Then use `rg` to confirm pointers against current code. Do not read `RESUME.md` or
`docs/current-state.md` as current proof. Do not load all skills or all workstream packets.

Every worker returns a compact handoff containing: commit/branch, files changed, contracts or
migrations affected, checks with exact results, unresolved risk, and the next safe action.
Only the integrator updates `STATUS.yaml`.

## The bounded delivery loop

For each PR:

1. **Select:** choose the highest-priority `ready` slice whose dependencies are merged.
2. **Recheck:** inspect current source, Git state, migration head and affected contracts.
3. **Specify:** turn the packet's first slice into acceptance tests and a file-ownership list.
4. **Implement:** test-first where practical; keep one behavioral outcome per PR.
5. **Verify:** run targeted tests, generated-contract checks and `scripts/dod.sh`.
6. **Review:** use `tn-change-review`; add tenant/security, lifecycle or release specialists
   where the packet requires them. A model never reviews only its own work.
7. **Integrate:** merge only green, current work; update `STATUS.yaml` with evidence.
8. **Recover:** if blocked, record the exact blocker and leave a reversible branch; never
   weaken a gate or claim an old pass.

The repository excludes the plugin `loop-me`; this explicit loop is the replacement.

Foundation packets use the same loop, but environment/authority evidence may be attached to a
verification PR instead of produced by a code diff. Never create code merely to make an
unavailable physical environment look green.

## Decision gates that require people

- Which mission-task/readiness vocabulary and authority is approved for CAF use.
- Which evidence may exist at each classification and which system is authoritative.
- Whether the Unclassified-to-Secret path is a data diode, guarded transfer or another
  approved mechanism; the design must support one-way, store-and-forward operation without
  claiming a specific accreditation.
- Retention, appeal, correction and expiry policy for official training records.
- Safety and range-control approval for adversary and physical actions.
- Vendor contracts/protocols for TinyTown, vehicle and future ICS/IoT assets.
- Scale tiers, workload definitions, recovery objectives and budget.

Unresolved decisions go in `STATUS.yaml`; an implementation agent must not invent them.

## Program definition of done

The program is complete only when all applicable `F0–F7` foundation gates are closed and the
12-step demonstration passes in a named supported environment; role journeys and
failure/recovery paths are tested; all assertions have provenance and missing-data behavior;
cross-enclave packages have signed receipts and replay evidence; physical/manual operations
are truthful; scale claims have workload evidence; docs, installer and runbooks match source;
and the repository DoD and release gates pass at the delivered commit.
