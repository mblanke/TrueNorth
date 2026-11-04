# TrueNorth code review improvement plan

Date: 2026-10-04  
Revision: 2 — executable hardening backlog and evidence rubric.  
Status: proposed delivery plan; implementation and release validation remain outstanding.  
Baseline: `main` at `083aeb750bbf8ff1c25c6efb0ac03489e708af99`, plus the feature worktrees listed below.

## Objective and scoring

Bring **every reviewed quality area to at least 8/10** across the implemented workstreams. The earlier scores were provisional judgments from a partial review, not measured coverage or certification. Re-score only against evidence from one integrated candidate revision. The immediate deliverable is reliable existing functionality; new greyspace, scheduler, or wiki UI development remains separate scope.

| Area | Initial estimate | Minimum evidence for 8/10 |
|---|---:|---|
| Architecture | 7 | External systems behind adapters; generated contracts consistent; lifecycle ownership explicit; no new violations of the MOSA ratchet. |
| Readability | 7 | Changed modules have clear responsibilities, accurate guarantees, explicit error handling, and documented invariants; reviewers can trace each critical journey end to end. |
| Testing | 6 | All six known defects have regression coverage; critical journeys cover success, authorization, concurrency, retries, and dependency failure; required checks pass on the candidate. |
| Security | 5 | Tenant and role boundaries enforced server-side across new surfaces; execution and network boundaries verified; release-blocking security findings resolved. |
| Operational reliability | 4 | Accepted jobs survive dispatch/process failures; stale work cannot reverse newer intent; timeouts and resource reservations are enforced; recovery is demonstrated. |
| Integration readiness | 5 | Compatible dependency baseline, validated migrations, browser journeys, and simulator/live-lab evidence for the features being released. |

### Repeatable scoring method

Score five checks per area, each from 0 to 2: **0** = absent, failing, or unverified; **1** = implemented with partial evidence; **2** = demonstrated by the acceptance evidence below on the candidate. Record the evidence link and reviewer for every check. Asterisks mark mandatory checks that must score 2. At least 8/10 in each row is required; scores cannot compensate across rows. This is a project review rubric, not an industry certification.

| Area | Five scored checks (maximum 2 each) |
|---|---|
| Architecture | Adapter boundaries; generated contract consistency*; one owner for operation/allocation truth*; focused service responsibilities; migration/backward-compatibility design. |
| Readability | Traceable critical control flow*; accurate comments/invariants*; explicit error outcomes; clear typed interfaces; focused diffs and useful maintenance documentation. |
| Testing | Known-defect regressions fail before/fix after*; deterministic failure/concurrency coverage*; production-equivalent database/broker evidence; real browser journeys; reproducible required gates*. |
| Security | Tenant/role denial matrix*; runner/artifact isolation*; identity/LTI validation; network/credential boundaries*; dependency and container verification. |
| Operational reliability | Durable accepted work*; ordered/idempotent operations*; bounded timeout/restart recovery*; collision-free resource lifecycle*; observable failures and recovery runbook. |
| Integration readiness | Frozen source/dependency manifest*; fresh-install and populated-upgrade migrations*; complete selected journeys*; applicable simulator/lab evidence*; tested release/rollback compatibility. |

Every implemented feature in the scope table needs its own acceptance result. A failing security or lifecycle journey prevents that feature from being labelled 8/10 even if the wider area totals 8. Any unresolved P0/P1 defect or skipped mandatory check blocks the overall target. A feature explicitly deferred or disabled may be excluded from a narrower release, but that release must not be described as having completed this entire plan.

### First three deliverables

1. **Runnable baseline and source manifest:** preserve local work, establish the supported toolchain, select compatible dependencies, and record current failures (S0).
2. **Course Studio ownership fix:** tenant-denial regression matrix, ownership/backfill, and regenerated contracts in one PR (S1a).
3. **Runner deadline fix:** an independent deadline and process-group cleanup, with silent-child and subsequent-job tests, in a separate PR (S2a).

Do not wait for a general job framework, broad refactor, or all branch integrations to land these bounded fixes.

## Scope and source inventory

Branches were changing during review. The table is a historical source map, not a frozen release manifest. Refresh SHAs and local diffs before implementation. For each workstream record: source branch and SHA, merge-base with main, included commits, tracked diff identity, non-ignored untracked source paths, owner, and target PR. A SHA alone does not identify an uncommitted feature. Preserve local source files without sweeping reference PDFs or secrets into snapshots, and never reset a shared checkout to prepare the candidate.

