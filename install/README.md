# TrueNorth Range — installer

Takes the platform host from *"bare Ubuntu 24.04, `/srv/truenorth` empty"* (Docker is
installed by `00-docker`) to a running, AD-federated TrueNorth with a working trainee registration path. AD is optional
(`tn_ldap_enabled`): a site without a domain controller gets local Keycloak groups and a
local bootstrap administrator ("Without Active Directory").

## What this does and does not cover

This installs the **application**. The ESXi hosts, vCenter, the vDS and the
management VMs beneath it are built by the PowerCLI tooling in the deployment
repo (`COTE/TrueNorth-Demo`).

Ansible starts at TN-MGMT01 because that is the first host in the stack it can
actually manage. ESXi has no package manager and no Python; it is configured
through the vSphere API instead, which is why there is no Ansible for it and
why the previous version of this directory — which targeted Proxmox over SSH —
did not fit the lab that exists.

| Layer | Built by | Where |
|---|---|---|
| ESXi hosts, vCenter, vDS, datastores | PowerCLI / govc | deployment repo |
| TN-MGMT01, TN-DC01 (AD DS, AD CS, LDAPS) | PowerCLI | deployment repo |
| TrueNorth platform (this) | Ansible → Docker Compose | here |
| Ranges, VLANs 100–199 | TrueNorth itself, via the vSphere provisioner | the app |

## Prerequisites

On the **control node** (where you run `ansible-playbook`):

```bash
pip install ansible-core
ansible-galaxy collection install -r requirements.yml
```

On the **platform host** (TN-MGMT01): Ubuntu 24.04 and your SSH key in
`~tnadmin/.ssh/authorized_keys` (the inventory expects `~/.ssh/id_ed25519_lab` on the
control node). In `git` mode it also needs HTTPS out to github.com; without it, use
`local` mode (below). Preflight checks both.

**Docker is not a manual prerequisite on Ubuntu 24.04.** `00-docker` (roles/tn_docker,
first in `site.yml`) installs it, when `tn_install_docker` is true (the default on
Ubuntu 24.04, false elsewhere):

- Docker's apt repository (`/etc/apt/sources.list.d/docker.sources`), with its signing key
  in `/etc/apt/keyrings/docker.asc` used only if its fingerprint is
  `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`; otherwise the key is deleted and the
  install stops.
- `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-buildx-plugin` and
  `docker-compose-plugin` at the versions in `group_vars/all/main.yml`
  (`tn_docker_version`, `tn_compose_version`, `tn_containerd_version`, `tn_buildx_version`),
  **held** (`apt-mark hold`) so unattended-upgrades cannot move them. To bump: check
  `apt-cache madison docker-ce docker-compose-plugin` on the host, change the variables,
  run `playbooks/00-docker.yml -K` (the daemon, and so the stack, restarts), then `site.yml`.
- `/etc/docker/daemon.json`: json-file logs rotated at 50 MB × 5 per container, and the
  **containerd image store** ("Air-gapped installs" depends on it). The keys are merged
  over the file's own (a registry mirror, `data-root`, … survive; the old file is kept as a
  timestamped backup) and Docker restarts only when the file changes. Switching an existing
  engine to the containerd store hides the images it pulled before: `50-stack-up` pulls them
  again. `tn_docker_manage_daemon_json: false` leaves the file alone.
- chrony (preflight asserts the clock is synchronised), and the install user in the
  `docker` group (the connection is reset so the next play has it).

