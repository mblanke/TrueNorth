# TAZ: contained coding worker

TAZ is the GPU server that runs a local coding model on narrow task cards. A local model is
never trusted. Its output counts as evidence only after independent checks pass. Claude
plans the work, writes the oracle tests, reviews and merges. This directory holds the parts
of that containment that live in the repository. The whole plan has seven layers: task
card, fork isolation, write-time hooks, self-check, CI guard, review and track record.

| Path | What it is | Layer |
|---|---|---|
| `hooks/write_gate.py` | PreToolUse hook, rings 1 to 6 | 3, write time |
| `hooks/dod_stop_gate.py` | Stop hook: no "done" without a green `dod.sh` on this exact state | 4, self-check |
| `hooks/policy.json` | default policy for both hooks and for `taz-guard` | 3 and 5 |
| `../../scripts/taz_guard.py` + `../../.github/workflows/taz-guard.yml` | CI check of a TAZ PR against its card | 5, CI |
| `bakeoff/` | model bakeoff replay kit (P2) | |
| `courses/` | course run kit: TAZ drafts the 37 unheld courses, Claude validates each | |

Tests: `tests/tools/test_taz_hooks.py`, `tests/tools/test_taz_guard.py` and
`tests/tools/test_check_course.py`.

## Courses

TAZ brings the 37 unheld course files in `content/courses/` up to the "technically proven"
bar: complete modules, a staff-only instructor pack, a `lab_range` on every module that
names a real range template (and optionally a scenario) with steps on hosts that exist in
it, and quizzes checked against the content. Seven courses stay held for cleared authors
and are never queued: C302, C305, C306, RMC C202, RMC C205, RMC C207 and RMC C211.

| Path | What it is |
|---|---|
| `courses/AGENTS-courses.md` | the rules the coder model follows for one course |
| `courses/queue.txt` | the 37 course files in run order, each with a suggested range template |
| `courses/FOREMAN.md` | the runbook for the Claude foreman session on TAZ |
| `courses/make_card.py` | prints the task card for one course: that file and its instructor pack only |
| `../../scripts/check_course.py` | the oracle; exit 0 only when every check passes |

The flow: the foreman clones main, gives the coder a card scoped to one course, and runs
`scripts/check_course.py` itself. A pass goes to Claude as a Remote Control message whose
first line is `TAZ course patch: <file>`, followed by `git format-patch` output; three
failed attempts send `TAZ course blocked: <file>` with the checker output. TAZ never
pushes. Claude applies the patch, re-checks and reviews it, sets
`readiness: technically_proven` and opens the PR. `readiness` never publishes anything:
a course reaches Students only after SME/Standards sign-off (`sme_signed`) and a course
release. The instructor pack lives in `content/courses/instructor/<file>`, which the
content loader does not read and the course ingest refuses (`audience: staff_only`).
`policy.json` protects the checker, the course schemas and this kit, and lets the coder
run the checker. It does not protect `content/courses/**`, so a course card can allow it.

## The task card

A GitHub issue labelled `taz-ready`. Only the owner or Claude can apply that label. The
issue holds exactly one fenced block tagged `taz-card`:

```taz-card
{"allowed_paths": ["control-plane/web/src/app/features/my-classes/**"],
 "oracle_tests": ["control-plane/web/src/app/features/my-classes/my-classes.component.spec.ts"],
 "diff_budget": 300,
 "base_ref": "taz-base/284"}
```

- **`allowed_paths`**: globs. `**` crosses directories; `*` and `?` stay within one path
  segment.
- **`oracle_tests`**: Claude's tests, committed on `base_ref` before TAZ starts. TAZ
  never edits them.
- **`diff_budget`**: the maximum of added plus deleted lines. The default is 300.

The runner writes a local copy for the hooks, adding `"repo_root": "/abs/path/to/clone"`,
and sets `TAZ_CARD` to that file. Keep the file outside the clone, owned by the runner
user. The PR body links the card with `taz-card: #284`.

## Installing the hooks on TAZ

1. Install the hooks from a reviewed commit into a directory that the user running the
   model **cannot write**. A model that can edit its own gate has no gate.
   ```bash
   sudo install -d -o root -g root -m 755 /opt/taz/hooks
   sudo install -o root -g root -m 644 tools/taz/hooks/{write_gate.py,dod_stop_gate.py,policy.json} /opt/taz/hooks/
   ```
2. Edit `/opt/taz/hooks/policy.json` for this deployment.
   - Add the bakeoff or production coder to `write_allowlist`. Use the name the backend
     reports, which is the served model in the transcript (for example `glm-4.6`), and
     not only the LiteLLM alias. The owner rule is **no Chinese models except GLM**.
   - Add the scratch directory, if the runner gives the model one, to `exempt_roots` as
     an absolute path, for example `/data/taz/scratch`.