Paths below are relative to the repository root. Within each worktree, source paths retain their normal repository-relative names.

| Workstream | Reviewed location | Scope to retain and validate |
|---|---|---|
| ARC², Course Studio, LMS UI | `.claude/worktrees/app-modularity-assessment-3771f3` (`098c632`) | Authoring pipeline, gates, runner, fallback, history, career board, role home screens, live AI status. Includes overlapping ARC²/LMS commits. |
| VMware | `.claude/worktrees/vmware-integration-plan-3091dd` (`56341f5`) | Provisioning, power controls, pfSense, Packer, metrics, mirroring, Colosseum, offline software. |
| MOSA/scoring | `.claude/worktrees/app-stack-moba-design-52b584` (`b509ad`) and `nifty-lamport-0086b4` | Remaining worker table/SQL and detection-scoring changes; avoid duplicating changes already on main. |
| Noise | `.claude/worktrees/network-traffic-noise-tool-8407d2` (uncommitted) | Personas, control API, Linux agents, deployment, management networking. |
| Windows roles | `.claude/worktrees/windows-server-service-selection-37904e` (uncommitted) | Role selection, validation, installation, and partial failures. |
| Wiki/tickets | `.claude/worktrees/wiki-ticket-system-a222f4` (`493bc83`) | Backend, migrations, revision handling, ticket workflows; frontend currently mockups. |
| Moodle | `.claude/worktrees/moodle-integration-courses-8bc905` (uncommitted) | LTI internal routing, setup/bootstrap, documentation. |
| Security/dependencies | `.claude/worktrees/trivy-security-scanner-eval-f41376` (moving branch) | Runtime hardening, dependency/JWT changes, security gates; reconcile with Angular 21 on main. |
| Greyspace | `.claude/worktrees/fake-internet-greyspace-module-7fa71c` (uncommitted) | Plan/mockup only; do not advertise it as a working module. |
| Already merged foundations | `main` | Contract/adaptor work, CI changes, UI polish, Keycloak/nginx/dev-stack fixes; validate in combination. |

The storage-optimization worktree had no distinct implementation. The scheduler ADR is a draft. Neither should be counted as a delivered feature. Reference PDFs are outside this code-hardening scope.

## Known-defect register

Reconfirm each finding against its source revision before fixing it. Add the smallest regression test first, and show that it fails before the fix. The six items below are review findings; execution sandboxing, runner leases, and broader compatibility checks elsewhere are proposed investigations/design work, not additional defects claimed as reproduced.

| ID | Priority | Finding and evidence | Required correction |
|---|---|---|---|
| CR1-01 | P1 | Course Studio endpoints use author permission without tenant ownership; metadata/jobs omit tenant identity. Source: `control-plane/api/app/routers/arc2_studio.py`, including list/detail/reply/file/package endpoints. Static trace. | Persist and enforce tenant ownership on every operation, including runner handoff and downloads. Legacy unowned runs must not become visible to every tenant. |
| CR1-02 | P1 | Runner timeout is checked only after receiving valid JSON from blocking stdout. Source: `tools/arc2/runner.py::_attempt`. A silent process ran about 640 ms with a 50 ms deadline and returned success. | Enforce a wall-clock deadline independently of output; terminate/reap the process group and record failure. |
| CR1-03 | P2 | Wiki revision comparison and update are not atomic. Concurrent content saves can pass the same check and collide on the revision unique constraint. Source: `control-plane/api/app/routers/wiki.py::update_page` and `models_wiki.py`. Static trace; database concurrency reproduction pending. | Serialize/check the version atomically and return 409 with current content, including restore/edit interactions. |
| CR1-04 | P1 | Noise allocation restarts at the same management address per range while vSphere uses a shared portgroup. Source: `worker/render.py::noise_mgmt_plan` and `provisioners/vsphere_api.py`. Two different range IDs produced `10.255.0.10/24`, VLAN 4001. | Persist unique shared-network reservations or provide enforced per-range network isolation; reconcile API deployment inventory with worker allocation. |
| CR1-05 | P1 | Start/stop commit the target state and ignore failed dispatch. Source: `app/routers/ranges.py`, `_dispatch_task`, and `app/celery_client.py`. Static trace. | Durable operation acceptance and dispatch; distinguish pending from observed state; return actionable errors when acceptance fails. |
| CR1-06 | P1 | Retried power tasks do not verify whether a newer operation superseded them before touching the hypervisor. Source: `worker/tasks.py::_power_range`. Static trace. | Serialize operations per range and fence stale generations, including task execution and completion events. |

