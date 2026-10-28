# TrueNorth Range — Deployment reference

> What the deployed platform is made of, and where each part is configured. To install
> TrueNorth, follow the role guides instead.

---

## Start here: the guides and the installer

**To install TrueNorth, follow [02 — Install guide](guides/02-install-guide.md)** (after
[01 — Site prerequisites](guides/01-site-prerequisites.md)), and to run it,
[06 — Operations runbook](guides/06-operations-runbook.md). The guides drive the Ansible
installer in [`install/`](../install/README.md), whose README is the reference for every
installer variable. Do not deploy by hand. The installer takes the platform host from bare
Ubuntu 24.04 with `/srv/truenorth` empty (it installs a pinned Docker; `tn_install_docker`)
to a running, AD-federated TrueNorth, and it handles several things that are easy to get
wrong manually:

- **Migration ordering.** `app/main.py`'s lifespan calls
  `Base.metadata.create_all()` on every API start, in every uvicorn worker. Bring
  the API up before Alembic on a fresh database and the migration dies on
  `DuplicateTable`. The installer brings up Postgres alone, migrates in a
  one-shot container, then starts everything else — and sets `DB_AUTO_CREATE=false`.
- **The Keycloak claim contract.** Registration reads AD group
  membership out of the access token. Miss the group-membership mapper and
  nothing errors; the platform simply loses half its input forever. See
  [identity.md](identity.md#the-claim-contract).
- **Secrets that must not be regenerated.** A second run that produced a new
  `POSTGRES_PASSWORD` would lock you out of your own database.
- **The env file location.** The installer writes the real env file to
  `/srv/truenorth/config/`, outside the git checkout. `infra/platform/docker/.env.production`
  is git-ignored (it was tracked, holding placeholder values, until 2026-10-05).
  `.env.production.example` documents the settings; the installer renders the real file.

This page keeps only what the guides and `install/README.md` do not: pointers for
Kubernetes, the Keycloak clients, metrics exposure and a few API settings. Hardware sizing
is in guide 01; backups, upgrades and troubleshooting are in guide 06. A development stack
(`compose.dev.yml`) is described in [development.md](development.md).

> **Note on `PROVISIONER_BACKEND`:** the valid values are the registry keys in
> `worker/provisioners/__init__.py` — `mock` and `vsphere_api` (supported), and
> `proxmox_api` and `hyperv` (experimental: the API, the workers and the installer
> preflight refuse them unless `EXPERIMENTAL_PROVISIONERS=true` /
> `tn_experimental_provisioners: true`). The Terraform backends were removed (ADR 0009).
> Bare `vsphere` and `proxmox` are **not** keys: creating a range answers 409.

---

## Contents

- [What runs where](#what-runs-where)
- [Kubernetes Helm Deployment](#kubernetes-helm-deployment)
- [Keycloak Realm Configuration](#keycloak-realm-configuration)
- [Metrics exposure](#metrics-exposure)
- [Backups](#backups)
- [Health check endpoints](#health-check-endpoints)
- [API settings worth knowing](#api-settings-worth-knowing)
- [Troubleshooting](#troubleshooting)

---

## What runs where

One Ubuntu 24.04 VM runs the whole platform as the Docker Compose project
`infra/platform/docker/compose.prod.yml`, deployed by digest from a signed release
([release.md](release.md)); ranges are built on vSphere through vCenter by the
`vsphere_api` provisioner. Sizing, networks and accounts: [guide 01](guides/01-site-prerequisites.md).
The containers, networks, data paths and production settings are described in
[`install/README.md`](../install/README.md) ("Networks and exposure", "Production settings",
"OpenSearch", "Runtime hardening"). There is no supported Proxmox, Terraform or Ollama
deployment path: the Terraform backends were removed (ADR 0009), and AI is any
OpenAI-compatible endpoint, normally LiteLLM in front of vLLM on a GPU node
([guide 02 Appendix C](guides/02-install-guide.md#appendix-c--add-a-gpu-ai-node)).

---

## Kubernetes Helm Deployment

The Helm chart and its documentation live with the chart under `infra/k8s/`. Follow
`infra/k8s/README.md` for prerequisites, the required values (secrets, Keycloak URL,
image digests), install, upgrade and the smoke install CI runs. This guide no longer
carries a copy of the values; the chart's README is the only reference, so the two
cannot drift.

---

## Keycloak Realm Configuration

The realm is not built by hand. `infra/keycloak/realm-truenorth.json` is imported on a
fresh install, and `install/playbooks/60-keycloak.yml` (`roles/tn_keycloak`) enforces the
security settings on every run; `tests/contracts/test_keycloak_hardening.py` keeps the two
equal. The settings are tabulated in `install/README.md` ("Identity hardening").

| Client | Type | Sign-in flow |
|---|---|---|
| `truenorth-web` | public | Authorization code + PKCE (S256). This is how people sign in. |
| `truenorth-cli` | public | OAuth device authorization grant |
| `truenorth-api` | confidential | No user flow; service account only. Tokens carry it as audience (`KEYCLOAK_AUDIENCE`). |
| `truenorth-smoke` | confidential, **disabled** | Password grant, for `95-smoke-test` only: `roles/tn_smoke` enables it for one token request and disables it again |

The password grant (direct access grants) is off on `truenorth-web`, `truenorth-api` and
`truenorth-cli`. Access tokens last 5 minutes; browser sessions 30 minutes idle, 10 hours
maximum. Roles are `admin`, `instructor`, `student`, `observer`, `range_ops`; the
installer's `truenorth-identity` client scope adds the `groups` and AD identity claims
and the API audience (`roles/tn_keycloak/defaults/main.yml`, `tn_kc_protocol_mappers`).

An existing realm keeps its own redirect URIs and web origins on re-import; update the
`truenorth-web` client's by hand (`CHANGELOG.md`, upgrade notes). The Keycloak admin
console is 404 at the edge unless the client is in `management-cidrs.conf`.

---

## Metrics exposure

The installed stack runs Prometheus, Alertmanager, Grafana and the exporters itself
(`compose.prod.yml`, network `tn-monitoring`); the alert rules and what to do about each are
in [operations.md](operations.md#alert-rules). Flower is at `https://<tn-fqdn>/flower/` for
the management networks only (`infra/platform/nginx/snippets/management-cidrs.conf`).

The API exposes Prometheus metrics at `/metrics`. Scrape `/metrics` on the internal network (`api:8080/metrics`). The bundled
nginx configs (`control-plane/web/nginx.conf`, `infra/platform/nginx/conf.d/truenorth.conf`)
answer `404` for `/api/metrics` and `/api/v1/metrics`. Under Helm the ingress forwards the
whole `/api` prefix, so set `METRICS_SCRAPE_TOKEN` on the API there (or wherever the API is
reachable from outside the cluster): `/metrics` then requires `Authorization: Bearer
<token>`, and the Prometheus job needs a matching `authorization: { credentials_file: ... }`.
Request metrics are labelled by route template (`/schedule/feed/{token}.ics`), never the
raw path; requests that match no route are labelled `unmatched`.

---

## Backups

Nightly, encrypted secrets escrow, copied off the host, with a restore drill:
[guide 06 §3](guides/06-operations-runbook.md#3-backup-restore-and-the-restore-drill) and
[runbooks/backup-restore.md](runbooks/backup-restore.md).

---

## Health check endpoints

Through the edge (`https://<tn-fqdn>`): `GET /api/health` (liveness and dependencies),
`GET /api/health/ready` (database, Redis and OpenSearch; what the api container's healthcheck
and `95-smoke-test` use) and `GET /api/health/deep`. On the host, `docker compose ps` shows
every container's own healthcheck (guide 06, the `dc` helper).

---

## API settings worth knowing

The installer renders every setting from its inventory (`install/README.md`, "Production
settings"); production refuses to start with a missing or unsafe one and names it. A few
that operators ask about:

| Variable | Default | Required | Description |
|----------|---------|----------|-------------|
| `TRUSTED_PROXY_CIDRS` | `127.0.0.0/8,::1/128,172.16.0.0/12` | Prod | Peers whose `X-Forwarded-For` is believed (comma-separated CIDRs; `none` ignores the header). Set it to your reverse proxies' addresses. Auth-zone IP allowlists, the rate limiter and `NOISE_AGENT_CIDRS` all use the resulting address. The bundled nginx configs overwrite `X-Forwarded-For` with `$remote_addr`; a proxy in front of them must be added to `set_real_ip_from` in `infra/platform/nginx/nginx.conf`. |
| `NOISE_AGENT_CIDRS` | -- | Recommended | Management network(s) noise agents call from; `/noise/agent/*` answers 403 to anyone else. nginx does not filter this path itself: put an `allow`/`deny` `location ^~ /api/noise/agent/` in front if you want it refused at the edge too. |
| `METRICS_SCRAPE_TOKEN` | -- | Helm | When set, `/metrics` requires `Authorization: Bearer <token>`. |
| `SCHEDULER_FEED_BASE_URL` | -- | Prod | Public origin of calendar-feed URLs (`https://range.example`). Unset, the API uses the request's own `Host` and, with auth on, logs a warning at startup; `X-Forwarded-Host` is never used. |

The provisioner (`PROVISIONER_BACKEND`, installer `tn_provisioner_backend`) and every
`VSPHERE_*` setting are rendered from the installer's `tn_vsphere_*` variables
(`install/inventory/group_vars/all/main.yml`); the AI orchestrator's endpoint and models
from `tn_ai_*` ("AI (optional)" in `install/README.md`).

---

## Troubleshooting

Common failures and their fixes are in
[guide 06 §5](guides/06-operations-runbook.md#5-common-failures-and-their-fixes); runtime
incidents in [runbooks/incident.md](runbooks/incident.md). On the host, with the compose
project the installer started:

```bash
cd /srv/truenorth/app/infra/platform/docker
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production ps
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production logs --tail 100 api
docker compose -f compose.prod.yml --env-file /srv/truenorth/config/.env.production logs --tail 100 worker-provision

# Keycloak's realm is up (OIDC discovery; no credentials needed).
# Do not test with grant_type=password: the password grant is off on every user client.
curl -s "https://<tn-fqdn>/auth/realms/truenorth/.well-known/openid-configuration" | python3 -m json.tool
```

Never "fix" a migration failure with `alembic stamp`: the installer stops and asks instead
(`install/README.md`, "The migration hazard").