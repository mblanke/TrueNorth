# TrueNorth Range — installer

Takes the platform host from *"Docker installed, `/srv/truenorth` empty"* to a
running, AD-federated TrueNorth with a working trainee registration path.

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

On the **platform host** (TN-MGMT01): Ubuntu 24.04, Docker ≥ 24, Compose ≥ 2.20,
and your SSH key in `~tnadmin/.ssh/authorized_keys` (the inventory expects
`~/.ssh/id_ed25519_lab` on the control node). In `git` mode it also needs HTTPS out to
github.com; without it, use `local` mode (below). Preflight checks both.

From the deployment repo you need `certs/corp-root-ca.cer` — copy it to
`install/files/`. Keycloak cannot bind to AD over LDAPS without trusting that
CA, and the failure it produces (`PKIX path building failed`) does not obviously
point at a missing certificate.

## Quick start

```bash
# 1. Secrets
cp inventory/group_vars/all/vault.yml.example inventory/group_vars/all/vault.yml
$EDITOR inventory/group_vars/all/vault.yml       # fill in the CHANGE_ME values
ansible-vault encrypt inventory/group_vars/all/vault.yml

# 2. Nominate the first administrator — a NAMED AD account.
#    Without this the install finishes with an approval queue nobody can drain.
#    And pin the release to install (docs/release.md): tn_release_version: v1.2.3
#    Set vault_alertmanager_webhook_url too, or alerts go nowhere ("Alerting").
$EDITOR inventory/group_vars/all/main.yml        # tn_bootstrap_admin_upn, tn_release_version

# 3. TLS: drop truenorth.crt / truenorth.key into files/tls/
#    (or use -e tn_tls_mode=selfsigned for a lab bring-up)

# 4. Check before you change anything
ansible-playbook site.yml --ask-vault-pass --check

# 5. Install
ansible-playbook site.yml --ask-vault-pass

# 6. Prove it — a clean second run is the idempotency proof
ansible-playbook site.yml --ask-vault-pass
```

## Stages

Each is independently re-runnable: `ansible-playbook playbooks/60-keycloak.yml`
is always safe on its own.

| Playbook | What it does |
|---|---|
| `00-preflight` | OS, Docker/Compose versions, disk, NTP, forward+reverse DNS, vCenter and LDAPS reachability, the app repository. Read-only, and runs for real under `--check`; fails loudly with the fix in the message. |
| `10-base` | Packages, `vm.max_map_count` (OpenSearch will not start without it), the `/srv/truenorth` tree with the uids each image runs as. |
| `20-fetch-app` | Reads the release's `release-manifest.json` (checked against its `SHA256SUMS`), then clones the app at the release's commit (`git_sha`) and refuses any other. Supports `local` and `tarball` modes for air-gapped installs. Records the deployed commit, release and image refs. |
| `30-config` | Renders `.env.production`, generating any secret the vault left blank **once** and persisting it on the target. |
| `40-tls` | Installs the AD CS root CA (DER or PEM, normalised to PEM) into the host trust store *and* Keycloak's truststore, then does a **real LDAPS bind** as the Keycloak service account against that CA; places the certificate nginx serves. |
| `50-stack-up` | Pulls the release's images **by digest** (or loads the air-gapped archives), stops unless every image resolves to the digest compose names, then datastores → **alembic** (with the new api image) → everything else. Builds nothing unless `tn_image_source=build`. See "Images" and "The migration hazard" below. |
| `60-keycloak` | Realm (imported without the development realm's sample users, with this host's redirect URIs and generated client secrets), token/session/password rules and client grants (all enforced on every run; "Identity hardening"), AD user federation over LDAPS, and the token claim mappers. **This is the join between the installer and the application** — see below. |
| `70-telemetry` | OpenSearch index templates, ISM policies, ingest pipelines. |
| `80-seed` | Verifies reference data actually seeded, creates the tenant and the bootstrap administrator. |
| `90-vsphere` | Provider wiring, and detects the unassigned vCenter role. |
| `95-smoke-test` | Container health, API health, and the AD claim-contract check (pass `-e tn_smoke_username=<upn> -e tn_smoke_password=<password>`; without them it is skipped and says so). The token comes from the `truenorth-smoke` client, enabled only for that one request. |
| `99-validate` | Final report, including accepted warnings. |

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

The security index is created from these files the **first** time OpenSearch starts.
Changing a password afterwards: set the new one in the vault (or the secrets file),
re-run `40-tls` and `30-config`, then load it into the running cluster and restart the
clients:

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production exec opensearch \
  plugins/opensearch-security/tools/securityadmin.sh -f /usr/share/opensearch/config/opensearch-security/internal_users.yml \
  -t internalusers -icl -nhnv -cacert config/certs/ca.pem -cert config/certs/admin.pem -key config/certs/admin-key.pem
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production up -d
```

**Lab/dev override only:** `-e tn_opensearch_disable_security=true` renders
`OPENSEARCH_DISABLE_SECURITY=true`, `OPENSEARCH_URL=http://opensearch:9200` and an empty
`OPENSEARCH_USER`: no authentication, plain http on `tn-backend`. Never on a platform
Students use. The dev and integration compose files (`compose.dev.yml`, `compose.itest.yml`)
run OpenSearch this way.