## Delivery order and ownership

Assign one accountable owner per slice and one integrator for shared routes, schemas, generated contracts, migrations, and dependency files. These are responsibilities, not authorization to spawn agents. Keep PRs small enough to review independently.

Dependency order: **S0 → bounded fixes in S1/S2/S3/S4 → S5 integration → S6 maintenance review → S7 journeys → S8 re-rating**. Select the dependency baseline in S0, not after feature integration. S5 consumes completed S3 and S4 contracts. Wiki work need not wait for provisioning; browser smoke tests begin with each feature PR, with complete journeys repeated on the candidate in S7.

| PR unit | Deliverable | Depends on | Primary owner / merge boundary |
|---|---|---|---|
| S0 | Source manifest, runnable gates, compatible dependency baseline | None | Release integrator; lockfiles, shared CI and migration ordering |
| S1a | Tenant-owned Studio API and legacy-run assignment | S0 | API/auth; Studio routes, metadata, queue ownership |
| S1b | Verify and enforce runner isolation | S1a | Authoring runtime; runner packaging/permissions only |
| S2a | Independent runner deadline and child cleanup | S0 | Authoring runtime; coordinate runner.py with S1b |
| S2b | Claim/restart recovery for authoring jobs | S1a, S2a | Authoring runtime; job protocol/versioning |
| S2c | Atomic wiki edit/restore conflicts | S0 | Wiki backend; independent of runner changes |
| S3a | Persisted range operations, durable dispatch, API acceptance | S0 | Lifecycle owner; migration, operation and task contracts |
| S3b | Serialized execution, stale-task rejection, reconciliation | S3a | Lifecycle owner; worker adapters and persistence |
| S3c | Truthful range UI and completion notifications | S3b | Range frontend; shared operation contract |
| S4a | Persistent noise reservations and shared inventory | S0 | Network owner; allocation schema/API contract |
| S4b | Provision/deploy/cleanup consume those reservations | S4a, S3a | Network + lifecycle owners; renderer/provisioner |
| S5a | VMware, roles, software and noise integration | S3c, S4b | Provisioning integrator; shared task/renderer changes |
| S5b | Scoring and xAPI integration | S0 and remaining MOSA contracts | Scoring owner; can land independently of S5a |
| S6–S8 | Maintenance review, complete journeys, release evidence | Relevant PR units above | Integrator and reviewer |

Each PR contains its contract exports, tests, and migration where relevant; none postpones correctness to S6. Intermediate schema/consumer PRs remain dormant behind feature flags until the whole path works. One owner resolves overlapping changes to `runner.py`, worker tasks/renderers, range routes, migration heads, and generated files. Build generated files from the selected source definitions after integration rather than manually merging their output.

### S0 — Establish a reproducible baseline

**Owner:** release verification. **Priority:** immediate.

- Record all source SHAs, local diff identities, and which commits already exist on main. Preserve active worktrees; create an isolated integration checkout when implementation starts.
- Establish a native Python 3.11 test environment from the current CI dependency files, or a documented container equivalent. Inspect executable architecture and interpreter version first.
- Verify Node/npm and browser requirements from the selected Angular configuration. Do not copy the Linux `.venv` onto macOS.
- Select the Angular/FastAPI/JWT/runtime baseline now, starting from current main and reviewing the security branch's compatible fixes. Main's package manifest uses Angular 21; do not adopt an older branch's package files wholesale. Use a separate prerequisite PR for required dependency repairs.
- Record baseline DoD, web, migration, and relevant feature-test results without weakening ratchets or silently excluding failures.
- When exercising model fallback, record the actual serving model and server version, not only the configured alias.

**Likely files/contracts:** `requirements-test.txt`, `pyproject.toml`, web package/lock files, `scripts/dod.sh`, CI configuration, build documentation. No migration expected.

