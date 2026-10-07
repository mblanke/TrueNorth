# TrueNorth session coordinator (`.coord/`)

Lets several Claude sessions (each in its own Claude app worktree) work on TrueNorth without
editing each other's modules, colliding on god-files, or committing at random times.
One file, `coord.py`, stdlib only, Python 3.9+ (runs on macOS `/usr/bin/python3`). No daemon.

| File | Tracked | What |
|---|---|---|
| `coord.py` | yes | CLI + hook entry points |
| `modules.json` | yes | static config: 17 modules and their paths, serialized/shared paths, stage gates |
| `CLAUDE.coord.md` | yes | the rules every session reads (included from `CLAUDE.md`) |
| `state.sqlite` | **no** | leases, locks, stages/blockers/notes, handoff items, scan cache, events |
| `dashboard.html` | **no** | generated status page |

All worktrees resolve the **main checkout** through `git rev-parse --git-common-dir`, so they
share one `modules.json` and one `state.sqlite` in `<main>/.coord/`. The main checkout never
goes dirty: state is gitignored and nothing rewrites `modules.json`.

## Activate

The hooks in `.claude/settings.json` run `<main checkout>/.coord/coord.py`, and do nothing
(exit 0) when that file is absent. So:

1. Merge this branch to `main`, then bring the main checkout up to date
   (`cd ~/Documents/Projects/TrueNorth && git pull` on `main`).
2. Worktrees created from that `main` get the hooks automatically. An existing worktree gets
   them once its branch contains `.claude/settings.json` (merge or rebase `main`); until then it
   is invisible to the coordinator except through `coord scan`.
3. Optional shell alias:
   ```bash
   coord() { python3 "$(git rev-parse --git-common-dir)/../.coord/coord.py" "$@"; }
   ```
4. `coord init` creates the state db and checks the install; it writes no tracked file.

## Daily use

```bash
coord next                       # unleased, unblocked modules by priority
coord claim greyspace            # lease the module to THIS worktree (refused on main / protected branches)
coord status                     # leases, locks, modules, open handoffs, in-flight work
coord scan                       # every worktree + local branch diff vs github/main (else main), by module
coord dashboard                  # writes .coord/dashboard.html in the main checkout; open it in a browser
coord handoff add lms "expose enrolment count on /courses" --why "greyspace needs it"
coord handoff list               # --all includes closed items; --module lms filters
coord handoff done 3 --note "landed in abc123"
coord gate greyspace             # run the next stage's gate in this worktree
coord module set greyspace --stage 5 --note "dod green"
coord release                    # end this worktree's lease and drop its locks
```

A new session in an unleased worktree gets a SessionStart brief that maps the branch's changes
(committed vs base + uncommitted) to modules and tells Claude to propose `coord claim <id>`.

## Hooks (`.claude/settings.json`)

Each hook is `f="$(git rev-parse --git-common-dir 2>/dev/null)/../.coord/coord.py"; [ -f "$f" ] || exit 0; python3 "$f" <cmd>`
so it is a no-op outside this repo and in checkouts that predate the coordinator. The guard
keeps Python's exit status, so exit 2 still blocks; the other two end in `|| true`.

| Event | Command | Effect |
|---|---|---|
| `PreToolUse` Edit/Write/MultiEdit/NotebookEdit | `guard` | exit 2 + reason when the path is another module's (strict leases), belongs to a module another live session holds, sits inside another leased worktree, or is a serialized path locked by another live session. First edit of a serialized path takes the lock. A coordinator crash allows the edit. |
| `SessionStart` | `session-start` | prints the brief (lease, owned paths, gate, blockers, handoff items for you, other leases, foreign locks) |
| `Stop` | `checkpoint --quiet` | module-scoped gate + local `wip(<module>)` commit; reports through `systemMessage`, never blocks Claude from stopping |

### Rules the guard enforces

Order: path outside this worktree (allowed unless inside another live leased worktree) ->
serialized path (lock) -> shared path (allowed) -> owning module (most specific pattern wins).

- **Strict leases**: a leased worktree may edit only its module's paths, shared paths
  (`docs/**` except `docs/interfaces/`, `RESUME.md`, `CHANGELOG.md`) and unowned files
  (agent config, `CLAUDE.md`, ...). An unleased worktree may edit anything not leased by a live
  session; those edits are logged.
