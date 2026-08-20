# Session resume — 2026-08-19

Read this after restarting tmux. Everything below is committed and pushed
(`origin/feat/aar-pdf-designer-xapi`).

## The box was rebooted at the end of this session

Driver episode is closed: NVIDIA is back on **580.173.02** across kernel module,
DKMS and userspace, with **no apt holds** (unattended-upgrades can resume normally).
DKMS is built for kernel 6.8.0-138, which is what boots.

If anything GPU-related looks wrong after the reboot, check these three agree:
```bash
grep -oE '[0-9]+\.[0-9]+\.[0-9]+' /proc/driver/nvidia/version | head -1   # loaded
modinfo nvidia | awk '/^version:/{print $2}'                              # on disk
ls /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.*.*.* | sed 's/.*so\.//'      # userspace
nvidia-smi -L
```

## First command after a reboot

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

## The two modes

| You want | Run | Then |
|---|---|---|
| Claude orchestrating several local models | `/opt/llm-stack/to-fleet.sh` | 4 models concurrent |
| Claude Code running *on* GLM-5.2 | `/opt/llm-stack/to-glm.sh` | `bash ~/gml5.txt` |

They are mutually exclusive — GLM-5.2 needs both H200s. `~/gml5.txt` refuses with a
clear message if GLM is not up, rather than silently degrading to a weaker model.

Scripts in `/opt/llm-stack/`: `to-fleet.sh`, `to-glm.sh`, `taz-status.sh`,
`apply-deferred.sh` (already run), `_helpers.sh`.

## What was done this session

**Security**
- Cross-tenant IDOR swept across all routers (~46 lookups). New `app/tenancy.py`
  (`get_owned` / `owned_or_404`), plus a static guard test so it cannot regress.
- `scheduling.py` had **no authentication at all** — all ten `/schedule` endpoints were
  open. Auth now enforced at router level.
- Data services no longer published on the LAN. Before: `GET /ranges` and `/tenants`
  returned 200 from `133.1.14.240` with no token (dev runs `AUTH_DISABLED=true`), and
  OpenSearch listed its indices. Now `9200`/`8081`/`6000` are loopback-only; `4200`
  stays published because nginx proxies `/api/` internally.
- Live LiteLLM master key removed from `.claude/settings.local.json` (never in git).

**AI environment**
- NVIDIA driver mismatch repaired; `nvidia-persistenced` restarted (dead since the
  2026-07-25 unattended upgrade) and pinned to boot.
- GLM-5.2 now has an API key and a systemd unit with `Restart=on-failure`.
- Codex was completely broken (`wire_api = "chat"` removed in codex-cli 0.144.1) and is
  now wired to Claude Code over MCP, running `coder-fast` — a different model from GLM.
- Fleet verified: 4 models answering concurrently in 2s total wall-clock.

**Repo**
- `make migrate` pointed at a stale 2-revision alembic tree; now targets the live
  12-revision tree under `control-plane/api/`.
- Removed duplicated dead trees (`scenario-engine/{injectors,validators,scoring}`).
- README CI/coverage badges were unbacked; replaced with the real number.
- `pyproject.toml` deps reconciled with `requirements.txt`; `httpx` pins aligned so a
  combined install resolves.
- QSP crosswalk: NICE mappings reconciled, and a false `component_version: v2.1.0` claim
  corrected to `SP800-181r1` (the ids in use are SP 800-181 rev 1).

**Test baseline: 603 passed, 5 xfailed, 0 skipped.**

The old "27 skipped — needs external services" line was wrong, and the wrongness was the
point: the services were up the whole time. The integration suite was gated on an
`INTEGRATION_TEST=1` flag nobody set, and behind that gate it had drifted completely off
the API contract — posting to `/telemetry/events` (the route is
`/telemetry/{range_id}/events`), creating ranges with a null `template_id` the schema
requires, and polling for a range state `provisioned` that is not in `RangeState`. It is
now gated on whether the API actually answers, so drift surfaces the day it appears.

The 5 xfails are real gaps, named in the tests, not hidden:

- 4 × no scenario-level execution API (`POST /scenarios/execute` and the execution
  results/timeline endpoints do not exist; scenarios run via `POST /exercises/{id}/start`).
- 1 × **snapshot restore cannot work on any backend.** `worker/tasks.py:912` calls
  `provisioner.restore()`, and no provisioner implements it — mock, vsphere, hyperv,
  proxmox and terraform all define `snapshot()` and none define `restore()`. The
  retrying task also stamps `failed` over whatever terminal state the range had already
  reached, so a failed restore can clobber a successful destroy.

## Still open

1. **Content is the real bottleneck** — ~2,600 build hours, ~57% gated on cleared humans
   and purchased courseware. No tooling changes that.
2. **DCWF codes** for the four Red Analyst rows (only a NICE mapping was available;
   rows carry a `DCWF-TODO` marker and a test asserts it stays).
3. **NICE v2.1.0 re-map** if the programme must cite that version — no authoritative
   components file was reachable, and inventing ids was not acceptable.
4. `scenario-engine/{runner,template_engine}` have zero importers and no packaged
   counterpart — possibly dead, not safe to delete blind.
5. vSphere work is unreachable from this box (separate LAN).

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