**Acceptance:** a fresh supported checkout can execute the documented checks; every existing failure has a current reproduction and owner. Classify failures as environment, existing-code, or new-change failures. Bounded fixes can proceed once their relevant checks run; baseline failure remains a blocker to release and must not be hidden or relabelled as a pass.

**Recovery:** use a separate environment and checkout; preserve existing working environments and uncommitted patches.

### S1 — Enforce Course Studio ownership and execution boundaries

**Owner:** API/auth. **Fixes:** CR1-01. **Depends on:** S0.

- Introduce authoritative tenant/run ownership and include it in queued jobs. Centralize ownership resolution across list, detail, reply, retry, file, and package routes.
- Migrate existing runs through explicit ownership assignment; leave ambiguous CLI-created runs inaccessible through tenant APIs until assigned.
- Add two-tenant tests for all routes, plus role-denial tests within one tenant. Check list filtering, direct slug access, instructor materials, and package downloads.
- Review the runner's effective permissions. Python shell execution can write beyond an Edit/Write path allowlist; use OS/container filesystem and credential boundaries where confinement is required. Treat prompts as guidance, not sandbox enforcement.

**Likely files/contracts:** `routers/arc2_studio.py`, `rbac.py`, `tools/arc2/runner.py`, API/runner tests, run metadata/queue schema. Ownership storage/backfill requires a migration strategy even if filesystem-backed.

**Acceptance:** tenant B cannot enumerate, read, download, or mutate tenant A's run; unauthorized requests create no queue/chat side effects. A runner isolation test cannot write outside its allowed artifact workspace or access unrelated credentials.

**Recovery:** feature-disable Studio while retaining runs; never roll back ownership checks by exposing legacy runs globally.

### S2 — Make authoring jobs and wiki edits recoverable

**Owner:** authoring services. **Fixes:** CR1-02 and CR1-03. **Depends on:** S0.

- Replace output-driven runner deadlines with an independent watchdog/nonblocking read mechanism; bound shutdown and reap descendants.
- Define interrupted-job recovery. If the deployment remains a single host runner, first consider an enforced singleton OS lock and persisted job ownership; introduce distributed leases only if multiple runners are an actual supported requirement. Startup recovery must not fail another active runner's work. Prevent concurrent jobs for the same run.
- Test silence, malformed output, deadline expiry, process crash, restart, duplicate claims, and model fallback failure. Preserve useful error/history records without logging credentials.
- Make wiki version checking atomic. Concurrent edit/edit and edit/restore requests must yield one valid result and a recoverable conflict, not an internal error. Use PostgreSQL for the concurrency test.
- Define metadata-edit concurrency separately if content revisions intentionally exclude tags, ordering, or publication state.

**Likely files/contracts:** `tools/arc2/runner.py`, job-record schema, `routers/wiki.py`, `models_wiki.py`, authoring tests. Add lease/version columns through migrations if the selected design needs them.

**Acceptance:** meet F02 and F03 below. A silent or malformed-output child is stopped and reaped within the configured deadline plus shutdown grace; the next queued job proceeds. Concurrent wiki writes preserve one consistent page/revision history and return 409 to the conflicting client. Do not silently turn restore into a last-writer-wins update; include a current-version precondition where required.

**Recovery:** preserve failed job artifacts; retries are explicit and cannot duplicate active work. Keep wiki revision history intact during migration and rollback.

### S3 — Make range operations durable and ordered

**Owner:** lifecycle services. **Fixes:** CR1-05 and CR1-06. **Depends on:** S0.

- Inspect existing job/persistence infrastructure before adding a new abstraction. Implement the smallest range-operation record needed: tenant, range, action, generation, idempotency key, timestamps, status, and structured error. Keep requested state separate from observed state; do not expand unrelated exercise semantics in this PR.
- Commit operation acceptance and pending delivery in one database transaction. The operation row itself may serve as the outbox; a second generic outbox table or new broker is not required. A bounded dispatcher retries pending deliveries; consumers handle duplicate delivery.
- Serialize hypervisor actions per range and check operation generation before side effects. A pre-call check alone is insufficient if an older action can still run concurrently; use a per-range lease/lock with clear expiry and recovery rules.
- Fence completion updates and notifications as well as execution. Reconcile uncertain outcomes against backend state after timeouts or worker crashes.
- Lease expiry is not proof that a remote hypervisor task stopped. If an action is already in flight, record its backend task identifier and prevent a conflicting successor until completion/cancellation is observed or reconciliation establishes the result. Specify behavior when the adapter cannot query/cancel a task.
- Make the UI show queued/starting/stopping/failed states and permit an appropriate retry without falsely claiming running/stopped.