- **Serialized paths** (`api/app/{models,schemas,main,seed,task_contracts,rbac}.py`,
  `routers/__init__.py`, `worker/{tasks,tables,db_ops,contracts,celery_app}.py`, alembic,
  `docs/interfaces/**`, `core/api/schema.d.ts`, `app.routes.ts`, both conftests,
  `.dod-*-baseline`, `.github/workflows/**`): any session may edit, one at a time. The lock is
  keyed by the pattern (all of alembic is one lock), so two sessions cannot both add migrations.
  Released when the holder's checkpoint leaves no dirty file under it, on `coord release`, or
  after 90 minutes without any hook activity from the holder.
- **Stale**: a lease or lock whose worktree has had no hook activity for 90 minutes stops
  blocking; `coord claim` takes it over.

## Checkpoints

On every Stop, in a leased worktree:

1. refuse on `main`, `master`, `feat/aar-pdf-designer-xapi` or a detached HEAD;
2. pick dirty files owned by the module plus serialized files under locks this worktree holds
   (never `git add -A`; foreign, shared and unowned files stay uncommitted and are reported);
3. refuse if any of them matches `secret_regex`;
4. if the module is at stage >= 3, run the **module-scoped** gate of `min(stage, 4)`:
   - `ruff check --select F821,F811,E9` on the module's Python files,
   - `pytest` on the module's own test files (not `tests/integration`, `e2e`, `load`; skipped if none),
   - stage 4 adds `pytest --collect-only tests/integration` (collects, never starts stacks);
   `{py}` is `<worktree>/.venv/bin/python`, else the main checkout's `.venv` if it runs here,
   else `python3`;
5. `git commit -- <those paths>` (other staged work is left alone). Never pushes.

The module gate is a fast sanity check, **not** the Definition of Done. `bash scripts/dod.sh`
(stages 5 and 6) is DoD. The ruff step uses only the breakage rules because the repo carries
ratcheted debt (`.dod-ruff-baseline`) in several modules; the ratchet is DoD's job, and a full
`ruff check` per module would refuse every checkpoint in those modules.

`coord checkpoint` (no `--quiet`) runs the same thing by hand; `--force` commits through a
failing gate; `-m` sets the summary.

## Interaction with other hooks

Claude Code merges hooks from `~/.claude/settings.json`, `.claude/settings.json` and
`.claude/settings.local.json` and runs all matching hooks for an event in parallel.

- **Write gate** (`~/.claude/hooks/model-write-gate.py`, PreToolUse, where installed): independent
  of `guard`; either one exiting 2 blocks the edit. Neither depends on the other's order.
- **DoD marker Stop hook** (`.dod-pass`, where installed): it compares `.dod-pass` with
  `HEAD` + the diff against `HEAD`. A checkpoint commit changes both, so a DoD pass recorded
  before the checkpoint no longer matches afterwards. Run `scripts/dod.sh` after the last
  checkpoint you care about. The two Stop hooks run concurrently, so the marker check may see
  the tree just before or just after the commit.
- **block-push** (`~/.claude/hooks/block-push.sh`): unaffected; the coordinator never pushes.
- On this Mac (2026-10-07) `~/.claude/settings.json` and the main checkout's
  `.claude/settings.local.json` define no hooks, so only the coordinator's run here.
  `settings.local.json` is untouched.

## Tests

`tests/coord/test_coord.py` builds a temporary repo with linked worktrees and runs the script
against it with `COORD_ROOT` (plus `COORD_NO_BG=1` to keep dashboard regeneration inline).
It covers path matching on the real `modules.json`, strict blocking, locks, stale takeover,
checkpoint staging and refusals, secret scan, gate, scan, handoff, dashboard, the hook wrapper's
exit code, and running under `/usr/bin/python3`.

## Rollback

Delete the three hook entries from `.claude/settings.json` (or the file), drop the
`@.coord/CLAUDE.coord.md` line from `CLAUDE.md`, remove `.coord/`. Checkpoint commits are
ordinary commits; `git reset --soft HEAD~1` undoes the last one.

## Known limits

- Leases are keyed by worktree directory, not Claude session id: two sessions in one worktree
  share a lease. One module per worktree; cross-module work goes through handoff or a re-claim.
- Ownership is path-based. Files outside any module (agent config, `CLAUDE.md`, `SKILLS/`) are
  unrestricted; catch-alls (`control-plane/api/**`, `tests/**` -> platform-core,
  `control-plane/web/**` -> web-shell) own anything not claimed by a narrower pattern.
- Edits made through Bash (sed, redirects) bypass the PreToolUse guard; checkpoints still only
  commit owned files.
- `coord scan` over ~55 worktrees and ~170 branches takes about 20 s; hooks regenerate the
  dashboard in a detached background process from a cached scan.
- `usage` depends on transcript field names that can change between Claude Code versions.