It stops rather than fight a Docker installed another way: Ubuntu's `docker.io`/`containerd`
packages, or a second apt source for Docker's repository (an older `docker.list`). Remove
those, or **opt out** with `tn_install_docker: false` (group_vars or host vars); then Docker
must already be there, and preflight still requires Docker ≥ 24 and Compose ≥ 2.24 (the
backup drill uses compose's `!reset`) either way.

Air-gapped, Docker's repository is unreachable: point `tn_docker_apt_repo_url` at an internal
mirror of it (the same key signs it), or set `tn_install_docker: false` and preinstall Docker
from Docker's `.deb` files, with the containerd image store enabled and the same versions.
chrony then needs a reachable time source (the DC) in `/etc/chrony/sources.d/`.

`--check` on a host without Docker reports what `00-docker` would do, then stops at
preflight's "docker present": nothing is installed in check mode.

With AD (`tn_ldap_enabled: true`, the TN lab) you need `certs/corp-root-ca.cer` from the
deployment repo — copy it to `install/files/`. Keycloak cannot bind to AD over LDAPS
without trusting that CA, and the failure it produces (`PKIX path building failed`) does
not obviously point at a missing certificate.

The install user needs sudo: the tasks that hand files to other uids (OpenSearch's 1000,
Prometheus/Alertmanager's 65534), packages, sysctl, logrotate and the trust store run with
`become`. Pass `-K` (`--ask-become-pass`) unless sudo is passwordless.

## Quick start

```bash
# 1. Secrets
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml       # replace every CHANGE_ME (preflight refuses them)
ansible-vault encrypt inventory/group_vars/all/vault.yml

# 2. Nominate the first administrator — a NAMED AD account (without AD: the local account
#    60-keycloak creates). Without this the install finishes with an approval queue nobody
#    can drain. Pin the release to install (docs/release.md): tn_release_version: v1.2.3.
#    The AI endpoint (tn_ai_base_url) has no default (no AI server yet: tn_ai_enabled:
#    false, "AI (optional)"). The backup escrow is REQUIRED
#    ("Backups"). Turn on alert email (tn_alert_email_enabled) or alerts go nowhere
#    ("Alerting").
$EDITOR inventory/group_vars/all/main.yml        # or inventory/hosts.yml host vars

# 3. TLS: drop truenorth.crt / truenorth.key into files/tls/
#    (or use -e tn_tls_mode=selfsigned for a lab bring-up)

# 4. Check before you change anything
ansible-playbook site.yml -K --ask-vault-pass --check

# 5. Install. The claim-contract smoke test is mandatory: with AD, name an AD account.
ansible-playbook site.yml -K --ask-vault-pass -e tn_smoke_username=<upn> -e tn_smoke_password=<pw>

# 6. Prove it — a clean second run is the idempotency proof
ansible-playbook site.yml -K --ask-vault-pass -e tn_smoke_username=<upn> -e tn_smoke_password=<pw>
```

Put the smoke-test password in a vaulted vars file and pass `-e @smoke.yml` rather than
typing it on a command line.

Then browse to `https://<tn_domain_fqdn>`, **by name, never by IP address**. Keycloak's
issuer and redirect URIs are the hostname, so opening `https://<ip>` loads an empty page
(the sign-in check is refused as `invalid_redirect_uri`). The workstation must resolve the
name: use the AD DNS (TN-DC01), or add a hosts-file line for the platform host.

A production install starts with an empty catalogue; see
[Shipped content](#shipped-content-optional) to load the repository's.

## Stages

Each is independently re-runnable: `ansible-playbook playbooks/60-keycloak.yml`
is always safe on its own.

| Playbook | What it does |
|---|---|
| `00-docker` | With `tn_install_docker` (default on Ubuntu 24.04): Docker from its apt repository (key fingerprint pinned), at pinned versions, held; `daemon.json` log rotation and the containerd image store; chrony; the install user in the `docker` group. Otherwise nothing ("Prerequisites"). |
| `00-preflight` | No `CHANGE_ME` left in the vault, the AI endpoint and the backup escrow set; OS, Docker/Compose versions, disk, NTP, vCenter reachability, the app repository; with AD, forward+reverse DNS through AD and the LDAPS port; without AD, that the service name resolves (or `tn_manage_etc_hosts`); a `vault_cmi5_lrs_auth`, if set, is usable; with ARC² on, its key, model endpoint (https, reachable), runs gid, user namespaces and disk. Read-only, and runs for real under `--check`; fails loudly with the fix in the message. |
| `10-base` | Packages, `vm.max_map_count` (OpenSearch will not start without it), the `/srv/truenorth` tree with the uids each image runs as, logrotate for `/srv/truenorth/logs`; with AD, the resolver drop-in for AD DNS. |
| `20-fetch-app` | Release mode: verifies `release-manifest.json`'s **cosign signature** against the release workflow's identity, checks it against `SHA256SUMS`, then fetches the app at the release's commit (`git_sha`) and refuses any other. Build mode: the installer's own commit. Refuses an older release/commit than the one deployed (rollback is explicit, `tn_allow_downgrade`) and an app whose compose file declares another compatibility level than `install/COMPAT`. `local` and `tarball` (checksum-verified, unpacked beside the app and swapped in) for air-gapped installs. Records what was deployed. |
| `30-config` | Persists every secret under `/srv/truenorth/config/secrets/` (the vault's value, or one generated **once**; an empty file is regenerated), refuses a vault value that differs for a first-start secret ("Secrets"), asserts all are ≥ 32 characters, renders `.env.production` and the backup environment, and stops without a backup escrow. |
| `40-tls` | Installs the AD CS root CA (DER or PEM, normalised to PEM; required with AD, optional without) into the host trust store *and* Keycloak's truststore; with AD, a **real LDAPS bind** as the Keycloak service account (password in a 0600 temp file, `ldapsearch -y`); the certificate nginx serves; the OpenSearch CA, certificates and `internal_users.yml` (as root). |
| `50-stack-up` | Pulls the release's images **by digest** (or loads the air-gapped archives), stops unless every image resolves to the digest compose names. On an **upgrade**: a pre-upgrade backup, then the old application services stopped ("Upgrades"). Then datastores → **alembic** (with the new api image; refuses a database newer than the code) → everything else. Builds nothing unless `tn_image_source=build`. Then, with `tn_cmi5_enabled`, the cmi5 AU's LRS credential: minted once, kept, rendered, api recreated ("cmi5"). See "Images" and "The migration hazard" below. |
| `55-arc2` | **Optional, off by default** (`tn_arc2_enabled`). The ARC² Course Studio runner: a `tn-arc2` account, the runs directory the api shares, bubblewrap with an AppArmor profile scoped to the runner, Node and Claude Code at pinned versions, a hardened systemd service whose self-test must pass. Off: nothing. "ARC² Course Studio (optional)". |
| `60-keycloak` | Realm (imported without the development realm's sample users, with this host's redirect URIs and generated client secrets), token/session/password rules and client grants (enforced on every run, written only when they differ; "Identity hardening"), the API's least-privilege service account (`truenorth-api-admin`), AD user federation over LDAPS (or, without AD, local groups and the local bootstrap administrator), and the token claim mappers. **This is the join between the installer and the application** — see below. |
| `70-telemetry` | OpenSearch index templates, ISM policies, ingest pipelines, and the backup's snapshot repository ("Backups"). |
| `80-seed` | Counts the reference data in the database (fails on none), creates the tenant and the bootstrap administrator. |
| `85-moodle` | Only with `tn_moodle_enabled` (off by default): TrueNorth's Moodle, its database and its registration as the tenant's Moodle ("Moodle (optional)"). |
| `90-vsphere` | Provider wiring, and detects the unassigned vCenter role. |
| `95-smoke-test` | Every container running and healthy (`ps -a`: an exited one fails it), API health and readiness (database, Redis **and OpenSearch**), cmi5 launches not failing closed ("cmi5"), and the claim-contract check, which is **mandatory**: with AD pass `-e tn_smoke_username=<upn> -e tn_smoke_password=<password>`; without AD it signs in as the local bootstrap administrator. The token comes from the `truenorth-smoke` client, enabled only for that one request. |
| `99-validate` | Final report, including accepted warnings. Fails when `tn_cmi5_enabled` and the env file has no `CMI5_LRS_AUTH`. |
| `rotate-secret` | Not in `site.yml`: changes a first-start password in its service and everywhere else (docs/runbooks/key-rotation.md). |

## Three things worth understanding before you run it

### The migration hazard

`app/main.py`'s lifespan calls `Base.metadata.create_all()` on every API start,
in every one of the four uvicorn workers. On a fresh database whichever worker
wins builds all 68 tables from the ORM, and a later `alembic upgrade head` then
starts from base and dies on `DuplicateTable`.

The installer handles this two ways: it sets `DB_AUTO_CREATE=false`, and it
brings up Postgres alone, runs alembic in a one-shot container, and only then
starts the API.

If it finds tables but no `alembic_version` — a database built by `create_all()`
on some earlier attempt — it **stops and asks**, rather than stamping. Stamping
declares "this schema matches revision X" without checking, and if that is wrong
every later migration is applied to a schema it was not written for. Opt in
deliberately with `-e tn_allow_alembic_stamp=true`.

A fresh install runs the chain from `base` to `head` and needs no workaround.
That was not always true: **37 of the 69 tables the ORM defines were created by
no migration at all**, existing only because the API's `create_all()` made them
at startup, so `upgrade head` on an empty database aborted partway through.
Revision `b0c1d2e3f4a5` creates those tables, and
`tests/api/test_migration_completeness.py` fails if a model is ever added
without a migration again.

### The claim contract (`60-keycloak`)

Registration reads AD identity out of the access token. `60-keycloak` creates a
`truenorth-identity` client scope carrying three protocol mappers, and one of
them is load-bearing:

| Mapper | What breaks without it |
|---|---|
| `oidc-group-membership-mapper` → `groups` | No group claim. Role suggestion is blank on every approval screen, and group-based registration eligibility silently permits everyone. |
| `ad_object_guid` | `User.ad_object_guid` stays NULL; re-linking after an AD rename degrades to matching on email. |
| `ad_distinguished_name` | The approval screen cannot show OU placement. |

Nothing errors when these are missing. The API just quietly loses half its
input, which is why `95-smoke-test` decodes a real token and asserts the claims
are present.

### vCenter privileges

The custom `TrueNorth-Provision` role exists in vCenter but is **not assigned**;
`svc-truenorth` is still ReadOnly. That is a reasonable guardrail — granting
write access to a cluster is a human decision — but it means the platform comes
up perfectly healthy and range provisioning fails days later with a 403.

`90-vsphere` probes for it and reports it at install time. Assign the role with
`scripts/create-vcenter-provision-role.ps1` from the deployment repo, then
re-run that playbook. Set `tn_fail_on_missing_vsphere_privs=true` once you
expect it to be granted.

## Networks and exposure

| Network | Internal | Who is on it |
|---|---|---|
| `tn-frontend` | no | nginx (80/443), api, keycloak, web |
| `tn-backend` | **yes**: no route off the host | datastores, workers, everything that talks to them |
| `tn-egress` | no; nothing publishes a port on it | `worker-provision` (vCenter), `ai-orchestrator` (the LLM endpoint) and `alertmanager` (the webhook receiver), the services that must leave the host |
| `tn-monitoring` | yes | exporters, Prometheus, Alertmanager, Grafana, Flower |

- Keycloak's port is also published on `127.0.0.1:8180` (`tn_keycloak_admin_port`),
  for this installer's Admin REST calls only. Users reach it through nginx (`/auth/`).
- OpenSearch publishes no port; it is only on `tn-backend`, and it still requires TLS and
  a password (next section).

## Production settings

The env file sets `TN_ENV=production`: the API and the AI orchestrator refuse to start
with a missing or unsafe setting, and name every one. The installer supplies them:
`CSRF_SECRET`, `TN_SECRETS_KEY`, `AI_SERVICE_TOKEN` and `METRICS_SCRAPE_TOKEN` are
generated (32 characters, the production floor) unless the vault sets them;
`KEYCLOAK_AUDIENCE` is the API client, which `60-keycloak` puts in every access token
through an audience mapper on the `truenorth-identity` scope (also on an existing
install); `TN_VERSION` is `tn_app_git_version`; `SCHEDULER_FEED_BASE_URL` is
`https://<tn_domain_fqdn>`; `TRUSTED_PROXY_CIDRS` is `tn_trusted_proxy_cidrs`
(loopback and Docker's bridge pool, where nginx sits). Prometheus reads the two bearer
tokens from `/srv/truenorth/config/prometheus/` (`PROMETHEUS_SECRETS_DIR`). The api's
healthcheck is `/health/ready`.

`PLATFORM_TENANT_ID` names the operator's own tenant; its admins are the platform
administrators (create tenants, approve into any tenant, platform-wide settings). Admins
are per tenant. Unset, an admin is the platform administrator only while one tenant
exists; once a second tenant exists nobody is, and the api logs an error at startup,
until it is set. Set, it must be a tenant UUID or production refuses to start. A
single-tenant install needs nothing.

URLs someone else chose are fetched through `app/net_guard.py`: loopback, link-local and
cloud-metadata addresses are always refused, and private (RFC 1918) addresses unless the
feature's flag is on: `INTEGRATION_ALLOW_PRIVATE_URLS` (an LMS platform's connectivity
test), `CURRICULUM_ALLOW_PRIVATE_URLS` (curriculum pages, up to 3 redirects, each hop
re-checked), `THREAT_INTEL_ALLOW_PRIVATE_FEEDS`. An LMS on the platform network needs the
first, or its "Test" button reports the URL as refused.

## OpenSearch

The security plugin is **on**. `40-tls` (`roles/tn_tls/tasks/opensearch.yml`) makes:

| What | Where | Used by |
|---|---|---|
| Internal CA (key) | `/srv/truenorth/tls/opensearch-ca/ca-key.pem`, root 0600 | signing only; never mounted |
| CA certificate | `/srv/truenorth/tls/opensearch/ca.pem` | OpenSearch, Dashboards, and every client (`/etc/truenorth/opensearch-ca.pem` in the api and worker containers) |
| Node certificate `CN=opensearch` | `…/opensearch/node.pem`, `node-key.pem` (PKCS#8, uid 1000) | TLS on HTTP and transport (`plugins.security.nodes_dn`) |
| Admin certificate `CN=tn-opensearch-admin` | `…/opensearch/admin.pem`, `admin-key.pem` | `securityadmin.sh` (`plugins.security.authcz.admin_dn`) |
| `internal_users.yml` | `/srv/truenorth/config/opensearch/internal_users.yml`, uid 1000 0600 | `admin` and `kibanaserver`, bcrypt hashes |

The two passwords are `vault_opensearch_admin_password` and
`vault_opensearch_dashboards_password`; left empty, they are generated on the target and
kept under `/srv/truenorth/config/secrets/` like every other secret. The env file then
carries `OPENSEARCH_URL=https://opensearch:9200`, `OPENSEARCH_USER=admin`,
`OPENSEARCH_PASS`, `OPENSEARCH_VERIFY_SSL=/etc/truenorth/opensearch-ca.pem`, and
Dashboards logs in as `kibanaserver` (`OPENSEARCH_DASHBOARDS_PASS`) with full certificate
verification. No demo certificates or demo users are installed
(`DISABLE_INSTALL_DEMO_CONFIG=true`).

The security index is created from these files the **first** time OpenSearch starts, so
both passwords are first-start secrets: change them with
`ansible-playbook playbooks/rotate-secret.yml -K -e tn_rotate=opensearch_admin_password`
(it rebuilds `internal_users.yml`, loads it with `securityadmin.sh` and recreates the
clients; a differing vault value alone is refused). By hand, the load step is:

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production exec opensearch \
  plugins/opensearch-security/tools/securityadmin.sh -f /usr/share/opensearch/config/opensearch-security/internal_users.yml \
  -t internalusers -icl -nhnv -cacert config/certs/ca.pem -cert config/certs/admin.pem -key config/certs/admin-key.pem
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production up -d
```

**One node, no replicas.** OpenSearch runs as a single node, which can never assign a
replica: an index asking for one leaves the cluster yellow, and readiness in
`95-smoke-test` fails ("Readiness: degraded … opensearch Cluster status: yellow"; on
TN-MGMT01 it was the job-scheduler plugin's `.opendistro-job-scheduler-lock`). OpenSearch
2.13 ignores `cluster.default_number_of_replicas` as a node setting when it creates an
index, so `70-telemetry` (and `95-smoke-test`, again, before readiness) runs
`roles/tn_telemetry/tasks/replicas.yml` in the opensearch container, authenticated with the
admin certificate (no password): it sets that default as a **persistent cluster setting**
(applies to every new index, hidden and plugin ones included) and sets
`number_of_replicas: 0` on any existing index, hidden ones included, that asks for more.
Changed only when something was changed; nothing on a multi-node cluster. Any other cause
of yellow or red still fails readiness. By hand, the same check:

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production exec opensearch \
  curl -s --cacert config/certs/ca.pem --cert config/certs/admin.pem --key config/certs/admin-key.pem \
  'https://localhost:9200/_cat/indices?h=index,rep,health&expand_wildcards=all'
```

**Lab/dev override only:** `-e tn_opensearch_disable_security=true` renders
`OPENSEARCH_DISABLE_SECURITY=true`, `OPENSEARCH_URL=http://opensearch:9200` and an empty
`OPENSEARCH_USER`: no authentication, plain http on `tn-backend`. Never on a platform
Students use. The dev and integration compose files (`compose.dev.yml`, `compose.itest.yml`)
run OpenSearch this way.

## Backups

`30-config` schedules `scripts/backup/cron-backup.sh` nightly (02:17). It dumps every
database (application, Keycloak, LRS) and MinIO under `/srv/truenorth/backups`, and
**escrows the secrets**: the env file and `/srv/truenorth/config/secrets/`, encrypted to the
escrow public key. The install stops without an escrow; set one of:

```yaml
# inventory (group_vars or the host): a public key ON THE CONTROL NODE
tn_backup_escrow_pubkey: /path/to/backup-escrow.pub   # private half kept offline
# or attest that the secrets are escrowed elsewhere (backups then hold none)
tn_backup_escrow: out-of-band
```

A backup does not start with less than `tn_backup_min_free_gb` free, and the oldest are
pruned while all backups exceed `tn_backup_max_total_gb` (never the newest). On a rebuilt
host: `scripts/backup/restore-secrets.sh` first, then the installer, then
`scripts/backup/restore.sh` (docs/runbooks/backup-restore.md "Rebuilt host").

OpenSearch telemetry is snapshotted by each backup into the filesystem repository
`tn_snapshots` at `/srv/truenorth/opensearch-snapshots` (OpenSearch's `path.repo`;
`10-base` makes the directory for uid 1000, `70-telemetry` registers it and fails if
OpenSearch cannot write there). The manifest names the snapshot and `restore.sh` restores
it. The snapshots stay in that directory, not in `backups/`, so copy both off the host.
The newest `tn_opensearch_snapshot_keep` (14) are kept. A failed snapshot never costs the
rest of the backup: it is kept, the manifest records the failure, an alert is raised and the
backup exits 5. Set `tn_opensearch_snapshot_repo: ""`
to leave telemetry out (scores and outcomes are in PostgreSQL either way).

Not in the backup set: Redis (a transient broker).

## Upgrades

Re-run `site.yml` with the new release (docs/runbooks/upgrade.md). When the running database
is behind the release's migrations, `50-stack-up` first takes a backup into
`backups/pre-upgrade/` (`-e tn_pre_upgrade_backup=false` to skip), then stops the api, web,
nginx, workers, beat and flower so no old code runs on the new schema, then migrates and
starts the new release. In-flight Celery work: `tn_upgrade_inflight: drain` (default; waits,
up to an hour for a range build) or `abandon` (60 s, interrupted ranges must be rebuilt).
Rolling back is never silent: an older release or commit, or a database newer than the code,
stops the install unless `-e tn_allow_downgrade=true` (the runbook says when that is safe).

## Without Active Directory

`tn_ldap_enabled: false` (the default in `group_vars`; `inventory/hosts.yml` turns it on for
TN-MGMT01): no resolver drop-in, no TN-DC01 checks, no LDAPS bind, no Keycloak federation
and no AD import check. Instead `60-keycloak` creates the `tn_ad_groups` as local Keycloak
groups and `tn_bootstrap_admin_upn` as a local account in `TN-Platform-Admins`, with the
generated password `bootstrap_admin_password` (`/srv/truenorth/config/secrets/`). The smoke
test signs in as that account, so the claim contract (a `groups` claim) is still proven.
With `tn_load_shipped_demo` (and `tn_load_shipped_content`) on, `60-keycloak` also creates
the shipped demo people (`tn_demo_people`: `instructor.demo@truenorth.test`,
`trainee1..3.demo@truenorth.test`, `observer.demo@truenorth.test`) as local accounts in
their `TN-*` groups, and `80-seed` links each to its roster row (`app.link_demo_accounts`)
so they can sign in with the role the loader gave them. Each password is generated once and
kept, mode 0600, next to the admin's: `/srv/truenorth/config/secrets/demo_instructor_password`,
`demo_trainee1_password` … `demo_trainee3_password`, `demo_observer_password`
(`sudo cat` one to sign in; delete one and re-run `60-keycloak` to rotate it). Re-runs reuse
them. Never turn the demo on where Students train.
`inventory/staging.yml` is such a site (tn-staging, 192.168.1.240):

```bash
ansible-playbook -i inventory/staging.yml site.yml -K --ask-vault-pass \
  -e tn_release_version=v1.0.0-rc1
```

A site with no vCenter has two options. `tn_provisioner_backend: mock` makes
`tn_uses_vcenter` false: preflight skips the vCenter checks, `90-vsphere` does nothing and
ranges are simulated. Or run a simulated, read-only vCenter on the host first
(`ansible-playbook -i inventory/staging.yml playbooks/lab-vcenter-sim.yml -K`,
`tools/vcenter-sim/`) and point `tn_vcenter_host`/`tn_vcenter_port` at it, as staging
does, so the hypervisor dashboards have an inventory. It builds no VM.
Its AI endpoint is an Open WebUI (`tn_ai_base_url: https://…/api`), so set
`tn_ai_default_model` and `tn_ai_embed_model` to ids from that server's `/api/models`.
The orchestrator's per-task models (`tn_ai_heavy_model`, `tn_ai_general_model`,
`tn_ai_code_model`) follow `tn_ai_default_model` unless set; `99-validate` fails when the
endpoint does not list one of them (the report's `ai models` line).

## AD test domain (CI) and AD on staging

Directory-backed sign-in is qualified in two places (plan M2), and the record keeps them
apart: **Samba-tested** is not **Windows AD / AD CS qualified**.

- **The AD test domain** (`tools/ad-test-dc/`, CI job `ad`): a Samba AD DC with its own test
  CA, TN-DC01's OUs, groups and bind account, and test users (one per TN-* group, nested,
  disabled, email collision), beside the pinned Keycloak, all on loopback under compose
  project `tn-ad-test`. The realm and federation are made by this role's own task files
  (`local_identity.yml`, `ldap_federation.yml`, `identity_scope.yml`, `ldap_sync.yml`), then
  `tests/integration/test_ad_directory.py` checks group → role and tenant, nested groups,
  untrusted CA, disabled accounts, group removal and its propagation, email collision, the
  Moodle SSO subject and the local recovery path with the DC stopped. It is never deployed
  on staging. Results and measured timings: `tools/ad-test-dc/README.md`.
- **AD on staging**: the owner's Windows AD DS / AD CS DC on the home LAN, mirroring
  TN-DC01 (`corp.tnrange.lab`, `OU=TrueNorth`, the TN-* groups, the bind account). Staging
  uses the normal `tn_ldap_*` path: the bind password from the vault, the root CA from
  `files/corp-root-ca.cer`. The same test file runs against it in external mode.

What the measurements mean for operators:

| Behaviour | Value |
|---|---|
| Nested groups | **Not expanded.** Membership is direct (`LOAD_GROUPS_BY_MEMBER_ATTRIBUTE`): a user in a group that is itself in `TN-Students` gets no `TN-Students` claim, no role suggestion and cannot register. Put people directly in a TN-* group. |
| A group removed in AD | The `groups` claim drops once Keycloak re-reads the user: at the next changed-users sync (`changedSyncPeriod`, 300 s), or at once if it was not cached. Tokens already issued live up to 5 min more. **The TrueNorth role does not change**: groups suggest, an approval grants (docs/identity.md). To take TrueNorth rights away, change or disable the TrueNorth account, or disable the AD account. |
| An AD account disabled | New sign-ins fail at once; a session's refresh keeps working until the next changed-users sync (≤ 300 s), then fails. Worst case to lose access: about 10 min (sync + token lifetime). |
| An AD account with a local account's email | Keycloak refuses to import it (it cannot sign in, and the full sync reports it as failed; `60-keycloak` prints a warning). The local account is untouched. An approval never adopts a staff account by email (409). |
| A local account with an AD account's email | Keycloak refuses to create it once AD is federated, so `60-keycloak` creates the local recovery administrator **before** federating. |
| DC unreachable | AD sign-ins fail after `connectionTimeout` (5 s; without it they hung past 60 s). Local accounts are unaffected. |

### AD on staging: turning it on (coordinator)

1. In `inventory/staging.yml` (the M2 block): `tn_ldap_enabled: true`, and uncomment
   `tn_dc_host: tn-dc01.corp.tnrange.lab`, `tn_dc_ip: 192.168.1.224`,
   `tn_dns_servers: ["192.168.1.224"]` (TN-DC01). Leave `tn_keycloak_local_admin: true`.
2. `ansible-vault edit inventory/group_vars/all/vault.yml`: `vault_ldap_bind_password` (the
   `TrueNorth Keycloak Bind` account's). Preflight refuses it empty or `CHANGE_ME`.
3. The enterprise root CA `corp-TN-DC01-CA` (DER or PEM) at `install/files/corp-root-ca.cer`.
   TN-DC01's LDAPS certificate must name `tn-dc01.corp.tnrange.lab` (hostname is verified).
4. DNS (done on TN-DC01 2026-10-12): forwarders 192.168.1.21 and 192.168.1.1 (staging will
   resolve everything through it), A record `tn-staging.corp.tnrange.lab → 192.168.1.240`
   and the PTRs (preflight asks the DC).
5. `ansible-playbook -i inventory/staging.yml site.yml -K --ask-vault-pass -e tn_release_version=<tag>`
   (a full run; or `--tags preflight,base,config,tls,stack,keycloak,seed,smoke,validate`).
   40-tls proves an LDAPS bind as the service account before Keycloak changes;
   60-keycloak keeps `tnadmin@tn-staging.local` local, federates, syncs, **fails unless all
   six TN-* groups are found in `OU=Groups,OU=TrueNorth`**, and reports any AD user it could
   not import. The smoke test signs in as the local administrator and warns that this does
   not prove AD; add `-e tn_smoke_username=<AD account in a TN-* group>
   -e tn_smoke_password=…` (from a vaulted file) to prove an AD sign-in and its claims.
6. Qualify: `tests/integration/test_ad_directory.py` in external mode
   (`tools/ad-test-dc/README.md`) with test accounts on the DC.

`tn_keycloak_local_admin` (with AD) keeps the bootstrap administrator a **local** Keycloak
account in `TN-Platform-Admins` with its generated password, beside the directory. Keycloak
finds local accounts before it asks LDAP, so it still signs in with the DC down, the bind
password wrong or the CA expired (tested with the DC stopped). Turning it **off** with AD on
disables that local account (never deletes it; turning it back on re-enables it), so no
unmanaged sign-in stays behind. Preflight refuses a
`tn_bootstrap_admin_upn` inside `tn_domain`, so it can never name an AD account.

## Work-domain cut-over and rollback

The cut-over from the staging DC to the work domain (TN-DC01, or any production AD) is a
change of the same variables, never of code. Do it in a window: sessions end.

**Before**: the work domain's AD CS root CA, the bind account's password (vault), the DC's
FQDN and address, DNS that forwards, and an A record for the platform name. Run the
external-mode tests against the work domain from a workstation first if it is reachable;
the result is what makes it "Windows AD / AD CS qualified" for that domain.

**Cut-over**:

1. Set `tn_dc_host`, `tn_dc_ip`, `tn_dns_servers`, and, if the domain differs,
   `tn_domain`, `tn_ldap_base_dn`, `tn_ldap_users_dn`, `tn_ldap_groups_dn`, `tn_ldap_bind_dn`;
   replace `files/corp-root-ca.cer` and `vault_ldap_bind_password`.
2. Keep `tn_keycloak_local_admin: true` until the first AD administrator has signed in and
   been approved; then decide whether to keep the break-glass account (recommended) or
   set it false (60-keycloak then disables the local account; its TrueNorth row stays).
3. Run `site.yml`. The federation component (`corp-ad`) is updated in place: the users the
   old directory imported keep their Keycloak ids only if the new directory has the same
   objectGUIDs (the same forest). **A different forest is a new identity for everyone**:
   their TrueNorth rows stay bound to the old Keycloak subjects, so each person registers
   again and an administrator approves them (roster rows adopt by email; staff rows never
   do — re-grant staff roles deliberately). Plan the approval queue.
4. Prove it: the smoke test, then an AD administrator signs in, then
   `tests/integration/test_ad_directory.py` in external mode.

**Rollback** (to local identity): set `tn_ldap_enabled: false` and run `site.yml`
(`--tags base,config,keycloak,smoke` is enough). 10-base removes the AD resolver drop-in,
30-config clears the LDAP settings in the API's environment, and the local administrator
still signs in. 60-keycloak does **not** delete the `corp-ad` federation component or the
users it imported: remove the component in the Keycloak admin console (User federation →
corp-ad → Delete) so AD users stop being able to sign in; their TrueNorth rows remain and
can be disabled. Rolling back to the previous directory is the cut-over in reverse.

## Shipped content (optional)

A production install starts with an empty catalogue: a site loads its own curriculum.
Staging and demo hosts can load the repository's `content/` instead (the programme
catalogue and course files, the VM catalogue, range templates, scenarios and Sigma rules):

| Variable | Default | Effect |
|---|---|---|
| `tn_load_shipped_content` | `false` | 80-seed loads `content/` through the API |
| `tn_publish_shipped_content` | `false` | publish every course, quiz and learning path (imports are drafts), except C105 and its quizzes: its committed release is a CI/CATAPULT fixture and the course stays a draft ([content-library.md](../docs/content-library.md#courses)) |
| `tn_load_shipped_demo` | `false` | demo people, ranges and exercises (not provisioned); without AD the demo people can sign in ([Without Active Directory](#without-active-directory)) |

The load runs `scripts/load_content_inprocess.py` in a one-off api container as the
bootstrap administrator: the API's routes, permissions, CSRF and validation all apply,
and no token or password is used. Re-running is safe. `inventory/staging.yml` turns all
three on.

On a staging host loaded this way, `scripts/staging_acceptance.py` then proves each module's
key flow end to end against the installed stack, in the same kind of one-off container
(docs/runbooks/staging-acceptance.md).

## Infrastructure page: the installer registers its vCenter

The worker's vCenter is configured from `tn_vcenter_host`, `tn_vcenter_port`,
`tn_vcenter_user`, `tn_vsphere_datacenter`, `tn_vsphere_verify_ssl` and
`vault_vsphere_password`. 80-seed also registers it as a hypervisor connection of the
bootstrap administrator's tenant, so `/infrastructure` lists it (with its discovered hosts)
straight after the install and the acceptance suite's provisioners check finds it. Before
this, an installed host showed an empty Infrastructure page until someone added the
vCenter by hand.

| Variable | Default | Effect |
|---|---|---|
| `tn_register_vcenter_connection` | `true` | register the vCenter (only with `tn_uses_vcenter` and a `tn_vcenter_host`) |
| `tn_vcenter_connection_name` | `Range vCenter` | the connection's name on the Infrastructure page |
| `tn_vcenter_connection_update_password` | `false` | also set an existing connection's password from `vault_vsphere_password` |

It runs `scripts/register_vcenter_inprocess.py` in a one-off api container as the bootstrap
administrator, like the content loader. The password reaches it only through the
environment (`TN_VCENTER_PASSWORD`, `no_log`), never on a command line, and is never
printed. Re-running is safe:

- A vSphere connection of that tenant with the same host is reused, never duplicated.
  Its username, datacenter and `verify_ssl` are corrected when they differ from the
  inventory; its host and port are not changed (a different port is reported).
- Its **password is set once**, when the connection is created, and then left alone: an
  operator may have rotated it on the Infrastructure page. After rotating
  `vault_vsphere_password`, re-run with
  `ansible-playbook playbooks/80-seed.yml -e tn_vcenter_connection_update_password=true`.
- If the tenant has no primary vSphere connection, this one becomes primary.
- The connection is then tested and discovered. A failed test or discovery (vCenter
  unreachable, wrong password, certificate) is printed as a `WARNING` and does **not**
  fail the install: the connection stays registered, and Test on the Infrastructure page
  re-checks it once fixed. The install fails here only if the connection could not be
  registered at all (the bootstrap administrator does not exist, or the API refused it).

To manage the connection only by hand, set `tn_register_vcenter_connection: false`.

## Images: deploy by digest

`compose.prod.yml` builds nothing. The four TrueNorth images run as `${TN_IMAGE_API}`,
`${TN_IMAGE_WORKER}` (the workers, `beat` and `flower`), `${TN_IMAGE_WEB}` and
`${TN_IMAGE_AI_ORCHESTRATOR}`, each an `image@sha256:…` ref; every third-party image is
pinned by digest in the file itself. Compose refuses to start without the four variables.

With `tn_image_source: release` (the default), `roles/tn_release`:

1. takes `release-manifest.json`, its `release-manifest.json.sigstore.json` and
   `SHA256SUMS` of `tn_release_version` from `tn_release_dir` on the control node, or
   downloads them from the GitHub release (`tn_release_repo`) into
   `install/.cache/releases/<tag>/`;
2. **verifies the manifest's cosign signature** against the release workflow's identity at
   that tag (a cosign it pins and checks by SHA-256; docs/release.md "Signatures"), then its
   SHA-256 against `SHA256SUMS`, `schema_version` 1, the version, and that every service has
   an `image@sha256` ref (docs/release.md);
3. renders the refs into the env file (`TN_IMAGE_*`), and `TN_VERSION=<tag>`;
4. in git mode, fetches the app source at the manifest's `git_sha`: the compose file and
   migrations the images were built with. Any other commit stops `20-fetch-app`.

`50-stack-up` then pulls (`docker compose pull`) and **checks that every image resolves to
the digest compose names** before anything starts; the migration runs in the release's api
image. If the GHCR packages are private, set `vault_ghcr_username`/`vault_ghcr_token` (a
`read:packages` token).

Until a release exists, or to try a change on a lab, `-e tn_image_source=build` builds
local tags (`truenorth-<service>:local`) from the checkout with
`compose.prod.yml -f compose.build.yml`. Never on a platform Students use: what runs is then
not what `release.yml` scanned.

## Air-gapped installs

The app source:

```bash
ansible-playbook site.yml \
  -e tn_app_source_mode=local \
  -e tn_app_local_path=/path/to/TrueNorth     # checked out at the release's git_sha
```

or with a source tarball of the release's commit via `tn_app_source_mode=tarball`, which
must come with `tn_app_tarball_sha256` or `tn_app_tarball_sums` (a SHA256SUMS file listing
it). The release files: put `release-manifest.json`, `release-manifest.json.sigstore.json`
and `SHA256SUMS` in a directory and pass `-e tn_release_dir=<dir>`, plus a Sigstore
`trusted_root.json` as `-e tn_release_trusted_root=<file>` for an offline signature check
(docs/release.md "Signatures"), and `-e tn_cosign_path=<cosign>` if the control node cannot
download the pinned cosign.

The images: on a connected machine, save every image the stack runs, **by tag, from
Docker's containerd image store**. The classic store does not keep a digest through
`docker save`/`docker load`, and the installer then stops at its digest check (verified
2026-10-08: with the containerd store, a tagged save loads back resolvable by
`repo@sha256`; an untagged one does not).

```bash
# Docker with the containerd image store ("features": {"containerd-snapshotter": true}
# in /etc/docker/daemon.json), at the release's commit, with the env file rendered:
cd infra/platform/docker && mkdir -p /tmp/tn-images
for ref in $(docker compose -f compose.prod.yml --env-file .env.production config --images | sort -u); do
  docker pull --platform linux/amd64 "$ref"
  repo="${ref%%@*}"; [[ "$repo" == *:* && "${repo##*/}" == *:* ]] || repo="$repo:${TN_RELEASE:?set TN_RELEASE=v1.2.3}"
  docker tag "$ref" "$repo"
  docker save -o "/tmp/tn-images/$(echo "$repo" | tr '/:' '__').tar" "$repo"
done
```

Copy the directory to the control node and run with `-e tn_image_archive_dir=<dir>`. The
target needs the containerd image store too: `00-docker` enables it, but it also needs
Docker's apt repository. Without a mirror of it, set `tn_install_docker: false` and
preinstall Docker with the store enabled ("Prerequisites"). `50-stack-up` copies the tarballs, `docker
load`s the ones that changed, and runs the same digest check instead of pulling.

## Alerting

Prometheus evaluates the rules in `monitoring/prometheus/alerts.yml` and sends what fires
to the `alertmanager` service. **By default Alertmanager routes everything to a `null`
receiver: nobody is told.** `30-config` says so in a warning, and `99-validate` reports
`alerts: NOWHERE (null receiver)` with a `WARNING: ALERTS ARE NOT DELIVERED` of its own.

### Email (the supported receiver)

Set, in the inventory and the vault, then run `00-preflight`, `30-config` and
`50-stack-up` (or the whole `site.yml`):

| Variable | Default | Note |
|---|---|---|
| `tn_alert_email_enabled` | `false` (staging too, until its SMTP details are set) | Off: the null receiver (or the webhook, below). |
| `tn_alert_email_to` | *(empty — required when on)* | One address, or several separated by commas. |
| `tn_alert_email_from` | *(empty — required when on)* | An address the relay lets this host send as. |
| `tn_alert_smtp_host` | *(empty — required when on)* | The SMTP relay, reached over `tn-egress`. |
| `tn_alert_smtp_port` | `587` | `465` is implicit TLS (Alertmanager detects it). |
| `tn_alert_smtp_require_tls` | `true` | STARTTLS required; `false` only for a relay on a trusted network. |
| `vault_alert_smtp_username` / `vault_alert_smtp_password` | `CHANGE_ME` in `vault.yml.example` | Both set, or both empty for a relay that accepts unauthenticated mail from this host. |

Preflight refuses an empty recipient, sender or relay, a `CHANGE_ME` in either credential,
or one credential without the other; it ignores them while alert email is off.
`30-config` renders `/srv/truenorth/config/alertmanager/alertmanager.email.yml` (0400,
owned by Alertmanager's uid 65534) and writes the password to `smtp_password` beside it
(0600), which the config reads with `smtp_auth_password_file`: the password is not in the
config, the env file or `docker inspect`, and both tasks are `no_log`. The env file then
selects that config (`ALERTMANAGER_CONFIG_DIR`, `ALERTMANAGER_CONFIG=alertmanager.email.yml`)
and the role restarts a running Alertmanager when either file changes. Turning it off
removes both files and selects the shipped config again.

Routing: a **critical** is mailed at once (`group_wait: 0s`) and again every hour while it
fires; **warnings** are batched (`group_wait: 5m`, `group_interval: 15m`) and repeat every
12 h; a critical inhibits the warning of the same name and instance; every message has a
**resolved** follow-up. Subjects start `[TrueNorth <tn_domain_fqdn>]`, so staging and
production can share an inbox. If `vault_alertmanager_webhook_url` is set as well, the same
alerts go to the webhook too.

### Prove delivery (`tn-alert-test`)

A config that loads is not a message that arrives. After every change, fire a synthetic
critical alert and look for it in the inbox:

```bash
ansible-playbook -i inventory/staging.yml playbooks/alert-test.yml --ask-vault-pass
# or: ansible-playbook -i inventory/staging.yml site.yml --tags tn-alert-test --ask-vault-pass
```

It sends `TrueNorthAlertTest` (severity `critical`, a fresh `run` label each time) with
`amtool` inside `tn-alertmanager`, then watches Alertmanager's
`alertmanager_notification_requests_total` / `_failed_total` counters (per attempt, so a
refusal shows at once): it fails if the relay refused it three times or never accepted it
(`docker logs --since 5m tn-alertmanager` says why), or if nothing was attempted within 2
minutes. The counters cover every email Alertmanager sends, so run it when nothing else is
firing. `SENT` means the relay accepted it; delivery is proven when
`[TrueNorth <fqdn>] [FIRING:1] TrueNorthAlertTest ...` is in `tn_alert_email_to`, followed
about 5 minutes later by `[RESOLVED]`. Record both (time, mailbox) as the delivery
evidence. The same alert by hand, on the platform host:

```bash
docker exec tn-alertmanager amtool --alertmanager.url=http://localhost:9093 \
  alert add TrueNorthAlertTest severity=critical source=tn-alert-test run=$(date +%s) \
  --annotation='summary="TrueNorth alert delivery test"' \
  --end=$(date -u -d '+5 minutes' +%Y-%m-%dT%H:%M:%SZ)
```

### Webhook

`vault_alertmanager_webhook_url` (anything that accepts Alertmanager's webhook JSON: a chat
bridge, an incident tool), then re-run `30-config` and `50-stack-up`. The installer writes
the URL to `/srv/truenorth/config/alertmanager/webhook_url` (read with `url_file`, so it is
not in the environment). Without alert email it selects the shipped
`alertmanager.webhook.yml`: criticals repeat hourly, warnings every 4 h. Test it with the
same playbook.

Check a config change before shipping it:

```bash
cd infra/platform/docker/monitoring
docker run --rm --entrypoint amtool -v "$PWD/alertmanager:/c:ro" prom/alertmanager:v0.27.0 \
  check-config /c/alertmanager.yml /c/alertmanager.webhook.yml
# The rendered email config, on the platform host:
docker exec tn-alertmanager amtool check-config /etc/alertmanager/alertmanager.yml
docker run --rm --entrypoint promtool -v "$PWD/prometheus/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  -v "$PWD/prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro" \
  -v "$PWD/prometheus/secrets-dev:/etc/prometheus/secrets:ro" prom/prometheus:v2.51.0 \
  check config /etc/prometheus/prometheus.yml
```

## Runtime hardening

`compose.prod.yml` (`tests/contracts/test_runtime_hardening.py` holds these):

- Every container has a read-only root filesystem, drops all capabilities and sets
  `no-new-privileges`, with tmpfs for the paths it writes. Exceptions, each explained in
  its block: Keycloak and OpenSearch keep a writable root (Quarkus build; keystore);
  postgres, redis, minio and nginx add back the few capabilities their root entrypoints
  need; cAdvisor runs privileged.
- Redis is the Celery broker and runs `noeviction`: at `REDIS_MAXMEMORY` writes are refused
  (and `RedisHighMemory` fires) instead of queued tasks being evicted.
- `beat` sends the periodic tasks (`health_check_ranges`, `collect_range_metrics`). Run
  exactly one.
- Every Celery task has a time limit (`CELERY_TASK_SOFT_TIME_LIMIT`/`CELERY_TASK_TIME_LIMIT`,
  default 1800/1900 s; range tasks keep 3300/3500 s), below the broker's 3600 s visibility
  timeout. The workers' `stop_grace_period` outlasts them, so `docker compose down` or a
  redeploy lets running tasks finish: stopping `worker-provision` can take up to an hour
  while a range builds.
- `PROVISIONER_BACKEND`, `OPENAI_BASE_URL` (empty only with AI disabled), `OPENAI_API_KEY` (may be set empty) and
  `KEYCLOAK_ISSUER` have no defaults (the AI ones used to be one lab's LiteLLM address and
  key); `VSPHERE_VERIFY_SSL` defaults to true (`tn_vsphere_verify_ssl` sets it); the api and
  workers log JSON lines (`LOG_FORMAT=json`).

## Identity hardening

`60-keycloak` enforces on every run (and `infra/keycloak/realm-truenorth.json` carries for a
fresh import; `tests/contracts/test_keycloak_hardening.py` keeps them equal):

| Setting | Value |
|---|---|
| Access token | 5 min (the SPA refreshes silently) |
| Browser session | 30 min idle, 10 h maximum |
| Offline session | 7 days idle and maximum |
| Local password policy | 12+ characters, not the username, not one of the last 5 (AD users' passwords are AD's) |
| Password grant | off on `truenorth-web`, `truenorth-api` and `truenorth-cli` |
| PKCE S256 | required on the public clients (`truenorth-web`, `truenorth-cli`); the CLI's out-of-band redirect is gone |

`95-smoke-test` still needs a password grant to prove the claim contract, so it has its own
confidential client, `truenorth-smoke`, with a generated secret. It is **disabled**; the
smoke test enables it for its one token request and disables it again, however that
request ends.

## AI (optional)

AI is on by default, and preflight stops without `tn_ai_base_url`, so a site that forgets
its endpoint fails loudly. A site with no AI server yet (one not attached, say) installs
without it:

```yaml
tn_ai_enabled: false   # inventory: group_vars or the host
```

Then:

- preflight checks no AI endpoint and says `AI disabled (tn_ai_enabled=false): AI features
  will show 'not configured'`;
- `30-config` renders `OPENAI_BASE_URL=` and `OPENAI_API_KEY=` empty (even if the vault holds
  a key) and `TN_AI_ENABLED=false`. The AI orchestrator reports `ai_configured: false` on
  `/health`. Every generation route (detection and scenario drafts, Course Studio, quizzes,
  the exercise forge, MESL, AI AAR) answers **503 "AI is not configured on this host"**,
  never mock text and never a 502. The AI page says "AI is not configured";
- `99-validate` skips the model check, reports `AI : disabled`, and fails if the orchestrator
  says it is configured anyway;
- ARC² does not use the orchestrator. With AI disabled, `tn_arc2_enabled: true` is refused
  unless ARC² has a model of its own: `tn_arc2_anthropic_base_url`, `tn_arc2_local_url`, or a
  `vault_arc2_claude_oauth_token` / `vault_arc2_anthropic_api_key` ("ARC² Course Studio");
- `playbooks/acceptance.yml` runs with `--skip-profile staging,ai-disabled`. The AI check is a
  declared SKIP ("AI disabled on this host") only when the flag, the orchestrator and the 503
  agree; otherwise it FAILs.

**Turning AI on later.** Set `tn_ai_enabled: true` and `tn_ai_base_url`, put the key in
`vault_openai_api_key` (`ansible-vault edit inventory/group_vars/all/vault.yml`; empty only
for an unauthenticated endpoint), set `tn_ai_default_model` (and `tn_ai_embed_model`) to ids
the endpoint lists, and re-run `site.yml`. Preflight then probes the endpoint, and `99-validate`
fails if a configured model is missing.

## ARC² Course Studio (optional)

**Off by default** (`tn_arc2_enabled: false`): nothing is installed, the api gets no `ARC2_*`
setting and no host mount, and `/api/arc2` answers 404. On, `55-arc2` installs the runner
that turns Course Studio requests (`/arc2` in the web app) into headless `/arc2` runs of the
seven ARC² agents in Claude Code, and the api serves the Studio from the runs directory it
shares with the runner. Read docs/arc2-course-studio.md §13 first: course requests, feedback
and generated content go to the model endpoint (Anthropic's API unless you set a gateway),
so decide whether that is acceptable for what this platform holds.

**What you provide**

| | |
|---|---|
| `tn_arc2_enabled: true` | inventory (group_vars or the host) |
| A model mode | `tn_arc2_mode`: `subscription` (default; Claude), `local` (only an Anthropic-compatible gateway; jobs never reach `api.anthropic.com`) or `subscription_with_local_fallback`. See "Model mode" below |
| One model credential | vault; **exactly one** of `vault_arc2_claude_oauth_token` or `vault_arc2_anthropic_api_key`, no default (preflight stops with neither, and with both, so it is never ambiguous which account is billed). Not needed in `local` mode. See "Model credential" below |
| HTTPS out to `api.anthropic.com` | from the platform host (preflight checks). Or `tn_arc2_anthropic_base_url`: an Anthropic-compatible gateway, `https://` on port 443 only; Claude Code reads `ANTHROPIC_BASE_URL` and the jobs' proxy then allows that host instead |
| At install time: PyPI, the npm registry, nodejs.org | or mirrors: `tn_arc2_pip_index_url`, `tn_arc2_npm_registry`, `tn_arc2_node_mirror` |
| A free gid | `tn_arc2_runs_gid` (`10010`; preflight checks) |
| Disk | `tn_arc2_min_free_gb` (5) for runs; `tn_arc2_min_free_system_gb` (3) for `/opt/truenorth-arc2` (about 0.8 GB) |

**Model credential.** Set one, leave the other empty (`ansible-vault edit
inventory/group_vars/all/vault.yml`; `vault.yml.example` documents both):

- **Claude subscription (recommended if you already have one).** Run `claude setup-token`
  on any machine signed in to the subscription and put the token in
  `vault_arc2_claude_oauth_token`. It is the operator's own credential, and usage counts
  against that subscription's limits: a job that hits a limit stops with Claude Code's
  usage-limit message and is not retried (the runner's local-model fallback stays off on
  the platform host), so send it again once the limit resets. The token is long-lived:
  revoke it and issue a new one if the host is rebuilt or decommissioned, or if the token
  may have been exposed. A subscription token is for Anthropic's own endpoint; with
  `tn_arc2_anthropic_base_url` (a gateway) use whatever credential that gateway accepts.
- **Pay-per-use API key.** Put it in `vault_arc2_anthropic_api_key`. Issue a key for this
  host alone, with a spending limit.

Either way it reaches only the runner: `/etc/truenorth-arc2/runner.env` (`root:root 0600`,
written with `no_log`), as `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`. Every job can
read it, and can use it only against the model endpoint (the jobs' egress proxy allows
nothing else). Preflight also refuses an API key (`sk-ant-api…`) in the token's variable
and a token (`sk-ant-oat…`) in the key's.

**Model mode.** `tn_arc2_mode: local` runs every job on an Anthropic-compatible gateway
(for example a LiteLLM router exposing `/v1/messages`) instead of Claude: set
`tn_arc2_local_url` (`https://host[:port][/path]`, any https port, never http; staging's is
`https://atlas.tail8d54ec.ts.net:4443`), `tn_arc2_local_model` (the model id the gateway
serves) and, in the vault, `vault_arc2_local_token` (its bearer token; issue one for this host
alone). The jobs' proxy then allows exactly that host:port and nothing else, and no Anthropic
credential is written to the runner's environment file. A tailnet or internal gateway sits in
a range `tn_arc2_ip_deny` denies (Tailscale: `100.64.0.0/10`): leave the list as it is.
`55-arc2` resolves the gateway's host on the platform host (it must resolve there: for a
tailnet name, the host must be on the tailnet) and the unit allows exactly those addresses
(`IPAddressAllow`); the rest of the range stays denied. Re-run `55-arc2` if the gateway's
address changes.
`subscription_with_local_fallback` needs both an Anthropic credential and the gateway: Claude
runs each step, and a step Claude could not run at all (signed out, usage limit, overloaded)
is re-run on the gateway. Preflight and `55-arc2` check what each mode needs; the token goes
only into `/etc/truenorth-arc2/runner.env` as `ARC2_LOCAL_TOKEN` (`no_log`).
docs/arc2-course-studio.md §13 "Model mode" has the details.

**Test hosts: auto-accept.** `tn_arc2_auto_accept: true` (staging sets it) makes the runner
accept the outline and preview gates itself when a job stops at one with nothing failing, so
a course builds end to end with no reviewer. It is recorded as `accepted_by: "auto (test
host)"`, and the Studio marks such runs "TEST CONTENT — gates auto-accepted, not reviewed".
It also writes `TN_TEST_HOST=true` for the api (`30-config`): only then does the api accept or
publish a release built from such a run, labelled "TEST CONTENT — not reviewed" on the
course page and in Moodle; anywhere else it refuses them.
Never set it on a platform whose courses reach Students.

Then `ansible-playbook site.yml`, or on a running platform `30-config`, `50-stack-up` (the
api picks up the setting and the mount) and `55-arc2`. Switching between the two
credentials, or between modes, later: change the vault or inventory and re-run `55-arc2` (it
rewrites the file and the unit and restarts the runner).

**What `55-arc2` installs**

| | |
|---|---|
| `tn-arc2` | system account: no login shell, no password, no sudo, not in `docker`; home `/var/lib/tn-arc2` (0700: per-job homes, deleted after each job) |
| `{{ tn_data_root }}/arc2/runs` | the runs root, `tn-arc2:tn-arc2-runs` 2750; `_queue/` and `_studio/` 2770 (the api writes them); `_jobs/`, `_history/` and each run 2750 (the runner's). The api container joins `tn-arc2-runs` by gid (`group_add`) and mounts the directory at `/srv/arc2/runs` |
| `/opt/truenorth-arc2/` | root-owned: `node` (pinned, SHA-256-checked tarball), `claude-code` (pinned, `npm ci` from `roles/tn_arc2/files/claude-code/package-lock.json`), `venv` (the engine's Python: the api's requirements, pytest, ruff), `bin/bwrap` (a copy, `root:tn-arc2 0750`) |
| `/etc/truenorth-arc2/runner.env` | the credentials only (per `tn_arc2_mode`: `CLAUDE_CODE_OAUTH_TOKEN` or `ANTHROPIC_API_KEY`, and/or `ARC2_LOCAL_TOKEN`), `root:root 0600`, read by systemd (`EnvironmentFile`). Never on a command line, never in `.env.production` |
| `/etc/apparmor.d/truenorth-arc2-bwrap` | see "Sandbox" below |
| `truenorth-arc2-runner.service` | the runner, from the deployed checkout (read-only), as `tn-arc2` |

**Sandbox.** Every job runs in a bubblewrap sandbox (`tools/arc2/confine.py`): its own PID,
network, IPC and mount namespaces; writes only to its own run; internet only through the
runner's proxy, to the model endpoint on 443 (`ARC2_EGRESS_ALLOW`). The runner fails closed
(`ARC2_CONFINE=auto`): no sandbox, no runner. Ubuntu 24.04 forbids unprivileged user
namespaces to unconfined programs (`kernel.apparmor_restrict_unprivileged_userns=1`), which
stock bubblewrap needs. The installer leaves that on for the host. Instead it lets one binary
create them: the runner's own copy of `bwrap`, which only root and `tn-arc2` can execute,
attached to an AppArmor profile. Everything that bwrap starts is stacked under a second profile
that may create no user namespace and holds no capability, so a job cannot reuse the
permission. Re-run `55-arc2` after a `bubblewrap` package update: it refreshes the copy.

The service adds the outer wall, which bubblewrap builds every job from: `NoNewPrivileges`,
an empty capability bounding set, `ProtectSystem=strict` (writable: the runs directory and
its own state directory), `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `PrivateIPC`,
`ProtectProc=invisible`, a system-call filter, only the namespaces bubblewrap creates, the
platform's config, TLS, state and datastore directories and `/var/lib/docker` made
inaccessible, no private-network addresses (`tn_arc2_ip_deny`; a local gateway's own resolved
addresses are let through, nothing else), and memory, CPU and task limits. `ProtectKernelTunables`, `ProtectKernelLogs`,
`ProtectHostname` and `ProcSubset=pid` are deliberately off: each overmounts part of
`/proc`, and bubblewrap then cannot mount the job's own `/proc`.

**Health.** Before the runner starts, the unit runs `arc2.runner --self-test`: `claude
--version` inside a job's sandbox, under the unit's hardening, with the egress proxy and the
job's network namespace. It calls no model and spends nothing. `55-arc2` probes bubblewrap as
`tn-arc2` before starting the service, then requires the self-test to have passed with
`confinement: bubblewrap` and the pinned Claude Code, and the runner to be watching the
queue; `99-validate` reports the service state. Without a sandbox the self-test exits 2 and
the runner never starts; systemd retries five times in ten minutes, then leaves the unit
failed (`55-arc2` clears that when it next runs).

```bash
journalctl -u truenorth-arc2-runner            # self-test, jobs started and finished
sudo systemctl restart truenorth-arc2-runner   # re-runs the self-test
```

**Runs started outside the Studio, or before ownership was recorded,** are listed to nobody.
Assign them (docs/arc2-course-studio.md §11):

```bash
sudo -u tn-arc2 env PYTHONPATH=/srv/truenorth/app/tools /opt/truenorth-arc2/venv/bin/python \
  -m arc2.assign_owner --runs /srv/truenorth/arc2/runs --tenant <tenant-uuid> --all-unowned --dry-run
```

**Backups** do not include the runs directory. A course becomes durable when its release is
uploaded into TrueNorth (database and MinIO, which are backed up; docs/arc2-course-studio.md §14).

**Bumping the pinned versions.** Claude Code: pick the `stable` dist-tag, then

```bash
npm view @anthropic-ai/claude-code dist-tags
cd install/roles/tn_arc2/files/claude-code
# package.json: "@anthropic-ai/claude-code": "<version>" (exact, no ^ or ~)
rm package-lock.json && npm install --package-lock-only --ignore-scripts
```

and set `tn_arc2_claude_code_version` to the same version (`tests/contracts/test_installer_arc2.py`
checks they agree, and `55-arc2` checks what `claude --version` reports). Node.js: set
`tn_arc2_node_version`, and `tn_arc2_node_sha256` in `roles/tn_arc2/defaults/main.yml` from that
release's `SHASUMS256.txt`, after checking `SHASUMS256.txt.asc` with a key from
github.com/nodejs/release-keys. Then re-run `55-arc2`.

**Turning it off.** Set `tn_arc2_enabled: false` and re-run `30-config` and `50-stack-up`: the
api loses the setting and the mount. `55-arc2` then does nothing, so stop the runner yourself
(`sudo systemctl disable --now truenorth-arc2-runner`); the runs stay in
`{{ tn_data_root }}/arc2/runs`.

## Moodle (optional)

`tn_moodle_enabled: true` (default `false`) makes `85-moodle` run TrueNorth's Moodle
(Moodle 5.2.3 with `local_truenorth`) for the tenant `tn_tenant_slug`, so accepted ARC²
course releases can be published into it and Students enter it from TrueNorth, signed in.
The walkthrough (install, publish, take a course as a Student, restore, remove) is
docs/runbooks/moodle.md.

- **Image:** `images.moodle` of the release's `release-manifest.json`, by digest (built,
  Trivy-scanned, SBOM'd and cosign-signed by `release.yml` like the others). Releases
  before it have none: the stage stops and says so (`-e tn_moodle_image=<ref@sha256>`
  pins one deliberately). Build mode builds `truenorth-moodle:local`. Air-gapped: add it
  to the archive directory (`docker save` of the manifest's moodle ref, tagged).
- **Address:** `https://<tn_domain_fqdn>:<tn_moodle_port>` (8443), served by its own nginx
  (`moodle-edge`) with the platform's certificate: no DNS record or certificate name to
  add, but open the port where 443 is open. Its own origin, so HTML authored in Moodle
  cannot reach TrueNorth's session.
- **Data:** database `moodle` (role `moodle`) in the platform's PostgreSQL, so every backup
  already dumps it; moodledata under `/srv/truenorth/moodle/<node>/`, which backups archive
  and `restore.sh` restores when `30-config` has rendered `MOODLE_COMPOSE_FILE` into
  `backup.env` (it does when Moodle is enabled).
- **Credentials:** `moodle_db_password` and `moodle_admin_password`, generated once into
  `/srv/truenorth/config/secrets/` (escrowed with the rest) unless the vault sets
  `vault_moodle_*`; never on a command line.
- **Wiring:** no manual steps. The node fetches TrueNorth's tool public key from the api,
  serves only this tenant's tickets, and is registered as platform `moodle-<node>`
  through `python -m app.moodle_backends.install_cli` in the api container. The stage's
  smoke check: the login page through the edge, the hand-off to TrueNorth, and a signed
  `describe_course` from TrueNorth's own publishing backend.
- **One tenant.** Further tenants get one node each (the farm model); the role takes a node
  name but loops over one (docs/runbooks/moodle.md, "More than one tenant").

## cmi5

TrueNorth launches its released courses as a cmi5 LMS (docs/cmi5.md). AU traffic reaches
the LRS with its own credential, `CMI5_LRS_AUTH`, never the server's `LRS_AUTH`; without it
every launch answers 503. With `tn_cmi5_enabled` (the default):

- **Minted, not generated.** The LRS must know the credential, so `50-stack-up` creates it
  once the stack is up (`roles/tn_cmi5`), through lrsql's admin API from a one-shot api
  container (`LRS_ADMIN_USER` / `LRS_ADMIN_PASSWORD` in its environment, never on a command
  line), with the scopes statements/write, state, agents_profile and activities_profile.
- **Persisted and kept.** It goes to `/srv/truenorth/config/secrets/cmi5_lrs_auth` (0600,
  escrowed by every backup, never printed) and is rendered into `.env.production`; the api is
  recreated with it. Every re-run checks the LRS still has it with those scopes and keeps it;
  only if the LRS lost it (a rebuilt or restored LRS database) is a new one minted, with a
  warning. `vault_cmi5_lrs_auth` supplies one instead: it must already be a credential of
  this LRS with exactly those scopes, and is never replaced (the stage stops if it is not).
- **Checked.** Preflight refuses an unusable `vault_cmi5_lrs_auth`; `99-validate` fails an
  install whose env file has no `CMI5_LRS_AUTH` (or has `LRS_AUTH` there); `95-smoke-test`
  calls a cmi5 route through nginx and, in the running api, the launch gate that answers
  503, then checks that the LRS accepts the credential and refuses it a statement read.
- **URLs.** `CMI5_WEB_BASE_URL` / `CMI5_API_BASE_URL` (where Students' browsers reach the AU
  runtime and the API) and `XAPI_ACCOUNT_HOMEPAGE` / `XAPI_IRI_BASE` (the actor's homePage
  and the IRI root) are rendered from the variables below, which default to what the api
  used before. The two xAPI values are written into every statement: never change them once
  a cohort has started.

`tn_cmi5_enabled: false` renders no credential: launches answer 503 (fail closed).

## Key variables

`inventory/group_vars/all/main.yml` is commented throughout. The ones you will
actually change:

| Variable | Default | Note |
|---|---|---|
| `tn_install_docker` | `true` on Ubuntu 24.04, else `false` | `00-docker`; `false` to manage Docker yourself ("Prerequisites"). |
| `tn_docker_version` / `tn_compose_version` | `29.9.0` / `5.6.0` | Pinned and held; with `tn_containerd_version`, `tn_buildx_version`. |
| `tn_docker_manage_daemon_json` | `true` | `false`: `/etc/docker/daemon.json` is left alone. |
| `tn_app_git_repo` | `github.com/mblanke/TrueNorth` (public) | |
| `tn_app_git_version` | *(empty: the installer's own commit)* | The installer and the app stay the same revision. In release mode the manifest's `git_sha` replaces it. |
| `tn_allow_downgrade` | `false` | Deliberate rollback only (docs/runbooks/upgrade.md). |
| `tn_ldap_enabled` | `false` (`true` for TN-MGMT01 in `hosts.yml`) | "Without Active Directory". |
| `tn_ai_enabled` | `true` | `false`: install without AI; AI features say "not configured" ("AI (optional)"). |
| `tn_ai_base_url` | *(empty — required unless `tn_ai_enabled: false`)* | The LiteLLM endpoint; no committed default. Key: `vault_openai_api_key`. |
| `tn_backup_escrow_pubkey` / `tn_backup_escrow` | *(empty — one required)* | "Backups". |
| `tn_backup_min_free_gb` / `tn_backup_max_total_gb` | `20` / `200` | Backup disk floor and cap. |
| `tn_opensearch_snapshot_repo` / `tn_opensearch_snapshot_keep` | `tn_snapshots` / `14` | Telemetry snapshots per backup ("Backups"); `""` = none. |
| `tn_upgrade_inflight` / `tn_pre_upgrade_backup` | `drain` / `true` | "Upgrades". |
| `tn_vsphere_verify_ssl` | `true` | TN-MGMT01 overrides it to `false` in `hosts.yml` (VMCA chain not yet trusted). |
| `tn_image_source` | `release` | `build` only for a lab ("Images") |
| `tn_release_version` | *(empty — required in release mode)* | The release tag to install, e.g. `v1.2.3` |
| `tn_release_dir` / `tn_image_archive_dir` | *(empty)* | Air-gapped: the release files, and `docker save` tarballs |
| `tn_alert_email_enabled` | `false` (staging too, until its SMTP details are set) | Alerts by email; with `tn_alert_email_to`, `tn_alert_email_from`, `tn_alert_smtp_host`, `tn_alert_smtp_port` (`587`), `tn_alert_smtp_require_tls` (`true`) and `vault_alert_smtp_username` / `vault_alert_smtp_password` ("Alerting"). Off: alerts go nowhere. |
| `vault_alertmanager_webhook_url` | *(empty)* | A webhook receiver, alone or beside email ("Alerting"). |
| `tn_bootstrap_admin_upn` | *(empty — required)* | The named AD account that admits everyone else. |
| `tn_tls_mode` | `selfsigned` | `provided` once the AD CS certificate is in `files/tls/` |
| `tn_opensearch_disable_security` | `false` | Lab/dev override only: OpenSearch without auth or TLS ("OpenSearch" above). |
| `tn_provisioner_backend` | `vsphere_api` | **Not** `vsphere` — that is not a registry key; preflight refuses it. `mock` for a site without vCenter. |
| `tn_experimental_provisioners` | `false` | `true` lets preflight, the API and the workers accept the experimental `proxmox_api` / `hyperv` backends (unsupported). |
| `tn_seed_demo_data` | `false` | Demo tenants have no place in a range holding CAF curriculum. |
| `tn_default_progression` | `DP1` | Developmental progression a new trainee joins (DP1 → DP2). |
| `tn_arc2_enabled` | `false` | ARC² Course Studio; with exactly one of `vault_arc2_claude_oauth_token` or `vault_arc2_anthropic_api_key` ("ARC² Course Studio (optional)"). |
| `tn_arc2_claude_code_version` / `tn_arc2_node_version` | `2.1.286` / `24.21.0` | Pinned; bump with the lockfile and checksums. |
| `tn_moodle_enabled` / `tn_moodle_port` | `false` / `8443` | "Moodle (optional)". |
| `tn_cmi5_enabled` | `true` | "cmi5": `50-stack-up` mints the AU's LRS credential (`CMI5_LRS_AUTH`) once and keeps it; validate fails without it. `false`: cmi5 launches answer 503. |
| `vault_cmi5_lrs_auth` | *(empty: minted on the LRS)* | base64 `key:secret` of a credential this LRS already has, with exactly the AU scopes, other than `vault_lrs_api_key`/`_secret` ("cmi5"). |
| `tn_cmi5_web_base_url` / `tn_cmi5_api_base_url` | `tn_lti_web_base_url` / that + `/api` | `CMI5_WEB_BASE_URL` / `CMI5_API_BASE_URL`: where Students' browsers reach the AU runtime and the API. |
| `tn_xapi_account_homepage` / `tn_xapi_iri_base` | `https://<tn_domain_fqdn>` / that + `/xapi` | `XAPI_ACCOUNT_HOMEPAGE` / `XAPI_IRI_BASE`. In every statement: **never change them once data exists**. |

## After the install

1. Sign in at `https://<tn_domain_fqdn>/` with the bootstrap admin's **AD**
   credentials. TrueNorth never holds a password.
2. Everyone else signs in and lands on the registration form rather than a 403.
3. Admit them from **Users → Approvals**. Approving is what creates the account
   and assigns the role — AD group membership only *suggests* one.

## Secrets

`inventory/group_vars/all/vault.yml` is gitignored and must be
`ansible-vault`-encrypted. The rendered `.env.production` is written to
`/srv/truenorth/config/` on the target, mode `0600` — deliberately **outside**
the git checkout, so a secret can never be swept into a commit (the in-repo
`infra/platform/docker/.env.production` was once tracked; it is now git-ignored).

Every secret is persisted under `/srv/truenorth/config/secrets/` (0700, one file each): the
vault's value when it sets one, otherwise one generated on the target once (an empty file
counts as missing and is regenerated). All must be at least 32 characters. They are never
regenerated: a re-run that changed `POSTGRES_PASSWORD` would lock you out of your own
database. For the passwords a service reads only on its first start (the PostgreSQL roles,
the Keycloak master admin, OpenSearch's users, Grafana's admin), a vault value that differs
from the persisted one **stops the install**; change them with `playbooks/rotate-secret.yml`
(docs/runbooks/key-rotation.md).

No secret is a process argument: Redis reads its password from a 0600 config file on its
tmpfs and `redis-cli` from `REDISCLI_AUTH`; Flower's broker URL and login are environment
settings; the LDAPS check reads the bind password from a temporary 0600 file; the migration's
`DATABASE_URL` is passed through the environment of `docker compose run`.

The API calls Keycloak's Admin API (`/ad-sync`) as its own service account,
`truenorth-api-admin` (client credentials; realm-management `view-realm` and `manage-users`
only). The master-realm admin password reaches the keycloak service (first start) and the
installer's one-shot `app.bootstrap_admin` run, never the api service.