**Likely files/contracts:** range routes, a dedicated operation service/model and Alembic migration, Celery client, worker lifecycle module, generated task contracts, range API schemas, range UI. Extend the existing generated worker table approach; do not add raw SQL to the task monolith.

**Acceptance:** broker outage after acceptance does not lose work; duplicate delivery has no duplicate effect; a delayed stop cannot undo a newer completed start; worker restart recovers accepted work; stale events cannot regress UI state. Cover start/stop versus destroy/restore interactions.

**Recovery:** feature-disable new requests and drain/reconcile accepted operations before rollback. Old workers must not consume incompatible task payloads; use an explicit compatibility/deployment sequence.

**Minimum contract:** the same idempotency key and payload return the same operation; reuse with a different payload returns 409. A successful 202 means the operation is durably recorded, not that VMs reached the target. A persistence failure returns an error with no accepted operation. Broker downtime may leave a durably accepted operation queued and visibly delayed. Reject or serialize a conflicting request while execution is in flight; document which policy the UI exposes. A timeout with unknown backend outcome enters reconciliation rather than immediate blind retry. Preserve compatibility for existing clients through an explicit versioned response or documented adaptation.

### S4 — Allocate noise resources safely across ranges

**Owner:** networking/provisioning. **Fixes:** CR1-04. **Depends on:** S0.

- Default proposal: preserve the existing shared management-network design and reserve unique addresses transactionally. Reuse tested pool parsing from `worker/uplink_pool.py` where appropriate, but do not copy its callers' locking assumptions without concurrency checks. Change to isolated networks only if the intended network boundary requires it; unique addresses alone do not provide tenant isolation.
- Include tenant/range ownership, capacity validation, uniqueness constraints, and reservation lifecycle. Uniqueness must cover the actual shared network domain, not merely `(tenant_id, address)`, which still permits collisions between tenants on the same network.
- Make the API and worker consume the same persisted allocation; do not independently derive deployment addresses from template order.
- Test concurrent allocations, repeated templates, capacity exhaustion, interrupted provisioning, retries, and destruction. Failed provisioning retains reservations until cleanup proves resources are gone.
- Reconcile deployed networks and agent addresses before adopting legacy ranges. Do not silently renumber live agents.
- Validate management access restrictions and token rotation/deployment failure recovery; confirm a compromised training guest cannot cross the intended network boundary.

**Likely files/contracts:** noise topology/models/router, worker renderer/provisioner, noise deployment task, Ansible role, migration and deployment inventory schema.

**Acceptance:** two simultaneously provisioned ranges have no address conflicts; deployments reach the intended nodes; retry reuses reservations; cleanup frees only the owning range's resources. Demonstrate using a simulator where possible and an authorized isolated lab for actual networking.

**Recovery:** stop new allocations while preserving existing assignments; reclaim only after backend reconciliation.

### S5 — Integrate provisioning, roles, software, and scoring

**Owner:** provisioning integrator. **Depends on:** S3 and S4.

- Merge the VMware, Windows-role, noise, and remaining MOSA changes against one base. Resolve shared renderer/provisioner/task contracts deliberately rather than accepting whichever branch wins a conflict.
- Define readiness: VM power-on alone must not imply required roles/software/network setup succeeded. Return per-node outcomes and distinguish optional degradation from failed required capabilities.
- Exercise Colosseum and a smaller representative template through create, provision, power, snapshot/restore, and destroy. Cover missing images, failed role installation, offline-package failure, and partial hypervisor success.
- Verify detection scoring with actual event-store fixtures: zero hits, matching hits, backend failure, tenant/range scoping, and xAPI score bounds. Ensure unavailable evidence is not interpreted as a successful detection.
- Verify metrics and port mirroring against their real backend semantics, including cleanup after partial failure.

**Likely files/contracts:** renderer, provisioner adapters, Windows/software modules, generated worker tables, scoring/event-store adapters, range output schemas, infrastructure tests. Reconcile all migration heads; generate contracts after the combined schema is settled.

