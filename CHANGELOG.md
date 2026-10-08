# Changelog

All notable changes to TrueNorth Range. Versions are git tags on `main`
(`docs/release.md`).

## v1.0.0 — unreleased

First production release. Covers everything merged to `main` after the
2026-10-06 reconciliation (`7120eaa`) up to `83d7d43`: PRs #44–#107. All 17
modules are at stage 4.

### Security

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

### Platform

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

### Ops / Release

- Release pipeline: images pushed by digest, blocking Trivy scan, per-image SBOMs,
  `release-manifest.json`; every action SHA-pinned; broken deploy workflows removed (#105).
- Backups that work: every database, MinIO, escrowed secrets, restore drill in CI,
  runbooks (#100).
- Installer: fixes from #35, lab plane, `tn-egress` network, Windows Server roles (#94).
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
15. **AI node credentials**: the LiteLLM master key that used to be committed as a
    default is compromised; rotate it on the AI node and set `OPENAI_BASE_URL` /
    `OPENAI_API_KEY` from your site values; do not rely on a compose default.