## Backups

`30-config` schedules `scripts/backup/cron-backup.sh` nightly (02:17). It dumps the
application, Keycloak and LRS databases, Redis and MinIO, plus the compose and nginx
configuration, under `/srv/truenorth/backups`. Restore with
`scripts/backup/restore.sh <backup-dir>` (same `COMPOSE_FILE`/`ENV_FILE` as
`config/backup.env`).

Not in the backup set: **secrets** (`/srv/truenorth/config/secrets/`, or your vault), which
must be kept offline separately, and OpenSearch telemetry (no snapshot repository is
configured; scores and outcomes are in PostgreSQL).

## Images: deploy by digest

`compose.prod.yml` builds nothing. The four TrueNorth images run as `${TN_IMAGE_API}`,
`${TN_IMAGE_WORKER}` (the workers, `beat` and `flower`), `${TN_IMAGE_WEB}` and
`${TN_IMAGE_AI_ORCHESTRATOR}`, each an `image@sha256:…` ref; every third-party image is
pinned by digest in the file itself. Compose refuses to start without the four variables.

With `tn_image_source: release` (the default), `roles/tn_release`:

1. takes `release-manifest.json` and `SHA256SUMS` of `tn_release_version` from
   `tn_release_dir` on the control node, or downloads them from the GitHub release
   (`tn_release_repo`) into `install/.cache/releases/<tag>/`;
2. checks the manifest's SHA-256 against `SHA256SUMS`, `schema_version` 1, the version,
   and that every service has an `image@sha256` ref (docs/release.md);
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

or with a release tarball via `tn_app_source_mode=tarball`. The release files: put
`release-manifest.json` and `SHA256SUMS` in a directory and pass `-e tn_release_dir=<dir>`.

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
target needs the containerd image store too. `50-stack-up` copies the tarballs, `docker
load`s the ones that changed, and runs the same digest check instead of pulling.

## Alerting

Prometheus evaluates the 19 rules in `monitoring/prometheus/alerts.yml` and sends what
fires to the `alertmanager` service. **By default Alertmanager routes everything to a
`null` receiver: nobody is told.** `30-config` says so in a warning, and `99-validate`
reports `alerts: NOWHERE`. For any platform Students use, set
`vault_alertmanager_webhook_url` (anything that accepts Alertmanager's webhook JSON: a chat
bridge, an incident tool) and re-run `30-config` and `50-stack-up`. The installer writes
the URL to `/srv/truenorth/config/alertmanager/webhook_url` (read with `url_file`, so it is
not in the environment) and selects `alertmanager.webhook.yml`. Criticals repeat hourly,
warnings every 4 h; a critical inhibits the warning of the same name and instance.

Check a config change before shipping it:

```bash
cd infra/platform/docker/monitoring
docker run --rm --entrypoint amtool -v "$PWD/alertmanager:/c:ro" prom/alertmanager:v0.27.0 \
  check-config /c/alertmanager.yml /c/alertmanager.webhook.yml
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
- `PROVISIONER_BACKEND` has no default (it used to fall back to `mock`);
  `VSPHERE_VERIFY_SSL` defaults to true (`tn_vsphere_verify_ssl` sets it); the api and
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

## Key variables

`inventory/group_vars/all/main.yml` is commented throughout. The ones you will
actually change:

| Variable | Default | Note |
|---|---|---|
| `tn_app_git_repo` | `github.com/mblanke/TrueNorth` (public) | |
| `tn_app_git_version` | a pinned SHA of `main` | **Pin a tag or SHA.** A branch makes re-runs non-deterministic. In release mode the manifest's `git_sha` replaces it. |
| `tn_image_source` | `release` | `build` only for a lab ("Images") |
| `tn_release_version` | *(empty — required in release mode)* | The release tag to install, e.g. `v1.2.3` |
| `tn_release_dir` / `tn_image_archive_dir` | *(empty)* | Air-gapped: the release files, and `docker save` tarballs |
| `vault_alertmanager_webhook_url` | *(empty: alerts go nowhere)* | "Alerting" |
| `tn_bootstrap_admin_upn` | *(empty — required)* | The named AD account that admits everyone else. |
| `tn_tls_mode` | `selfsigned` | `provided` once the AD CS certificate is in `files/tls/` |
| `tn_opensearch_disable_security` | `false` | Lab/dev override only: OpenSearch without auth or TLS ("OpenSearch" above). |
| `tn_provisioner_backend` | `vsphere_api` | **Not** `vsphere` — that is not a registry key and raises `ValueError`. |
| `tn_seed_demo_data` | `false` | Demo tenants have no place in a range holding CAF curriculum. |
| `tn_default_progression` | `DP1` | Developmental progression a new trainee joins (DP1 → DP2). |

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

Values left blank in the vault are generated on the target and persisted under
`/srv/truenorth/config/secrets/`. They are never regenerated: a re-run that
changed `POSTGRES_PASSWORD` would lock you out of your own database.
