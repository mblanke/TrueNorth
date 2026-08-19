# Session resume — 2026-08-19

Read this after restarting tmux. Everything below is committed and pushed
(`origin/feat/aar-pdf-designer-xapi`).

## First command after a reboot

```bash
/opt/llm-stack/taz-status.sh      # what is actually running
/opt/llm-stack/to-fleet.sh        # bring the 4-model fleet up (default mode)
```

Nothing auto-starts the models by design: `glm52.service` is installed but **disabled**
so GLM never fights the fleet for GPUs. Fleet vLLM engines are `restart: unless-stopped`
and usually return on their own; `to-fleet.sh` is idempotent, so run it either way.

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

**Test baseline: 461 passed, 27 skipped.** The 27 skips need external services.

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
- **`--profile fleet up -d` also starts a container dcgm-exporter** that collides with
  the host exporter on `:9400` and aborts the whole run. `to-fleet.sh` starts the four
  engines by name instead.
