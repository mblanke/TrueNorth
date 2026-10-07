# Session coordination (TrueNorth)

Several Claude sessions work on this repo at once, each in its own Claude app worktree
(`.claude/worktrees/<name>`, `claude/*` branch). `.coord/coord.py` tracks who owns which
module. `coord` below means `python3 "$(git rev-parse --git-common-dir)/../.coord/coord.py"`;
it always runs the main checkout's copy, so every worktree shares one config and one state db.

1. **Claim before editing.** The SessionStart brief says whether this worktree holds a lease.
   If it does not, propose `coord claim <module>` to the user (the brief suggests a module from
   this branch's diff) and wait for a yes before editing. `coord modules` lists the 17 modules.
2. **Stay inside your lease.** Leases are strict: a PreToolUse hook blocks edits to another
   module's paths. Need a change elsewhere? Record it and carry on:
   `coord handoff add <target-module> "<change needed>" --why "<reason>"`.
   Never edit `.coord/handoff.md`, `.coord/modules.json` or `.coord/state.sqlite` by hand;
   `coord handoff done <id>` closes an item addressed to you.
3. **Serialized god-files lock on first edit** (models/schemas/main/seed/rbac/task_contracts,
   `routers/__init__.py`, worker tasks/tables/db_ops/contracts/celery_app, alembic,
   `docs/interfaces/`, `schema.d.ts`, `app.routes.ts`, the conftests, `.dod-*-baseline`,
   workflows). If another live session holds the lock you are blocked: wait for its next
   checkpoint or hand the change over. Keep such edits small and checkpoint soon.
4. **Checkpoints are the sanctioned exception to "commit only when asked".** At every Stop the
   hook commits `wip(<module>): ...` on this worktree's branch, staging only dirty files your
   module owns plus god-files you hold locks for. It never commits on `main`, `master` or
   `feat/aar-pdf-designer-xapi`, never pushes, refuses on a likely secret, and is skipped while
   the module-scoped gate fails. Do not make other commits unless the user asks.
5. **The module-scoped gate is not DoD.** It runs ruff breakage rules and the module's own
   tests (stage 4 adds `pytest --collect-only tests/integration`). `bash scripts/dod.sh` is the
   Definition of Done; run it before saying work is done.
6. Stages: 0 spec, 1 scaffolded, 2 functional, 3 tested, 4 integrated, 5 hardened (DoD green),
   6 release-ready. `coord gate <module>` runs the next gate; the user promotes with
   `coord module set <module> --stage N`.
