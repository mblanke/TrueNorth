# TAZ model bakeoff: replay kit

Measures how well a local model does real TrueNorth work. Each task replays a ticket that
is already merged: the model gets the repository at the commit **before** the fix and a
problem statement, never the fix or the tests that judged it. Afterwards the hidden oracle
tests from the merged commit are written over its tree and run.

Part of the TAZ plan (P2, bakeoff). A model's output counts as evidence only when the
independent checks pass; its own account of the work does not count.

| File | What it is |
|---|---|
| `tasks.yaml` | 8 replay tasks (3 backend, 2 Angular, 1 test-only, 1 docs, 1 feature slice) |
| `runner.py` | one model alias × one task → `result.json` |
| `score.py` | all `result.json` under a dir → markdown leaderboard |
| `AGENTS-taz.md` | rules appended to every prompt |
| `oracles/` | kit-side oracle helpers (the test-only task's pinned clock) |

Tests: `tests/tools/test_taz_bakeoff.py` (a fake `claude`; no model is run).

## The tasks

| id | from | kind | why it is in the set |
|---|---|---|---|
| `invite-tenant-scope` | #291 | backend, hard | the one tenancy case: a well-bounded leak with a clear oracle |
| `rc-release-order` | #225 | backend | pure logic + an Ansible expression the oracle evaluates for real |
| `metrics-histogram` | #154 | backend | diagnosis from a production symptom, three linked faults |
| `ticket-triage-order` | #215 | Angular | small, existing component, Karma spec |
| `schedule-live-session` | #215 | Angular | template + util function, two Karma specs |
| `noise-tamper-flake` | #158 | test-only | a flaky test; judged with the clock pinned to the failing window |
| `docs-claims-reconcile` | #157 | docs | judged by the grep-style contract test that PR added |
| `arc2-status-endpoint` | #123 | feature | API + tests + OpenAPI and `schema.d.ts` regeneration |

Auth, provisioning and the other security-sensitive changes are left out of this first set.
Every task was checked both ways: the merged fix applied by a fake `claude` scores PASS,
and an empty diff that claims "Done" scores FALSE_DONE.

## Running it on TAZ

One-time setup, as an unprivileged user with **no GitHub push credentials** (no `gh auth`,
no SSH key, no credential helper):

```bash
# Work dirs live on /data, never /tmp: other jobs can read and race there, and a reboot
# wipes the evidence. The runner refuses /tmp, /private/tmp and /var/tmp.
mkdir -p /data/bakeoff/{work,claude-config}

# Test environment for the oracles (same file CI installs).
git clone https://github.com/mblanke/TrueNorth.git /data/bakeoff/kit
uv venv /data/bakeoff/venv --python 3.11
uv pip install --python /data/bakeoff/venv/bin/python -r /data/bakeoff/kit/requirements-test.txt
# The two Angular tasks also need Node 22 + npm and Chrome/Chromium: export CHROME_BIN.
```

A **separate `CLAUDE_CONFIG_DIR`** (`/data/bakeoff/claude-config`) holds the session's
settings. Never point it at `~/.claude`; the runner refuses that. In it:

- `settings.json` registers the write-gate hook (`model-write-gate.py`) as a `PreToolUse`
  hook, as on the foreman.
- The hook's `gate-policy.json` allowlists the bakeoff models for writes (ring 1). The hook
  reads the **served** model from the transcript, so list the names vLLM reports
  (e.g. `MiniMax-M2`, `GLM-4.6`, `Qwen3-Coder-Next`), not only the LiteLLM aliases. A
  read-only model left off the list shows up as gate denials and a FAIL, which is the
  correct result.
- Rings 2 and 3 stay on. A model that tries a whole-file rewrite is refused, and the
  refusal is counted in `result.json` (`gate_denials`).

Run one task against one alias:

```bash
export TAZ_BAKEOFF_TOKEN=...          # LiteLLM key; sent only as ANTHROPIC_AUTH_TOKEN
python /data/bakeoff/kit/tools/taz/bakeoff/runner.py \
  --task rc-release-order --model-alias taz-minimax \
  --base-url http://127.0.0.1:4000 \
  --work-dir /data/bakeoff/work --claude-config-dir /data/bakeoff/claude-config \
  --python /data/bakeoff/venv/bin/python --max-turns 80
```

Every task against every alias, one at a time (a single coder is served at a time; switch
the vLLM model between aliases), then the leaderboard:

```bash
for alias in taz-minimax taz-glm taz-qwen-next; do
  for t in $(python -c "import yaml;print(' '.join(t['id'] for t in yaml.safe_load(open('/data/bakeoff/kit/tools/taz/bakeoff/tasks.yaml'))['tasks']))"); do
    python /data/bakeoff/kit/tools/taz/bakeoff/runner.py --task "$t" --model-alias "$alias" \
      --base-url http://127.0.0.1:4000 --work-dir /data/bakeoff/work \
      --claude-config-dir /data/bakeoff/claude-config --python /data/bakeoff/venv/bin/python
  done
done
python /data/bakeoff/kit/tools/taz/bakeoff/score.py /data/bakeoff/work --out /data/bakeoff/leaderboard.md
```

Repeat important runs; one pass per task is a sample, not a measurement. Results go into
`docs/taz-model-recommendation.md` with the leaderboard attached.

## What one run does

1. A fresh `git clone` (GitHub by default, `--repo-url` to override), `git checkout
   base_commit`, the remote removed. Every other ref is then deleted and the clone is pruned,
   so it holds no commit after the base: the fix cannot be read out of git history.
2. The task's `setup_cmd` (`npm ci` for the Angular tasks).
3. `claude -p "<prompt + allowed paths + environment + AGENTS-taz.md>"` with
   `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` from the arguments, the separate config dir,
   `--max-turns`, `--permission-mode acceptEdits`, and the task's time budget as a hard
   timeout. GitHub tokens, `ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN` and SSH agent
   variables are removed from its environment. The transcript is `transcript.jsonl`
   (stream-json).
4. `model.diff` against the base (new files included, caches and `node_modules` ignored),
   and the scope check against `allowed_paths`.
5. Only now is `fix_commit` fetched. Its oracle files are written over the tree and each
   `oracle_cmd` runs from the repo root; every one must exit 0.
6. `result.json` in the run dir.

### Verdicts

| verdict | meaning |
|---|---|
| `PASS` | oracle green and every changed path allowed |
| `FAIL` | oracle red, and the model did not claim success (e.g. it reported `BLOCKED:`) |
| `SCOPE_VIOLATION` | oracle green but the diff touched a path outside `allowed_paths` |
| `FALSE_DONE` | the final report claims done/fixed/passing and the oracle is red: an automatic fail, counted separately |

`result.json` also records the oracle commands with exit codes and output tails, diff stats,
out-of-scope paths, oracle files the model edited, wall time, turns, tokens (when the CLI
reports them), write-gate denials, permission denials, and any Bash command that reached
for GitHub or upstream history (`history_peeks`).

## Security notes

- **No push credentials** on the account that runs the bakeoff. The runner also strips
  `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK` and the askpass variables from the session,
  and the clone has no remote.
- **Work dirs outside `/tmp`**: on `/data`, owned by the bakeoff user, mode 700.
- **Separate `CLAUDE_CONFIG_DIR`** with the write-gate hook on and the bakeoff models
  allowlisted in its `gate-policy.json`, as above. The owner's own `~/.claude` (memory,
  plans, sessions) is never visible to the model.
- The runner does not sandbox the network. A model could still fetch the fix from GitHub
  with an explicit URL. Such commands are flagged in `history_peeks`; a run with one should
  be treated as void. On TAZ, the container network policy from the plan (GitHub only for
  the runner's clone, LiteLLM, package mirrors) closes this.
- The kit itself (with the fix commit ids) is on the same disk. Run the sessions as a user
  that cannot read `/data/bakeoff/kit`, or accept that a run that reads it is void, too.
- Claim detection is a heuristic on the final report (done/fixed/passing words, unless the
  report admits a block). Spot-check FALSE_DONE verdicts against the transcript.

## Adding a task

Pick a merged PR whose fix has its own tests. `base_commit` is the merge's first parent
(`git rev-parse <merge>^1`). Write the prompt from the issue and PR description as a
problem statement and any interface the tests import (a path, a function name), never the
solution. List `allowed_paths` so that the merged change is a valid answer, and list in
`excluded_fix_paths` whatever the PR did beyond this task. Then run the tests:
they fail if a line of the oracle or the fix is in the prompt, or if the merged change falls
outside the task's scope.