3. Wire the hooks in the settings of the session that runs the model. Use the separate
   `CLAUDE_CONFIG_DIR` the runner passes (as in `bakeoff/README.md`). Where Claude Code
   supports it, put them in the managed settings file (on Linux
   `/etc/claude-code/managed-settings.json`) so project and user settings cannot remove
   them.
   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Write|Edit|MultiEdit|NotebookEdit|Bash",
           "hooks": [{"type": "command", "timeout": 30,
                      "command": "python3 /opt/taz/hooks/write_gate.py --policy /opt/taz/hooks/policy.json"}]
         }
       ],
       "Stop": [
         {"hooks": [{"type": "command", "timeout": 120,
                     "command": "python3 /opt/taz/hooks/dod_stop_gate.py"}]}
       ]
     }
   }
   ```
4. Turn off every other way to write files: no MCP servers that write files, and no
   other tools beyond the ones the hook matches.
5. To check the install, run `python3 -m pytest tests/tools/test_taz_hooks.py` from a
   clone. Then try one denied write in a throwaway session and confirm that the denial
   appears in the transcript and in the log.

Environment and configuration:

| Name | Effect |
|---|---|
| `--policy PATH` / `TAZ_GATE_POLICY` | Policy file. If neither is set, the gate uses `policy.json` beside the script. If no policy file is found, or the one found is broken, every call is refused. |
| `TAZ_CARD` | Task card JSON. Rings 4 and 5 use its paths. A card that is set but broken refuses every call. |
| `TAZ_GATE_LOG` / policy `log_path` | Optional JSON-lines decision log. |
| `TAZ_STOP_STATE` | Where the Stop hook keeps its remind-once state. The default is `~/.claude/hooks/.dod-stop-state`. |

## The rings

| Ring | Refuses | Applies to |
|---|---|---|
| 1 capability | a code write by a served model that is not on `write_allowlist` (case-insensitive substring); an unknown model behind `ANTHROPIC_BASE_URL` | code files |
| 2 whole-file | `Write` over a tracked file of more than 150 lines | code files |
| 3 amnesia | an edit to a file not Read since the last compaction | code files |
| 4 card | with `TAZ_CARD` set, any write outside the card's `repo_root` + `allowed_paths` | every file |
| 5 protected | the card's oracle tests, and policy `protected_paths`: `.dod-*baseline`, `.git/`, `.github/`, `.claude/`, the hooks, the guard, `dod.sh`, test and lint config, `alembic/versions/`, auth, rbac and tenancy, `provisioners/`, `result_transfer/`, `classes/onboarding.py`, `package.json`/`angular.json`, lockfiles | every file, every model |
| 6 bash | any Bash command that is not provably read-only or a test runner | every model |

"Code files" are the suffixes in `CODE_SUFFIXES`. Prose (`.md`, `.txt` and others) skips
rings 1 to 3 only, so a read-only model can still write a report. Rings 4 and 5 check both
the path as given and the path with symlinks resolved, and the write must pass for both.
Ring 5 matches case-insensitively, so `.GitHub/` on a case-insensitive filesystem does not
get through.

**Exempt roots.** The original gate matched `always_allow_globs` as substrings, so any
path containing `/tmp/` or `/scratchpad/` skipped every ring, including
`repo/src/tmp/x.py`. Now `exempt_roots` are absolute directories, compared as whole path
components after realpath, and a path inside any work dir is never exempt. A work dir is
the card's `repo_root`, the git repository of the target, or the git repository of the
session's cwd. Relative entries and `/` are ignored. The `.agent-patches/` escape hatch
still works without a card, because `.patch` files are prose.

**Fail closed.** If the gate meets stdin that is not JSON, a missing tool name, a
`tool_input` that is not an object, a missing file path or command, a broken policy or
card, bad arguments, or an internal exception, it denies.

## Ring 6: what it does and its limits

Ring 6 is defence in depth, **not a sandbox**. It parses the command roughly the way bash
would: quotes, escapes, line continuations, comments, here-documents, `;` `&&` `||` `|`
`&` and newlines, and subshell and brace groups. It then allows only:

- **Read-only commands**: `ls`, `cat`, `grep`, `head`, `wc`, `diff`, `jq` and similar
  (`READONLY_COMMANDS`, plus `bash.extra_readonly_commands` in the policy).
- **Restricted forms of other commands**:
  - read-only `git` subcommands, with no `-c`, `--output` or `-O`;
  - `sed` without `-i`, `-f`, or a `w`/`W`/`e` command;
  - `awk` without `>`, `|`, `system` or `getline`;
  - `find` without `-exec` or `-delete`;
  - `sort` without `-o`.
- **The test runners**:
  - `pytest` and `python -m pytest`, without options that write files or load plugins or
    config;
  - `ruff check` without `--fix`, and `ruff format --check`/`--diff`;
  - `npm test`, and `npm run` of the scripts in `bash.npm_scripts` (default `test` and
    `gen:api`);
  - `ng`/`npx ng` with `test`, `lint` or `build`;
  - the repo scripts in `bash.allowed_scripts` (default `scripts/dod.sh` and
    `scripts/export_openapi.py`), run with `bash` or `python`. Each must resolve to the
    repo's own copy: a `cd` earlier in the command, or a same-named script elsewhere,
    is refused. Ring 5 protects these scripts.
- **`python -c`**, when the code shows no write, exec or obfuscation pattern.

Output redirects are refused unless they go to `/dev/null` or to another file descriptor.
Variable assignments are refused unless they are in `bash.safe_env`. Command and process
substitution are refused everywhere, including in unquoted here-documents. A restricted
command that gets an argument containing `$` is refused, because its value cannot be
checked. Anything the parser does not understand is refused.

Known limits. Each of these is closed by another layer, not by ring 6.

- **The test runners execute code that the model wrote.** A test file, a Karma spec or
  anything `dod.sh` runs can open files for writing, delete them or start processes. Ring 6
  stops a model that reaches for the shell to edit files, which was the 2026-08-19 failure.
  It does not stop a model that deliberately writes code to escape. The container (layer
  2: a fresh clone, no secrets, restricted network) contains that. Ring 5 keeps the
  config that tests load (`conftest.py`, `pytest.ini`, `pyproject.toml`, `karma.conf.js`,
  `angular.json`, `package.json`) out of reach. The CI guard (layer 5) and review
  (layer 6) catch the result.
- **`python -c` is checked with a heuristic.** It is a list of dangerous names and
  patterns. A determined model can obfuscate past it, and the same answer applies.
- **The shell environment is trusted.** Aliases and functions from the user's profile, and
  the `PATH` directories, belong to the deployment. Keep the model user's profile and
  `PATH` free of writable directories. Commands run by path are limited to the test
  runners.
- **Only the hooked tools are seen.** An MCP server or a new built-in tool that writes
  files bypasses all six rings. Do not configure any.
- **`npx ng`** may download a package if `node_modules` is missing. Run `npm ci` in the
  runner before the session, not in it.
- **Hardlinks are not resolved by realpath**, so rings 4 and 5 judge a hardlink by its own
  path. Ring 6 refuses `ln` and every other way to create one, so the model cannot make one.

## The Stop hook

The Stop hook fires when code files (Python and Angular sources) differ from HEAD or new
untracked ones exist, and `scripts/dod.sh` has not passed on this exact state. It blocks
the stop once per session and state, with exit 2 and the list of changed files, then
stands down so the session can always end.

"This state" is the first line of `.dod-pass`, which `dod.sh` writes:
`<HEAD>:<sha256(git diff HEAD)[:16]>`. A test runs the block from `dod.sh` itself and
compares the result byte for byte. Known limit of that recipe: `git diff HEAD` does not
see untracked files, so editing a new file after a green run does not change the state.
The runner's own gate run before the PR (layer 4) covers this.

## taz-guard (CI)

`.github/workflows/taz-guard.yml` runs on `pull_request_target` for PRs from a fork whose
name contains `taz`, or from a `taz/*` branch. The workflow and `scripts/taz_guard.py`
come from the **base** branch. The PR head is fetched as git objects only and is never
checked out or run. With plain `pull_request`, a fork PR would run its own edited copy of
the workflow and the guard.

The card comes from the issue that the PR body links. That issue must carry the
`taz-ready` label and be written by a trusted author (`TAZ_CARD_AUTHORS`, default
`mblanke`). A card block in the PR body counts only if it equals the issue's card.
`--allow-inline-card` exists for local runs only, because a card that the PR author wrote
proves nothing.

The guard fails the PR on any of the following:

- **scope**: a path outside `allowed_paths`, or any protected path;
- **budget**: more changed lines than `diff_budget`, or any binary file;
- **oracle**: an oracle file that differs from `base_ref`;
- **suppression**: a new skip, xfail, noqa, `type: ignore`, eslint-disable, ts-ignore,
  or Jasmine `x`/`f` focus line;
- **baseline**: a raised `.dod-*baseline`;
- **lockfile**: any changed lockfile;
- **mode**: a symlink or submodule.

A lowered baseline passes, because that is the ratchet working.

Run it locally:

```bash
python scripts/taz_guard.py --base origin/main --head HEAD --card card.json
```

To make it binding, mark `taz-guard` as a required check on `main`. A PR that is not from
TAZ skips the job, which counts as passing.