**Acceptance:** required setup failure cannot appear as ready; retries do not duplicate resources; teardown leaves no unexpected owned VMs/networks/reservations; scoring results match known evidence. Mock-only success is insufficient for hardware-dependent claims.

**Recovery:** retain backend resource identifiers for cleanup; disable optional integrations independently; record restore/reconciliation procedures before lab tests.

### S6 — Verify compatibility and reduce maintenance risk

**Owner:** platform/contracts. **Depends on:** S1–S5 integration.

- Verify integrated features against S0's selected Angular/FastAPI/JWT/runtime baseline. If an integration forces a version change, review and retest that change explicitly rather than silently replacing lockfiles.
- Reconcile lockfiles, auth route guards, JWT validation, OIDC/LTI behavior, container users, and writable artifact paths against that baseline.
- Keep security scans blocking with documented, narrowly scoped exception policy. A dependency version change is not evidence of application compatibility.
- Extract changed lifecycle/allocation logic into focused modules with typed results and explicit invariants. Keep external SDKs behind adapters and regenerate API/task/table contracts where applicable.
- Correct comments that claim guarantees unsupported by code. Avoid broad cosmetic rewrites or arbitrary file-size targets.

**Likely files/contracts:** package/lock files, requirements, Dockerfiles, auth/LTI modules, export scripts, CI, new focused service modules. Usually no data migration beyond earlier slices; verify rather than assume.

**Acceptance:** production builds and auth/LTI regression tests pass; runtime containers can perform required writes as their configured users; no unexplained contract drift or ratchet increase; changed modules receive a readability review.

**Recovery:** keep previous compatible image/lockfile sets together. Do not revert only half of an API/client or auth/dependency transition.

**Migration contract:** use additive schema changes first, deploy compatible readers/consumers, perform resumable backfill with count/invariant checks, then enable new writes. Remove legacy fields or consumers only in a later change after pending work is drained. Test an upgrade from a populated prior schema with `DB_AUTO_CREATE=false`; an empty SQLite schema check does not prove PostgreSQL upgrade safety. Resolve intended Alembic heads explicitly. For irreversible migrations, specify restore or forward repair instead of promising an unsupported downgrade.

### S7 — Verify complete user journeys and unfinished features

**Owner:** frontend/product integration. **Depends on:** S6.

| Feature | Required acceptance journey |
|---|---|
| Course Studio / ARC² | Instructor creates a course, reviews outline, requests changes, approves preview, downloads a validated package; failure/retry and refresh preserve truthful progress. A package candidate is not labelled published. |
| LMS shell / career board | Learner sees only their enrollment/progress; prerequisites match catalogue data; old deep links work; role changes and forbidden routes are handled server-side and in the UI. |
| AI status | Real backend state and model identity are shown; unreachable/stale metrics display as unavailable rather than fabricated healthy values. |
| Range Designer | Roles/software selections survive save/reload, invalid combinations receive useful errors, and required installation failure remains visible after provisioning. |
| Noise controls | Authorized staff can deploy, adjust, pause, rotate credentials, and inspect activity for the correct range; students cannot access control or ground-truth endpoints. |
| Wiki/tickets | Verify staff/student visibility and tenant boundaries across all operations; demonstrate conflict recovery and ticket lifecycle. Build the UI only as a separately scoped slice; mockups are not completion evidence. |
| Moodle | Bootstrap is repeatable; launch/JWKS/token/grade paths work using public browser URLs and internal service routing; invalid state, nonce, deployment, or tenant context is rejected. |
| Greyspace | Keep marked planned until implemented through separately reviewed slices with network isolation, lifecycle, and recovery tests. Assess its design quality separately from implementation readiness. |
| Dev stack | Fresh startup initializes identity correctly; service recreation recovers; intended loopback bindings remain enforced. |

**Likely files/contracts:** Angular routes/shared clients/feature components, demo interceptor, LTI/bootstrap code, feature API tests and browser journeys. Use the reconciled schemas; do not hand-edit generated interfaces.

**Acceptance:** each implemented journey passes with demo mode off; loading, empty, denied, failed, retry, and stale-data states are covered. Keyboard navigation, focus, accessible names, and usable layouts are verified on changed screens.

**Recovery:** retain feature flags or hide unfinished navigation entries without breaking existing deep links. Preserve user-created content.

