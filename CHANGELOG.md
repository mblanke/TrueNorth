# Changelog

All notable changes to TrueNorth Range. Versions are git tags on `main`
(`docs/release.md`).

## v1.1.0 — unreleased

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
