# Resume — 2026-10-09

Read this first after a restart. It describes the current state. History is in
`git log` and `docs/hardening/`. Product state, module by module: `docs/current-state.md`.
Releasing: `docs/release.md`.

## Where the code is

- **`main` on GitHub (`mblanke/TrueNorth`) is the line.** It is the default branch, CI
  runs on PRs into it, and releases are tags on it. Local `main` and `github/main` have
  diverged before, so check `git rev-list --left-right --count main...github/main` before
  branching "from main". `feat/aar-pdf-designer-xapi` is an old line, not the base.
- **All 17 modules are at stage 4. Release status: `v1.0.0-rc3` (`e015d27`, #117) is
  the latest signed pre-release and is installed on staging** (`tn-staging`, an upgrade
  from rc2). `v1.0.0` is not tagged yet. Changes: `CHANGELOG.md`. Known limitations and
  the staging record: `docs/release-notes/v1.0.0.md`. The section modules live under
  `control-plane/api/app/<module>/`: scheduler + capacity, range leases and range ops,
  lab sessions, network inventory, noise, greyspace, detections, scenario runs, course
  releases/publishing, LMS, wiki/support tickets/notifications. Everything up to #117
  is merged (R-series, final security review #112, installer v1 #111, Helm #108).
- **Open PRs** (2026-10-09): #118 (nightly backup wrapper on a new host; found on
  staging), #92 (session coordinator). #3–#51 predate the re-land on main. Check each
  against main before acting on it, and do not merge them as they are.

## The gate and the CI lanes

`bash scripts/dod.sh` is Definition of Done (CLAUDE.md). On a Mac you need a per-worktree
venv (`uv venv .venv --python 3.11`, then
`uv pip install --python .venv/bin/python -r requirements-test.txt "ruff==0.16.3"`).
Capture the exit code: piping into `tail` hides failures.

`ci.yml`, on PRs into main and on push to main:

| Job | What | Blocking |
|---|---|---|
| lint-python | ruff ratchet (`scripts/ruff-gate.sh`), `ruff format --check` report | ratchet yes, format no |
| test-python | `pytest tests/ --ignore=tests/integration` with PostgreSQL 16 + OpenSearch services; fails if any PostgreSQL test skipped | yes |
| mosa | MOSA ratchet, OpenAPI / task-contract / table-mirror drift | yes |
| supply-chain | pip-audit, npm audit (prod deps), repo SBOM | yes |
| lint-angular, build-angular | eslint, Karma (ChromeHeadless), generated API types match, prod build | yes |
| build-docker | five images built and Trivy-scanned (pinned v0.74.0) | fixable CRITICAL yes; HIGH reported |
| helm-lint, keycloak-realm | chart lint; realm imports into the pinned Keycloak | yes |
| integration | itest stack (`scripts/itest.sh`), `tests/integration`; zero passed = fail | yes |
| e2e | Playwright (lockfile, `npm ci`) on itest + keycloak + web | yes |
| load-smoke | k6, every scenario at 1 VU for 30 s (`tests/load/README.md`) | yes (since #107) |
| moodle, vsphere-sim, greyspace | disposable Moodle publish, vcsim provisioner, Greyspace T0 stack | yes |

Other workflows: `release.yml` (tag `v*`: images by digest, Trivy gate, SBOMs,
`release-manifest.json`), `lab.yml` (nightly live vSphere on a self-hosted runner),
`packer-build.yml` (manual), `terraform-plan.yml` (Proxmox PRs). `deploy-dev.yml` and
`deploy-prod.yml` were deleted on 2026-10-08 (they could not work; see
`docs/release.md`). **Nothing in CI deploys.** The installer (`install/`) deploys.

## How to release

Tag a green commit of main `vX.Y.Z` and push the tag. Then verify the release assets
(`SHA256SUMS`, the digests in `release-manifest.json`, the Trivy reports and SBOMs) as in
`docs/release.md`. A green gate, or a published release, is not approval to deploy.

## Box operations (Taz AI box; last verified 2026-08-19)

The rest of this file is about the GPU/LLM development box, not the product. It has not
been re-verified since 2026-08-19. NVIDIA was on **580.173.02** (kernel module, DKMS and
userspace), with no apt holds. If GPU behaviour looks wrong, compare
`/proc/driver/nvidia/version`, `modinfo nvidia` and `nvidia-smi -L`.

### First command after a reboot

```bash
/opt/llm-stack/taz-status.sh              # what is actually running
/opt/llm-stack/to-fleet.sh                # bring the 4-model fleet up (default mode)
docker start taz-dashboard-prometheus-1   # the dashboard's data source
```

**Assume nothing comes back on its own.** `restart: unless-stopped` does *not* restart a
container that was cleanly stopped before the reboot, so anything stopped on the way down
stays down. Verified the hard way on 2026-08-19: the four vLLM engines *and*
`taz-dashboard-prometheus-1` all stayed `Exited (0)` after the boot. A post-reboot restore
is always explicit. `to-fleet.sh` is idempotent, so run it either way.

`glm52.service` is installed but **disabled** by design, so GLM never fights the fleet
for GPUs.

**A dark dashboard usually means Prometheus, not a dead platform.** Every status tile is a
Prometheus query, so when Prometheus is down the whole board reads "offline" while the
services behind it are perfectly healthy. Check the platform directly before believing it:

```bash
curl -s localhost:4200/api/health          # {"status":"ok","db":true,"redis":true}
docker ps --filter health=unhealthy        # empty is good
curl -s localhost:9090/api/v1/targets | head -c 200
```

Note the API is **not** published on the LAN (it maps `8080/tcp -> 127.0.0.1:8081` and is
reached via nginx at `:4200/api/`). A probe of `:8000` fails by design, not by fault.

### The two modes

| You want | Run | Then |
|---|---|---|
| Claude orchestrating several local models | `/opt/llm-stack/to-fleet.sh` | 4 models concurrent |
| Claude Code running *on* GLM-5.2 | `/opt/llm-stack/to-glm.sh` | `bash ~/gml5.txt` |

They are mutually exclusive — GLM-5.2 needs both H200s. `~/gml5.txt` refuses with a
clear message if GLM is not up, rather than silently degrading to a weaker model.

Scripts in `/opt/llm-stack/`: `to-fleet.sh`, `to-glm.sh`, `taz-status.sh`,
`apply-deferred.sh` (already run), `_helpers.sh`.

## Still open (product)

1. **v1.0.0 known limitations** (`docs/release-notes/v1.0.0.md`): LTI refuses staff
   emails (no deep linking for them), pre-upgrade telemetry indices, `vsphere_api`
   ignores per-tenant credentials, `PLATFORM_TENANT_ID` on multi-tenant installs,
   OpenSearch backups need `OPENSEARCH_SNAPSHOT_REPO`, LTI needs HTTPS.
   (Installing by digest is done: #109, #111.)
2. **Live vSphere evidence** needs the self-hosted lab runner and its credentials
   (`lab.yml`, `docs/runbooks/lab-runner.md`). The Taz box cannot reach that LAN.
   Staging has no vCenter (simulated, read-only).
3. **Content is the real bottleneck:** about 2,600 build hours, about 57% of them gated
   on cleared humans and purchased courseware. The four Red Analyst crosswalk rows still
   carry `DCWF-TODO`.

## Gotchas that cost time — do not relearn them

- **LiteLLM's config is a bind mount.** `docker compose up -d litellm` does NOT reload
  it. Use `docker restart llm-stack-litellm-1`.
- **A busy GLM looks dead.** At `--parallel 1` it stops answering `/health` while
  processing a large prompt. `to-fleet.sh` refuses unless `FORCE=1`.
- **`pkill -f <pattern>` matches your own shell** when the pattern is in its command
  line. Use `safe_kill()` from `_helpers.sh`.
- **Reasoning models return empty `content` at low `max_tokens`** (`agent`, `glm-5.2`) —
  the reasoning channel eats the budget. Use >=200 when smoke-testing.
- **`--profile fleet up -d` also starts a container dcgm-exporter**, which historically
  aborted the whole run with a `:9400` bind collision. `to-fleet.sh` starts the four
  engines by name instead. Correction (2026-08-19): the earlier note blamed a *host-side*
  exporter for holding `:9400`. There is no host dcgm-exporter on this box — no binary,
  no systemd unit, no `*dcgm*` file outside Docker images. The collision was this same
  container being started twice, so the by-name workaround is now belt-and-braces rather
  than load-bearing.
- **`docker start` can silently resurrect a container without its port mapping.** The
  dcgm-exporter came back `Up` and logged `HTTP server started`, but `docker port` was
  empty and Prometheus's `dcgm` target sat at `connection refused`. A container created
  during a failed networking attempt keeps that broken config; only
  `docker compose up -d --force-recreate dcgm-exporter` restored `9400:9400`. If a target
  is refused while the process looks healthy, check `docker port` before the process.
- **CI can go red with no code change** (since 2026-10-04). The `supply-chain` job's
  pip-audit and npm audit are blocking, so a newly published advisory against an
  unchanged pin fails the build. The `supply-chain` artifact holds `pip-audit.json` and
  `npm-audit.json` naming the package. Fix by upgrading or replacing the package; an
  ignore needs its own ADR (ADR 0003).