### S8 — Release evidence and independent re-review

**Owner:** release integrator/reviewer. **Depends on:** all applicable slices.

- Freeze one candidate revision and source manifest. Run targeted regressions, repository DoD, web checks, fresh-install/upgrade migrations, and the selected end-to-end journeys on that revision.
- Run database concurrency tests on PostgreSQL, real-broker tests for job delivery, simulator tests for provisioning, and authorized lab checks for network/hypervisor behavior that mocks cannot establish.
- Perform a bounded interruption/recovery exercise covering broker downtime, worker/runner restart, and partial provisioning; retain structured operation/event evidence without secrets.
- Observe operation queue age, last dispatcher/runner heartbeat, retry count, reconciliation count, and stuck reservation count. Include tenant/range/operation correlation IDs in structured events, excluding credentials, private course content, and agent tokens. Set alert thresholds from the configured polling/deadline policy and verify an induced failure is visible to its operator.
- Review the final integrated diff for new regressions, migration compatibility, tenant isolation, and misleading status. Reassess every score using the acceptance rubric rather than carrying forward historical scores.
- Publish a compact evidence report with commit, environment, command, outcome, log location, and remaining limitations for each check.

**Acceptance:** all six quality areas meet their 8/10 criteria; no unresolved P0/P1 findings in release scope; lower-priority defects have an owner and do not contradict the rubric. Any critical unexecuted check prevents release-readiness sign-off. Planned features remain explicitly planned.

**Recovery:** retain the prior release artifacts, a tested backup/restore procedure, and a rollout sequence compatible with migrations and queued task versions. This plan does not itself authorize deployment or live provisioning.

## Required failure experiments

These are proposed acceptance tests, not passing results. Use synchronization barriers/events and a controllable backend fake to force the interleaving; random sleeps are not concurrency evidence. Verify the behavior through the actual route/task path where applicable, not only a reimplementation in test code.

| Test | Controlled trigger | Observable pass condition | Execution environment / starting tests |
|---|---|---|---|
| F01 / CR1-01 | Tenant A creates a run; tenant B tries list/detail/file/package/reply/retry; repeat with disallowed roles and a legacy unowned run. | No foreign run appears or is accessible; denied calls produce no job, file, or chat mutation; A retains permitted access. | API fixtures; `tests/api/test_arc2_studio.py` in Studio branch. |
| F02 / CR1-02 | Child emits nothing, invalid JSON, or a partial line; child spawns a descendant; queue a second job. | With a 1-second test deadline, terminate/reap within deadline + 2-second configured grace + 1-second CI tolerance; state is failed with timeout reason; second job runs. No remaining child process. | Local subprocess test; `tests/arc2/test_arc2_runner.py`. Avoid paid/live models. |
| F03 / CR1-03 | Two clients load version N; synchronize edit/edit and edit/restore submissions before the write. | Exactly one accepted write and one 409 under the selected version contract; one consistent next revision, no lost acknowledged edit, no 500. | Two independent PostgreSQL sessions; `tests/api/test_wiki.py`. |
| F04 / CR1-04 | Two ranges request the same template/address pool concurrently; retry, exhaust the pool, interrupt creation, then destroy one. | No shared-network address collision; stable retry assignment; explicit exhaustion; no premature reuse after failure; other range remains reachable and unchanged. | PostgreSQL reservation test, renderer test, then authorized isolated network lab; `test_noise_topology.py`, `test_vsphere_noise.py`. |
| F05 / CR1-05 | Accept an operation while the broker is down; recover broker. Separately fail DB commit and crash dispatcher after send but before delivery bookkeeping. | Durable acceptance survives outage; failed persistence is not accepted; duplicate delivery cannot duplicate the logical operation; status never falsely reports observed running/stopped. | Real broker + PostgreSQL; extend `test_range_power.py` and lifecycle integration tests. |
| F06 / CR1-06 | Delay an old stop retry until a newer start completes; separately expire ownership while a remote action is still in flight. | Stale retry makes no hypervisor call or regressive event; unresolved remote action blocks unsafe conflicting execution until reconciliation. | Controllable adapter plus worker persistence path; `tests/worker/test_vsphere_tasks.py`, then simulator. |
| F07 / recovery | Kill worker after backend success but before DB completion; restart it; repeat with partial provisioning. | Reconciliation records actual state and existing resource IDs; no duplicate resources or false ready state; cleanup remains possible. | Durable DB/broker with adapter fault hooks; lifecycle integration tests. |
| F08 / migration | Upgrade populated previous schema, backfill, restart with compatible consumers, retry backfill; test documented rollback/restore route. | Data counts and ownership remain correct; one intended migration lineage; no orphaned operations/reservations; recovery procedure works. | Disposable PostgreSQL; extend migration completeness tests beyond their SQLite coverage. |

