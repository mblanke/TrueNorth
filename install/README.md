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
and your SSH key in `~tnadmin/.ssh/authorized_keys`.

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
$EDITOR inventory/group_vars/all/main.yml        # tn_bootstrap_admin_upn

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
| `00-preflight` | OS, Docker/Compose versions, disk, NTP, forward+reverse DNS, vCenter reachability, and a **real LDAPS bind** as the Keycloak service account. Read-only; fails loudly with the fix in the message. |
| `10-base` | Packages, `vm.max_map_count` (OpenSearch will not start without it), the `/srv/truenorth` tree with the uids each image runs as. |
| `20-fetch-app` | Clones the app at a pinned ref. Supports `local` and `tarball` modes for air-gapped installs. Records the deployed commit. |
| `30-config` | Renders `.env.production`, generating any secret the vault left blank **once** and persisting it on the target. |
| `40-tls` | Installs the AD CS root CA into the host trust store *and* Keycloak's truststore; places the certificate nginx serves. |
| `50-stack-up` | Datastores → **alembic** → everything else. See "The migration hazard" below. |
| `60-keycloak` | Realm, AD user federation over LDAPS, and the token claim mappers. **This is the join between the installer and the application** — see below. |
| `70-telemetry` | OpenSearch index templates, ISM policies, ingest pipelines. |
| `80-seed` | Verifies reference data actually seeded, creates the tenant and the bootstrap administrator. |
| `90-vsphere` | Provider wiring, and detects the unassigned vCenter role. |
| `95-smoke-test` | Container health, API health, and the AD claim-contract check. |
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

## Air-gapped installs

```bash
ansible-playbook site.yml \
  -e tn_app_source_mode=local \
  -e tn_app_local_path=/path/to/TrueNorth
```

or with a release tarball via `tn_app_source_mode=tarball`. Container images
still have to reach the host — pre-pull them and `docker load` before running.

## Key variables

`inventory/group_vars/all/main.yml` is commented throughout. The ones you will
actually change:

| Variable | Default | Note |
|---|---|---|
| `tn_app_git_repo` | `git.guapo613.beer/soadmin/truenorth` | |
| `tn_app_git_version` | `main` | **Pin a tag or SHA.** A branch makes re-runs non-deterministic. |
| `tn_bootstrap_admin_upn` | *(empty — required)* | The named AD account that admits everyone else. |
| `tn_tls_mode` | `provided` | or `selfsigned` for a lab |
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
the git checkout, because `infra/platform/docker/.env.production` is a tracked
path and rendering secrets there would commit them.

Values left blank in the vault are generated on the target and persisted under
`/srv/truenorth/config/secrets/`. They are never regenerated: a re-run that
changed `POSTGRES_PASSWORD` would lock you out of your own database.
