# TrueNorth on Kubernetes (Helm)

`helm/truenorth-range` installs the application: api, three Celery worker pools, Celery
beat, the AI orchestrator and the web app, plus a pre-install/pre-upgrade migration Job.
It does **not** run PostgreSQL, Redis, MinIO, OpenSearch or Keycloak. Bring those (a
managed service or an in-cluster operator) and point `config.*` at them.

CI proves the chart on every PR: `.github/workflows/helm-kind.yml` installs it into kind
with production settings and runs `kind/smoke.sh` (migrations, rollouts, `/health/ready`,
worker pings, `helm test`).

## Install

1. **Pin the images to a release.** Each release publishes `release-manifest.json`
   (docs/release.md). Turn it into values, checking it against the release's
   `SHA256SUMS` first:

   ```bash
   V=v1.2.3
   gh release download "$V" -R mblanke/TrueNorth -p release-manifest.json -p SHA256SUMS
   python3 infra/k8s/scripts/values_from_manifest.py release-manifest.json \
       --sums SHA256SUMS > "images-$V.yaml"
   ```

   This sets `images.<service>.repository` and `.digest` (digest wins over tag) and
   `version` (`TN_VERSION`). Install the chart from the release's `git_sha`, so the
   templates match the images.

2. **Secrets.** Every one is required; a render without it fails and names it. Known
   defaults (`changeme`, `minioadmin`, ...) and keys under 32 characters are refused.
   Either set `secrets.*` (quote every value: a hex string like `1234e567` is otherwise
   read as a number and refused), or create a Secret yourself and set
   `secrets.existingSecret` to its name. Its keys:

   | Key | Notes |
   |---|---|
   | `DATABASE_PASSWORD`, `REDIS_PASSWORD` | URL-safe (`openssl rand -hex 32`): they go into `DATABASE_URL` / `REDIS_URL` |
   | `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | |
   | `CSRF_SECRET`, `TN_SECRETS_KEY` | >= 32 chars. `TN_SECRETS_KEY` seals stored credentials: back it up with the database; rotate as `new,old` |
   | `AI_SERVICE_TOKEN` | >= 32 chars; api, workers and the AI orchestrator share it |
   | `METRICS_SCRAPE_TOKEN` | Prometheus sends it as a bearer token to the api's `/metrics` |
   | `OPENSEARCH_USER`, `OPENSEARCH_PASS` | empty only with `opensearch.securityDisabled` (lab only) |
   | optional | `KEYCLOAK_ADMIN_PASSWORD`, `KEYCLOAK_CLIENT_SECRET`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`; `FLOWER_BASIC_AUTH` (`user:pass`) with `flower.enabled` |

3. **Required settings** (`my-values.yaml`): `ingress.host`, `config.database.host`,
   `config.redis.host`, `config.minio.endpoint`, `opensearch.url` (https),
   `config.keycloak.url` and `.audience`, and `config.trustedProxyCidrs` (the ingress
   controller's pod CIDR: the API believes `X-Forwarded-For` only from there). Anything
   else compose.prod.yml sets (vSphere, LRS, LDAP, registration) goes in
   `config.extraEnv` / `secrets.extraEnv`.

4. ```bash
   helm upgrade --install tn infra/k8s/helm/truenorth-range -n truenorth --create-namespace \
       -f my-values.yaml -f "images-$V.yaml" --wait --timeout 15m
   helm test tn -n truenorth
   ```

## What the chart does

- **Migrations** run once per install/upgrade in the `<release>-migrate` Job (a Helm hook,
  before anything rolls), never in the api pods. A failed migration fails the
  `helm upgrade`, and the old pods keep serving. The Job is kept for a day for its log.
- **Security context** everywhere: non-root (uid 10001; web 101), read-only root
  filesystem with emptyDir `/tmp` and `$HOME`, no privilege escalation, all
  capabilities dropped, seccomp `RuntimeDefault`, no service-account token.
- **Probes**: api startup and liveness on `/health/live`, readiness on `/health/ready`
  (database, Redis, OpenSearch). Workers: `celery inspect ping -d <node>@$HOSTNAME`,
  where `<node>` is `provision`, `scenario` or `telemetry` (their `-n` names). The AI
  orchestrator's only public route is `/health`.
- **Exposure**: the Ingress publishes `/` (web), `/api` (rewritten to `/`, as the compose
  edge does) and `/ws`, and `/auth` when `ingress.keycloak` names an in-namespace
  Keycloak. The AI orchestrator and Flower are never published. NetworkPolicies admit
  the api and web from the ingress controller's namespace and the release's own pods,
  the AI orchestrator from the api and workers only, and nothing to workers, beat, the
  migration Job or Flower. Egress is open unless `networkPolicy.egress.enabled`, then
  limited to DNS, the release and `networkPolicy.egress.to`.
- **Beat** is one replica with the `Recreate` strategy: two would double every periodic
  task. **Flower** is off; when on, reach it with `kubectl port-forward`.
- The **scenario-engine** image is not a service: an init container copies it into the
  api pod's `SCENARIO_ENGINE_DIR`.

## Known gaps

- compose.prod.yml mounts `content/catalogue` and `content/mitre` into the api and
  workers; no image contains them, so the software catalogue and ATT&CK id checks are
  empty under Helm until the images ship that content.
- The ingress is written for ingress-nginx (`rewrite-target`, `use-regex`). The compose
  edge also hides `/api/docs`, `/api/metrics` and `/api/health/deep`; here the api's own
  controls apply (docs off in production, `/metrics` needs the token).