Process timing above is a test policy, not an application performance benchmark. Real hypervisor deadlines must be configured from supported operations and verified separately. Capture actual elapsed times and cleanup evidence rather than only asserting a timeout flag.

## Verification commands and evidence policy

Use the interpreter validated in S0. Baseline commands, from the selected checkout:

```sh
.venv/bin/python -c 'import sys, platform; print(sys.executable); print(sys.version); print(platform.machine())'
DOD_WEB=1 bash scripts/dod.sh
```

Run targeted feature tests before the full gate. For backend-only PRs use `bash scripts/dod.sh`; for UI changes and the final candidate use `DOD_WEB=1 bash scripts/dod.sh`, which also runs the backend gate, avoiding a redundant full run. Supply the verified browser configuration. Recheck the current test runner and database configuration rather than copying stale commands. OpenAPI/task/table generation must use the current export scripts; inspect generated diffs before accepting them. Do not increase lint/modularity baselines to conceal new defects.

For each fix, retain: original failing reproduction, changed behavior, regression result, exact source revision, relevant environment/service versions, and remaining untested boundaries. Do not count SQLite tests as PostgreSQL concurrency evidence or simulated provisioning as proof of lab networking. Treat any newly discovered concern as unconfirmed until traced or reproduced.

Use a review record per PR with these fields: `PR unit; source SHA + local diff identity; finding/test IDs; baseline outcome; candidate outcome; exact command; environment versions; evidence location; reviewer; remaining blockers`. Valid outcomes are `PASS`, `FAIL`, `BLOCKED`, and `NOT RUN`; blocked checks never earn passing score. Mark an inapplicable check only with a concrete scope explanation reviewed by the integrator.

Targeted CI should run the relevant new regression on every affected PR; the full candidate runs all applicable journeys. A failing regression must be shown on the original faulty source, not merely inferred from a new test name. Fresh evidence is required after changes affecting the tested behavior; do not repeat unrelated expensive lab checks without a reason.

## Immediate next slice and completion checklist

Start with **S0**, then **S1a tenant isolation** as the first functional PR and **S2a runner deadline** as the next bounded fix. Prepare S3's minimal operation contract before changing range states. Keep the security-critical authoring surface disabled in shared environments until its ownership and execution boundaries pass verification. If a lab or external system is unavailable, continue unaffected slices, record the exact blocked experiment, and leave the affected readiness score unverified.

- [ ] Source manifest and runnable validation baseline recorded.
- [ ] CR1-01 through CR1-06 fixed with regression evidence.
- [ ] Shared operation and allocation contracts integrated without duplicated logic.
- [ ] Feature branches reconciled with main, including dependency versions and migrations.
- [ ] All implemented feature journeys verified; mockups and plans labelled accurately.
- [ ] DoD, web, migration, broker, concurrency, simulator, and applicable lab checks pass.
- [ ] Final independent review and evidence-based scores reach at least 8 in every area.

## Validation of this planning document

This document changes no runtime behavior. Revision 2 moves dependency selection before feature integration, splits broad phases into independently reviewable PRs, distinguishes investigations from established findings, permits simpler persistence/runner designs, and adds scored evidence and deterministic failure experiments. Scope, source references, and acceptance coverage were checked against the inspected source and review findings; proposed tests are not claimed to have run.

Document structure checks passed: balanced code fences, unique headings, all six findings represented, and eight failure experiments mapped. `bash scripts/dod.sh` was retried for revision 2 on 2026-10-04 and stopped before tests with `ruff not installed in .../.venv/bin/python`. Direct inspection reconfirmed the underlying problem: `.venv/bin/python` is an x86-64 Linux ELF executable, and invoking it on this Mac returns `exec format error`. The application gate did not pass. Re-run using the supported environment established in S0. No application, infrastructure, or feature-worktree files were changed while revising this plan.
