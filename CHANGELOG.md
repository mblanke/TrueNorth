# Changelog

All notable changes to TrueNorth Range. Versions are git tags on `main`
(`docs/release.md`).

## v1.2.0 — unreleased

Everything switched on and working on staging: the full Moodle loop, cmi5 with conformant
xAPI, Greyspace on vSphere (proven on vcsim and the T0 stack, not real vSphere), ARC² batch
course production with a local-model fallback, and the cleanup of unsupported surfaces.

### Learning records and LMS
- **cmi5 and conformant xAPI** (#139): xAPI `account` actors (no email), configurable IRIs,
  `context.registration`, course-locale language maps, per-activity pass marks; reversible
  migration recording legacy `mailto:` identities, `xapi_reissue` (dry run by default) for
  LRS history. TrueNorth is a cmi5 LMS: served `cmi5.xml` per release, an AU runtime in the
  SPA, a checked xAPI endpoint for AUs (statements bound to the session's AU, server-marked
  passes, separate AU LRS credential, read-only profiles), moveOn/satisfied/abandon/waive.
  CI lane `cmi5`: ADL CATAPULT judges the runtime. Moodle cmi5 plugins evaluated and
  rejected; Moodle launches TrueNorth AUs instead.
- **cmi5 over LTI 1.3** (#140): Moodle launches TrueNorth AUs and receives TrueNorth's best
  mark over AGS (never the AU's number), only for an account bound to the Student (exact LTI
  identity, an explicit link, or a TrueNorth-managed farm node's locked idnumber; never by
  email). AGS traffic goes through net_guard; farm nodes may use private addresses.
- **Moodle loop** (#132): completions and quiz grades flow back to TrueNorth per course;
  LTI hand-off session for LTI-created Students; quiz/exercise launch targets; tool-config
  endpoint; staff deep linking after an explicit, browser-bound one-time link (closes the
  v1.0.0 limitation); LTI base URLs from one source (gap #6).

### ARC² Course Studio
- Test-host gate auto-accept, recorded as `accepted_by: auto (test host)` and badged
  "TEST CONTENT" (#130).
- Local / gateway model: `subscription | local | subscription_with_local_fallback`, any
  Anthropic-compatible gateway over HTTPS on any port, egress pinned to that host:port (#130);
  staging falls back to `atl-coder` on the home-lab router (#137).
- Batch driver: catalogue course → run → release → accepted → published (#136).
- Fix: the Studio read `ARC2_RUNS_DIR` after an image-incompatible repo default and 500'd on
  every call on installed hosts (#134).
- Fix: on a Linux runner (bubblewrap) every new run stopped at `/arc2` init with `EROFS`:
  the request file sat in the read-only runs root, where the Write tool's temp-then-rename
  cannot work. The runner now writes it to `<run>/request.txt` before the job starts and
  `arc2.check init` reads it from there; nothing in the runs root is writable to a job.
  The tracked `.env.example` templates are no longer masked in the sandbox, which made every
  job record `provenance.git_dirty: true`.
- Batch hardening: the driver follows only runs it queued and refuses a release for another
  course; open actions stop at needs-review unless `--ack-open-actions` (each id recorded);
  the runner no longer auto-accepts past an open `blocks_promotion` action. `release.json`
  carries `auto_accepted`; accepting or publishing such a release needs `TN_TEST_HOST`
  (installer: only with `tn_arc2_auto_accept`), and Students see a TEST CONTENT banner and
  Moodle name. `--publish-platform` defaults to `none`; retries keep the auto attribution;
  the egress proxy refuses `CONNECT host:port:port`.

### Ranges
- **Greyspace on vSphere** (#133): gs-core VM in the range build, `configure_range` stage,
  mail/NTP/HTTPS, NPC traffic, `greyspace_breadcrumb` inject, designer stencil, corpus ingest.
  Proven on vcsim and the T0 Docker stack only.
- Per-tenant vCenter credentials for every vSphere operation; VyOS and OPNsense get the
  range's rendered config; template `vm_count` corrected; Range Designer YAML import (#131).
- **Range metrics are measured or absent.** `collect_range_metrics` published random CPU,
  memory, disk and network figures as `range_metrics` telemetry. It now publishes only
  what the backend reads (vSphere quickStats: CPU and active memory of the range's own
  powered-on VMs, ownership-checked like a delete, one login per vCenter), tagged
  `source`; no disk or network figures. A backend that cannot report (mock, Proxmox,
  Hyper-V, a host sending no stats) publishes nothing. The telemetry table shows
  "Not reported" for a missing figure and no figures for pre-change events.
- **Operator screens for existing APIs (R0-B).** Range recovery at `/ranges/:id`
  (operations, abandon an operation, abandon a failed range for platform admins with the
  VMs and port groups left for manual cleanup, network reservations, snapshots with
  restore/delete confirmations); team membership at `/teams`; per-Student course progress
  and mark-complete at `/learning/admin/progress`; Moodle results and "Pull now" on the
  Integrations page. Each action states its impact first and shows the API's outcome.
- **Force-release needs proof.** It deleted an abandoned operation's lease tombstone at
  any time, so a hung worker's clones or port groups could run beside the next build, or
  a destroy queued behind the tombstone started at once. It is now refused (409, with the
  quarantine guidance) unless nothing is in flight behind it, no snapshot is busy, and the
  abandoned worker has not renewed the tombstone for two heartbeats. New `GET
  /ranges/{id}/lease` reports the holder and that proof; the UI offers force-release only
  when it holds. A dead worker's vCenter tasks can still be running: check vCenter first.
- Team members list no longer 404s when a member's account was deleted; add/remove
  audit entries name the member; staff mark-complete and manual Moodle pulls are audited;
  the Moodle results status reports `stale`, its reason and the schedule.

### vSphere per-tenant credentials (hardening of #131)
- A tenant's vCenter connection is used whole or not at all: host, username and password
  come only from the connection, never from the platform's `VSPHERE_*` settings. A vSphere
  connection needs a password. Templates may not carry `credentials` (422).
- Each range records the connection it was built on (`provisioner_output.connection`).
  Destroy, power, snapshots, health, guest commands and the browser console reach only that
  vCenter, and act only on VMs whose annotation names the range. Ranges built earlier record
  theirs the first time one of their VMs is found.
- Connections with live ranges cannot be deleted, deactivated, repointed (host or port), or
  replaced as the preferred connection while older ranges would follow (409). A failed
  connection test no longer deactivates a vSphere connection, and shared (tenant-less)
  connections are no longer assigned to the first tenant on startup.
- **Behaviour change:** a connection's port defaults by type (vSphere 443, Proxmox 8006,
  Hyper-V 5985); `HypervisorConnectionIn.port` is optional. The worker honours the port, and
  IPv6 hosts are supported.
- Health and metrics runs log in once per vCenter and log out. Lab leak cleanup searches
  each range's own vCenter. Ranges on an experimental backend can still be destroyed after
  `EXPERIMENTAL_PROVISIONERS` is turned off.
- New: `POST /ranges/{id}/abandon` (platform administrator only) records a failed range as
  destroyed without touching the hypervisor, for ranges whose VMs can no longer be found.
  It requires a reason and is audit-logged.

### Platform
- Terraform provisioners removed; Proxmox and Hyper-V behind `EXPERIMENTAL_PROVISIONERS`
  (#131).
- Demo people can sign in on staging/demo hosts (local Keycloak accounts, passwords in
  `config/secrets`) (#131).
- **Behaviour change:** Student detection submissions are limited to exercise participants
  (`DETECTION_SUBMIT_PARTICIPANTS_ONLY`, default on) (#131).
- Fix: `alembic heads`/`history` crashed in the image on revisions importing `app`
  (`prepend_sys_path`) — the v1.2.0-rc1 upgrade blocker (#135).
- CI: `junit_require_pass` also fails on failing or erroring tests (#138).
- Installer fixes from the rc2 staging upgrade: the ARC² health check matches the runner's
  confinement field (not a literal ')'), and the demo-account steps take a fresh Keycloak
  admin token (they outlived the ~60 s one) (#141).
- Fix: `/metrics` exported invalid latency histograms. Every bucket a request fitted was
  counted and then summed again, so the `_bucket` series were non-monotonic and `+Inf` did
  not equal `_count`. `ApiHighLatencyP95` then read a constant 7.75 s on staging, where real
  latency is about 15 ms. Each observation now lands in exactly one bucket. Also:
  `/health/live`, `/health/ready` and `/health/deep` are no longer timed. The rule ignores
  `path=~"/health.*|unmatched"`, with promtool cases for both. `http_requests_in_flight` no
  longer drops by two when a handler raises.
- **Behaviour change:** no default AI endpoint. A fresh database no longer gets an active
  primary "Ollama (Primary)" backend pointing at a personal internet host; the AI page
  says "AI is not configured" until an operator adds one, and test-generate answers from
  the mock. The orchestrator's unused `OLLAMA_PROXY_URL` (same host) is gone and `/health`
  gains `ai_configured`. Installed sites take the endpoint only from `tn_ai_base_url`.
  Existing backend rows are not touched: delete a seeded one by hand if it is still there.
- Installer: cmi5 launches answered 503 on an installed platform (v1.2.0-rc4 on staging): the
  installer never provided `CMI5_LRS_AUTH`. `50-stack-up` now mints the AU's own LRS
  credential on the LRS (scopes statements/write, state, agents_profile, activities_profile),
  persists it in `config/secrets/cmi5_lrs_auth` (0600, escrowed with the other secrets, never
  echoed), keeps it on every re-run (minting again only if the LRS lost it), renders it into
  `.env.production` and recreates the api. Also rendered: `CMI5_WEB_BASE_URL`,
  `CMI5_API_BASE_URL`, `XAPI_ACCOUNT_HOMEPAGE`, `XAPI_IRI_BASE`, with the values the api
  already derived. New variables `tn_cmi5_enabled` (default on), `vault_cmi5_lrs_auth`,
  `tn_cmi5_web_base_url`, `tn_cmi5_api_base_url`, `tn_xapi_account_homepage`,
  `tn_xapi_iri_base`. Preflight refuses an unusable `vault_cmi5_lrs_auth`; `99-validate`
  fails an install that ends without the credential; `95-smoke-test` checks a cmi5 route
  through nginx and that the running api's launch gate does not answer 503.

### Release readiness (#147)
- **Behaviour change:** the competency catalogue is platform-shared: `POST
  /competency/frameworks` and `/frameworks/import-nice` need `course:author` + `tenant:read`
  (the QSP spine gate, `PLATFORM_CURRICULUM_WRITE`); tenant authors (instructors) get 403.
- Monitoring matches `docs/operations.md`: node_exporter textfile metrics from `backup.sh`
  and `scripts/monitoring/host-probes.sh`; alerts for OpenSearch red/yellow/unreachable,
  Moodle down, ARC² runner down, backup failed/incomplete/stale (>26 h), stale probes;
  promtool unit tests. The never-implemented WebSocket alert is gone from the docs.
- Backups include the ARC² runs directory (`ARC2_RUNS_DIR` with `tn_arc2_enabled`),
  owners kept; restore refuses while the runner is active (`--skip-arc2`).
- CI: `lab.yml` is manual-only until the self-hosted lab runner is registered.
- ADR 0010: deleting what history depends on is 409; retire/archive instead; no
  NULL-on-delete (pre-existing exceptions listed and frozen by a test).
- C105 stays a draft: its committed release is a CI/CATAPULT fixture; the shipped-content
  publish and the ARC² batch skip it. Hosts that published it earlier must unpublish it.
- `scenario-engine/template_engine` deleted (no importers); `runner` kept.
- Docs: v1.2.0 release notes (simulator-proven list, staging record placeholder), H6
  marked fixed, user-guide telemetry queries rewritten to the closed grammar (tested).

### R0-B: web correctness and role access
- Fix: Resume on a paused exercise called `/start` (409 unless pending), so nothing resumed;
  it now calls `POST /exercises/{id}/resume`, sends one request per click and shows the
  server's reason when the exercise is no longer paused. The exercise page has Resume too.
- Menus, route guards, dashboard links and buttons follow the API's permissions: the web
  app mirrors `ROLE_PERMISSIONS` (`core/auth/permissions.ts`, a test keeps it equal to
  `rbac.py`). Students: their courses, exercises, own AARs, progress. Observers: Ops Center,
  Telemetry and Scoring & AAR, read only. range_ops: Authoring with Ranges and Content
  (templates). Instructors: a new **Registration approvals** page (`/approvals`). Telemetry
  is in the menu for `telemetry:read`. Deep links to a page a role cannot open go to the
  dashboard; the approvals queue says when it failed to load instead of "Nothing waiting".
- **Behaviour change:** a Student reads an AAR only for a completed exercise they took part
  in this run (rostered by an assigned enrolment made before the run ended and not
  withdrawn, on one of its teams, or a detection submitted since the run started); other
  exercises' reports, including an earlier cohort's run of the same course, are 404. They were readable
  tenant-wide, mid-run reports (evidence, inject playbook) included.
- **Behaviour change:** `enrollments.source` (migration `a1e2b3c4d5f6`; existing rows
  `assigned`). A Student's own enrolment (Enrol, or a path picked in onboarding) is `self`:
  course content, but no seat in an exercise booked for the course — no telemetry,
  detection submission or AAR of another class's run. Staff enrolling them upgrades it.
- Self-enrolment: **Enrol** on the course catalogue and course page. `POST
  /courses/{id}/enroll` now accepts the shared (global) catalogue, refuses retired courses
  (409), and answers a repeat with 200 and the existing enrolment (was 409); drafts and other
  tenants' courses stay 404. New `GET /courses/enrollments/mine`.
- The fictional LMS and readiness previews are off unless `TN_DEMO_PREVIEWS=true`, and then
  carry a "Demo — fictional data" banner.

### Alert delivery by email
- Installer: `tn_alert_email_enabled` (default off; staging off until its SMTP details are known) renders an
  Alertmanager email receiver from `tn_alert_email_to` / `_from`, `tn_alert_smtp_host` /
  `_port` (587) / `_require_tls` (true) and `vault_alert_smtp_username` / `_password`.
  Criticals mailed at once and hourly while firing, warnings batched (5 m / 15 m, repeat
  12 h), resolved messages on, subjects tagged with the host. The SMTP password is only in a
  0600 file (`smtp_auth_password_file`), tasks `no_log`. Preflight refuses `CHANGE_ME` and
  a missing recipient, sender or relay while it is on.
- Off, the null receiver stays and `99-validate` prints `WARNING: ALERTS ARE NOT DELIVERED`.
- `playbooks/alert-test.yml` (`site.yml --tags tn-alert-test`) fires a synthetic critical
  `TrueNorthAlertTest` and fails unless Alertmanager hands it to the relay; delivery is
  proven by the inbox (install/README.md "Alerting", docs/operations.md "Alert delivery").
- `compose.prod.yml`: the Alertmanager config directory is `ALERTMANAGER_CONFIG_DIR`
  (default the shipped one). Contract tests run `amtool check-config` on every rendered
  variant in the pinned image.

### Scoring and inject evidence (R0-B)
- **Behaviour change:** no objective passes without evidence. The `firewall_rule` validator
  passed every objective; it, `process_killed` and `network_isolated` are now refused
  (no telemetry can prove them): `POST /scenarios/validate` rejects them with the reason and
  the engine leaves them unscored. Use `manual_ack`. `user_disabled` is real: Windows event
  4725 for the account, observed telemetry only. Unknown validators and phase-level
  `validators:` blocks are rejected; incident-response-drill's (`network_isolation`,
  `firewall_rule`, `artifact_removal`, never evaluated) are gone, scored by its existing
  `manual_ack` objectives. Every shipped scenario validates (tested).
- `manual_ack` is an explicit instructor action: content cannot pre-approve it, and until
  acknowledged it is unscored, not failed. Unknown validators, a deliverable with no
  location, or a store outage are unscored too; error rows no longer earn partial credit.
- Simulated injects say so: results and telemetry carry `simulated: true`,
  `evidence_class: "synthetic"`, the exercise, range and inject; none claims `delivered` or
  `created`. Their random choices are seeded from exercise id + inject id. A query objective
  with `require_observed: true` ignores synthetic records (default false: detection
  objectives score finding the inject's records).

### Known limitations in v1.2.0
- Greyspace, VyOS/OPNsense configs and per-tenant vCenter are proven on vcsim/fakes, not on
  a real vCenter (the live run on TN-MGMT01 is still pending).
- ARC² test-host content is auto-accepted and unreviewed; never release it to Students.
- cmi5 Moodle activities are tied to one release; LTI cmi5 launches do not enrol.
- WebSockets do not accept the LTI hand-off session.
- Scenario create/update and exercise start do not yet run objective validation; a stored
  scenario naming a refused validator still launches, and those objectives stay unscored.
  On the mock backend the worker still marks every objective achieved. Evidence is not
  windowed to the exercise: a reused range index can carry an earlier exercise's events.
- A range built before connection records with port groups but no VM stays unrecorded until
  destroyed; shared vCenter connections are managed outside the tenant API; an abandoned
  range's leftover VMs and port groups must be removed by hand (`docs/release-notes/v1.2.0.md`).

## v1.1.0 — 2026-10-09

Optional features for staging and demo hosts, and CI that survives registry limits.

- **ARC² Course Studio on an installed host** (`tn_arc2_enabled`, off by default): the
  runner as a sandboxed systemd service (dedicated user, bubblewrap with a scoped AppArmor
  profile, egress to the model endpoint only, fails closed), pinned Claude Code and Node
  (#124). Signs in with a Claude subscription token (`vault_arc2_claude_oauth_token`, from
  `claude setup-token`) or an API key, exactly one, enforced at preflight (#128). When the Studio is off, the web app hides it and says why instead
  of failing with "Not Found" (`GET /arc2/status`, #123).
- **Moodle on an installed host** (`tn_moodle_enabled`, off by default): the TrueNorth Moodle
  image released by digest, scanned, signed and in `release-manifest.json`; its own edge on
  `:8443`; database and moodledata in the backups; platform registration and a smoke check
  in the role (#125).
- **Shipped content** (`tn_load_shipped_content`, `tn_publish_shipped_content`,
  `tn_load_shipped_demo`, off by default): the curriculum, VM catalogue, range templates,
  scenarios and Sigma rules loaded through the API as the bootstrap administrator, no token
  involved (#127).
- **CI pulls Docker Hub images through `mirror.gcr.io`**, anonymously, so the runners' rate
  limits no longer fail required checks (#126).
- Staging turns all three features on.

## v1.0.0 — 2026-10-09

First production release. Covers everything merged to `main` after the
2026-10-06 reconciliation (`7120eaa`): PRs #44–#121. `v1.0.0` is `v1.0.0-rc4` plus #121
(an installer-only fix). All 17 modules are at stage 4. Release candidates are listed
under "Release candidates" below; what does not work yet is under "Known limitations
in v1.0.0" and, with the staging record, in
[`docs/release-notes/v1.0.0.md`](docs/release-notes/v1.0.0.md).

### Security

- Final pre-release security review, every finding fixed with behaviour tests (#112):
  **C1** an approver cannot grant a role above their own; **H1** curriculum URL ingest
  goes through a shared SSRF guard (`app/net_guard.py`, size cap, private URLs off,
  vetted redirects), as does the LMS platform connectivity test; **H3** Students see when an
  inject ran, not what it was; **H4** Students' detection queries are a closed grammar
  and inject labels moved under unindexed `tn_ground_truth` (ADR 0005); **H5** tenant
  admins stay in their tenant, only the platform admin (`PLATFORM_TENANT_ID`) crosses,
  and unset fails closed; **H6** the worker builds a range with its own tenant's
  hypervisor connection, never another tenant's; **M1** quiz attempts counted, key only
  on the final attempt, server-side time limit; **M2** Students search only telemetry of
  ranges they take part in; **M3** Students cannot start, close or replay team
  exercises; **M4** YAML without anchors/aliases and with a 1 MiB cap; **M5** noise
  deploy validates every Ansible input; **M6** Proxmox VM routes tenant-scoped, TLS
  verified by default.
- CSRF (release blocker): exactly four self-authenticated cross-site POSTs are exempt
  (`/lti/login`, `/lti/launch`, `/lti/deeplink/finish`, `/noise/agent/report`). LTI binds
  each launch to the browser that began it (state cookie) and links an existing account
  by email only for Students (#112).
- Every API YAML parse goes through `safe_yaml`; an AST test forbids raw PyYAML loaders
  in `app/` (#114).
- Keycloak: 5-minute access tokens, 30 min idle / 10 h sessions, password policy,
  password grant off on `truenorth-web`, `truenorth-api` and `truenorth-cli`, PKCE S256
  on the public clients; a dedicated, normally disabled `truenorth-smoke` client (#109).
  The API uses a least-privilege `truenorth-api-admin` service account, not the master
  admin (#111).
- Secrets out of process arguments; no committed AI endpoint or key defaults (#110,
  #111).
- Credentials at rest sealed with `TN_SECRETS_KEY` (Fernet, `new,old` rotation); LTI and
  JWKS signing keys sealed and rotatable; non-root images (#90, #103).
- Production fail-fast settings: with `TN_ENV=production` the API refuses to start on
  disabled auth, missing audience, short or missing `CSRF_SECRET`/`TN_SECRETS_KEY`, or
  default datastore credentials; audience always verified (#103).
- AI orchestrator requires `AI_SERVICE_TOKEN` on every route but `/health`; CORS closed
  by default (#103).
- Production web build ships with auth on and reads Keycloak settings at runtime; CI
  refuses a prod bundle with dev auth or dev hosts (#99).
- Security sweep: calendar-feed tokens no longer leak through `/metrics`; Student inject
  launch, cross-tenant Moodle SSO and four tenancy leaks closed; `X-Forwarded-For` trusted
  only from `TRUSTED_PROXY_CIDRS` (#102).
- Edge nginx: no CORS echo, internals (Swagger, Keycloak admin, Flower, `/ai/`) denied
  outside `management-cidrs.conf`, CSP without `unsafe-eval`, rate limits, TLS 1.2/1.3
  only (#106).
- Permissions: curricula staff-only, catalogue tenant-scoped, sign-in-only route guard
  (#104); tenant scoping and write permissions across platform core, LMS, telemetry,
  reporting and objectives (#58, #76, #77, #78, #79, #82).
- Detection feeds pinned against DNS rebinding; ATT&CK IDs validated against MITRE's
  catalogue (#97).
- Course Studio runner sandboxed (Seatbelt/bwrap, deadline, egress allow-list) (#91).
- Controlled and vendor documents removed from the tree; contract test blocks committed
  site keys, the AI node address and private-key PEM blocks.

### Platform / modules

- All 17 modules at stage 4 (section modules under `control-plane/api/app/<module>/`;
  status per module in `docs/current-state.md`).
- R-series hardening stack (R0–R3, `/ws` scoped to the signed-in tenant) (#55, #61, #71).
- Stage-4 foundations: real integration CI, `worker/tasks.py` split, web specs and e2e
  (#72); worker import cycle fixed (#65).
- Platform core to stage 4: tenant scoping, write permissions, JSON logs (#76).
- DELETE endpoints return 409 or clean up children instead of FK 500s (#86); unit-test DB
  enforces foreign keys (#88); binary uploads fixed app-wide (#54).
- Truthful health endpoints; version reported from `TN_VERSION` (#103).
- Web polish: `returnUrl`, author-only quiz controls, heatmap states, dialogs, View AAR,
  Moodle availability (#98).

### Training

- LMS and curriculum to stage 4: quiz/competency authz, enrollment tests, Moodle farm SSO
  (#77).
- Content to stage 4: course schema, duplicate checks, C105 release (#73).
- AI/ARC²: every model call through a backend adapter (#74).
- Detection credit comes from what the Student submitted (ADR 0005) (#67, #87); only
  instructors acknowledge objectives (#58).
- Reporting/AAR to stage 4: real AAR, cross-tenant read fixed (#79); AAR CSP (#101).
- Wiki, Support tickets and in-app notifications (#44, #59, #85).

### Range

- vSphere provisioner to stage 4: per-range port groups, vcsim lane, noise management NIC
  (#81); Windows Server roles in the designer and provisioner (#94).
- Scheduler (ADR 0004) and CapacityService (ADR 0006) (#80).
- Scenario engine fires exercise injects (#83); exercises carry scenario objectives (#89);
  exercise pause/resume (#101).
- Greyspace simulated internet for ranges (#84); noise engine and network reservations
  (#75).
- Range leases: heartbeat, 180 s TTL, abandon releases, force-release (#95, #96).
- Telemetry: one ingest path, MITRE tags, constrained search (#78); empty-range handling
  (#101).

### Installer

- Installer v1: app version follows the installer's commit (or the manifest's
  `git_sha`), compatibility and downgrade guards, `become` where ownership changes,
  LDAP/AD optional (local groups and bootstrap admin when off), install refuses without
  backup escrow, secret drift refused with `rotate-secret.yml`, upgrades back up and drain
  first, logrotate, preflight rejects `CHANGE_ME` (#111).
- **Signed releases**: tags must be on `main`; cosign keyless signatures of every image,
  `release-manifest.json` and `SHA256SUMS`, with provenance and SBOM attestations; the
  installer verifies the manifest signature before trusting any digest (#111;
  `docs/release.md`).
- Deploy by digest: `compose.prod.yml` has no `build:`; the `tn_release` role checks
  `release-manifest.json` against `SHA256SUMS` and `50-stack-up` refuses mismatched
  images (#109). Staging installs the signed release; AI model ids configurable
  (`tn_ai_default_model`, `tn_ai_embed_model`) (#113).
- `tn_docker` role installs a pinned, held Docker Engine on Ubuntu 24.04 before preflight
  (verified repository key, containerd snapshotter, log rotation); compose floor 2.24
  (#115).
- Sites without vCenter: `tn_provisioner_backend: mock` skips every vCenter step, and a
  read-only simulated vCenter (`tools/vcenter-sim`, `playbooks/lab-vcenter-sim.yml`) gives
  the hypervisor dashboards an inventory; `tn_vcenter_port` (#116).
- Fresh-install fixes found installing rc2 on staging: downgrade-guard message crashed
  every first release install; redis-exporter healthcheck could never pass; smoke test
  misread services without a healthcheck; single-node OpenSearch stayed yellow (replicas
  now `auto_expand_replicas: 0-1`, `OPENSEARCH_REPLICAS` pins a count) (#117).
- Earlier: fixes from #35, lab plane, `tn-egress` network, Windows Server roles (#94).

### Operations

- Release pipeline: images pushed by digest, blocking Trivy scan, per-image SBOMs,
  `release-manifest.json`; every action SHA-pinned; broken deploy workflows removed (#105).
- Backups that work: every database, MinIO, escrowed secrets, restore drill in CI,
  runbooks (#100); persisted secrets escrowed, `restore-secrets.sh`, disk floor and size
  cap (#111). The nightly wrapper (`cron-backup.sh`) no longer reports a good backup as
  failed on a host's first nights, before `weekly/` and `monthly/` exist (#118).
- OpenSearch telemetry is backed up: the installer registers a filesystem snapshot
  repository (`tn_snapshots`), backups snapshot the telemetry indices and keep the newest
  14, restore brings them back. A failed snapshot keeps the data backup and exits 5 with an
  alert (#120); an upgrade's pre-upgrade backup accepts that exit (#121).
- The api and worker images carry `content/catalogue` and `content/mitre`, so the software
  catalogue and ATT&CK lookups work under Helm; `tools/cli/requirements.txt` is pinned (#120).
- Runtime: Redis `noeviction`, Celery beat service, task time limits, read-only and
  capability-dropped containers, third-party images pinned by digest,
  `PROVISIONER_BACKEND` required, JSON logs (#109).
- Monitoring: Prometheus alerts route through Alertmanager (null receiver by default,
  webhook by file); all 19 rules load (#109).
- Helm chart is a supported target: migration hook Job, hardened `securityContext`,
  required secrets, images by digest from `release-manifest.json`, HPA/PDBs, and a kind
  smoke install in CI (`helm-kind.yml`) (#108; `infra/k8s/README.md`).
- Hardening evidence: populated-upgrade test, dispatch black-box, rehearsal scripts (#93);
  reversible migrations (#101).
- Tests deflaked; k6 suite rewritten for the current API and **load-smoke is blocking**
  in CI (#107).

### Breaking / Ops changes

- `TN_SECRETS_KEY` is required; production refuses to start without it (#90).
- `TN_ENV=production` turns misconfiguration into a startup failure (#103).
- The AI orchestrator rejects unauthenticated calls (#103).
- The web container listens on **:8080** (was :80); the dev stack on :4200 requires
  sign-in (#90, #99).
- Admin consoles and API docs are 404 at the edge unless the client is in
  `management-cidrs.conf`, which ships empty (#106).
- `VSPHERE_NETWORK` and `VSPHERE_CONTENT_LIBRARY` default to empty (clone inventory
  templates unless a library is set); installer `tn_vsphere_network` default empty (was
  `dPG-TN-MGMT`) (#81, #94).
- `X-Forwarded-For` is ignored unless the peer is in `TRUSTED_PROXY_CIDRS` (#102).
- The OAuth password grant is off on every user client; scripts use the
  `truenorth-cli` device grant (`docs/api.md`) (#109).
- `compose.prod.yml` no longer builds; the installer defaults to release mode
  (`tn_release_version`, or `-e tn_image_source=build`) (#109).
- `KEYCLOAK_ISSUER`, `PROVISIONER_BACKEND` and `tn_ai_base_url` have no defaults; the
  installer refuses to finish without backup escrow (#109, #111).
- On an install with more than one tenant, nobody is platform admin until
  `PLATFORM_TENANT_ID` is set (#112).

### Known limitations in v1.0.0

Details and workarounds: [`docs/release-notes/v1.0.0.md`](docs/release-notes/v1.0.0.md).

- **LTI and staff emails.** A launch whose email matches a staff (non-Student) account in
  the same tenant is refused, so an instructor whose LMS email is their TrueNorth email
  cannot use LTI deep linking.
- **Telemetry indices from before the upgrade.** On range indices created before #112,
  a Student's free-text telemetry search can still match inject events (labels are
  stripped from results, but the matching events are returned). Fresh installs are
  unaffected; re-run the telemetry bootstrap and recreate those indices.
- **Per-tenant hypervisor credentials (H6).** The worker selects the range's own
  tenant's connection, but the `vsphere_api` backend ignores it and uses the worker's
  `VSPHERE_*` environment (one vCenter per install).
- **Multi-tenant installs need `PLATFORM_TENANT_ID`**, or there is no platform admin.
- **OpenSearch telemetry is not backed up** unless `OPENSEARCH_SNAPSHOT_REPO` is
  configured.
- **LTI requires HTTPS** for its state cookie; `LTI_REQUIRE_STATE_COOKIE=false` for
  iframe / third-party-cookie-blocking setups.
- **No live vCenter run yet.** `lab.yml` has not been executed against a real vCenter;
  provisioning is validated against vcsim in CI and the hypervisor dashboards against a
  simulated, read-only vCenter on staging. The live run on TN-MGMT01 is planned for
  v1.0.1.

## Upgrade notes (v1.0.0)

Operator actions before upgrading an existing site:

1. **`TN_SECRETS_KEY`**: generate (≥32 chars), set the same value on api and every
   worker, and escrow it with the backups (`BACKUP_ESCROW_PUBKEY` in
   `/srv/truenorth/config/backup.env`, or `BACKUP_ESCROW=out-of-band`). Required if any
   plaintext credentials exist; losing it loses every sealed credential.
2. **`TN_ENV=production`** and its fail-fast requirements: `CSRF_SECRET` (≥32 chars),
   `TN_SECRETS_KEY`, non-default DB/Redis/MinIO credentials, auth enabled, audience set.
   Set `TN_VERSION` (the release pipeline bakes it into images).
3. **`AI_SERVICE_TOKEN`** (≥32 chars): same value on api, workers and ai-orchestrator.
4. **`KEYCLOAK_AUDIENCE`** and an **audience mapper** on the Keycloak client so tokens
   carry it.
5. **`TRUSTED_PROXY_CIDRS`**: the addresses of nginx (and any load balancer, which also
   goes in nginx `set_real_ip_from`).
6. **`METRICS_SCRAPE_TOKEN`** for Prometheus; `/api/metrics` is 404 at the edge.
7. **`SCHEDULER_FEED_BASE_URL`**: the public base URL for calendar feeds (no longer taken
   from `X-Forwarded-Host`).
8. **`TN_TENANT_ID`** on each Moodle node (verified in SSO tickets).
9. **Web on :8080**: anything targeting `web:80` (proxies, probes, firewall rules) moves
   to 8080.
10. **`management-cidrs.conf`**: add admin networks to
    `/etc/nginx/snippets/management-cidrs.conf` to reach Swagger, Keycloak admin and
    Flower; narrow `noise-agent-cidrs.conf` to match `NOISE_AGENT_CIDRS`.
11. **OpenSearch security plugin is on**: run `install/playbooks/40-tls.yml` to issue the
    node/admin certificates, then `30-config`.
12. **`VSPHERE_CONTENT_LIBRARY` / `VSPHERE_NETWORK`** now default to empty: set a content
    library explicitly if templates live in one, and never point `VSPHERE_NETWORK` at a
    management network.
13. **Existing Keycloak realms**: update the web client's redirect URIs and web origins by
    hand (realm import does not overwrite them).
14. **Images by digest**: pull the images named in the release's
    `release-manifest.json` by digest, not by tag.
15. **`PLATFORM_TENANT_ID`** on multi-tenant installs: the operator's tenant id.
16. **Telemetry indices**: re-run `telemetry/pipelines/bootstrap.py` so range templates
    carry `tn_ground_truth` unindexed; recreate range indices made before the upgrade
    (see Known limitations).
17. **LTI over HTTPS**: the launch state cookie is `Secure; SameSite=None`. Set
    `LTI_REQUIRE_STATE_COOKIE=false` only where the tool runs in an iframe whose browsers
    block third-party cookies.
18. **AI node credentials**: the LiteLLM master key that used to be committed as a
    default is compromised; rotate it on the AI node and set `OPENAI_BASE_URL` /
    `OPENAI_API_KEY` from your site values; do not rely on a compose default.

## Release candidates

Pre-releases of v1.0.0, each a signed GitHub release (`docs/release.md`). Entries list
what changed since the previous candidate. Dates are GitHub release dates (UTC).

### v1.0.0-rc4 — 2026-10-09 (`ceb0d32`)

- Nightly backup wrapper on a new host (#118); release notes and docs (#119); OpenSearch
  telemetry snapshots in backups, content in the images for Helm, pinned CLI (#120).
- Installed on staging as an upgrade from rc3, which needed #121 (pre-upgrade backup
  accepts exit 5). Staging also came back unattended after an unplanned power cycle, and
  a nightly backup snapshotted telemetry from the secured OpenSearch (11/11 shards).

### v1.0.0-rc3 — 2026-10-09 (`e015d27`)

- Fresh-install fixes from the rc2 staging install: downgrade-guard message, redis-exporter
  healthcheck, smoke health parsing, single-node OpenSearch replicas (#117).
- Installed on staging as an upgrade from rc2 (`docs/release-notes/v1.0.0.md`).

### v1.0.0-rc2 — 2026-10-09 (`32a8bed`)

- Every API YAML parse through `safe_yaml` (#114).
- `tn_docker` role: pinned, held Docker before preflight (#115).
- Sites without vCenter; read-only simulated vCenter for staging (#116).
- First clean install on staging from the signed release; it found the four bugs fixed
  in rc3.

### v1.0.0-rc1 — 2026-10-08 (`d9e04dc`)

- First candidate: everything through #112 (final security review), including the Helm
  chart (#108), runtime hardening and deploy by digest (#109), release housekeeping
  (#110), installer v1 with signed releases (#111) and configurable AI model ids (#113).
